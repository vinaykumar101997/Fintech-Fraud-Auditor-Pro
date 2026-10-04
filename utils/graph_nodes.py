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

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import islice
from typing import Any, Dict, List, Literal, Optional, Tuple, TypedDict

import networkx as nx

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


def _edge(h: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """(sender, receiver) if the row is a usable graph edge, else None.

    Placeholder counterparties and self-transfers are not graph nodes/edges:
    "Unknown_Entity" is a data gap, not one account, and would otherwise join
    unrelated flows into false loops.
    """
    src, dst = str(h.get("sender", "")).strip(), str(h.get("receiver", "")).strip()
    if src and dst and src != dst and not _is_placeholder(src) and not _is_placeholder(dst):
        return src, dst
    return None


def _as_datetime(value: Any) -> Optional[datetime]:
    if value is None or value != value:  # None, NaN, NaT
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None


def _as_amount(h: Dict[str, Any]) -> Optional[float]:
    raw = h.get("amount_float", h.get("amount"))
    try:
        value = abs(float(raw))
    except (TypeError, ValueError):
        return None
    return None if value != value else value


def _nodes_on_cycles_through(graph: nx.DiGraph, sender: str, max_length: int) -> List[str]:
    """A node on a cycle of length L through sender satisfies
    dist(sender, v) + dist(v, sender) <= L. Pruning to those nodes first makes
    the search scale with the sender's neighbourhood, not the whole ledger."""
    if sender not in graph:
        return []
    forward = nx.single_source_shortest_path_length(graph, sender, cutoff=max_length - 1)
    backward = nx.single_source_shortest_path_length(
        graph.reverse(copy=False), sender, cutoff=max_length - 1
    )
    return [v for v in forward if v in backward and forward[v] + backward[v] <= max_length]


def _rotate_to(cycle: List[str], sender: str) -> List[str]:
    start = cycle.index(sender)
    return cycle[start:] + cycle[:start]


def cycles_through(
    sender: str, history: List[Dict[str, Any]], max_length: Optional[int] = None
) -> Tuple[List[List[str]], bool]:
    """Structural cycles: simple directed cycles of length 2..max_length that
    pass through sender, ignoring time and amount.

    Only the fallback for a ledger with no timestamps - on real traffic, any
    two businesses that trade both ways form one. See fund_flow_cycles.
    Returns (cycles, truncated). Each cycle is rotated to start at sender.
    """
    max_length = max_length or config.CYCLE_MAX_LENGTH
    graph = nx.DiGraph()
    graph.add_edges_from(e for e in map(_edge, history) if e)
    sub = graph.subgraph(_nodes_on_cycles_through(graph, sender, max_length))

    cycles: List[List[str]] = []
    examined = 0
    for cycle in islice(nx.simple_cycles(sub, length_bound=max_length), config.CYCLE_SEARCH_LIMIT):
        examined += 1
        if sender in cycle:
            cycles.append(_rotate_to(cycle, sender))
    cycles.sort(key=lambda c: (len(c), c))
    return cycles, examined >= config.CYCLE_SEARCH_LIMIT


class _SearchLimitReached(Exception):
    pass


@dataclass
class CycleSearch:
    cycles: List[List[str]]  # each rotated to start at the sender
    truncated: bool          # CYCLE_SEARCH_LIMIT cut the search short
    timed: int               # usable rows with a timestamp
    untimed: int             # usable rows without one: not placeable in any cycle
    anchored: bool           # cycles had to include the audited transaction


def fund_flow_cycles(
    sender: str,
    history: List[Dict[str, Any]],
    max_length: Optional[int] = None,
    transaction_id: Optional[str] = None,
) -> CycleSearch:
    """Cycles that model money going round, not graph shape.

    A cycle of length 2..max_length counts only if one transaction can be
    chosen per leg such that, starting from some leg, the legs run in strictly
    increasing timestamp order, the last leg lands within
    CYCLE_MAX_WINDOW_DAYS of the first, and leg amounts stay within
    CYCLE_AMOUNT_TOLERANCE of each other ((largest - smallest) / largest).
    Any starting leg is allowed, so every account on a ring is flagged, not
    only the one the money left first. Legs with no amount are not held to
    the amount check.

    When transaction_id names a timed row in the history, a cycle must use
    that transaction as one of its legs: the question is whether THIS
    transfer was part of a round trip, not whether its sender was ever on
    one. Asking the second question flagged every row of every business that
    trades both ways. If the transaction is not in the timed history, any
    qualifying cycle through sender counts (anchored=False).
    """
    max_length = max_length or config.CYCLE_MAX_LENGTH
    window = timedelta(days=config.CYCLE_MAX_WINDOW_DAYS)
    tolerance = config.CYCLE_AMOUNT_TOLERANCE

    Leg = Tuple[datetime, str, str, Optional[float], bool]
    legs: List[Leg] = []
    untimed = 0
    for h in history:
        edge = _edge(h)
        if not edge:
            continue
        ts = _as_datetime(h.get("timestamp"))
        if ts is None:
            untimed += 1
            continue
        is_anchor = transaction_id is not None and str(h.get("transaction_id")) == str(transaction_id)
        legs.append((ts, edge[0], edge[1], _as_amount(h), is_anchor))

    anchor = next((leg for leg in legs if leg[4]), None)
    graph = nx.DiGraph()
    graph.add_edges_from((leg[1], leg[2]) for leg in legs)
    nodes = set(_nodes_on_cycles_through(graph, sender, max_length))

    out_legs: Dict[str, List[Leg]] = {}
    for leg in sorted((l for l in legs if l[1] in nodes and l[2] in nodes), key=lambda l: l[0]):
        out_legs.setdefault(leg[1], []).append(leg)
    out_times = {node: [l[0] for l in node_legs] for node, node_legs in out_legs.items()}

    found: set = set()
    steps = 0

    def extend(path: List[str], first: datetime, last: datetime,
               low: Optional[float], high: Optional[float], used_anchor: bool) -> None:
        nonlocal steps
        node = path[-1]
        node_legs = out_legs.get(node, [])
        for ts, _, dst, amount, is_anchor in node_legs[bisect_right(out_times.get(node, []), last):]:
            if ts - first > window:
                break  # sorted by time: every later leg is outside too
            steps += 1
            if steps > config.CYCLE_SEARCH_LIMIT:
                raise _SearchLimitReached
            lo, hi = low, high
            if amount is not None:
                lo = amount if lo is None else min(lo, amount)
                hi = amount if hi is None else max(hi, amount)
                if hi > 0 and (hi - lo) / hi > tolerance:
                    continue
            used = used_anchor or is_anchor
            if dst == path[0]:
                if sender in path and (used or anchor is None):
                    found.add(tuple(_rotate_to(path, sender)))
            elif dst not in path and len(path) < max_length:
                extend(path + [dst], first, ts, lo, hi, used)

    truncated = False
    try:
        for node_legs in out_legs.values():
            for ts, src, dst, amount, is_anchor in node_legs:
                # A cycle using the anchor starts at most one window before it.
                if anchor is not None and not (anchor[0] - window <= ts <= anchor[0]):
                    continue
                extend([src, dst], ts, ts, amount, amount, is_anchor)
    except _SearchLimitReached:
        truncated = True

    cycles = sorted((list(c) for c in found), key=lambda c: (len(c), c))
    return CycleSearch(cycles, truncated, len(legs), untimed, anchor is not None)


def analyze_network_topology(state: AuditState) -> Dict[str, Any]:
    """Node 1: circular layering and account velocity.

    Circular flow is bounded fund-flow cycle detection (lengths
    2..CYCLE_MAX_LENGTH, legs time-ordered within a window and of similar
    amount, through the audited transaction) over the network history, so
    A->B->C->A is caught, not only A<->B. See fund_flow_cycles. Scores per
    unique counterparty on those cycles, not per row or per cycle, so the
    result depends on the shape of the network rather than on how many rows
    happened to be uploaded.
    """
    txn = state.get("transaction_metadata", {})
    history = state.get("network_history", []) or []
    sender = str(txn.get("sender", "")).strip()

    findings = list(state.get("detected_patterns", []))
    score = int(state.get("risk_score", 0))

    if not _is_placeholder(sender):
        search = fund_flow_cycles(sender, history, transaction_id=state.get("transaction_id"))
        cycles, truncated, untimed = search.cycles, search.truncated, search.untimed
        structural = untimed > 0 and not search.timed
        if structural:
            # No timestamps at all: fund flow cannot be checked, but dropping
            # loop detection silently would be worse than shape-only.
            cycles, truncated = cycles_through(sender, history)
        loop_partners = sorted({p for cycle in cycles for p in cycle if p != sender})

        if loop_partners:
            points = min(
                len(loop_partners) * config.CIRCULAR_FLOW_POINTS, config.CIRCULAR_FLOW_CAP
            )
            score += points
            preview = ", ".join(loop_partners[:3])
            more = f" (+{len(loop_partners) - 3} more)" if len(loop_partners) > 3 else ""
            shortest = " -> ".join(cycles[0] + [sender])
            basis = (
                "Structural only: the ledger has no timestamps, so leg timing and "
                "amounts were not checked."
                if structural else
                f"Legs run forward in time, complete within {config.CYCLE_MAX_WINDOW_DAYS:g} "
                f"day(s), and differ in amount by at most {config.CYCLE_AMOUNT_TOLERANCE:.0%}."
                + ("" if search.anchored else
                   " The audited transaction is not in the timed history, so cycles "
                   "through any of the sender's transfers are shown.")
            )
            findings.append(
                f"Circular flow: funds returned to {sender} through {len(cycles)} cycle(s) "
                f"of length 2-{config.CYCLE_MAX_LENGTH} involving {len(loop_partners)} "
                f"counterparty(ies): {preview}{more}. Shortest: {shortest}. {basis}"
            )
        if truncated:
            findings.append(
                f"Cycle search stopped after {config.CYCLE_SEARCH_LIMIT} steps; "
                "circular-flow findings may be incomplete."
            )
        if untimed and not structural:
            findings.append(
                f"{untimed} ledger row(s) have no timestamp and were left out of "
                "circular-flow analysis."
            )

    # Velocity, banded rather than a single cliff-edge count. History is the
    # whole ledger, so count only the rows this account is party to.
    volume = sum(
        1 for h in history
        if sender in (str(h.get("sender", "")).strip(), str(h.get("receiver", "")).strip())
    )
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


VALID_DECISIONS = ("approve", "reject", "escalate")


def record_human_decision(state: AuditState) -> Dict[str, Any]:
    """Node 3: capture the authorisation, not just the fact that it happened.

    Runs after the interrupt. The app writes human_decision / human_approver
    into the state before resuming; this node stamps and records it.
    """
    findings = list(state.get("detected_patterns", []))
    # Fail safe: a missing or unrecognised decision is never an approval.
    raw_decision = state.get("human_decision")
    decision = str(raw_decision or "").strip().lower()
    if decision not in VALID_DECISIONS:
        findings.append(
            f"Human review: decision {raw_decision!r} is missing or not recognised; "
            "defaulted to escalate."
        )
        decision = "escalate"
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


HISTORY_COLUMNS = ("transaction_id", "sender", "receiver", "amount_float", "country", "timestamp")


def network_history_from_ledger(ledger) -> List[Dict[str, Any]]:
    """Topology history from the FULL loaded ledger.

    Not the funnel-forwarded subset: a cycle leg the funnel dropped is still
    a leg of the cycle, and velocity is a property of the account, not of
    what Tier 0 happened to keep. Timestamps become plain datetimes (or None)
    so the history checkpoints cleanly.
    """
    cols = [c for c in HISTORY_COLUMNS if c in ledger.columns]
    records = ledger[cols].to_dict("records")
    if "timestamp" in cols:
        for r in records:
            ts = r["timestamp"]
            r["timestamp"] = None if ts is None or ts != ts else getattr(ts, "to_pydatetime", lambda: ts)()
    return records


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
