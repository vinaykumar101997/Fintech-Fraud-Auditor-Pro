"""Measure the compliance tier's verdicts against a labelled case set.

`scripts/evaluate.py --with-llm` only counts the verdicts the tier returns on
the sample ledger; nothing there says which verdict was right. This harness
runs ComplianceAuditEngine.assess_transaction on synthetic cases with a known
expected verdict (tests/fixtures/llm_cases.json) and scores the answers.

By default it uses a deterministic fake model and a fake retriever, so it needs
no network and no credentials. The fake modes test the harness itself:

    oracle             answers the expected verdict (accuracy should be 100%)
    always-clear       answers CLEAR (SUSPICIOUS recall should be 0)
    always-suspicious  answers SUSPICIOUS (CLEAR cases all wrong)
    error              every model call raises (every review should be ERROR)

    python scripts/eval_llm_tier.py
    python scripts/eval_llm_tier.py --fake always-clear
    python scripts/eval_llm_tier.py --cases tests/fixtures/llm_cases_heldout.json
    python scripts/eval_llm_tier.py --exclude-category pass_through
    python scripts/eval_llm_tier.py --live --max-calls 40 --out llm_eval.md \
        --cases-out data/llm_cases_live.jsonl

--live runs the configured provider (MODEL_PROVIDER) through the same factory
and retrieval stack the app uses. --max-calls caps every model call, including
retries; once the cap is reached no further call is made, the case in progress
is dropped and the cases completed so far are reported.

--cases-out appends one JSON row per finished case (verdict, confidence, the
reviewer's rationale, error detail, the rule reasons sent, retrieved passage
ids, calls, tokens, seconds) and flushes it at once, so an early stop or a
crash keeps every case finished before it. A live row also lists each model
call's billed output tokens and finish reason; a reply cut off at the output
limit is listed with MAX_TOKENS.

Each case is passed to the engine with exactly the arguments the app passes:
the transaction fields and the rule reasons. The behaviour-model flags in the
case file describe why such a row would be forwarded, but the engine has no
parameter for them, so the review does not see them.

--engine picks the review:

    fixed           the fixed review (default), as above
    agent           the investigation agent (utils/investigation.py, REVIEW_MODE=agent)
    fixed+history   the fixed review with both parties' account history (the
                    summary the agent's party_history tool returns) added to
                    the record; needs a case file with a ledger

A case file with a "ledger" entry (tests/fixtures/agent_ledger_cases.json)
names a generate_sample_ledger.generate() call; the generated batch is the
ledger the history reads. Other case files have no ledger, and the agent's
party_history tool says so.

    python scripts/eval_llm_tier.py --engine agent
    python scripts/eval_llm_tier.py --engine agent --live --pause 4 --resume \
        --cases-out data/agent_dev_run1.jsonl --out data/agent_dev_run1.md

--pause waits after every live model call. --resume skips the cases already
in --cases-out and scores the whole file. For the agent the report adds tool
selection against tests/fixtures/agent_tool_labels.json (--tool-labels) and
per-case calls, tokens, cost and time; scripts/compare_llm_runs.py counts the
discordant cases between two runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, TextIO

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from evaluate import wilson_interval  # noqa: E402
from utils.trace import billed_tokens  # noqa: E402
from utils.verdicts import AuditOutcome, Verdict  # noqa: E402

CASES_PATH = config.PROJECT_ROOT / "tests" / "fixtures" / "llm_cases.json"
HELDOUT_CASES_PATH = config.PROJECT_ROOT / "tests" / "fixtures" / "llm_cases_heldout.json"
LEDGER_CASES_PATH = config.PROJECT_ROOT / "tests" / "fixtures" / "agent_ledger_cases.json"
TOOL_LABELS_PATH = config.PROJECT_ROOT / "tests" / "fixtures" / "agent_tool_labels.json"
GUIDANCE_CACHE_DIR = config.PROJECT_ROOT / "eval" / "cache" / "guidance"
PASS_THROUGH_NOTE = (
    "Note: pass_through cases cannot be answered by the review. Their signal is in the "
    "behaviour flags, and the review does not receive behaviour flags, only the transaction "
    "fields and rule reasons."
)
CATEGORIES = (
    "obvious_clear", "over_threshold_legitimate", "structuring", "high_risk_jurisdiction",
    "layering_shell", "pass_through", "prompt_injection",
)
# Categories of the ledger-backed cases (pass_through is shared).
LEDGER_CATEGORIES = (
    "pass_through", "fan_in", "structuring_history", "circular", "clean_large", "clean_ordinary",
)
ALL_CATEGORIES = CATEGORIES + tuple(c for c in LEDGER_CATEGORIES if c not in CATEGORIES)
ENGINES = ("fixed", "agent", "fixed+history")
EXPECTED = (Verdict.SUSPICIOUS.value, Verdict.CLEAR.value)
FAKE_MODES = ("oracle", "always-clear", "always-suspicious", "error")
DEFAULT_LIVE_MAX_CALLS = 40

# USD per million input / output tokens, list prices as of October 2026. Only
# used for the estimate printed after a live run; check current pricing.
PRICES_PER_MILLION = {
    "gemini-2.5-flash": (0.30, 2.50),
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": (1.00, 5.00),
}


# --------------------------------------------------------------------------
# Case set
# --------------------------------------------------------------------------
def load_cases(path: Path = CASES_PATH) -> List[Dict[str, Any]]:
    """Read and validate the case file. Raises ValueError on a malformed case."""
    cases = json.loads(Path(path).read_text(encoding="utf-8"))["cases"]
    seen = set()
    for case in cases:
        cid = case.get("id")
        if not cid or cid in seen:
            raise ValueError(f"Missing or duplicate case id: {cid!r}")
        seen.add(cid)
        if case.get("category") not in ALL_CATEGORIES:
            raise ValueError(f"{cid}: unknown category {case.get('category')!r}")
        if case.get("expected_verdict") not in EXPECTED:
            raise ValueError(f"{cid}: expected_verdict must be one of {EXPECTED}")
        txn = case.get("transaction") or {}
        missing = {"amount", "country", "sender", "receiver"} - set(txn)
        if missing:
            raise ValueError(f"{cid}: transaction is missing {sorted(missing)}")
        Decimal(txn["amount"])  # raises on a non-numeric amount
        for key in ("rule_reasons", "behaviour_flags"):
            if not isinstance(case.get(key), list):
                raise ValueError(f"{cid}: {key} must be a list")
    return cases


def ledger_spec(path: Path) -> Optional[Dict[str, Any]]:
    """The case file's "ledger" entry, or None for a case file without a ledger."""
    return json.loads(Path(path).read_text(encoding="utf-8")).get("ledger")


def build_case_ledger(spec: Dict[str, Any]):
    """The generated batch a ledger-backed case file names, as the app loads a ledger.

    Generated on the fly from the seed, so no ledger data is stored with the
    cases. Amounts become Decimals with a float mirror and timestamps
    datetimes, as utils.data_loader.load_ledger leaves them.
    """
    import pandas as pd
    from generate_sample_ledger import generate
    from utils.data_loader import parse_money

    ledger = generate(seed=int(spec["seed"]), hard=bool(spec.get("hard", False)))
    ledger = ledger.drop(columns=[c for c in ("is_laundering", "pattern") if c in ledger.columns])
    ledger["amount"] = [parse_money(a) for a in ledger["amount"]]
    ledger["amount_float"] = [float(abs(a)) for a in ledger["amount"]]
    ledger["transaction_id"] = ledger["transaction_id"].astype(str)
    ledger["timestamp"] = pd.to_datetime(ledger["timestamp"], errors="coerce")
    return ledger


def load_tool_labels(path: Path = TOOL_LABELS_PATH) -> Dict[str, Dict[str, Any]]:
    return json.loads(Path(path).read_text(encoding="utf-8"))["labels"]


# --------------------------------------------------------------------------
# Call cap
# --------------------------------------------------------------------------
class CallCapReached(RuntimeError):
    """Raised instead of making a model call once the cap is used up."""


class CallBudget:
    """Counts model calls; refuses the call that would go over the cap."""

    def __init__(self, cap: Optional[int] = None):
        self.cap = cap
        self.used = 0

    @property
    def exhausted(self) -> bool:
        return self.cap is not None and self.used >= self.cap

    def spend(self) -> None:
        if self.exhausted:
            raise CallCapReached(f"call cap of {self.cap} reached")
        self.used += 1


# --------------------------------------------------------------------------
# Fake model and retriever
# --------------------------------------------------------------------------
class FakeProviderError(RuntimeError):
    """A transient-looking failure, so the engine's retry loop runs on it."""


class FakeLLM:
    """Stands in for the chat model in both stages of the review.

    Stage 1 goes through FakeRetrievalQA.invoke, stage 2 through the runnable
    returned by with_structured_output. Each counts as one model call.
    """

    def __init__(self, mode: str, budget: CallBudget):
        if mode not in FAKE_MODES:
            raise ValueError(f"Unknown fake mode {mode!r}; use one of {FAKE_MODES}")
        self.mode = mode
        self.budget = budget
        self.case: Optional[Dict[str, Any]] = None  # set by run_cases before each case

    def call(self) -> None:
        self.budget.spend()
        if self.mode == "error":
            raise FakeProviderError("ServiceUnavailable: fake provider outage")

    def verdict(self) -> str:
        if self.mode == "always-clear":
            return Verdict.CLEAR.value
        if self.mode == "always-suspicious":
            return Verdict.SUSPICIOUS.value
        return self.case["expected_verdict"]

    def with_structured_output(self, schema):
        from langchain_core.runnables import RunnableLambda

        def respond(_prompt):
            self.call()
            return schema.model_validate({
                "verdict": self.verdict(),
                "confidence": 0.9,
                "rationale": f"Fake {self.mode} review.",
            })
        return RunnableLambda(respond)

    # The investigation agent: one round of lookups, then notes.
    FAKE_LOOKUPS = (
        ("search_guidance", {"topics": ["general_monitoring"]}),
        ("party_history", {"role": "originator"}),
    )

    def bind_tools(self, tools):
        from langchain_core.messages import AIMessage, ToolMessage
        from langchain_core.runnables import RunnableLambda

        def respond(messages):
            self.call()
            usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            if not any(isinstance(m, ToolMessage) for m in messages):
                return AIMessage(content="", usage_metadata=usage, tool_calls=[
                    {"name": name, "args": args, "id": f"call-{n}", "type": "tool_call"}
                    for n, (name, args) in enumerate(self.FAKE_LOOKUPS)])
            return AIMessage(content=f"Fake {self.mode} investigation notes.", usage_metadata=usage)
        return RunnableLambda(respond)


class FakeRetriever:
    """Stands in for the guidance index the agent searches: fixed passages, no model call."""

    def __init__(self, passages: int = 5):
        self.passages = passages

    def invoke(self, query, config=None):
        from langchain_core.documents import Document

        return [Document(page_content=f"Fake regulatory passage {i + 1}.",
                         metadata={"source": f"fake-passage-{i + 1}", "page": i})
                for i in range(self.passages)]


class FakeRetrievalQA:
    """Stands in for the retrieval chain: fixed passages, one model call."""

    def __init__(self, llm: FakeLLM, passages: int = 5):
        self.llm = llm
        self.passages = passages

    def invoke(self, payload: Dict[str, str], config=None) -> Dict[str, Any]:
        from langchain_core.documents import Document

        self.llm.call()
        docs = [Document(page_content=f"Fake regulatory passage {i + 1}.",
                         metadata={"source": f"fake-passage-{i + 1}"})
                for i in range(self.passages)]
        return {"result": "Fake first-pass finding.", "source_documents": docs}


# --------------------------------------------------------------------------
# Retrieved passages
# --------------------------------------------------------------------------
def passage_id(doc) -> str:
    """Short id for a retrieved passage: title or file name, plus page."""
    meta = getattr(doc, "metadata", None) or {}
    name = meta.get("title") or os.path.basename(str(meta.get("source") or meta.get("file_path") or ""))
    page = meta.get("page")
    if not name:
        return "unknown"
    return f"{name} p{page}" if page is not None else name


class PassageRecorder:
    """Wraps the retrieval chain and keeps the passages of its last answer.

    Both the live chain and the fake one return their source documents; the
    retriever callback keeps the passages when a chain does not.
    """

    def __init__(self, chain):
        from langchain_core.callbacks import BaseCallbackHandler

        recorder = self

        class _OnRetrieve(BaseCallbackHandler):
            def on_retriever_end(self, documents, **kwargs):
                recorder.documents = list(documents)

        self.chain = chain
        self.callback = _OnRetrieve()
        self.documents: List[Any] = []

    def invoke(self, payload: Dict[str, str]) -> Dict[str, Any]:
        self.documents = []
        result = self.chain.invoke(payload, config={"callbacks": [self.callback]})
        if (result or {}).get("source_documents"):
            self.documents = list(result["source_documents"])
        return result


class RetrieverRecorder:
    """Wraps the agent's retriever and keeps every passage returned for the current case.

    With cache_dir it also keeps the passages per query text on disk, so a
    rerun or a second run makes no embedding call for a query already
    searched. Retrieval is deterministic for a given query and index, so a
    cached answer is the same answer.
    """

    def __init__(self, retriever, cache_dir: Optional[Path] = None, index_key: str = ""):
        self.retriever = retriever
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.index_key = index_key
        self.documents: List[Any] = []
        self.hits = self.misses = 0

    def reset(self) -> None:
        self.documents = []

    def invoke(self, query: str, config=None):
        from langchain_core.documents import Document

        path = None
        if self.cache_dir is not None:
            key = hashlib.sha256(f"{self.index_key}\n{query}".encode("utf-8")).hexdigest()
            path = self.cache_dir / f"{key}.json"
            if path.exists():
                self.hits += 1
                docs = [Document(page_content=d["page_content"], metadata=d["metadata"])
                        for d in json.loads(path.read_text(encoding="utf-8"))]
                self.documents.extend(docs)
                return docs
        self.misses += 1
        docs = list(self.retriever.invoke(query))
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(
                [{"page_content": d.page_content, "metadata": dict(d.metadata or {})} for d in docs],
                ensure_ascii=False, default=str), encoding="utf-8")
        self.documents.extend(docs)
        return docs


# --------------------------------------------------------------------------
# Live provider
# --------------------------------------------------------------------------
def usage_meter(budget: CallBudget, pause: float = 0.0, sleep=time.sleep):
    """LangChain callback that spends the budget on every model call and
    collects token usage. raise_error makes a refused call fail before any
    request is sent. `pause` seconds are waited after every model call, to
    stay under a per-minute quota."""
    from langchain_core.callbacks import BaseCallbackHandler

    class UsageMeter(BaseCallbackHandler):
        raise_error = True

        def __init__(self):
            self.input_tokens = 0
            self.output_tokens = 0
            # One entry per answered call: billed output tokens and why it stopped.
            self.calls: List[Dict[str, Any]] = []

        def on_chat_model_start(self, *args, **kwargs):
            budget.spend()

        def on_llm_start(self, *args, **kwargs):
            budget.spend()

        def _count(self, usage, finish_reason) -> None:
            # Reasoning tokens are billed as output (utils.trace.billed_tokens).
            tokens_in, tokens_out = billed_tokens(usage)
            self.input_tokens += tokens_in
            self.output_tokens += tokens_out
            self.calls.append({"output_tokens": tokens_out, "finish_reason": finish_reason})

        def on_llm_end(self, response, **kwargs):
            for generations in response.generations:
                for gen in generations:
                    self._count(getattr(getattr(gen, "message", None), "usage_metadata", None),
                                (getattr(gen, "generation_info", None) or {}).get("finish_reason"))
            if pause > 0:
                sleep(pause)

        def on_llm_error(self, error, **kwargs):
            # A reply cut off at the output limit is raised, not returned
            # (utils.llm_provider.OutputTruncated); it was still billed.
            if hasattr(error, "usage"):
                self._count(error.usage, "MAX_TOKENS")
                if pause > 0:
                    sleep(pause)

    return UsageMeter()


def build_live(budget: CallBudget, engine_kind: str = "fixed", pause: float = 0.0,
               cache_dir: Optional[Path] = GUIDANCE_CACHE_DIR):
    """Engine and retrieval from the app's own factory, metered.

    Returns (engine, rag_engine, meter, retriever): the fixed review uses the
    retrieval chain, the agent the retriever (with an on-disk cache).
    """
    from utils.agents import ComplianceAuditEngine
    from utils.chat_agent import ComplianceIntelligenceProvider

    provider = ComplianceIntelligenceProvider()
    meter = usage_meter(budget, pause=pause)
    provider.llm.callbacks = [meter]  # before the engine binds the model
    if engine_kind == "agent":
        from utils.investigation import InvestigationEngine

        key = f"{config.MODEL_PROVIDER}|{config.QDRANT_COLLECTION}|{config.RETRIEVER_K}"
        retriever = RetrieverRecorder(provider.retriever, cache_dir, key)
        return InvestigationEngine(llm=provider.llm), None, meter, retriever
    return ComplianceAuditEngine(llm=provider.llm), provider.engine, meter, None


def build_fake(mode: str, budget: CallBudget, engine_kind: str = "fixed"):
    """(engine, rag_engine, fake). For the agent the second item is a fake
    retriever for its guidance search instead of a retrieval chain."""
    from utils.agents import ComplianceAuditEngine

    llm = FakeLLM(mode, budget)
    if engine_kind == "agent":
        from utils.investigation import InvestigationEngine

        return InvestigationEngine(llm=llm), RetrieverRecorder(FakeRetriever()), llm
    return ComplianceAuditEngine(llm=llm), FakeRetrievalQA(llm), llm


# --------------------------------------------------------------------------
# Run and score
# --------------------------------------------------------------------------
@dataclass
class CaseResult:
    case_id: str
    category: str
    expected: str
    verdict: str
    errored: bool
    error_detail: Optional[str] = None


@dataclass
class Run:
    results: List[CaseResult] = field(default_factory=list)
    total_cases: int = 0
    calls: int = 0
    stopped_at_cap: bool = False


def case_row(case, outcome: AuditOutcome, passages: List[Any], calls: int,
             tokens, seconds: float, engine_kind: str = "fixed") -> Dict[str, Any]:
    """One --cases-out record. Rule reasons are the list passed to the engine.

    Agent and fixed+history rows also name the engine; agent rows add the
    tools called, with their arguments, and why the investigation stopped.
    """
    errored = outcome.verdict is Verdict.ERROR or outcome.error_detail is not None
    extra: Dict[str, Any] = {}
    if engine_kind != "fixed":
        extra["engine"] = engine_kind
    trace = getattr(outcome, "trace", None)
    if trace is not None:
        extra.update({
            "tools_called": trace.tools_called,
            "tool_calls": [{"name": s.name, "args": s.args, "error": s.error}
                           for s in trace.steps if s.kind == "tool"],
            "investigation_calls": trace.investigation_calls,
            "stop_reason": trace.stop_reason,
            "notes": outcome.initial_finding or None,
        })
    return {
        "id": case["id"],
        "category": case["category"],
        "expected": case["expected_verdict"],
        "returned": Verdict.ERROR.value if errored else outcome.verdict.value,
        "confidence": outcome.confidence,
        "reasoning": outcome.reviewer_note or None,
        "error_detail": outcome.error_detail,
        "rule_reasons": case["rule_reasons"],
        "passages_retrieved": len(passages),
        "passage_ids": [passage_id(doc) for doc in passages],
        "model_calls": calls,
        "input_tokens": tokens[0] if tokens else None,
        "output_tokens": tokens[1] if tokens else None,
        "seconds": round(seconds, 3),
        **extra,
    }


def _is_cap_failure(outcome: AuditOutcome) -> bool:
    return bool(outcome.error_detail) and CallCapReached.__name__ in outcome.error_detail


def run_cases(cases, engine, rag_engine, budget: CallBudget, fake: Optional[FakeLLM] = None,
              meter=None, cases_out: Optional[TextIO] = None, progress: bool = False,
              engine_kind: str = "fixed", ledger=None, retriever=None) -> Run:
    """Assess each case in order; stop cleanly when the call budget runs out.

    With cases_out, each finished case is written as one JSON line and
    flushed before the next case starts. `engine_kind` "agent" passes the
    ledger and the retriever to the investigation; "fixed+history" adds both
    parties' history from the ledger to the record.
    """
    run = Run(total_cases=len(cases))
    agent = engine_kind == "agent"
    recorder = PassageRecorder(rag_engine) if rag_engine is not None and not agent else None
    for case in cases:
        if budget.exhausted:
            run.stopped_at_cap = True
            break
        if fake is not None:
            fake.case = case
        calls_before = budget.used
        tokens_before = (meter.input_tokens, meter.output_tokens) if meter is not None else None
        answered_before = len(meter.calls) if meter is not None else 0
        started = time.perf_counter()
        txn = case["transaction"]
        # The record always shows the case id. A ledger-backed case is looked up
        # in the ledger under the row id it names, which never reaches the model:
        # the generator's ids differ by pattern and would give the label away.
        row_id = case.get("ledger_transaction_id") or case["id"]
        kwargs: Dict[str, Any] = dict(
            transaction_id=case["id"],
            amount=abs(Decimal(txn["amount"])),
            country=txn["country"],
            sender=txn["sender"],
            receiver=txn["receiver"],
            rule_reasons=case["rule_reasons"],
            rag_engine=recorder,
        )
        if agent:
            if retriever is not None and hasattr(retriever, "reset"):
                retriever.reset()
            kwargs.update(ledger=ledger, retriever=retriever, ledger_row_id=row_id)
        elif engine_kind == "fixed+history":
            from utils.agent_tools import CaseContext, history_signals

            ctx = CaseContext(transaction_id=row_id, amount=kwargs["amount"], country=txn["country"],
                              sender=txn["sender"], receiver=txn["receiver"],
                              rule_reasons=case["rule_reasons"], ledger=ledger)
            kwargs["behaviour_signals"] = history_signals(ctx)
        outcome = engine.assess_transaction(**kwargs)
        seconds = time.perf_counter() - started
        if _is_cap_failure(outcome):
            # Cut off by the cap, not a verdict: dropped from the results.
            run.stopped_at_cap = True
            break
        result = CaseResult(
            case_id=case["id"], category=case["category"], expected=case["expected_verdict"],
            verdict=outcome.verdict.value,
            errored=outcome.verdict is Verdict.ERROR or outcome.error_detail is not None,
            error_detail=outcome.error_detail,
        )
        run.results.append(result)
        if cases_out is not None:
            tokens = None
            if tokens_before is not None:
                spent = (meter.input_tokens - tokens_before[0], meter.output_tokens - tokens_before[1])
                tokens = spent if any(spent) else None  # provider reported no usage
            if agent:
                passages = list(getattr(retriever, "documents", []) or [])
            else:
                passages = recorder.documents if recorder else []
            row = case_row(case, outcome, passages, budget.used - calls_before, tokens, seconds,
                           engine_kind=engine_kind)
            if meter is not None:
                row["calls"] = meter.calls[answered_before:]
            cases_out.write(json.dumps(row, ensure_ascii=False) + "\n")
            cases_out.flush()
        if progress:
            returned = "ERROR" if result.errored else result.verdict
            print(f"{result.case_id:<8} expected {result.expected:<10} returned {returned:<10} "
                  f"{seconds:6.2f}s", flush=True)
    run.calls = budget.used
    return run


def score(results: List[CaseResult]) -> Dict[str, Any]:
    """Accuracy, SUSPICIOUS precision and recall, confusion matrix, per category.

    An ERROR is never a correct answer: it counts against accuracy and recall.
    It is only ever counted in the ERROR column, never as CLEAR.
    """
    n = len(results)
    correct = sum(r.verdict == r.expected for r in results)
    confusion = {exp: Counter() for exp in EXPECTED}
    for r in results:
        confusion[r.expected]["ERROR" if r.errored else r.verdict] += 1

    sus = Verdict.SUSPICIOUS.value
    tp = confusion[sus][sus]
    predicted_sus = tp + confusion[Verdict.CLEAR.value][sus]
    actual_sus = sum(confusion[sus].values())

    per_category: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
    for r in results:
        per_category[r.category][0] += r.verdict == r.expected
        per_category[r.category][1] += 1

    errored = sum(r.errored for r in results)
    errored_as_clear = [r.case_id for r in results if r.errored and r.verdict == Verdict.CLEAR.value]
    return {
        "n": n,
        "accuracy": (correct, n, wilson_interval(correct, n)),
        "precision": (tp, predicted_sus, wilson_interval(tp, predicted_sus)),
        "recall": (tp, actual_sus, wilson_interval(tp, actual_sus)),
        "confusion": {exp: dict(confusion[exp]) for exp in EXPECTED},
        "per_category": {c: tuple(per_category[c]) for c in ALL_CATEGORIES if c in per_category},
        "errored": (errored, n),
        "errored_as_clear": errored_as_clear,
    }


def _ratio(k: int, n: int, interval=None) -> str:
    if n == 0:
        return "n/a (0 cases)"
    text = f"{k}/{n} = {k / n:.0%}"
    if interval is not None:
        text += f" (95% CI {interval[0]:.0%} to {interval[1]:.0%})"
    return text


def display_path(path: Path) -> str:
    """Path relative to the project when it is inside it, else the file name."""
    try:
        return Path(path).resolve().relative_to(config.PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return Path(path).name


def report_lines(run: Run, metrics: Dict[str, Any], label: str,
                 cases_file: Path = CASES_PATH, excluded=()) -> List[str]:
    """The report as markdown; it also reads fine on the console."""
    lines = [f"## LLM tier evaluation: {label}", ""]
    lines.append(f"Case file: {display_path(cases_file)}; excluded categories: "
                 f"{', '.join(excluded) if excluded else 'none'}")
    lines.append(f"Cases reviewed: {metrics['n']} of {run.total_cases}; model calls: {run.calls}")
    if run.stopped_at_cap:
        lines.append("Stopped at the call cap; results below are partial.")
    lines += [
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Verdict accuracy | {_ratio(*metrics['accuracy'])} |",
        f"| SUSPICIOUS precision | {_ratio(*metrics['precision'])} |",
        f"| SUSPICIOUS recall | {_ratio(*metrics['recall'])} |",
        f"| Reviews that errored | {_ratio(*metrics['errored'])} |",
        f"| Errored reviews counted as CLEAR | {len(metrics['errored_as_clear'])} |",
        "",
        "| Expected \\ returned | SUSPICIOUS | CLEAR | ERROR |",
        "|---|---|---|---|",
    ]
    for exp in EXPECTED:
        row = metrics["confusion"][exp]
        lines.append(f"| {exp} | {row.get('SUSPICIOUS', 0)} | {row.get('CLEAR', 0)} | {row.get('ERROR', 0)} |")
    lines += ["", "| Category | Correct |", "|---|---|"]
    for category, (k, n) in metrics["per_category"].items():
        lines.append(f"| {category} | {_ratio(k, n)} |")
    return lines


def cost_line(meter, model: str, price_in=None, price_out=None) -> str:
    if meter is None or not (meter.input_tokens or meter.output_tokens):
        return "Token usage: not reported by the provider."
    prices = PRICES_PER_MILLION.get(model)
    if price_in is not None and price_out is not None:
        prices = (price_in, price_out)
    usage = f"Token usage: {meter.input_tokens:,} input, {meter.output_tokens:,} output."
    if prices is None:
        return usage + f" No price on file for {model}; pass --price-in and --price-out."
    cost = (meter.input_tokens * prices[0] + meter.output_tokens * prices[1]) / 1e6
    return usage + f" Estimated cost: ${cost:.4f} at ${prices[0]}/${prices[1]} per million tokens."


# --------------------------------------------------------------------------
# Rows on file, tool selection, usage
# --------------------------------------------------------------------------
def read_rows(path: Path) -> List[Dict[str, Any]]:
    """Every row of a --cases-out file (one JSON object per line)."""
    if not Path(path).exists():
        return []
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip()]


def results_from_rows(rows: List[Dict[str, Any]]) -> List[CaseResult]:
    """CaseResults from --cases-out rows. An errored row was recorded as ERROR."""
    return [CaseResult(case_id=r["id"], category=r["category"], expected=r["expected"],
                       verdict=r["returned"], errored=r["returned"] == Verdict.ERROR.value,
                       error_detail=r.get("error_detail")) for r in rows]


def tool_selection(rows: List[Dict[str, Any]], labels: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Tool choice against the pre-registered labels, pooled over cases.

    recall: required tools called / required tools, over cases with a
    required tool. precision: distinct tools called that are required or
    acceptable / distinct tools called, over cases with at least one call.
    Errored rows are included: a lookup made before an investigation stopped
    was still made. required_role counts whether party_history was called for
    the labelled party.
    """
    recall_k = recall_n = prec_k = prec_n = no_calls = role_k = role_n = 0
    unnecessary: Counter = Counter()
    missed: Counter = Counter()
    for row in rows:
        label = labels.get(row["id"])
        if label is None or "tools_called" not in row:
            continue
        called = set(row["tools_called"])
        required, allowed = set(label["required"]), set(label["required"]) | set(label["acceptable"])
        recall_n += len(required)
        recall_k += len(required & called)
        missed.update(required - called)
        if called:
            prec_n += len(called)
            prec_k += len(called & allowed)
            unnecessary.update(called - allowed)
        else:
            no_calls += 1
        for tool, role in (label.get("required_role") or {}).items():
            role_n += 1
            role_k += any(c["name"] == tool and (c.get("args") or {}).get("role") == role
                          for c in row.get("tool_calls") or [])
    return {"recall": (recall_k, recall_n), "precision": (prec_k, prec_n), "no_calls": no_calls,
            "required_role": (role_k, role_n), "unnecessary": dict(unnecessary),
            "missed": dict(missed)}


def _p95(values: List[float]) -> float:
    ordered = sorted(values)
    return ordered[max(math.ceil(0.95 * len(ordered)) - 1, 0)] if ordered else 0.0


def usage_summary(rows: List[Dict[str, Any]], prices=None) -> Dict[str, Any]:
    """Per-case means of calls, tokens and cost, and mean and 95th-percentile seconds."""
    n = len(rows)
    if n == 0:
        return {"n": 0}
    tokens_in = [r.get("input_tokens") or 0 for r in rows]
    tokens_out = [r.get("output_tokens") or 0 for r in rows]
    seconds = [r.get("seconds") or 0.0 for r in rows]
    summary = {
        "n": n,
        "model_calls": sum(r.get("model_calls") or 0 for r in rows) / n,
        "tool_calls": sum(len(r.get("tool_calls") or []) for r in rows) / n,
        "input_tokens": sum(tokens_in) / n,
        "output_tokens": sum(tokens_out) / n,
        "seconds_mean": sum(seconds) / n,
        "seconds_p95": _p95(seconds),
        "cost_total": None,
    }
    if prices is not None:
        total = (sum(tokens_in) * prices[0] + sum(tokens_out) * prices[1]) / 1e6
        summary["cost_total"] = total
        summary["cost_per_case"] = total / n
    return summary


def usage_lines(summary: Dict[str, Any]) -> List[str]:
    if not summary.get("n"):
        return []
    lines = ["", "| Per case | Value |", "|---|---|",
             f"| Model calls (mean) | {summary['model_calls']:.2f} |",
             f"| Tool calls (mean) | {summary['tool_calls']:.2f} |",
             f"| Input tokens (mean) | {summary['input_tokens']:,.0f} |",
             f"| Output tokens (mean) | {summary['output_tokens']:,.0f} |",
             f"| Seconds (mean / 95th percentile) | {summary['seconds_mean']:.1f} / "
             f"{summary['seconds_p95']:.1f} |"]
    if summary.get("cost_total") is not None:
        lines.append(f"| Estimated cost (per case / run) | ${summary['cost_per_case']:.4f} / "
                     f"${summary['cost_total']:.4f} |")
    return lines


def tool_lines(selection: Dict[str, Any]) -> List[str]:
    lines = ["", "| Tool selection | Value |", "|---|---|",
             f"| Recall (required tools called) | {_ratio(*selection['recall'])} |",
             f"| Precision (calls required or acceptable) | {_ratio(*selection['precision'])} |",
             f"| Cases with no tool call | {selection['no_calls']} |"]
    if selection["required_role"][1]:
        lines.append(f"| party_history on the labelled party | {_ratio(*selection['required_role'])} |")
    if selection["missed"]:
        lines.append("| Required but not called | "
                     + ", ".join(f"{k} {v}" for k, v in sorted(selection["missed"].items())) + " |")
    if selection["unnecessary"]:
        lines.append("| Unnecessary calls | "
                     + ", ".join(f"{k} {v}" for k, v in sorted(selection["unnecessary"].items())) + " |")
    return lines


def _prices(model: str, price_in=None, price_out=None):
    if price_in is not None and price_out is not None:
        return (price_in, price_out)
    return PRICES_PER_MILLION.get(model)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--cases", type=Path, default=CASES_PATH)
    parser.add_argument("--engine", choices=ENGINES, default="fixed",
                        help="the review to run (default: fixed)")
    parser.add_argument("--fake", choices=FAKE_MODES, default="oracle",
                        help="fake model behaviour (ignored with --live)")
    parser.add_argument("--live", action="store_true",
                        help="call the configured provider (costs money; needs credentials)")
    parser.add_argument("--max-calls", type=int, default=None,
                        help=f"cap on model calls including retries (default {DEFAULT_LIVE_MAX_CALLS} "
                             "with --live, none for fakes)")
    parser.add_argument("--out", type=Path, default=None, help="also write the report as markdown")
    parser.add_argument("--cases-out", type=Path, default=None,
                        help="append one JSON row per finished case to this JSONL file")
    parser.add_argument("--resume", action="store_true",
                        help="skip cases already in --cases-out and score the whole file")
    parser.add_argument("--pause", type=float, default=0.0,
                        help="seconds to wait after every live model call")
    parser.add_argument("--tool-labels", type=Path, default=TOOL_LABELS_PATH,
                        help="tool labels for scoring the agent's tool selection")
    parser.add_argument("--price-in", type=float, default=None, help="USD per million input tokens")
    parser.add_argument("--price-out", type=float, default=None, help="USD per million output tokens")
    parser.add_argument("--exclude-category", action="append", default=[], choices=ALL_CATEGORIES,
                        metavar="NAME", help="leave out every case in this category (repeatable)")
    args = parser.parse_args(argv)

    excluded = tuple(dict.fromkeys(args.exclude_category))
    all_cases = [c for c in load_cases(args.cases) if c["category"] not in excluded]
    if not all_cases:
        parser.error("no cases left after --exclude-category")
    if args.resume and not args.cases_out:
        parser.error("--resume needs --cases-out")
    spec = ledger_spec(args.cases)
    if args.engine == "fixed+history" and spec is None:
        parser.error("--engine fixed+history needs a case file with a ledger")
    ledger = build_case_ledger(spec) if spec is not None else None

    done = set()
    if args.resume:
        done = {r["id"] for r in read_rows(args.cases_out)}
    cases = [c for c in all_cases if c["id"] not in done]

    cap = args.max_calls
    if args.live and cap is None:
        cap = DEFAULT_LIVE_MAX_CALLS
    budget = CallBudget(cap)

    meter = fake = retriever = None
    if args.live:
        engine, rag_engine, meter, retriever = build_live(budget, args.engine, pause=args.pause)
        label = f"live, {config.MODEL_PROVIDER} {config.chat_model_name()}"
    else:
        # Fake failures need no backoff; this keeps the error mode instant.
        config.LLM_RETRY_BASE_DELAY = 0.0
        engine, rag_engine, fake = build_fake(args.fake, budget, args.engine)
        label = f"fake model, {args.fake}"
        if args.engine == "agent":
            rag_engine, retriever = None, rag_engine
    if args.engine != "fixed":
        label += f", engine {args.engine}"

    cases_out = None
    if args.cases_out:
        args.cases_out.parent.mkdir(parents=True, exist_ok=True)
        cases_out = args.cases_out.open("a", encoding="utf-8")
    try:
        run = run_cases(cases, engine, rag_engine, budget, fake=fake, meter=meter,
                        cases_out=cases_out, progress=True, engine_kind=args.engine,
                        ledger=ledger, retriever=retriever)
    finally:
        if cases_out is not None:
            cases_out.close()
    print()

    wanted = {c["id"] for c in all_cases}
    rows = [r for r in read_rows(args.cases_out) if r["id"] in wanted] if args.cases_out else []
    if args.resume:
        # Score every case on file, earlier invocations included.
        results = results_from_rows(rows)
        run = Run(results=results, total_cases=len(all_cases),
                  calls=sum(r.get("model_calls") or 0 for r in rows),
                  stopped_at_cap=run.stopped_at_cap)
    metrics = score(run.results)
    lines = report_lines(run, metrics, label, cases_file=args.cases, excluded=excluded)
    if args.live and not args.resume:
        lines += ["", cost_line(meter, config.chat_model_name(), args.price_in, args.price_out)]
    if rows:
        prices = _prices(config.chat_model_name(), args.price_in, args.price_out) if args.live else None
        lines += usage_lines(usage_summary(rows, prices))
    if args.engine == "agent" and rows and Path(args.tool_labels).exists():
        lines += tool_lines(tool_selection(rows, load_tool_labels(args.tool_labels)))
    if retriever is not None and getattr(retriever, "cache_dir", None) is not None:
        lines += ["", f"Guidance searches: {retriever.misses} sent to the index, "
                      f"{retriever.hits} served from the cache."]
    if spec is None:
        lines += ["", PASS_THROUGH_NOTE]

    text = "\n".join(lines)
    print(text)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"\nWrote {args.out}")
    if args.cases_out:
        print(f"Appended {len(cases) if not run.stopped_at_cap else 'some'} case rows to {args.cases_out}")

    if metrics["errored_as_clear"]:
        print(f"\nFAILED: errored reviews counted as CLEAR: {metrics['errored_as_clear']}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
