"""Measure the pipeline against a labelled ledger.

Replaces the original stress_test.py, which reported an "accuracy" figure for a
code path the application never executed: it called the audit engine directly
and skipped Tiers 0 and 1 entirely, so its $9,990 Cayman Islands case passed the
benchmark while being silently discarded in production.

This harness measures the tiers the app actually runs, and by default needs no
LLM and no network. Pass --with-llm to include the compliance tier.

    python scripts/evaluate.py
    python scripts/evaluate.py --with-llm
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from utils.data_loader import load_ledger  # noqa: E402
from utils.funnel import run_funnel  # noqa: E402
from utils.rules import apply_rules  # noqa: E402
from utils.screening import rule_screener  # noqa: E402

LABELLED = config.DATA_DIR / "sample_ledger_labelled.csv"


def metrics(truth: pd.Series, predicted: pd.Series) -> dict:
    tp = int((predicted & (truth == 1)).sum())
    fp = int((predicted & (truth == 0)).sum())
    fn = int((~predicted & (truth == 1)).sum())
    tn = int((~predicted & (truth == 0)).sum())
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": precision, "recall": recall, "f1": f1}


def legacy_pipeline(df: pd.DataFrame) -> pd.Series:
    """Reproduce the original ordering, for comparison.

    Amount-only IsolationForest(contamination=0.15), then a rule pass that drops
    anything under the reporting threshold that is not from a high-risk country.
    """
    from sklearn.ensemble import IsolationForest

    kept = IsolationForest(contamination=0.15, random_state=42).fit_predict(df[["amount_float"]]) == -1
    high_risk = df["country"].str.lower().apply(
        lambda c: any(h in c for h in config.HIGH_RISK_COUNTRIES)
    )
    tier1 = (df["amount_float"] >= float(config.REPORTING_THRESHOLD)) | high_risk
    return pd.Series(kept, index=df.index) & tier1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, default=LABELLED)
    parser.add_argument("--with-llm", action="store_true", help="also run the compliance tier")
    args = parser.parse_args()

    if not args.ledger.exists():
        print(f"No labelled ledger at {args.ledger}.")
        print("Run: python scripts/generate_sample_ledger.py")
        return 1

    labels = pd.read_csv(args.ledger)
    ledger, warnings = load_ledger(args.ledger)
    for warning in warnings:
        print(f"  warning: {warning}")

    # load_ledger passes unknown columns through, so drop any label columns it
    # carried in before merging the authoritative ones back on.
    ledger = ledger.drop(columns=["is_laundering", "pattern"], errors="ignore").merge(
        labels[["transaction_id", "is_laundering", "pattern"]], on="transaction_id", how="left"
    )
    truth = ledger["is_laundering"].fillna(0).astype(int)

    result = run_funnel(apply_rules(ledger, screener=rule_screener))
    kept_ids = set(result.flagged["transaction_id"])
    predicted = ledger["transaction_id"].isin(kept_ids)
    legacy_predicted = legacy_pipeline(ledger)

    print("\n" + "=" * 66)
    print(f"FUNNEL EVALUATION  ({len(ledger):,} rows, {int(truth.sum())} labelled laundering)")
    print("=" * 66)

    print(f"\n{'pattern':<16}{'total':>7}{'caught':>8}{'recall':>9}   (current pipeline)")
    for pattern, group in ledger.groupby("pattern"):
        caught = predicted[group.index]
        print(f"{pattern:<16}{len(group):>7}{int(caught.sum()):>8}{caught.mean():>8.0%}")

    print(f"\n{'pattern':<16}{'total':>7}{'caught':>8}{'recall':>9}   (original pipeline, for comparison)")
    for pattern, group in ledger.groupby("pattern"):
        caught = legacy_predicted[group.index]
        print(f"{pattern:<16}{len(group):>7}{int(caught.sum()):>8}{caught.mean():>8.0%}")

    current = metrics(truth, predicted)
    legacy = metrics(truth, legacy_predicted)

    print(f"\n{'':<14}{'recall':>9}{'precision':>11}{'F1':>8}{'forwarded':>11}")
    for name, m, fwd in (
        ("original", legacy, legacy["tp"] + legacy["fp"]),
        ("current", current, result.forwarded),
    ):
        print(f"{name:<14}{m['recall']:>8.0%}{m['precision']:>11.0%}{m['f1']:>8.2f}"
              f"{fwd:>7} ({fwd / len(ledger):.0%})")

    print(f"\nMissed by the original but caught now: {legacy['fn'] - current['fn']}")
    print(f"Rows kept by a rule despite a normal model score: {result.rescued_by_rules}")
    print(f"Tier 0+1 latency: {result.elapsed_ms:.0f} ms")
    if result.top_features:
        print("Top separating features: " + ", ".join(result.top_features))

    missed = ledger[(truth == 1) & ~predicted]
    if not missed.empty:
        print(f"\nFALSE NEGATIVES ({len(missed)}):")
        print(missed[["transaction_id", "amount_float", "country", "pattern"]].to_string(index=False))

    if args.with_llm:
        print("\n" + "=" * 66)
        print("COMPLIANCE TIER (live LLM)")
        print("=" * 66)
        try:
            from utils.agents import ComplianceAuditEngine
            from utils.chat_agent import ComplianceIntelligenceProvider

            provider = ComplianceIntelligenceProvider()
            engine = ComplianceAuditEngine(llm=provider.llm)
        except Exception as exc:
            print(f"Backends unavailable, skipping: {exc}")
            return 0

        sample = result.flagged.head(20)
        verdicts = []
        for _, row in sample.iterrows():
            outcome = engine.assess_transaction(
                transaction_id=row["transaction_id"], amount=abs(row["amount"]),
                country=row.get("country", ""), sender=row.get("sender", ""),
                receiver=row.get("receiver", ""), rule_reasons=row.get("rule_reasons"),
                rag_engine=provider.engine,
            )
            verdicts.append(outcome.verdict.value)
            print(f"  {row['transaction_id']:<12} {outcome.verdict.value:<11} "
                  f"conf={outcome.confidence:.2f}  {outcome.rationale[:60]}")

        counts = pd.Series(verdicts).value_counts().to_dict()
        print(f"\nVerdicts across {len(sample)} sampled rows: {counts}")
        if counts.get("ERROR"):
            print(f"  {counts['ERROR']} assessment(s) errored. These stay open, never cleared.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
