"""Measure which guidance fragments a retrieval query brings back.

The verdict evaluation (scripts/eval_llm_tier.py) scores the review's answer.
This script scores the step before it: for each labelled case it embeds a
query, takes the nearest fragments from the index and compares them with the
fragments labelled relevant for that case (tests/fixtures/retrieval_cases.json).
No chat model is called.

    python scripts/eval_retrieval.py --verify-labels
    python scripts/eval_retrieval.py --out eval/retrieval.md --cases-out eval/retrieval_cases.jsonl
    python scripts/eval_retrieval.py --arms old new

Three queries are compared for the same case:

    old       the query sent before the change: the review's instruction text
              followed by the fenced transaction record
    new       the query built from the rule hits and key facts
              (utils/retrieval_query.py)
    evidence  the fenced transaction record alone

Metrics per case: recall@5, precision@5 and MRR (first relevant fragment
within the top 10). Means carry a 95% percentile bootstrap interval over
cases. A case with more than five relevant fragments cannot reach a recall@5
of 1; the report prints the best mean the labels allow.

--verify-labels needs only the guidance PDF: it splits the document locally
and checks every labelled fragment id against the split. A full run needs the
vector store and the embedding model. Each distinct query text is embedded
once, one request at a time through utils.retry.call_with_retry, and the
vector is cached on disk, so a rerun makes no embedding calls and a run cut
short by a quota error resumes where it stopped. --pause spaces the requests
out for a low per-minute quota. Before scoring,
the fragment ids in the index must equal those of the local split; if they do
not, the run stops without results.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from utils.retry import call_with_retry  # noqa: E402

FIXTURES = config.PROJECT_ROOT / "tests" / "fixtures"
LABELS_PATH = FIXTURES / "retrieval_cases.json"
CACHE_DIR = config.PROJECT_ROOT / "eval" / "cache"
K = 5
MRR_DEPTH = 10
RESAMPLES = 10_000
STRATA = ("rule_hit", "no_rule")
METRICS = ("recall", "precision", "mrr")
METRIC_LABELS = {"recall": f"Recall@{K}", "precision": f"Precision@{K}", "mrr": "MRR"}
VECTOR_NAME = "text-dense"

# The instruction text that used to be embedded in front of the record. Kept
# here word for word so the `old` arm keeps measuring the query as it was.
LEGACY_INSTRUCTIONS = (
    "You are a senior forensic auditor specialising in AML/CTF. Assess the "
    "transaction record for structuring, layering, and jurisdictional risk "
    "against FATF standards and the retrieved regulatory context. Base your "
    "assessment only on the evidence provided and the retrieved context. If the "
    "evidence is insufficient for a confident judgement, say so in the rationale "
    "and lower your confidence rather than speculating."
)

Embed = Callable[[str], List[float]]
Search = Callable[[List[float], int], List[str]]


class IndexMismatch(RuntimeError):
    """The index, the local split and the labels do not describe the same fragments."""


# --------------------------------------------------------------------------
# Labels and cases
# --------------------------------------------------------------------------
def load_labels(path: Path = LABELS_PATH) -> Tuple[Dict[str, frozenset], List[Dict[str, Any]], Dict[str, Any]]:
    """Topics (name -> fragment ids), cases and the index description.

    Raises ValueError on a malformed file."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    topics: Dict[str, frozenset] = {}
    for name, topic in data["topics"].items():
        ids = [fragment["id"] for fragment in topic["fragments"]]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError(f"topic {name}: fragment ids must be non-empty and distinct")
        for fragment in topic["fragments"]:
            if not isinstance(fragment.get("page"), int):
                raise ValueError(f"topic {name}: fragment {fragment.get('id')} has no page")
        topics[name] = frozenset(ids)

    seen = set()
    for case in data["cases"]:
        cid = case.get("id")
        if not cid or cid in seen:
            raise ValueError(f"Missing or duplicate case id: {cid!r}")
        seen.add(cid)
        if case.get("stratum") not in STRATA:
            raise ValueError(f"{cid}: stratum must be one of {STRATA}")
        if not case.get("topics"):
            raise ValueError(f"{cid}: at least one topic is required")
        unknown = [t for t in case["topics"] if t not in topics]
        if unknown:
            raise ValueError(f"{cid}: unknown topics {unknown}")
    return topics, data["cases"], data.get("index", {})


def labels_sha256(path: Path = LABELS_PATH) -> str:
    """Hash of the label file with LF line endings, whatever the checkout wrote."""
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def labelled_ids(topics: Dict[str, frozenset]) -> frozenset:
    return frozenset().union(*topics.values())


def relevant_ids(case: Dict[str, Any], topics: Dict[str, frozenset]) -> frozenset:
    return frozenset().union(*(topics[t] for t in case["topics"]))


def load_records(cases: Iterable[Dict[str, Any]], fixtures: Path = FIXTURES) -> Dict[str, Dict[str, Any]]:
    """The transaction fields and rule reasons of each labelled case, by id."""
    files: Dict[str, Dict[str, Any]] = {}
    records = {}
    for case in cases:
        name = case["file"]
        if name not in files:
            content = json.loads((Path(fixtures) / name).read_text(encoding="utf-8"))
            files[name] = {c["id"]: c for c in content["cases"]}
        if case["id"] not in files[name]:
            raise ValueError(f"{case['id']}: not found in {name}")
        records[case["id"]] = files[name][case["id"]]
    return records


# --------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------
def _fields(record: Dict[str, Any]) -> Dict[str, Any]:
    txn = record["transaction"]
    return dict(amount=abs(Decimal(txn["amount"])), country=txn["country"],
                sender=txn["sender"], receiver=txn["receiver"],
                rule_reasons=record["rule_reasons"])


def evidence_query(record: Dict[str, Any]) -> str:
    from utils.agents import evidence_block

    return evidence_block(transaction_id=record["id"], **_fields(record))


def old_query(record: Dict[str, Any]) -> str:
    return f"{LEGACY_INSTRUCTIONS}\n\n{evidence_query(record)}"


def new_query(record: Dict[str, Any]) -> str:
    from utils.retrieval_query import build_retrieval_query

    return build_retrieval_query(**_fields(record)).text


ARMS: Dict[str, Callable[[Dict[str, Any]], str]] = {
    "old": old_query, "new": new_query, "evidence": evidence_query,
}
BASELINE = "old"


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------
def score_ranking(ranked: Sequence[str], relevant: frozenset, k: int = K,
                  depth: int = MRR_DEPTH) -> Dict[str, float]:
    """recall@k, precision@k and the reciprocal rank within `depth`."""
    if not relevant:
        raise ValueError("a case needs at least one relevant fragment")
    hits = sum(1 for fragment in ranked[:k] if fragment in relevant)
    rank = next((i for i, fragment in enumerate(ranked[:depth], start=1) if fragment in relevant), None)
    return {"recall": hits / len(relevant), "precision": hits / k,
            "mrr": 1 / rank if rank else 0.0}


def recall_ceiling(relevant: frozenset, k: int = K) -> float:
    """The best recall@k a case can reach: below 1 with more than k relevant fragments."""
    return min(len(relevant), k) / len(relevant)


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def bootstrap_interval(values: Sequence[float], resamples: int = RESAMPLES,
                       seed: int = 0) -> Tuple[float, float]:
    """95% percentile bootstrap interval of the mean, resampling cases."""
    if len(values) < 2:
        return (values[0], values[0]) if values else (0.0, 0.0)
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(rng.choices(values, k=n)) / n for _ in range(resamples))
    return means[int(0.025 * resamples)], means[min(int(0.975 * resamples), resamples - 1)]


# --------------------------------------------------------------------------
# Embedding cache
# --------------------------------------------------------------------------
class EmbeddingCache:
    """Query vectors kept on disk, one file per (model, text).

    A miss makes one embed call through call_with_retry; a hit makes none.
    Only the vector is stored, under a hash of the model name and the text.
    `pause` seconds are waited after every request that reached the model, to
    stay under a per-minute quota; a cache hit never waits.
    """

    def __init__(self, directory: Path, model: str, embed: Embed,
                 sleep: Optional[Callable[[float], None]] = None, pause: float = 0.0):
        self.directory = Path(directory)
        self.model = model
        self._embed = embed
        self._sleep = sleep
        self.pause = pause
        self.hits = 0
        self.calls = 0

    def _path(self, text: str) -> Path:
        key = hashlib.sha256(f"{self.model}\n{text}".encode("utf-8")).hexdigest()
        return self.directory / f"{key}.json"

    def __call__(self, text: str) -> List[float]:
        path = self._path(text)
        if path.exists():
            self.hits += 1
            return json.loads(path.read_text(encoding="utf-8"))["vector"]

        def request() -> List[float]:
            self.calls += 1
            return list(self._embed(text))

        extra = {"sleep": self._sleep} if self._sleep is not None else {}
        vector = call_with_retry(
            request, "Query embedding",
            max_retries=config.LLM_MAX_RETRIES,
            base_delay=config.LLM_RETRY_BASE_DELAY,
            rate_limit_base_delay=config.LLM_RATE_LIMIT_BASE_DELAY,
            max_wait=config.LLM_RETRY_MAX_WAIT,
            **extra,
        )
        self.directory.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"model": self.model, "vector": vector}), encoding="utf-8")
        if self.pause > 0:
            (self._sleep or time.sleep)(self.pause)
        return vector


# --------------------------------------------------------------------------
# Index guard
# --------------------------------------------------------------------------
def check_index(index_ids: Iterable[str], local_ids: Iterable[str], label_ids: Iterable[str],
                expected: Optional[int] = None) -> int:
    """Raise IndexMismatch unless the index holds exactly the fragments of the
    local split and every labelled fragment is one of them. Returns the count."""
    index_ids, local_ids, label_ids = set(index_ids), set(local_ids), set(label_ids)
    if expected is not None and len(local_ids) != expected:
        raise IndexMismatch(
            f"The local split has {len(local_ids)} fragments; the labels were made for {expected}.")
    if index_ids != local_ids:
        raise IndexMismatch(
            f"The index holds {len(index_ids)} fragments and the local split {len(local_ids)}; "
            f"{len(local_ids - index_ids)} are missing from the index and "
            f"{len(index_ids - local_ids)} are not in the document. Re-ingest before evaluating.")
    unknown = label_ids - local_ids
    if unknown:
        raise IndexMismatch(f"{len(unknown)} labelled fragment id(s) are not in the document.")
    return len(local_ids)


def local_fragment_ids(pdf: Path) -> List[str]:
    """Fragment ids of the document, split exactly as ingestion splits it. No network."""
    from ingest_pdf import fragment_id, split_pdf

    if not Path(pdf).exists():
        raise SystemExit(f"Guidance document not found: {display_path(pdf)}")
    return [fragment_id(chunk.page_content) for chunk in split_pdf(Path(pdf))]


def qdrant_fragment_ids(client, collection: str) -> List[str]:
    ids: List[str] = []
    offset = None
    while True:
        points, offset = client.scroll(collection_name=collection, limit=256, offset=offset,
                                       with_payload=False, with_vectors=False)
        ids.extend(str(point.id) for point in points)
        if offset is None:
            return ids


def qdrant_search(client, collection: str) -> Search:
    def search(vector: List[float], limit: int) -> List[str]:
        response = client.query_points(collection_name=collection, query=vector, using=VECTOR_NAME,
                                       limit=limit, with_payload=False)
        return [str(point.id) for point in response.points]
    return search


# --------------------------------------------------------------------------
# Run and report
# --------------------------------------------------------------------------
def run(cases: List[Dict[str, Any]], records: Dict[str, Dict[str, Any]],
        topics: Dict[str, frozenset], arms: Dict[str, Callable[[Dict[str, Any]], str]],
        embed: Embed, search: Search) -> List[Dict[str, Any]]:
    """One row per case: the relevant set and, per arm, the ranking and its scores.

    Cases and arms are handled strictly one after another."""
    rows = []
    for case in cases:
        relevant = relevant_ids(case, topics)
        row = {"id": case["id"], "stratum": case["stratum"], "topics": list(case["topics"]),
               "relevant": len(relevant), "ceiling": recall_ceiling(relevant), "arms": {}}
        for name, build in arms.items():
            text = build(records[case["id"]])
            ranked = search(embed(text), MRR_DEPTH)
            row["arms"][name] = {
                "query_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "ranked": ranked,
                "relevant_ranks": [i for i, f in enumerate(ranked, start=1) if f in relevant],
                **score_ranking(ranked, relevant),
            }
        rows.append(row)
    return rows


def _cell(values: Sequence[float], signed: bool = False) -> str:
    low, high = bootstrap_interval(values)
    fmt = "{:+.3f}" if signed else "{:.3f}"
    return f"{fmt.format(mean(values))} ({fmt.format(low)} to {fmt.format(high)})"


def _table(rows: List[Dict[str, Any]], arms: Sequence[str]) -> List[str]:
    lines = ["| Arm | " + " | ".join(METRIC_LABELS[m] for m in METRICS) + " |", "|---|---|---|---|"]
    for arm in arms:
        cells = [_cell([r["arms"][arm][m] for r in rows]) for m in METRICS]
        lines.append(f"| {arm} | " + " | ".join(cells) + " |")
    for arm in arms:
        if arm == BASELINE or BASELINE not in arms:
            continue
        cells = [_cell([r["arms"][arm][m] - r["arms"][BASELINE][m] for r in rows], signed=True)
                 for m in METRICS]
        lines.append(f"| {arm} minus {BASELINE} | " + " | ".join(cells) + " |")
    return lines


def report_lines(rows: List[Dict[str, Any]], arms: Sequence[str], notes: Sequence[str] = ()) -> List[str]:
    """The report as markdown; it also reads fine on the console."""
    lines = ["## Retrieval evaluation", ""]
    lines += list(notes)
    lines += [
        f"Cases: {len(rows)}. Means with a 95% bootstrap interval over cases; "
        f"MRR counts the first relevant fragment within the top {MRR_DEPTH}.",
        f"Best mean recall@{K} the labels allow: {mean([r['ceiling'] for r in rows]):.3f} "
        f"({sum(r['relevant'] > K for r in rows)} of {len(rows)} cases have more than {K} "
        "relevant fragments).",
        "Distinct query texts: " + ", ".join(
            f"{arm} {len({r['arms'][arm]['query_sha256'] for r in rows})}" for arm in arms) + ".",
        "", "### All cases", "",
    ]
    lines += _table(rows, arms)
    for stratum in STRATA:
        subset = [r for r in rows if r["stratum"] == stratum]
        if not subset:
            continue
        lines += ["", f"### Stratum {stratum} ({len(subset)} cases; best mean recall@{K} "
                      f"{mean([r['ceiling'] for r in subset]):.3f})", ""]
        lines += _table(subset, arms)

    by_topic: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        for topic in row["topics"]:
            by_topic[topic].append(row)
    lines += ["", f"### Mean recall@{K} by topic", "",
              "A case with several topics counts under each of them.", "",
              "| Topic | Cases | " + " | ".join(arms) + " |", "|---|---|" + "---|" * len(arms)]
    for topic in sorted(by_topic):
        subset = by_topic[topic]
        cells = [f"{mean([r['arms'][arm]['recall'] for r in subset]):.3f}" for arm in arms]
        lines.append(f"| {topic} | {len(subset)} | " + " | ".join(cells) + " |")
    return lines


def display_path(path: Path) -> str:
    """Path relative to the project when it is inside it, else the file name."""
    try:
        return Path(path).resolve().relative_to(config.PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return Path(path).name


def embedding_model_name() -> str:
    if config.MODEL_PROVIDER == "bedrock":
        return config.BEDROCK_EMBED_MODEL_ID
    return config.VERTEX_EMBED_MODEL


def build_live(cache_dir: Path, pause: float = 0.0) -> Tuple[EmbeddingCache, Search, Any]:
    """Embedding cache, index search and the client, from the app's own settings."""
    from qdrant_client import QdrantClient

    from utils.llm_provider import get_embeddings

    if not config.QDRANT_URL:
        raise SystemExit("QDRANT_URL is not set.")
    client = QdrantClient(url=config.QDRANT_URL, api_key=config.QDRANT_API_KEY, timeout=30)
    embedder = get_embeddings()
    cache = EmbeddingCache(cache_dir, embedding_model_name(), embedder.embed_query, pause=pause)
    return cache, qdrant_search(client, config.QDRANT_COLLECTION), client


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--labels", type=Path, default=LABELS_PATH)
    parser.add_argument("--arms", nargs="+", choices=list(ARMS), default=list(ARMS))
    parser.add_argument("--cache-dir", type=Path, default=CACHE_DIR,
                        help="where query vectors are kept between runs")
    parser.add_argument("--pause", type=float, default=0.0,
                        help="seconds to wait after each embedding request (not after cache hits)")
    parser.add_argument("--verify-labels", action="store_true",
                        help="check the labelled ids against a local split of the document and exit")
    parser.add_argument("--out", type=Path, default=None, help="also write the report as markdown")
    parser.add_argument("--cases-out", type=Path, default=None,
                        help="write one JSON row per case (rankings and scores) to this JSONL file")
    args = parser.parse_args(argv)

    topics, cases, index = load_labels(args.labels)
    records = load_records(cases)
    local_ids = local_fragment_ids(config.GUIDELINES_PDF)

    if args.verify_labels:
        try:
            count = check_index(local_ids, local_ids, labelled_ids(topics), index.get("fragments"))
        except IndexMismatch as exc:
            print(f"FAILED: {exc}")
            return 1
        print(f"{len(labelled_ids(topics))} labelled fragments, all among the {count} "
              f"fragments of {display_path(config.GUIDELINES_PDF)}.")
        return 0

    cache, search, client = build_live(args.cache_dir, args.pause)
    try:
        index_ids = qdrant_fragment_ids(client, config.QDRANT_COLLECTION)
    except Exception as exc:  # noqa: BLE001 - connection errors come in many types
        raise SystemExit(f"Vector store unreachable, nothing was scored: "
                         f"{type(exc).__name__}: {str(exc)[:200]}") from exc
    try:
        count = check_index(index_ids, local_ids, labelled_ids(topics), index.get("fragments"))
    except IndexMismatch as exc:
        raise SystemExit(f"Index check failed, nothing was scored: {exc}") from exc

    arms = {name: ARMS[name] for name in args.arms}
    try:
        rows = run(cases, records, topics, arms, cache, search)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"Run stopped, no results: {type(exc).__name__}: {str(exc)[:300]}") from exc

    notes = [
        f"Labels: {display_path(args.labels)}, SHA-256 {labels_sha256(args.labels)}.",
        f"Index: {count} fragments, ids equal to the local split. Embedding model: "
        f"{embedding_model_name()}. Embedding calls made: {cache.calls}; served from cache: {cache.hits}.",
    ]
    text = "\n".join(report_lines(rows, list(arms), notes))
    print(text)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"\nWrote {display_path(args.out)}")
    if args.cases_out:
        args.cases_out.parent.mkdir(parents=True, exist_ok=True)
        with args.cases_out.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        print(f"Wrote {len(rows)} case rows to {display_path(args.cases_out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
