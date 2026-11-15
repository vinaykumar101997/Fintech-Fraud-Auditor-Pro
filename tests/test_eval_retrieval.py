"""The retrieval evaluation harness, offline.

A word-count embedder and an in-memory vector store stand in for the embedding
model and the index, so nothing here needs a network, credentials or the
guidance document. Needs langchain_core (requirements-dev.txt).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("langchain_core")

from langchain_core.embeddings import Embeddings  # noqa: E402
from langchain_core.vectorstores import InMemoryVectorStore  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import config  # noqa: E402
import eval_retrieval as harness  # noqa: E402

VOCABULARY = ("structuring", "threshold", "sanctions", "freeze", "trust", "wire", "payment")


class ResourceExhausted(Exception):
    """Same class name google-api-core uses for HTTP 429 / RESOURCE_EXHAUSTED."""


class WordCountEmbedder(Embeddings):
    """One dimension per vocabulary word; counts every embed_query call."""

    def __init__(self, failures: int = 0):
        self.calls = 0
        self.failures = failures

    def _vector(self, text):
        words = re.findall(r"[a-z]+", text.lower())
        return [float(words.count(word)) + 0.01 for word in VOCABULARY]

    def embed_documents(self, texts):
        return [self._vector(text) for text in texts]

    def embed_query(self, text):
        self.calls += 1
        if self.calls <= self.failures:
            raise ResourceExhausted("429 Resource exhausted")
        return self._vector(text)


FRAGMENTS = {
    "s1": "structuring below the threshold", "s2": "structuring and linked operations threshold",
    "x1": "sanctions freeze without delay", "x2": "sanctions designated persons freeze",
    "t1": "trust and trustee", "w1": "wire payment information", "w2": "wire payment originator",
}
TOPICS = {"structuring": frozenset({"s1", "s2"}), "sanctions": frozenset({"x1", "x2"})}
CASES = [
    {"id": "A", "file": "f.json", "stratum": "rule_hit", "topics": ["structuring"]},
    {"id": "B", "file": "f.json", "stratum": "no_rule", "topics": ["sanctions"]},
]
RECORDS = {"A": {"hint": "structuring threshold"}, "B": {"hint": "sanctions freeze"}}
TEST_ARMS = {
    "old": lambda record: "wire payment wire payment " + record["hint"].split()[0],
    "new": lambda record: record["hint"],
}


@pytest.fixture
def index():
    embedder = WordCountEmbedder()
    store = InMemoryVectorStore(embedder)
    store.add_texts(list(FRAGMENTS.values()), ids=list(FRAGMENTS))

    def search(vector, limit):
        return [doc.id for doc in store.similarity_search_by_vector(vector, k=limit)]
    return embedder, search


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch):
    monkeypatch.setattr(config, "LLM_MAX_RETRIES", 3)
    monkeypatch.setattr(config, "LLM_RETRY_BASE_DELAY", 1.5)
    monkeypatch.setattr(config, "LLM_RATE_LIMIT_BASE_DELAY", 5.0)
    monkeypatch.setattr(config, "LLM_RETRY_MAX_WAIT", 60.0)


# --- the committed label file -------------------------------------------------
def test_label_file_is_valid_and_points_at_real_cases():
    topics, cases, index_info = harness.load_labels()
    records = harness.load_records(cases)
    assert index_info["fragments"] == 410
    assert len(cases) == 47
    assert sum(case["stratum"] == "rule_hit" for case in cases) == 35
    for case in cases:
        has_rule = bool(records[case["id"]]["rule_reasons"])
        assert (case["stratum"] == "rule_hit") == has_rule, case["id"]
        assert harness.relevant_ids(case, topics), case["id"]


def test_label_file_matches_the_hash_recorded_in_the_evaluation_doc():
    """The labels were fixed before any retrieval was run; a later edit to
    them has to show up here."""
    doc = (ROOT / "docs" / "EVALUATION.md").read_text(encoding="utf-8")
    recorded = re.search(r"retrieval_cases\.json`, SHA-256 `([0-9a-f]{64})`", doc).group(1)
    assert harness.labels_sha256() == recorded


def test_label_file_holds_ids_and_pages_only():
    data = json.loads(harness.LABELS_PATH.read_text(encoding="utf-8"))
    for topic in data["topics"].values():
        for fragment in topic["fragments"]:
            assert set(fragment) == {"id", "page"}


@pytest.mark.parametrize("change, message", [
    (lambda d: d["cases"].append(dict(d["cases"][0])), "duplicate case id"),
    (lambda d: d["cases"][0].update(stratum="other"), "stratum"),
    (lambda d: d["cases"][0].update(topics=["nope"]), "unknown topics"),
    (lambda d: d["cases"][0].update(topics=[]), "at least one topic"),
    (lambda d: d["topics"]["sanctions"]["fragments"].append(d["topics"]["sanctions"]["fragments"][0]),
     "distinct"),
], ids=["duplicate-id", "bad-stratum", "unknown-topic", "no-topic", "repeated-fragment"])
def test_a_malformed_label_file_is_refused(tmp_path, change, message):
    data = json.loads(harness.LABELS_PATH.read_text(encoding="utf-8"))
    change(data)
    path = tmp_path / "labels.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        harness.load_labels(path)


# --- metrics -----------------------------------------------------------------
def test_scores_for_a_known_ranking():
    ranked = ["a", "r1", "b", "c", "d", "r2", "e"]
    scores = harness.score_ranking(ranked, frozenset({"r1", "r2", "r3"}))
    assert scores == {"recall": pytest.approx(1 / 3), "precision": pytest.approx(1 / 5),
                      "mrr": pytest.approx(1 / 2)}


def test_first_relevant_fragment_beyond_the_top_five_still_counts_for_mrr_only():
    ranked = ["a", "b", "c", "d", "e", "f", "r1"]
    scores = harness.score_ranking(ranked, frozenset({"r1"}))
    assert (scores["recall"], scores["precision"]) == (0.0, 0.0)
    assert scores["mrr"] == pytest.approx(1 / 7)


def test_nothing_relevant_within_the_depth_scores_zero():
    ranked = [f"n{i}" for i in range(10)] + ["r1"]
    assert harness.score_ranking(ranked, frozenset({"r1"}))["mrr"] == 0.0


def test_a_case_without_relevant_fragments_is_refused():
    with pytest.raises(ValueError):
        harness.score_ranking(["a"], frozenset())


def test_recall_ceiling_drops_below_one_past_five_relevant_fragments():
    assert harness.recall_ceiling(frozenset("abc")) == 1.0
    assert harness.recall_ceiling(frozenset("abcdefghij")) == 0.5


def test_bootstrap_interval_is_repeatable_and_brackets_the_mean():
    values = [0.0, 0.2, 0.2, 0.4, 0.6, 1.0, 0.0, 0.8]
    low, high = harness.bootstrap_interval(values, resamples=2000)
    assert (low, high) == harness.bootstrap_interval(values, resamples=2000)
    assert low < harness.mean(values) < high
    assert harness.bootstrap_interval([0.3]) == (0.3, 0.3)
    assert harness.bootstrap_interval([0.5, 0.5, 0.5], resamples=200) == (0.5, 0.5)


# --- embedding cache -----------------------------------------------------------
def test_each_text_is_embedded_once_and_then_read_from_disk(tmp_path):
    embedder = WordCountEmbedder()
    cache = harness.EmbeddingCache(tmp_path, "model-a", embedder.embed_query)
    first = cache("structuring threshold")
    assert cache("structuring threshold") == first
    assert (embedder.calls, cache.calls, cache.hits) == (1, 1, 1)

    again = harness.EmbeddingCache(tmp_path, "model-a", embedder.embed_query)
    assert again("structuring threshold") == first
    assert (embedder.calls, again.calls, again.hits) == (1, 0, 1)


def test_another_model_does_not_reuse_the_vector(tmp_path):
    embedder = WordCountEmbedder()
    harness.EmbeddingCache(tmp_path, "model-a", embedder.embed_query)("sanctions")
    harness.EmbeddingCache(tmp_path, "model-b", embedder.embed_query)("sanctions")
    assert embedder.calls == 2


def test_cache_files_hold_the_vector_but_not_the_query_text(tmp_path):
    harness.EmbeddingCache(tmp_path, "model-a", WordCountEmbedder().embed_query)("sanctions freeze")
    (stored,) = tmp_path.glob("*.json")
    assert set(json.loads(stored.read_text(encoding="utf-8"))) == {"model", "vector"}


def test_a_rate_limited_embedding_is_retried_with_the_long_backoff(tmp_path, monkeypatch):
    monkeypatch.setattr("utils.retry.random.uniform", lambda a, b: 0.0)
    embedder, sleeps = WordCountEmbedder(failures=2), []
    cache = harness.EmbeddingCache(tmp_path, "model-a", embedder.embed_query, sleep=sleeps.append)
    assert cache("wire") == embedder._vector("wire")
    assert (embedder.calls, cache.calls) == (3, 3)
    assert sleeps == [5.0, 10.0]


def test_a_failed_embedding_is_not_cached(tmp_path):
    embedder = WordCountEmbedder(failures=10)
    cache = harness.EmbeddingCache(tmp_path, "model-a", embedder.embed_query, sleep=lambda _: None)
    with pytest.raises(ResourceExhausted):
        cache("wire")
    assert list(tmp_path.glob("*.json")) == []


# --- index guard ---------------------------------------------------------------
def test_index_guard_passes_when_index_split_and_labels_agree():
    assert harness.check_index(list(FRAGMENTS), list(FRAGMENTS), {"s1", "x1"}, expected=7) == 7


@pytest.mark.parametrize("index_ids, local_ids, label_ids, expected, message", [
    (list(FRAGMENTS)[:-1], list(FRAGMENTS), {"s1"}, 7, "1 are missing from the index"),
    (list(FRAGMENTS) + ["zz"], list(FRAGMENTS), {"s1"}, 7, "1 are not in the document"),
    (list(FRAGMENTS), list(FRAGMENTS), {"s1", "gone"}, 7, "labelled fragment"),
    (list(FRAGMENTS), list(FRAGMENTS), {"s1"}, 410, "labels were made for 410"),
], ids=["index-short", "index-extra", "label-unknown", "split-size"])
def test_index_guard_stops_on_any_disagreement(index_ids, local_ids, label_ids, expected, message):
    with pytest.raises(harness.IndexMismatch, match=message):
        harness.check_index(index_ids, local_ids, label_ids, expected=expected)


# --- run and report ------------------------------------------------------------
def test_run_scores_each_arm_against_the_labels(index, tmp_path):
    embedder, search = index
    embedder.calls = 0
    cache = harness.EmbeddingCache(tmp_path, "fake", embedder.embed_query)
    rows = harness.run(CASES, RECORDS, TOPICS, TEST_ARMS, cache, search)

    assert [row["id"] for row in rows] == ["A", "B"]
    for row in rows:
        new, old = row["arms"]["new"], row["arms"]["old"]
        assert new["recall"] == 1.0 and new["mrr"] == 1.0
        assert new["precision"] == pytest.approx(2 / 5)
        assert new["relevant_ranks"] == [1, 2]
        assert old["mrr"] < 1.0
        assert row["ceiling"] == 1.0
    # Four distinct texts, one embedding call each; a second run makes none.
    assert embedder.calls == 4
    harness.run(CASES, RECORDS, TOPICS, TEST_ARMS, cache, search)
    assert embedder.calls == 4


def test_report_shows_every_arm_the_difference_the_strata_and_the_ceiling(index, tmp_path):
    embedder, search = index
    cache = harness.EmbeddingCache(tmp_path, "fake", embedder.embed_query)
    rows = harness.run(CASES, RECORDS, TOPICS, TEST_ARMS, cache, search)
    text = "\n".join(harness.report_lines(rows, list(TEST_ARMS), ["Note line."]))
    for expected in ("Note line.", "| old |", "| new |", "| new minus old |", "Stratum rule_hit (1 cases",
                     "Stratum no_rule (1 cases", "Best mean recall@5 the labels allow: 1.000",
                     "Distinct query texts: old 2, new 2.", "| structuring | 1 |", "| sanctions | 1 |"):
        assert expected in text, expected


def test_old_arm_is_the_instruction_text_followed_by_the_fenced_record():
    _, cases, _ = harness.load_labels()
    record = harness.load_records(cases)["ST-01"]
    evidence = harness.evidence_query(record)
    assert harness.old_query(record) == f"{harness.LEGACY_INSTRUCTIONS}\n\n{evidence}"
    assert evidence.startswith("<transaction_record>\n- transaction_id: ST-01\n- amount: 9,850.00\n")
    assert "sits in the structuring band" in evidence


def test_new_arm_is_the_query_the_review_builds():
    from utils.retrieval_query import build_retrieval_query

    _, cases, _ = harness.load_labels()
    record = harness.load_records(cases)["HR-01"]
    txn = record["transaction"]
    expected = build_retrieval_query(
        amount=txn["amount"], country=txn["country"], sender=txn["sender"],
        receiver=txn["receiver"], rule_reasons=record["rule_reasons"]).text
    assert harness.ARMS["new"](record) == expected
    assert "higher-risk jurisdiction; sanctions evasion" in expected
    assert list(harness.ARMS) == ["old", "new", "evidence"]


def test_pause_follows_each_request_to_the_model_but_not_a_cache_hit(tmp_path):
    embedder, sleeps = WordCountEmbedder(), []
    cache = harness.EmbeddingCache(tmp_path, "model-a", embedder.embed_query,
                                   sleep=sleeps.append, pause=7.0)
    cache("wire")
    cache("wire")
    cache("trust")
    assert sleeps == [7.0, 7.0]
    assert (cache.calls, cache.hits) == (2, 1)
