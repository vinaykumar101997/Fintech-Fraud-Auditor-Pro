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
from utils.jurisdictions import is_blank, resolve_country
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
    """Apply every deterministic rule to a single transaction.

    Notes (low severity) are reported alongside the reasons but do not flag
    the row on their own.
    """
    result = RuleResult()
    reasons: List[str] = []
    notes: List[str] = []
    magnitude = abs(amount)
    country_code = resolve_country(country)

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

    if country_code is None:
        if is_blank(country):
            notes.append("Unresolved jurisdiction: no country given.")
        else:
            notes.append(
                f"Unresolved jurisdiction: '{country}' is not a recognised country name or ISO code."
            )
    else:
        if country_code in config.HIGH_RISK_COUNTRIES:
            reasons.append(
                f"Counterparty jurisdiction '{country}' ({country_code}) is on the high-risk list."
            )
        if country_code in config.SECRECY_JURISDICTIONS:
            reasons.append(
                f"Counterparty jurisdiction '{country}' ({country_code}) is a recognised secrecy jurisdiction."
            )

    for label, name in (("Sender", sender), ("Receiver", receiver)):
        if not name:
            continue
        hit = screener(name)
        if hit:
            reasons.append(f"{label} '{name}' matches sanctions list entry '{hit}'.")
            result.sanctions_hits.append((label, name, hit))

    if str(sender).strip().lower() in config.PLACEHOLDER_ENTITIES and str(
        receiver
    ).strip().lower() in config.PLACEHOLDER_ENTITIES:
        reasons.append("Both counterparties are unidentified.")

    if reasons:
        result.flagged = True
        result.force_review = True
    result.reasons = reasons + notes
    return result


def apply_rules(df: pd.DataFrame, screener: Screener = _no_screening) -> pd.DataFrame:
    """Add `rule_flag`, `rule_reasons` and `sanctions_hits` columns to the ledger."""
    flags, reasons, sanctions = [], [], []
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
        sanctions.append(outcome.sanctions_hits)

    out = df.copy()
    out["rule_flag"] = flags
    out["rule_reasons"] = reasons
    out["sanctions_hits"] = sanctions
    return out


def sanctions_hit_events(ruled: pd.DataFrame, *, session_id: str, batch_id: str) -> List[dict]:
    """sanctions_hit audit events (one per matched party) for a ruled batch.

    Pure: the caller writes them with audit_log.record_many, before releasing
    the batch, so a match that cannot be recorded blocks the batch.
    """
    events = []
    for _, row in ruled.iterrows():
        for label, name, entry in row.get("sanctions_hits") or []:
            events.append(dict(
                session_id=session_id, batch_id=batch_id,
                transaction_id=str(row["transaction_id"]), stage="sanctions_hit",
                verdict="SUSPICIOUS", rationale=f"{label} '{name}' matched {entry}",
                detail=f"{label}: {name}",
            ))
    return events
