"""Fintech Fraud Auditor - Streamlit prototype.

Pipeline order matters and is deliberate:
    load/validate -> deterministic rules -> statistical funnel -> LLM tier
                  -> topology graph -> human review -> report

The rules run BEFORE the funnel so that a compliance rule can force a row
through regardless of its anomaly score. Reversing these two stages is what made
the original build blind to structuring.
"""

from __future__ import annotations

import json
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pandas as pd
import plotly.express as px
import streamlit as st
from dotenv import load_dotenv

load_dotenv(override=True)

import config  # noqa: E402
from utils import audit_log  # noqa: E402
from utils.agents import ComplianceAuditEngine  # noqa: E402
from utils.audit_log import evidence_hash  # noqa: E402
from utils.chat_agent import ComplianceIntelligenceProvider, ProviderUnavailable  # noqa: E402
from utils.data_loader import LedgerSchemaError, load_ledger  # noqa: E402
from utils.funnel import explain_row, run_funnel  # noqa: E402
from utils.llm_provider import describe as provider_description  # noqa: E402
from utils.graph_logic import HUMAN_NODE, build_compliance_graph, thread_config  # noqa: E402
from utils.graph_nodes import initial_state  # noqa: E402
from utils.rules import apply_rules  # noqa: E402
from utils.schemas import SuspiciousActivityReport  # noqa: E402
from utils.screening import (  # noqa: E402
    best_effort_match,
    load_watchlist,
    render_freeze_directive,
    rule_screener,
    screen,
)
from utils.verdicts import Verdict  # noqa: E402

logging.basicConfig(level=config.LOG_LEVEL)
logger = logging.getLogger(__name__)

st.set_page_config(page_title="Fintech Fraud Auditor", page_icon="shield", layout="wide")

VERDICT_COLOURS = {"CLEAR": "#1D9E75", "SUSPICIOUS": "#D85A30", "ERROR": "#BA7517"}


# --------------------------------------------------------------------------
# Session identity and cached resources
# --------------------------------------------------------------------------
def session_id() -> str:
    """Per-session UUID. Checkpoint threads are namespaced with this so two
    users auditing the same transaction ID cannot read each other's state."""
    if "session_id" not in st.session_state:
        st.session_state.session_id = uuid.uuid4().hex[:12]
    return st.session_state.session_id


@st.cache_resource
def get_graph():
    return build_compliance_graph()


@st.cache_resource
def get_backends():
    """Returns (provider, engine, error). Never calls st.stop() inside a cached
    function, so a transient failure is retryable rather than sticky."""
    try:
        provider = ComplianceIntelligenceProvider()
        return provider, ComplianceAuditEngine(llm=provider.llm), None
    except (ProviderUnavailable, ValueError) as exc:
        return None, None, str(exc)


def new_batch_id() -> str:
    return f"batch-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


# --------------------------------------------------------------------------
# Tier 2 execution
# --------------------------------------------------------------------------
def assess_batch(flagged: pd.DataFrame, engine, provider, batch_id: str) -> pd.DataFrame:
    """Run the LLM tier across flagged rows in parallel, with a result cache."""
    cache = st.session_state.setdefault("verdict_cache", {})
    rows = flagged.to_dict("records")

    def key_for(row) -> str:
        return evidence_hash(
            {
                "amount": str(row["amount"]),
                "country": row.get("country", ""),
                "sender": row.get("sender", ""),
                "receiver": row.get("receiver", ""),
                "rules": sorted(row.get("rule_reasons") or []),
            }
        )

    def work(row):
        cache_key = key_for(row)
        if cache_key in cache:
            return row["transaction_id"], cache[cache_key], True
        outcome = engine.assess_transaction(
            transaction_id=row["transaction_id"],
            amount=abs(row["amount"]),
            country=row.get("country", ""),
            sender=row.get("sender", ""),
            receiver=row.get("receiver", ""),
            rule_reasons=row.get("rule_reasons"),
            rag_engine=getattr(provider, "engine", None),
        )
        cache[cache_key] = outcome
        return row["transaction_id"], outcome, False

    results, progress, done = {}, st.progress(0.0, text="Assessing flagged transactions..."), 0
    with ThreadPoolExecutor(max_workers=config.LLM_MAX_WORKERS) as pool:
        for txn_id, outcome, was_cached in pool.map(work, rows):
            results[txn_id] = outcome
            done += 1
            progress.progress(done / len(rows), text=f"Assessed {done}/{len(rows)} transactions")
            audit_log.record(
                session_id=session_id(),
                batch_id=batch_id,
                transaction_id=txn_id,
                stage="tier2_assessment",
                verdict=outcome.verdict.value,
                rationale=outcome.rationale,
                model_name=config.chat_model_name(),
                detail="cache hit" if was_cached else outcome.error_detail,
            )
    progress.empty()

    out = flagged.copy()
    out["verdict"] = [results[t].verdict.value for t in out["transaction_id"]]
    out["confidence"] = [results[t].confidence for t in out["transaction_id"]]
    out["typology"] = [results[t].typology or "" for t in out["transaction_id"]]
    out["forensic_analysis"] = [results[t].rationale for t in out["transaction_id"]]
    out["error_detail"] = [results[t].error_detail or "" for t in out["transaction_id"]]
    return out


# --------------------------------------------------------------------------
# Sidebar
# --------------------------------------------------------------------------
def render_sidebar(provider, backend_error):
    with st.sidebar:
        st.header("Control centre")

        if st.button("Reset session", use_container_width=True):
            keep = st.session_state.session_id
            st.session_state.clear()
            st.session_state.session_id = keep
            st.rerun()

        if st.button("Reconnect backends", use_container_width=True):
            ComplianceIntelligenceProvider.reset()
            get_backends.clear()
            st.rerun()

        st.divider()
        st.caption("System status")

        # Real checks, not decorative labels.
        if backend_error:
            st.error(f"Model provider / Qdrant unavailable\n\n{backend_error}")
        else:
            st.success(f"Model: {provider_description()}")
            ok, detail = provider.health()
            (st.success if ok else st.warning)(f"Qdrant: {detail}")

        entries, real_list = load_watchlist()
        if real_list:
            st.success(f"Watchlist: {len(entries)} entries loaded")
        else:
            st.warning("Watchlist: built-in demo list. Load an OFAC SDN export for real use.")

        st.divider()
        st.caption(
            f"Threshold {config.REPORTING_THRESHOLD:,.0f} | "
            f"structuring band from {config.STRUCTURING_FLOOR:,.0f} | "
            f"HITL at {config.HITL_REVIEW_THRESHOLD}"
        )
        st.caption(f"Session {session_id()}")


# --------------------------------------------------------------------------
# Phase 1: ingestion and funnel
# --------------------------------------------------------------------------
def render_ingestion(engine, provider):
    st.subheader("1. Load a transaction ledger")

    uploaded = st.file_uploader("Ledger CSV", type=["csv"])
    use_sample = st.checkbox("Use the bundled sample ledger", value=False)

    source = None
    if use_sample and config.SAMPLE_LEDGER.exists():
        source = config.SAMPLE_LEDGER
    elif use_sample:
        st.warning("No sample ledger found. Run `python scripts/generate_sample_ledger.py`.")
    elif uploaded is not None:
        source = uploaded

    if source is None:
        st.info(
            "Required columns: transaction_id, amount, country. "
            "Optional: sender, receiver, timestamp. Common aliases are mapped automatically."
        )
        return

    try:
        ledger, warnings = load_ledger(source)
    except LedgerSchemaError as exc:
        st.error(str(exc))
        return

    for warning in warnings:
        st.warning(warning)

    st.dataframe(ledger.head(10), use_container_width=True, hide_index=True)
    st.caption(f"{len(ledger):,} rows loaded.")

    if not st.button("Run audit", type="primary"):
        return

    batch_id = new_batch_id()
    audit_log.initialize()

    with st.spinner("Applying deterministic rules..."):
        ruled = apply_rules(ledger, screener=rule_screener)

    with st.spinner("Scoring behavioural anomalies..."):
        result = run_funnel(ruled)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Rows processed", f"{result.total_rows:,}")
    c2.metric("Forwarded", f"{result.forwarded:,}", f"{1 - result.drop_rate:.0%} of batch")
    c3.metric("Rule hits", f"{result.rule_flagged:,}")
    c4.metric("Saved by rules", f"{result.rescued_by_rules:,}", help="Scored normal by the model but kept by a compliance rule")

    if not result.ml_applied:
        st.info(
            f"Batch is under {config.MIN_ROWS_FOR_ML} rows, too small for the distribution to "
            "mean anything. Statistical scoring skipped; every row forwarded."
        )
    elif result.top_features:
        st.caption("Features separating flagged rows: " + ", ".join(result.top_features))

    for _, row in result.flagged.iterrows():
        audit_log.record(
            session_id=session_id(), batch_id=batch_id, transaction_id=row["transaction_id"],
            stage="funnel_forwarded", selection_reason=row.get("selection_reason"),
            rule_reasons=row.get("rule_reasons"),
        )

    if result.flagged.empty:
        st.success("No transaction triggered a rule or a behavioural anomaly. Nothing forwarded.")
        return

    if engine is None:
        st.error("Backends unavailable, so the compliance tier cannot run. "
                 "Rule and funnel results below are still valid.")
        st.dataframe(result.flagged, use_container_width=True, hide_index=True)
        return

    assessed = assess_batch(result.flagged, engine, provider, batch_id)
    st.session_state.audit_results = assessed
    st.session_state.batch_id = batch_id
    st.session_state.funnel_stats = {
        "total": result.total_rows, "forwarded": result.forwarded,
        "rescued": result.rescued_by_rules, "elapsed_ms": result.elapsed_ms,
    }
    st.rerun()


# --------------------------------------------------------------------------
# Phase 2: investigation
# --------------------------------------------------------------------------
def render_investigation(provider):
    df: pd.DataFrame = st.session_state.audit_results
    batch_id = st.session_state.get("batch_id", "unknown")

    errors = int((df["verdict"] == Verdict.ERROR.value).sum())
    if errors:
        st.error(
            f"{errors} transaction(s) could not be assessed. They are recorded as ERROR and "
            "remain open. They are not cleared."
        )

    st.subheader("Risk overview")
    c1, c2 = st.columns(2)
    with c1:
        counts = df["verdict"].value_counts().reset_index()
        counts.columns = ["verdict", "count"]
        st.plotly_chart(
            px.bar(counts, x="verdict", y="count", title="Verdicts", color="verdict",
                   color_discrete_map=VERDICT_COLOURS),
            use_container_width=True,
        )
    with c2:
        by_country = df.groupby("country").size().reset_index(name="count").nlargest(10, "count")
        st.plotly_chart(
            px.bar(by_country, x="country", y="count", title="Jurisdictions (top 10)",
                   color_discrete_sequence=["#7F77DD"]),
            use_container_width=True,
        )

    st.subheader("Investigative ledger")
    display_cols = [
        "transaction_id", "amount_float", "country", "sender", "receiver",
        "verdict", "confidence", "typology", "selection_reason", "forensic_analysis",
    ]
    st.dataframe(
        df[[c for c in display_cols if c in df.columns]].rename(columns={"amount_float": "amount"}),
        use_container_width=True, hide_index=True,
    )

    with st.expander("Why was a transaction forwarded?"):
        pick = st.selectbox("Transaction", df["transaction_id"].tolist(), key="explain_pick")
        row = df[df["transaction_id"] == pick].iloc[0]
        for reason in explain_row(row):
            st.write(f"- {reason}")
        if row.get("error_detail"):
            st.warning(f"Assessment error: {row['error_detail']}")

    st.divider()
    render_topology(df, batch_id)
    st.divider()

    tab_sar, tab_screen, tab_trail = st.tabs(
        ["SAR generator", "Sanctions screening", "Audit trail"]
    )
    with tab_sar:
        render_sar(df, provider, batch_id)
    with tab_screen:
        render_screening(batch_id)
    with tab_trail:
        render_audit_trail(batch_id)


def render_topology(df: pd.DataFrame, batch_id: str):
    st.subheader("2. Network topology and human review")

    graph = get_graph()
    left, right = st.columns([1, 2])

    with left:
        target = st.selectbox("Transaction", df["transaction_id"].tolist(), key="topo_pick")
        cfg = thread_config(session_id(), str(target))

        if st.button("Run topology audit", use_container_width=True):
            row = df[df["transaction_id"] == target].iloc[0]
            neighbours = df[(df["sender"] == row["sender"]) | (df["receiver"] == row["sender"])]
            history = neighbours[["transaction_id", "sender", "receiver", "amount_float", "country"]].to_dict("records")

            state = initial_state(
                transaction_id=str(row["transaction_id"]),
                sender=str(row["sender"]), receiver=str(row["receiver"]),
                amount=float(row["amount_float"]), network_history=history,
                rag_verdict=str(row["verdict"]),
            )
            with st.spinner("Traversing the network..."):
                graph.invoke(state, config=cfg)
            st.rerun()

        st.caption(
            "Topology runs on the forwarded subset. Rows dropped by the funnel are "
            "not part of the graph."
        )

    with right:
        snapshot = graph.get_state(cfg)
        if not (snapshot and snapshot.values):
            st.info("No topology audit has been run for this transaction yet.")
            return

        values = snapshot.values
        score = int(values.get("risk_score", 0))
        st.metric(
            "Topology risk score", f"{score}/100",
            delta="Above review threshold" if score >= config.HITL_REVIEW_THRESHOLD else "Below threshold",
            delta_color="inverse",
        )

        if values.get("detected_patterns"):
            with st.expander("Findings", expanded=True):
                for pattern in values["detected_patterns"]:
                    st.write(f"- {pattern}")

        paused = bool(snapshot.next) and HUMAN_NODE in snapshot.next
        if paused:
            st.error("Execution paused. Human authorisation required before this case proceeds.")
            approver = st.text_input("Reviewing officer", key=f"approver_{target}")
            note = st.text_area("Decision note", key=f"note_{target}", height=80)
            approve, reject = st.columns(2)

            def resolve(decision: str):
                if not approver.strip():
                    st.warning("Enter the reviewing officer's name before deciding.")
                    return
                decided_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                graph.update_state(
                    cfg,
                    {"human_decision": decision, "human_approver": approver.strip(),
                     "human_note": note.strip(), "human_decided_at": decided_at},
                )
                final = graph.invoke(None, config=cfg)
                audit_log.record(
                    session_id=session_id(), batch_id=batch_id, transaction_id=str(target),
                    stage="human_review", verdict=decision.upper(),
                    risk_score=int(final.get("risk_score", 0)), actor=approver.strip(),
                    rationale=final.get("forensic_summary", ""), detail=note.strip() or None,
                )
                # Persisted, so the outcome survives the next rerun.
                st.session_state.setdefault("graph_outcomes", {})[str(target)] = final
                st.rerun()

            if approve.button("Approve findings", use_container_width=True, type="primary"):
                resolve("approve")
            if reject.button("Reject and escalate", use_container_width=True):
                resolve("reject")
        else:
            stored = st.session_state.get("graph_outcomes", {}).get(str(target), values)
            summary = stored.get("forensic_summary") or "Audit complete."
            (st.warning if stored.get("outcome") == "escalated" else st.success)(summary)
            if stored.get("human_approver"):
                st.caption(
                    f"Decision: {stored.get('human_decision')} by {stored.get('human_approver')} "
                    f"at {stored.get('human_decided_at')}"
                )


def render_sar(df: pd.DataFrame, provider, batch_id: str):
    candidates = df[df["verdict"] == Verdict.SUSPICIOUS.value]["transaction_id"].tolist()
    if not candidates:
        st.info("No suspicious transactions in this batch.")
        return
    if provider is None:
        st.error("Backends unavailable; SAR drafting is offline.")
        return

    target = st.selectbox("Flagged transaction", candidates, key="sar_pick")
    if not st.button("Draft SAR", type="primary"):
        return

    row = df[df["transaction_id"] == target].iloc[0]
    report_id = str(uuid.uuid4())  # generated here, never invented by the model

    from utils.sanitize import build_evidence_block

    evidence = build_evidence_block(
        {
            "transaction_id": target, "amount": f"{abs(row['amount']):,.2f}",
            "jurisdiction": row.get("country", ""), "originator": row.get("sender", ""),
            "beneficiary": row.get("receiver", ""),
            "assessment": row.get("forensic_analysis", ""),
            "typology_hint": row.get("typology", ""),
            "rule_hits": "; ".join(row.get("rule_reasons") or []),
        }
    )

    try:
        with st.spinner("Drafting..."):
            structured = provider.llm.with_structured_output(SuspiciousActivityReport)
            report = structured.invoke(
                "Draft a suspicious activity report from the evidence below. "
                "Use only facts present in the record.\n\n" + evidence
            )
        payload = {"report_id": report_id, **report.model_dump()}
        st.success("SAR drafted.")
        st.json(payload)
        st.download_button(
            "Download SAR (JSON)",
            data=json.dumps(payload, indent=2, default=str),
            file_name=f"SAR-{report_id[:8]}.json", mime="application/json",
        )
        audit_log.record(
            session_id=session_id(), batch_id=batch_id, transaction_id=str(target),
            stage="sar_drafted", verdict=Verdict.SUSPICIOUS.value,
            risk_score=report.overall_risk_score, model_name=config.chat_model_name(),
            evidence=evidence, detail=f"report_id={report_id}",
        )
    except Exception as exc:  # noqa: BLE001
        st.error(f"SAR drafting failed: {exc}")
        audit_log.record(
            session_id=session_id(), batch_id=batch_id, transaction_id=str(target),
            stage="sar_failed", detail=str(exc),
        )


def render_screening(batch_id: str):
    entries, real_list = load_watchlist()
    if not real_list:
        st.warning(
            "Screening against the built-in demo list. Place an OFAC SDN export at "
            "`data/sanctions_list.csv` (columns: name, aliases, type, program) for real use."
        )

    query = st.text_input("Entity name (person or company)")
    if not query.strip():
        return

    try:
        hit = screen(query)
        fallback = best_effort_match(query)
    except ImportError:
        st.error("rapidfuzz is not installed. Run `pip install -r requirements.txt`.")
        return

    if hit:
        st.error(f"Potential match: {hit.matched_name} ({hit.entry_type})")
        st.metric("Match score", f"{hit.score:.1f}%", delta="Above threshold", delta_color="inverse")
        directive = render_freeze_directive(
            hit, reference=f"FRZ-{uuid.uuid4().hex[:8].upper()}",
            issued_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        st.code(directive, language="text")
        st.caption("Fixed template with slot filling. Not model-generated.")
        audit_log.record(
            session_id=session_id(), batch_id=batch_id, transaction_id=query,
            stage="sanctions_hit", verdict="SUSPICIOUS", risk_score=int(hit.score),
            rationale=f"Matched {hit.matched_name}",
        )
    else:
        if fallback:
            name, score = fallback
            st.success(
                f"No match above the {config.SCREENING_MATCH_THRESHOLD}% threshold. "
                f"Closest entry: {name} at {score:.1f}%."
            )
        else:
            st.success("No match found.")
        st.caption("Name screening alone is not identity verification. Confirm against DOB and nationality.")


def render_audit_trail(batch_id: str):
    st.caption(f"Durable log at `{config.AUDIT_DB_PATH}`. Survives refresh and restart.")
    events = audit_log.recent(limit=300)
    if not events:
        st.info("No events recorded yet.")
        return
    frame = pd.DataFrame(events)
    only_batch = st.checkbox("This batch only", value=True)
    if only_batch and "batch_id" in frame.columns:
        frame = frame[frame["batch_id"] == batch_id]
    st.dataframe(
        frame[["occurred_at", "transaction_id", "stage", "verdict", "actor", "rationale"]],
        use_container_width=True, hide_index=True,
    )
    st.download_button(
        "Export trail (CSV)", data=frame.to_csv(index=False),
        file_name=f"audit_trail_{batch_id}.csv", mime="text/csv",
    )


# --------------------------------------------------------------------------
def main():
    st.title("Fintech Fraud Auditor")
    st.caption(
        "Tiered AML screening. Deterministic rules run ahead of the statistical "
        "funnel so a compliance rule can never be overridden by a cost optimisation."
    )

    provider, engine, backend_error = get_backends()
    render_sidebar(provider, backend_error)

    if backend_error:
        st.warning(
            f"Compliance backends are unavailable: {backend_error}\n\n"
            "Rules and the statistical funnel still run; the LLM tier does not."
        )

    if st.session_state.get("audit_results") is None:
        render_ingestion(engine, provider)
    else:
        render_investigation(provider)


if __name__ == "__main__":
    main()
