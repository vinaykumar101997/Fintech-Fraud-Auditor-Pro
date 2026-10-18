"""Measure the pipeline against a labelled ledger.

Replaces the original stress_test.py, which reported an "accuracy" figure for a
code path the application never executed: it called the audit engine directly
and skipped Tiers 0 and 1 entirely, so its $9,990 Cayman Islands case passed the
benchmark while being silently discarded in production.

This harness measures the tiers the app actually runs, and by default needs no
LLM and no network. Pass --with-llm to include the compliance tier.

    python scripts/evaluate.py
    python scripts/evaluate.py --with-llm
    python scripts/evaluate.py --min-recall 1.0 --min-pattern-recall 1.0 \
        --gate-exclude circular_camouflaged
    python scripts/evaluate.py --seeds 7 11 13 17 19 23

--seeds generates a fresh labelled ledger per seed in memory and reports each
one, then pooled counts over the held-out seeds with Wilson 95% intervals.
Seed 7 is the development seed: thresholds were chosen while looking at it.
"""

from __future__ import annotations

import argparse
import io
import math
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from utils.data_loader import load_ledger  # noqa: E402
from utils.funnel import run_funnel  # noqa: E402
from utils.rules import apply_rules  # noqa: E402
from utils.screening import rule_screener  # noqa: E402

LABELLED = config.DATA_DIR / "sample_ledger_labelled.csv"

LEGACY_HIGH_RISK_NAMES = (
    "russia", "north korea", "iran", "syria", "belarus", "myanmar", "afghanistan",
)


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


def rule_flagged_dropped_ids(dropped: pd.DataFrame) -> list[str]:
    """Transaction IDs of any rule-flagged row the funnel dropped.

    Should always be empty - utils.funnel.run_funnel guarantees a rule hit is
    force-review - so this is the check for that invariant, not a duplicate
    of it. In every path this module drives it, `dropped` always carries
    `rule_flag` (run_funnel raises if the input lacks it, and filtering never
    drops a column, even down to zero rows); the column check below is a
    guard against a future run_funnel refactor changing that, not a known gap.
    """
    if "rule_flag" not in dropped.columns:
        return []
    return list(dropped.loc[dropped["rule_flag"], "transaction_id"])


def quality_gate(
    current: dict,
    pattern_recall: dict,
    rule_dropped_ids: list,
    min_recall: float | None,
    min_pattern_recall: float | None,
) -> list[str]:
    """Pure gate logic: returns failure messages, empty if the gate passes."""
    failures: list[str] = []

    if rule_dropped_ids:
        failures.append(
            f"{len(rule_dropped_ids)} rule-flagged row(s) were dropped by the funnel: "
            + ", ".join(rule_dropped_ids)
        )

    if min_recall is not None and current["recall"] < min_recall:
        failures.append(
            f"overall recall {current['recall']:.0%} is below --min-recall {min_recall:.0%}"
        )

    if min_pattern_recall is not None:
        for pattern, recall in sorted(pattern_recall.items()):
            if recall < min_pattern_recall:
                failures.append(
                    f"pattern '{pattern}' recall {recall:.0%} is below "
                    f"--min-pattern-recall {min_pattern_recall:.0%}"
                )

    return failures


def legacy_pipeline(df: pd.DataFrame) -> pd.Series:
    """Reproduce the original ordering, for comparison.

    Amount-only IsolationForest(contamination=0.15), then a rule pass that drops
    anything under the reporting threshold that is not from a high-risk country.
    """
    from sklearn.ensemble import IsolationForest

    kept = IsolationForest(contamination=0.15, random_state=42).fit_predict(df[["amount_float"]]) == -1
    # The original build's list and substring match, frozen here so the
    # comparison does not move when the current rules change.
    high_risk = df["country"].str.lower().apply(
        lambda c: any(h in c for h in LEGACY_HIGH_RISK_NAMES)
    )
    tier1 = (df["amount_float"] >= float(config.REPORTING_THRESHOLD)) | high_risk
    return pd.Series(kept, index=df.index) & tier1


DEV_SEED = 7
HELD_OUT_SEEDS = (11, 13, 17, 19, 23)


def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion k/n (95% by default)."""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def _country_rule_fired(reasons) -> bool:
    return any("high-risk list" in r or "secrecy jurisdiction" in r for r in reasons or [])


def evaluate_seed(seed: int, hard: bool = False) -> dict:
    """Generate the labelled ledger for `seed` and score Tiers 0+1 on it.

    Goes through the same CSV round trip and loader as the file-based run, so
    seed 7 reproduces the numbers of `python scripts/evaluate.py`. With
    hard=True the ledger also carries the harder patterns.
    """
    from generate_sample_ledger import generate

    text = generate(seed=seed, hard=hard).to_csv(index=False)
    labels = pd.read_csv(io.StringIO(text))
    ledger, _ = load_ledger(io.StringIO(text))
    ledger = ledger.drop(columns=["is_laundering", "pattern"], errors="ignore").merge(
        labels[["transaction_id", "is_laundering", "pattern"]], on="transaction_id", how="left"
    )
    truth = ledger["is_laundering"].fillna(0).astype(int)
    ruled = apply_rules(ledger, screener=rule_screener)
    result = run_funnel(ruled)
    predicted = ledger["transaction_id"].isin(set(result.flagged["transaction_id"]))
    m = metrics(truth, predicted)
    name_fired = ruled["sanctions_hits"].map(bool)
    country_fired = ruled["rule_reasons"].map(_country_rule_fired)
    return {
        "seed": seed,
        "rows": len(ledger),
        "laundering": int(truth.sum()),
        "tp": m["tp"], "fp": m["fp"], "fn": m["fn"], "tn": m["tn"],
        "forwarded": int(result.forwarded),
        "per_pattern": {
            pattern: (len(group), int(predicted[group.index].sum()))
            for pattern, group in ledger.groupby("pattern")
        },
        # (rows where the sanctions-name rule fired, rows where a country rule fired)
        "rule_hits": {
            pattern: (int(name_fired[group.index].sum()), int(country_fired[group.index].sum()))
            for pattern, group in ledger.groupby("pattern")
        },
    }


def _sum_pairs(runs: list[dict], key: str) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for run in runs:
        for pattern, (a, b) in run.get(key, {}).items():
            x, y = out.get(pattern, (0, 0))
            out[pattern] = (x + a, y + b)
    return out


def pool(runs: list[dict]) -> dict:
    """Sum the counts of several seed runs."""
    pooled = {key: sum(r[key] for r in runs)
              for key in ("rows", "laundering", "tp", "fp", "fn", "tn", "forwarded")}
    pooled["per_pattern"] = _sum_pairs(runs, "per_pattern")
    pooled["rule_hits"] = _sum_pairs(runs, "rule_hits")
    return pooled


def _rates(r: dict) -> tuple[float, float, float]:
    recall = r["tp"] / (r["tp"] + r["fn"]) if r["tp"] + r["fn"] else 0.0
    precision = r["tp"] / (r["tp"] + r["fp"]) if r["tp"] + r["fp"] else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return recall, precision, f1


def report_seeds(seeds: list[int], hard: bool = False) -> int:
    runs = [evaluate_seed(s, hard=hard) for s in seeds]

    print("\n" + "=" * 78)
    print(f"FUNNEL EVALUATION BY SEED  (development seed {DEV_SEED}; others held out"
          + ("; harder patterns included)" if hard else ")"))
    print("=" * 78)
    print(f"\n{'seed':<6}{'role':<10}{'rows':>6}{'laund.':>8}{'TP':>5}{'FP':>5}{'FN':>5}"
          f"{'recall':>9}{'precision':>11}{'F1':>7}{'forwarded':>11}")
    for r in runs:
        recall, precision, f1 = _rates(r)
        role = "dev" if r["seed"] == DEV_SEED else "held-out"
        print(f"{r['seed']:<6}{role:<10}{r['rows']:>6}{r['laundering']:>8}{r['tp']:>5}{r['fp']:>5}"
              f"{r['fn']:>5}{recall:>8.0%}{precision:>11.0%}{f1:>7.2f}{r['forwarded']:>11}")

    patterns = sorted({p for r in runs for p in r["per_pattern"]})
    print("\ncaught/total by pattern")
    print(f"{'pattern':<22}" + "".join(f"{r['seed']:>9}" for r in runs))
    for pattern in patterns:
        cells = []
        for r in runs:
            total, caught = r["per_pattern"].get(pattern, (0, 0))
            cells.append(f"{caught}/{total}")
        print(f"{pattern:<22}" + "".join(f"{c:>9}" for c in cells))

    held_out = [r for r in runs if r["seed"] != DEV_SEED]
    groups = [(f"seed {r['seed']}", r) for r in runs if r["seed"] == DEV_SEED]
    if held_out:
        groups.append(("pooled held-out", pool(held_out)))
    for label, r in groups:
        print(f"\nrule hits by pattern, {label}: rows / name rule / country rule")
        for pattern in patterns:
            total, _ = r["per_pattern"].get(pattern, (0, 0))
            name, country = r["rule_hits"].get(pattern, (0, 0))
            print(f"  {pattern:<28}{total:>6}{name:>6}{country:>6}")

    if held_out:
        p = pool(held_out)
        recall, precision, f1 = _rates(p)
        r_lo, r_hi = wilson_interval(p["tp"], p["tp"] + p["fn"])
        p_lo, p_hi = wilson_interval(p["tp"], p["tp"] + p["fp"])
        names = ", ".join(str(r["seed"]) for r in held_out)
        print(f"\nPOOLED HELD-OUT  (seeds {names})")
        print(f"  rows {p['rows']}, laundering {p['laundering']}, forwarded {p['forwarded']}")
        print(f"  TP {p['tp']}  FP {p['fp']}  FN {p['fn']}  TN {p['tn']}")
        print(f"  recall    {recall:.1%}  ({p['tp']}/{p['tp'] + p['fn']}), "
              f"95% Wilson {r_lo:.1%} to {r_hi:.1%}")
        print(f"  precision {precision:.1%}  ({p['tp']}/{p['tp'] + p['fp']}), "
              f"95% Wilson {p_lo:.1%} to {p_hi:.1%}")
        print(f"  F1        {f1:.2f}")
        print("  by pattern: " + ", ".join(
            f"{pat} {c}/{t}" for pat, (t, c) in sorted(p["per_pattern"].items())))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, default=LABELLED)
    parser.add_argument("--with-llm", action="store_true", help="also run the compliance tier")
    parser.add_argument(
        "--min-recall", type=float, default=None,
        help="exit 1 if overall recall on the labelled set falls below this (0-1)",
    )
    parser.add_argument(
        "--min-pattern-recall", type=float, default=None,
        help="exit 1 if any single laundering typology's recall falls below this (0-1)",
    )
    parser.add_argument(
        "--gate-exclude", action="append", default=[], metavar="PATTERN",
        help="typology to report normally but exclude from --min-recall and "
             "--min-pattern-recall entirely (repeatable) - for a known, "
             "accepted gap rather than something --min-pattern-recall should fail on",
    )
    parser.add_argument(
        "--seeds", type=int, nargs="+", metavar="SEED",
        help=f"generate and score one ledger per seed (development seed {DEV_SEED}, "
             f"held out: {' '.join(map(str, HELD_OUT_SEEDS))}); prints results, no gate",
    )
    parser.add_argument(
        "--hard", action="store_true",
        help="with --seeds: add the harder patterns (established accounts, name and "
             "country variants, near-miss clean names) to each generated ledger",
    )
    args = parser.parse_args(argv)

    if args.hard and not args.seeds:
        parser.error("--hard needs --seeds")

    if args.seeds:
        if args.with_llm or args.min_recall is not None or args.min_pattern_recall is not None \
                or args.gate_exclude:
            parser.error("--seeds prints results only; it cannot be combined with --with-llm or the gate options")
        return report_seeds(args.seeds, hard=args.hard)

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

    # Per-pattern recall, restricted to typologies that actually contain
    # labelled laundering rows - "clean" would otherwise report a meaningless
    # "recall" (it is a false-positive rate, not a recall, for truth-negative rows).
    pattern_recall: dict[str, float] = {}
    print(f"\n{'pattern':<16}{'total':>7}{'caught':>8}{'recall':>9}   (current pipeline)")
    for pattern, group in ledger.groupby("pattern"):
        caught = predicted[group.index]
        print(f"{pattern:<16}{len(group):>7}{int(caught.sum()):>8}{caught.mean():>8.0%}")
        if truth[group.index].sum() > 0:
            pattern_recall[pattern] = float(caught.mean())

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

    # Excluded typologies are dropped from the gate's inputs entirely - not
    # just skipped in the per-pattern loop - so a known, accepted gap (e.g.
    # circular_camouflaged) cannot drag the *overall* --min-recall down either.
    # The report above still shows its true recall; only the pass/fail
    # decision ignores it.
    gate_exclude = set(args.gate_exclude)
    gate_mask = ~ledger["pattern"].isin(gate_exclude)
    gate_metrics = metrics(truth[gate_mask], predicted[gate_mask]) if gate_exclude else current
    gated_pattern_recall = {p: r for p, r in pattern_recall.items() if p not in gate_exclude}

    gate_failures = quality_gate(
        current=gate_metrics,
        pattern_recall=gated_pattern_recall,
        rule_dropped_ids=rule_flagged_dropped_ids(result.dropped),
        min_recall=args.min_recall,
        min_pattern_recall=args.min_pattern_recall,
    )

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
            provider = engine = None

        if engine is not None:
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

    if gate_failures:
        print("\n" + "=" * 66)
        print("QUALITY GATE FAILED")
        print("=" * 66)
        for msg in gate_failures:
            print(f"  - {msg}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
