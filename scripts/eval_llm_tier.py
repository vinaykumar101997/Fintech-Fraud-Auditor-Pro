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
    python scripts/eval_llm_tier.py --live --max-calls 40 --out llm_eval.md

--live runs the configured provider (MODEL_PROVIDER) through the same factory
and retrieval stack the app uses. --max-calls caps every model call, including
retries; once the cap is reached no further call is made, the case in progress
is dropped and the cases completed so far are reported.

Each case is passed to the engine with exactly the arguments the app passes:
the transaction fields and the rule reasons. The behaviour-model flags in the
case file describe why such a row would be forwarded, but the engine has no
parameter for them, so the review does not see them.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from evaluate import wilson_interval  # noqa: E402
from utils.verdicts import AuditOutcome, Verdict  # noqa: E402

CASES_PATH = config.PROJECT_ROOT / "tests" / "fixtures" / "llm_cases.json"
CATEGORIES = (
    "obvious_clear", "over_threshold_legitimate", "structuring", "high_risk_jurisdiction",
    "layering_shell", "pass_through", "prompt_injection",
)
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
        if case.get("category") not in CATEGORIES:
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


class FakeRetrievalQA:
    """Stands in for the RetrievalQA chain: fixed passages, one model call."""

    def __init__(self, llm: FakeLLM, passages: int = 5):
        self.llm = llm
        self.passages = passages

    def invoke(self, payload: Dict[str, str]) -> Dict[str, Any]:
        self.llm.call()
        docs = [f"Fake regulatory passage {i + 1}." for i in range(self.passages)]
        return {"result": "Fake first-pass finding.", "source_documents": docs}


# --------------------------------------------------------------------------
# Live provider
# --------------------------------------------------------------------------
def usage_meter(budget: CallBudget):
    """LangChain callback that spends the budget on every model call and
    collects token usage. raise_error makes a refused call fail before any
    request is sent."""
    from langchain_core.callbacks import BaseCallbackHandler

    class UsageMeter(BaseCallbackHandler):
        raise_error = True

        def __init__(self):
            self.input_tokens = 0
            self.output_tokens = 0

        def on_chat_model_start(self, *args, **kwargs):
            budget.spend()

        def on_llm_start(self, *args, **kwargs):
            budget.spend()

        def on_llm_end(self, response, **kwargs):
            for generations in response.generations:
                for gen in generations:
                    usage = getattr(getattr(gen, "message", None), "usage_metadata", None) or {}
                    self.input_tokens += usage.get("input_tokens", 0)
                    self.output_tokens += usage.get("output_tokens", 0)

    return UsageMeter()


def build_live(budget: CallBudget):
    """Engine and retrieval chain from the app's own factory, metered."""
    from utils.agents import ComplianceAuditEngine
    from utils.chat_agent import ComplianceIntelligenceProvider

    provider = ComplianceIntelligenceProvider()
    meter = usage_meter(budget)
    provider.llm.callbacks = [meter]  # before the engine binds the model
    return ComplianceAuditEngine(llm=provider.llm), provider.engine, meter


def build_fake(mode: str, budget: CallBudget):
    from utils.agents import ComplianceAuditEngine

    llm = FakeLLM(mode, budget)
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


def _is_cap_failure(outcome: AuditOutcome) -> bool:
    return bool(outcome.error_detail) and CallCapReached.__name__ in outcome.error_detail


def run_cases(cases, engine, rag_engine, budget: CallBudget, fake: Optional[FakeLLM] = None) -> Run:
    """Assess each case in order; stop cleanly when the call budget runs out."""
    run = Run(total_cases=len(cases))
    for case in cases:
        if budget.exhausted:
            run.stopped_at_cap = True
            break
        if fake is not None:
            fake.case = case
        txn = case["transaction"]
        outcome = engine.assess_transaction(
            transaction_id=case["id"],
            amount=abs(Decimal(txn["amount"])),
            country=txn["country"],
            sender=txn["sender"],
            receiver=txn["receiver"],
            rule_reasons=case["rule_reasons"],
            rag_engine=rag_engine,
        )
        if _is_cap_failure(outcome):
            # Cut off by the cap, not a verdict: dropped from the results.
            run.stopped_at_cap = True
            break
        run.results.append(CaseResult(
            case_id=case["id"], category=case["category"], expected=case["expected_verdict"],
            verdict=outcome.verdict.value,
            errored=outcome.verdict is Verdict.ERROR or outcome.error_detail is not None,
            error_detail=outcome.error_detail,
        ))
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
        "per_category": {c: tuple(per_category[c]) for c in CATEGORIES if c in per_category},
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


def report_lines(run: Run, metrics: Dict[str, Any], label: str) -> List[str]:
    """The report as markdown; it also reads fine on the console."""
    lines = [f"## LLM tier evaluation: {label}", ""]
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


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--cases", type=Path, default=CASES_PATH)
    parser.add_argument("--fake", choices=FAKE_MODES, default="oracle",
                        help="fake model behaviour (ignored with --live)")
    parser.add_argument("--live", action="store_true",
                        help="call the configured provider (costs money; needs credentials)")
    parser.add_argument("--max-calls", type=int, default=None,
                        help=f"cap on model calls including retries (default {DEFAULT_LIVE_MAX_CALLS} "
                             "with --live, none for fakes)")
    parser.add_argument("--out", type=Path, default=None, help="also write the report as markdown")
    parser.add_argument("--price-in", type=float, default=None, help="USD per million input tokens")
    parser.add_argument("--price-out", type=float, default=None, help="USD per million output tokens")
    args = parser.parse_args(argv)

    cases = load_cases(args.cases)
    cap = args.max_calls
    if args.live and cap is None:
        cap = DEFAULT_LIVE_MAX_CALLS
    budget = CallBudget(cap)

    meter = fake = None
    if args.live:
        engine, rag_engine, meter = build_live(budget)
        label = f"live, {config.MODEL_PROVIDER} {config.chat_model_name()}"
    else:
        # Fake failures need no backoff; this keeps the error mode instant.
        config.LLM_RETRY_BASE_DELAY = 0.0
        engine, rag_engine, fake = build_fake(args.fake, budget)
        label = f"fake model, {args.fake}"

    run = run_cases(cases, engine, rag_engine, budget, fake=fake)
    metrics = score(run.results)
    lines = report_lines(run, metrics, label)
    if args.live:
        lines += ["", cost_line(meter, config.chat_model_name(), args.price_in, args.price_out)]

    text = "\n".join(lines)
    print(text)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"\nWrote {args.out}")

    if metrics["errored_as_clear"]:
        print(f"\nFAILED: errored reviews counted as CLEAR: {metrics['errored_as_clear']}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
