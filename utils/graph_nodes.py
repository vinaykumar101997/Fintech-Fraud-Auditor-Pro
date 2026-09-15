"""Topology analysis nodes as pure functions.

Deliberately free of LangGraph imports so the scoring logic can be unit-tested
offline. graph_logic.py wires these into the state machine.

Three defects from the original implementation are fixed here:

1. Risk inflated with batch size. The circular-flow check added 50 points per
   matching history row with no dedup and no cap, so five rows scored 250 and
   any sender with two inbound transfers tripped the review threshold. Scoring
   is now per unique counterparty and capped.

2. State was mutated in place (`findings = state["detected_patterns"]` then
   `.append`). Under a checkpointer that corrupts replay and double-appends on
   resume. Every node now copies before mutating.

3. The human node was `lambda state: state`, so an approval recorded nothing.
   It now captures decision, approver, timestamp, and note, and a rejection
   routes away from report generation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, TypedDict

import config


class AuditState(TypedDict, total=False):
    transaction_id: str
    transaction_metadata: Dict[str, Any]
    network_history: List[Dict[str, Any]]
    rag_verdict: str
    detected_patterns: List[str]
    risk_score: int
    forensic_summary: str
    requires_human_review: bool
    human_decision: Optional[Literal["approve", "reject", "escalate"]]
    human_approver: Optional[str]
    human_note: Optional[str]
    human_decided_at: Optional[str]
    outcome: str


def _is_placeholder(name: object) -> bool:
    return str(name or "").strip().lower() in config.PLACEHOLDER_ENTITIES


def _clamp(value: int) -> int:
    return max(0, min(int(value), 100))


def analyze_network_topology(state: AuditState) -> Dict[str, Any]:
    """Node 1: circular layering and account velocity.

    Scores per unique counterparty, not per row, so the result depends on the
    shape of the network rather than on how many rows happened to be uploaded.
    """
    txn = state.get("transaction_metadata", {})
    history = state.get("network_history", []) or []
    sender = str(txn.get("sender", ""))

    findings = list(state.get("detected_patterns", []))
    score = int(state.get("risk_score", 0))

    if not _is_placeholder(sender):
        # Counterparties that both sent to and received from this account.
        sent_to = {
            str(h.get("receiver", "")).strip()
            for h in history
            if str(h.get("sender", "")).strip() == sender
        }
        received_from = {
            str(h.get("sender", "")).strip()
            for h in history
            if str(h.get("receiver", "")).strip() == sender
        }
        loop_partners = sorted(
            p for p in (sent_to & received_from) if p and not _is_placeholder(p) and p != sender
        )

        if loop_partners:
            points = min(
                len(loop_partners) * config.CIRCULAR_FLOW_POINTS, config.CIRCULAR_FLOW_CAP
            )
            score += points
            preview = ", ".join(loop_partners[:3])
            more = f" (+{len(loop_partners) - 3} more)" if len(loop_partners) > 3 else ""
            findings.append(
                f"Circular flow: funds returned to {sender} via {len(loop_partners)} "
                f"counterparty(ies): {preview}{more}."
            )

    # Velocity, banded rather than a single cliff-edge count.
    volume = len(history)
    if volume > 20:
        score += 25
        findings.append(f"Very high account transit: {volume} linked transactions in this batch.")
    elif volume > 10:
        score += 15
        findings.append(f"Elevated account transit: {volume} linked transactions in this batch.")
    elif volume > 5:
        score += 8
        findings.append(f"Moderate account transit: {volume} linked transactions in this batch.")

    distinct_receivers = len(
        {str(h.get("receiver", "")).strip() for h in history if str(h.get("sender", "")).strip() == sender}
    )
    if distinct_receivers >= 8:
        score += 15
        findings.append(f"Wide distribution: funds dispersed to {distinct_receivers} distinct beneficiaries.")

    return {"detected_patterns": findings, "risk_score": _clamp(score)}


def detect_obfuscation(state: AuditState) -> Dict[str, Any]:
    """Node 2: missing counterparty data, weighted by the compliance verdict."""
    txn = state.get("transaction_metadata", {})
    rag_status = str(state.get("rag_verdict", "")).strip().upper()

    findings = list(state.get("detected_patterns", []))
    score = int(state.get("risk_score", 0))

    sender_missing = _is_placeholder(txn.get("sender"))
    receiver_missing = _is_placeholder(txn.get("receiver"))
    missing_data = sender_missing or receiver_missing

    if rag_status == "SUSPICIOUS":
        score += config.RAG_SUSPICIOUS_POINTS
        findings.append("Compliance tier returned a suspicious verdict for this transaction.")
    elif rag_status == "ERROR":
        findings.append("Compliance tier did not complete; treat topology score as incomplete.")

    if missing_data and rag_status == "SUSPICIOUS":
        score += config.OBFUSCATION_POINTS
        side = "originator" if sender_missing else "beneficiary"
        findings.append(
            f"Topological dead end: {side} data is obfuscated on a transaction already "
            "assessed as high risk."
        )
    elif missing_data:
        score += config.MISSING_DATA_POINTS
        findings.append("Incomplete network graph: counterparty data is missing.")

    score = _clamp(score)
    return {
        "detected_patterns": findings,
        "risk_score": score,
        "requires_human_review": score >= config.HITL_REVIEW_THRESHOLD,
    }


def record_human_decision(state: AuditState) -> Dict[str, Any]:
    """Node 3: capture the authorisation, not just the fact that it happened.

    Runs after the interrupt. The app writes human_decision / human_approver
    into the state before resuming; this node stamps and records it.
    """
    findings = list(state.get("detected_patterns", []))
    decision = state.get("human_decision") or "approve"
    approver = state.get("human_approver") or "unattributed"
    note = state.get("human_note") or ""
    decided_at = state.get("human_decided_at") or datetime.now(timezone.utc).isoformat(timespec="seconds")

    entry = f"Human review: {decision} by {approver} at {decided_at}."
    if note:
        entry += f" Note: {note}"
    findings.append(entry)

    return {
        "detected_patterns": findings,
        "human_decision": decision,
        "human_approver": approver,
        "human_decided_at": decided_at,
    }


def synthesize_forensic_report(state: AuditState) -> Dict[str, Any]:
    """Node 4: final narrative."""
    patterns = list(state.get("detected_patterns", []))
    score = int(state.get("risk_score", 0))

    if score >= config.HITL_REVIEW_THRESHOLD:
        band = "CRITICAL RISK: severe obfuscation or layering indicators."
    elif score >= 45:
        band = "HIGH RISK: network consistent with layering activity."
    elif score > 0:
        band = "CAUTION: anomalous network activity or incomplete metadata."
    else:
        band = "STABLE: no topological or obfuscation indicators identified."

    detail = " Findings: " + "; ".join(patterns) if patterns else ""
    return {
        "forensic_summary": f"{band}{detail}",
        "outcome": "reported",
    }


def escalate_case(state: AuditState) -> Dict[str, Any]:
    """Terminal node for a rejected review. Does not produce a filing."""
    patterns = list(state.get("detected_patterns", []))
    approver = state.get("human_approver") or "unattributed"
    return {
        "forensic_summary": (
            f"ESCALATED: reviewing officer ({approver}) rejected the automated findings. "
            "Case routed for manual investigation; no report generated. "
            + ("Findings on record: " + "; ".join(patterns) if patterns else "")
        ),
        "outcome": "escalated",
    }


def route_after_obfuscation(state: AuditState) -> Literal["human_review", "report_generation"]:
    return "human_review" if state.get("requires_human_review") else "report_generation"


def route_after_human(state: AuditState) -> Literal["report_generation", "escalate_case"]:
    return "escalate_case" if state.get("human_decision") in ("reject", "escalate") else "report_generation"


def initial_state(
    transaction_id: str,
    sender: str,
    receiver: str,
    amount: float,
    network_history: List[Dict[str, Any]],
    rag_verdict: str,
) -> AuditState:
    return {
        "transaction_id": str(transaction_id),
        "transaction_metadata": {"sender": sender, "receiver": receiver, "amount": amount},
        "network_history": network_history,
        "rag_verdict": rag_verdict,
        "detected_patterns": [],
        "risk_score": 0,
        "forensic_summary": "",
        "requires_human_review": False,
        "human_decision": None,
        "human_approver": None,
        "human_note": None,
        "human_decided_at": None,
        "outcome": "pending",
    }
