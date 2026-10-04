"""Smoke tests that drive app.py through streamlit.testing.v1.AppTest.

The model provider and the audit engine are replaced with in-process fakes
before the script runs, so nothing here reaches an LLM, Qdrant, or the
network. Each test gets its own audit database. Needs the full
requirements.txt (Streamlit, LangGraph); skipped on the dev-only install.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("streamlit.testing.v1")
pytest.importorskip("langgraph")

import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from utils import agents, audit_log, chat_agent  # noqa: E402
from utils.schemas import SuspiciousActivityReport  # noqa: E402
from utils.verdicts import AuditOutcome, Verdict  # noqa: E402

# A SAN-* row: rule hit (Russia/Iran/North Korea) and Unknown_Entity
# receiver, so the fake engine calls it SUSPICIOUS and its topology score
# reaches the HITL threshold (20 suspicious + 50 obfuscation).
SUSPICIOUS_HIGH_SCORE = "SAN-000"


# --- fakes -------------------------------------------------------------------
class FakeStructured:
    def __init__(self, calls):
        self.calls = calls

    def invoke(self, prompt):
        self.calls.append(prompt)
        return SuspiciousActivityReport(
            primary_typology="structuring", narrative_investigation="Fake narrative.",
            recommended_action="file_sar", overall_risk_score=80,
        )


class FakeLLM:
    def __init__(self):
        self.sar_calls = []

    def with_structured_output(self, schema):
        assert schema is SuspiciousActivityReport
        return FakeStructured(self.sar_calls)


class FakeProvider:
    engine = None  # no RAG chain
    instances = []

    def __init__(self):
        self.llm = FakeLLM()
        FakeProvider.instances.append(self)

    def health(self):
        return True, "fake"

    @classmethod
    def reset(cls):
        pass


class FakeEngine:
    """Rule-hit rows are SUSPICIOUS, everything else CLEAR."""

    assessed = []

    def __init__(self, llm=None):
        pass

    def assess_transaction(self, *, transaction_id, rule_reasons=None, **_):
        FakeEngine.assessed.append(transaction_id)
        verdict = Verdict.SUSPICIOUS if rule_reasons else Verdict.CLEAR
        return AuditOutcome(verdict=verdict, rationale="Fake assessment.", confidence=0.9)


# --- harness -----------------------------------------------------------------
@pytest.fixture
def db(monkeypatch, tmp_path):
    path = tmp_path / "audit.sqlite3"
    monkeypatch.setattr(config, "AUDIT_DB_PATH", path)
    monkeypatch.setattr(chat_agent, "ComplianceIntelligenceProvider", FakeProvider)
    monkeypatch.setattr(agents, "ComplianceAuditEngine", FakeEngine)
    FakeProvider.instances, FakeEngine.assessed = [], []
    st.cache_resource.clear()  # get_backends / get_graph from an earlier test
    return path


def _fail_stage(monkeypatch, tmp_path, stage):
    """Make every audit write for `stage` genuinely fail (parent is a file)."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    real = audit_log.record

    def record(**kwargs):
        if kwargs.get("stage") == stage:
            kwargs["db_path"] = blocker / "audit.sqlite3"
        return real(**kwargs)

    monkeypatch.setattr(audit_log, "record", record)


def _button(at, label):
    return next(b for b in at.button if b.label == label)


def _texts(elements):
    return " | ".join(str(e.value) for e in elements)


def _start(db) -> AppTest:
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120)
    at.run()
    next(c for c in at.checkbox if c.label == "Use the bundled sample ledger").check().run()
    return at


def _run_audit(db) -> AppTest:
    at = _start(db)
    _button(at, "Run audit").click().run()
    assert not at.exception, at.exception
    assert at.session_state["audit_results"] is not None
    return at


def _stages(db, txn):
    return [e["stage"] for e in audit_log.history(txn, db)]


# --- tests -------------------------------------------------------------------
def test_audit_runs_on_the_sample_ledger(db):
    at = _run_audit(db)
    results = at.session_state["audit_results"]
    assert set(FakeEngine.assessed) == set(results["transaction_id"])
    assert (results["verdict"] == "SUSPICIOUS").any()
    assert audit_log.batch_summary(at.session_state["batch_id"], db)["SUSPICIOUS"] > 0
    assert any(s.value == "Investigative ledger" for s in at.subheader)


def test_sar_refused_without_approval_and_allowed_after_officer_approval(db):
    at = _run_audit(db)
    target = at.selectbox(key="sar_pick").value
    assert "requires officer approval" in _texts(at.warning)
    assert not any(b.label == "Draft SAR" for b in at.button)

    at.text_input(key=f"sar_officer_{target}").input("officer.k").run()
    at.button(key=f"sar_approve_{target}").click().run()
    assert not at.exception, at.exception
    assert _stages(db, target)[-1] == "human_review"

    _button(at, "Draft SAR").click().run()
    assert "SAR drafted." in _texts(at.success)
    assert _stages(db, target)[-1] == "sar_drafted"
    assert len(FakeProvider.instances[-1].llm.sar_calls) == 1


def test_screening_unavailable_blocks_the_audit(db, monkeypatch):
    monkeypatch.setitem(sys.modules, "rapidfuzz", None)  # import fails
    at = _start(db)
    _button(at, "Run audit").click().run()
    assert "rapidfuzz" in _texts(at.error)
    assert "audit_results" not in at.session_state
    assert FakeEngine.assessed == []


def test_officer_review_audit_failure_blocks_the_decision(db, monkeypatch, tmp_path):
    at = _run_audit(db)
    target = at.selectbox(key="sar_pick").value
    _fail_stage(monkeypatch, tmp_path, "human_review")

    at.text_input(key=f"sar_officer_{target}").input("officer.k").run()
    at.button(key=f"sar_approve_{target}").click().run()
    assert "Decision not applied" in _texts(at.error)
    assert "human_review" not in _stages(db, target)
    assert not any(b.label == "Draft SAR" for b in at.button)


def test_topology_human_review_audit_failure_blocks_the_decision(db, monkeypatch, tmp_path):
    at = _run_audit(db)
    at.selectbox(key="topo_pick").set_value(SUSPICIOUS_HIGH_SCORE).run()
    _button(at, "Run topology audit").click().run()
    assert "Execution paused" in _texts(at.error)

    _fail_stage(monkeypatch, tmp_path, "human_review")
    at.text_input(key=f"approver_{SUSPICIOUS_HIGH_SCORE}").input("officer.k").run()
    _button(at, "Approve findings").click().run()
    assert "Decision not applied" in _texts(at.error)
    assert "Execution paused" in _texts(at.error)  # graph did not resume
    assert "human_review" not in _stages(db, SUSPICIOUS_HIGH_SCORE)


def test_sanctions_hit_audit_failure_withholds_the_freeze_directive(db, monkeypatch, tmp_path):
    at = _run_audit(db)
    _fail_stage(monkeypatch, tmp_path, "sanctions_hit")
    next(t for t in at.text_input if t.label.startswith("Entity name")).input("Viktor Bout").run()
    assert "Potential match" in _texts(at.error)
    assert "Freeze directive withheld" in _texts(at.error)
    assert not any("COMPLIANCE DIRECTIVE" in c.value for c in at.code)


# --- sanctions matches found during the batch --------------------------------
SANCTIONED_TXN = "SDN-TEST-1"


@pytest.fixture
def sanctioned_ledger(monkeypatch, tmp_path):
    """The sample ledger plus one transfer from a watchlisted name. (AppTest
    cannot drive the file uploader, so the bundled-sample path is redirected.)"""
    path = tmp_path / "ledger_with_sdn.csv"
    text = config.SAMPLE_LEDGER.read_text(encoding="utf-8").rstrip("\n")
    path.write_text(text + f"\n{SANCTIONED_TXN},2026-03-10 09:00:00,Viktor Bout,"
                           "Acme Logistics,450.00,USA\n", encoding="utf-8")
    assert text.splitlines()[0] == "transaction_id,timestamp,sender,receiver,amount,country"
    monkeypatch.setattr(config, "SAMPLE_LEDGER", path)
    return path


def test_batch_sanctions_match_is_recorded_before_results_release(db, sanctioned_ledger):
    at = _run_audit(db)
    hits = [e for e in audit_log.history(SANCTIONED_TXN, db) if e["stage"] == "sanctions_hit"]
    assert len(hits) == 1
    assert hits[0]["batch_id"] == at.session_state["batch_id"]
    assert "Viktor Bout" in hits[0]["rationale"]


def test_batch_sanctions_hit_write_failure_blocks_the_batch(db, sanctioned_ledger,
                                                            monkeypatch, tmp_path):
    _fail_stage(monkeypatch, tmp_path, "sanctions_hit")
    at = _start(db)
    _button(at, "Run audit").click().run()
    assert not at.exception, at.exception
    assert "sanctions_hit" in _texts(at.error)
    assert "audit_results" not in at.session_state
    assert FakeEngine.assessed == []  # no LLM spend on an unrecorded batch
