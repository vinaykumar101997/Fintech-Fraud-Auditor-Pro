"""SAR drafting gate.

An LLM SUSPICIOUS verdict is a triage signal, not an authorisation to file. A
SAR may only be drafted once a named officer has approved the findings, and
the durable audit trail - not session state - is the evidence that they did.

Pure function over audit events so it is unit-testable without Streamlit.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from utils import audit_log

APPROVED = "APPROVE"
OFFICER_DECISIONS = ("APPROVE", "REJECT", "ESCALATE")


class OfficerReviewRefused(ValueError):
    """The review request is invalid; nothing was recorded. Message is user-facing."""


@dataclass(frozen=True)
class SarGateDecision:
    allowed: bool
    reason: str


def sar_approval(
    events: Iterable[Mapping[str, Any]],
    transaction_id: str,
    batch_id: Optional[str] = None,
) -> SarGateDecision:
    """Allow a SAR only if the latest human_review event for this transaction
    (in this batch, when given) is an approval by a named officer.

    The latest decision wins, so a later rejection revokes an earlier approval.
    Scoped to the batch so an approval for a same-named transaction ID in a
    different ledger cannot unlock this one.
    """
    reviews = [
        e for e in events
        if e.get("stage") == "human_review"
        and str(e.get("transaction_id")) == str(transaction_id)
        and (batch_id is None or e.get("batch_id") == batch_id)
    ]
    if not reviews:
        return SarGateDecision(
            False,
            f"SAR drafting for {transaction_id} requires officer approval. "
            "Record an officer approval for it first.",
        )

    latest = max(enumerate(reviews), key=lambda pair: (pair[1].get("id") or 0, pair[0]))[1]
    verdict = str(latest.get("verdict") or "").strip().upper()
    actor = str(latest.get("actor") or "").strip()

    if verdict != APPROVED:
        return SarGateDecision(
            False,
            f"SAR drafting for {transaction_id} requires officer approval. "
            f"The latest human review decision is {verdict or 'missing'}.",
        )
    if not actor:
        return SarGateDecision(
            False,
            f"SAR drafting for {transaction_id} requires officer approval. "
            "The recorded approval has no reviewing officer.",
        )
    return SarGateDecision(True, f"Approved by {actor}.")


def record_officer_review(
    *,
    session_id: str,
    batch_id: str,
    transaction_id: str,
    row_verdict: str,
    officer: str,
    decision: str,
    note: Optional[str] = None,
    rationale: Optional[str] = None,
    risk_score: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Record a named officer's decision on an LLM-SUSPICIOUS row.

    Independent of the topology graph, which only pauses for review at
    HITL_REVIEW_THRESHOLD; without this a SUSPICIOUS row scoring below it
    could never be approved, so could never get a SAR. Writes the same
    human_review event the graph path does, which is what sar_approval reads.

    Raises OfficerReviewRefused (nothing recorded) for a row that is not
    SUSPICIOUS, a blank officer, or a decision outside OFFICER_DECISIONS.
    Raises AuditWriteError if the event cannot be written: the decision has
    not taken effect. Returns the recorded event on success.
    """
    if str(row_verdict or "").strip().upper() != "SUSPICIOUS":
        raise OfficerReviewRefused(
            f"Officer review applies to SUSPICIOUS rows; {transaction_id} is {row_verdict or 'unassessed'}."
        )
    actor = str(officer or "").strip()
    if not actor:
        raise OfficerReviewRefused("Enter the reviewing officer's name before deciding.")
    verdict = str(decision or "").strip().upper()
    if verdict not in OFFICER_DECISIONS:
        raise OfficerReviewRefused(
            f"Decision {decision!r} is not one of {', '.join(OFFICER_DECISIONS)}."
        )

    event = dict(
        session_id=session_id, batch_id=batch_id, transaction_id=str(transaction_id),
        stage="human_review", verdict=verdict, risk_score=risk_score, actor=actor,
        rationale=rationale, detail=(note or "").strip() or None,
    )
    audit_log.record(**event, db_path=db_path)  # raises AuditWriteError
    return event
