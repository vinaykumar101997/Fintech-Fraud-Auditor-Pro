"""State machine assembly. All scoring logic lives in graph_nodes.

The graph is no longer built at import time. A module-level compiled graph with
an in-memory checkpointer is shared by every user of the Streamlit server, and
the original thread_id was the bare transaction ID, so two people auditing a
transaction with the same ID read and wrote each other's state. The app now owns
the graph's lifetime and namespaces thread IDs per session.
"""

from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from utils.graph_nodes import (
    AuditState,
    analyze_network_topology,
    detect_obfuscation,
    escalate_case,
    record_human_decision,
    route_after_human,
    route_after_obfuscation,
    synthesize_forensic_report,
)

HUMAN_NODE = "human_review"


def build_compliance_graph(checkpointer=None):
    """Compile the audit state machine.

    Pass a durable checkpointer (e.g. langgraph-checkpoint-postgres) in
    production. MemorySaver loses every paused case on restart, which for a
    compliance pause is a real loss, not a cosmetic one.
    """
    workflow = StateGraph(AuditState)

    workflow.add_node("topology_analysis", analyze_network_topology)
    workflow.add_node("obfuscation_analysis", detect_obfuscation)
    workflow.add_node(HUMAN_NODE, record_human_decision)
    workflow.add_node("report_generation", synthesize_forensic_report)
    workflow.add_node("escalate_case", escalate_case)

    workflow.set_entry_point("topology_analysis")
    workflow.add_edge("topology_analysis", "obfuscation_analysis")

    workflow.add_conditional_edges(
        "obfuscation_analysis",
        route_after_obfuscation,
        {"human_review": HUMAN_NODE, "report_generation": "report_generation"},
    )
    workflow.add_conditional_edges(
        HUMAN_NODE,
        route_after_human,
        {"report_generation": "report_generation", "escalate_case": "escalate_case"},
    )

    workflow.add_edge("report_generation", END)
    workflow.add_edge("escalate_case", END)

    return workflow.compile(
        checkpointer=checkpointer or MemorySaver(),
        interrupt_before=[HUMAN_NODE],
    )


def thread_config(session_id: str, transaction_id: str) -> dict:
    """Namespace the checkpoint thread so sessions cannot collide."""
    return {"configurable": {"thread_id": f"{session_id}:{transaction_id}"}}
