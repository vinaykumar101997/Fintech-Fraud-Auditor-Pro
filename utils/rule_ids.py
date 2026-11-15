"""Stable identifiers for the reasons utils.rules.evaluate_row writes.

The rules engine reports each hit as an English sentence. Anything that needs
to know which rule fired (the per-rule breakdown in scripts/eval_external.py,
the retrieval query in utils/retrieval_query.py) maps the sentence back to an
identifier here, so there is one table to keep in step with utils/rules.py.
tests/test_eval_external.py checks that every reason maps to exactly one id.
"""

from __future__ import annotations

from typing import Iterable, List

# Text unique to each reason utils.rules.evaluate_row writes. Matched as a
# substring; "reporting threshold" alone would also match the structuring band.
RULE_REASONS = {
    "reporting_threshold": "is at or above the",
    "structuring_band": "sits in the structuring band",
    "high_risk_country": "is on the high-risk list",
    "secrecy_jurisdiction": "is a recognised secrecy jurisdiction",
    "sanctions_name": "matches sanctions list entry",
    "unidentified_parties": "Both counterparties are unidentified",
    "unresolved_jurisdiction": "Unresolved jurisdiction",
    "self_transfer": "Self-transfer: sender and receiver are the same account",
    "non_cash_channel": "is not a cash channel",
}
# Low-severity notes: reported with the reasons but never forward a row.
NOTES = {"unresolved_jurisdiction", "self_transfer", "non_cash_channel"}


def rule_id(reason: str) -> str:
    for rid, text in RULE_REASONS.items():
        if text in reason:
            return rid
    return "other"


def rule_ids(reasons: Iterable[str]) -> List[str]:
    """Ids of the rules behind `reasons`, in RULE_REASONS order, without repeats."""
    found = {rule_id(reason) for reason in reasons or []}
    return [rid for rid in RULE_REASONS if rid in found] + (["other"] if "other" in found else [])
