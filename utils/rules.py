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


def is_self_transfer(sender: object, receiver: object) -> bool:
    """True when money moves between one account and itself.

    Unidentified parties ("Unknown_Entity" on both sides) are not a
    self-transfer: two unknowns are not known to be the same account.
    """
    s, r = str(sender or "").strip(), str(receiver or "").strip()
    return bool(s) and s == r and s.lower() not in config.PLACEHOLDER_ENTITIES


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
    # Money an account moves to itself is internal: the amount rules do not
    # apply, but sanctions and jurisdiction checks still do.
    internal = is_self_transfer(sender, receiver)

    if internal:
        notes.append(
            "Self-transfer: sender and receiver are the same account, so the amount rules "
            "and the behavioural model do not score it."
        )

    if not internal and magnitude >= config.REPORTING_THRESHOLD:
        reasons.append(
            f"Value {magnitude:,.2f} is at or above the {config.REPORTING_THRESHOLD:,.0f} reporting threshold."
        )

    # The band the original pipeline threw away.
    if not internal and config.STRUCTURING_FLOOR <= magnitude < config.REPORTING_THRESHOLD:
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
    """Add `rule_flag`, `rule_reasons`, `sanctions_hits` and `self_transfer` columns.

    A screener that also has a `screen_many(names)` method is called once with
    every distinct sender and receiver, instead of once per row.
    """
    screen_many = getattr(screener, "screen_many", None)
    if screen_many is not None:
        names = set()
        for col in ("sender", "receiver"):
            if col in df.columns:
                names.update(str(value) for value in df[col])
        found = screen_many(sorted(n for n in names if n))
        screener = found.get

    flags, reasons, sanctions, internal = [], [], [], []
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
        internal.append(is_self_transfer(row.get("sender", ""), row.get("receiver", "")))

    out = df.copy()
    out["rule_flag"] = flags
    out["rule_reasons"] = reasons
    out["sanctions_hits"] = sanctions
    out["self_transfer"] = internal
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
