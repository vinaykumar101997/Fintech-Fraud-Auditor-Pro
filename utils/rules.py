"""Deterministic rules. These run BEFORE the statistical funnel.

The ordering is the whole point. In the original build, Isolation Forest ran
first on the dollar amount and discarded anything statistically ordinary, so a
$500 transfer from a sanctioned jurisdiction never reached the country check,
and a $9,400 structuring leg never reached anything at all.

Here, a rule hit sets force_review, and the funnel is not permitted to drop a
row that carries it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Callable, List, Optional

import pandas as pd

import config
from utils.verdicts import RuleResult

Screener = Callable[[str], Optional[str]]


def _no_screening(_: str) -> Optional[str]:
    return None


def evaluate_row(
    amount: Decimal,
    country: str,
    sender: str = "",
    receiver: str = "",
    screener: Screener = _no_screening,
) -> RuleResult:
    """Apply every deterministic rule to a single transaction."""
    result = RuleResult()
    reasons: List[str] = []
    magnitude = abs(amount)
    country_norm = (country or "").strip().lower()

    if magnitude >= config.REPORTING_THRESHOLD:
        reasons.append(
            f"Value {magnitude:,.2f} is at or above the {config.REPORTING_THRESHOLD:,.0f} reporting threshold."
        )

    # The band the original pipeline threw away.
    if config.STRUCTURING_FLOOR <= magnitude < config.REPORTING_THRESHOLD:
        reasons.append(
            f"Value {magnitude:,.2f} sits in the structuring band "
            f"({config.STRUCTURING_FLOOR:,.0f}-{config.REPORTING_THRESHOLD:,.0f}), just below the reporting threshold."
        )

    if any(c in country_norm for c in config.HIGH_RISK_COUNTRIES if c):
        reasons.append(f"Counterparty jurisdiction '{country}' is on the high-risk list.")

    if any(c in country_norm for c in config.SECRECY_JURISDICTIONS if c):
        reasons.append(f"Counterparty jurisdiction '{country}' is a recognised secrecy jurisdiction.")

    for label, name in (("Sender", sender), ("Receiver", receiver)):
        if not name:
            continue
        hit = screener(name)
        if hit:
            reasons.append(f"{label} '{name}' matches sanctions list entry '{hit}'.")

    if str(sender).strip().lower() in config.PLACEHOLDER_ENTITIES and str(
        receiver
    ).strip().lower() in config.PLACEHOLDER_ENTITIES:
        reasons.append("Both counterparties are unidentified.")

    if reasons:
        result.flagged = True
        result.force_review = True
        result.reasons = reasons
    return result


def apply_rules(df: pd.DataFrame, screener: Screener = _no_screening) -> pd.DataFrame:
    """Add `rule_flag` and `rule_reasons` columns to the ledger."""
    flags, reasons = [], []
    for _, row in df.iterrows():
        outcome = evaluate_row(
            amount=row["amount"],
            country=row.get("country", ""),
            sender=str(row.get("sender", "")),
            receiver=str(row.get("receiver", "")),
            screener=screener,
        )
        flags.append(outcome.force_review)
        reasons.append(outcome.reasons)

    out = df.copy()
    out["rule_flag"] = flags
    out["rule_reasons"] = reasons
    return out
