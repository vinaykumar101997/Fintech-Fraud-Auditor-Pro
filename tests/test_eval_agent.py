"""Evaluation harness for the investigation agent: case files, labels, scoring.

Offline, on the fake model only. Needs langchain_core and pydantic, which
requirements-dev.txt installs.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("langchain_core")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import compare_llm_runs  # noqa: E402
import config  # noqa: E402
import eval_llm_tier as harness  # noqa: E402
from generate_sample_ledger import generate  # noqa: E402
from utils.agent_tools import TOOL_NAMES  # noqa: E402
from utils.data_loader import parse_money  # noqa: E402
from utils.rules import evaluate_row  # noqa: E402
from utils.screening import rule_screener  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
LEDGER_CASES = harness.load_cases(harness.LEDGER_CASES_PATH)
LABELS = harness.load_tool_labels()
ALL_CASE_FILES = ("llm_cases.json", "llm_cases_heldout.json", "llm_cases_norule.json",
                  "agent_ledger_cases.json")


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch):
    monkeypatch.setattr(config, "LLM_RETRY_BASE_DELAY", 0.0)


def _documented_hash(name: str) -> str:
    text = (ROOT / "docs" / "EVALUATION.md").read_text(encoding="utf-8")
    match = re.search(rf"`tests/fixtures/{re.escape(name)}`, SHA-256 `([0-9a-f]{{64}})`", text)
    assert match, f"no SHA-256 recorded for {name}"
    return match.group(1)


# --- frozen files --------------------------------------------------------------------
@pytest.mark.parametrize("name", ["agent_tool_labels.json", "agent_ledger_cases.json"])
def test_frozen_files_match_their_recorded_hash(name):
    data = (FIXTURES / name).read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(data).hexdigest() == _documented_hash(name)


def test_labels_cover_every_case_once_with_known_tools():
    ids = [c["id"] for f in ALL_CASE_FILES for c in harness.load_cases(FIXTURES / f)]
    assert len(ids) == len(set(ids)) == 92
    assert set(LABELS) == set(ids)
    for cid, label in LABELS.items():
        required, acceptable = set(label["required"]), set(label["acceptable"])
        assert required | acceptable <= set(TOOL_NAMES), cid
        assert not required & acceptable, cid


def test_label_policy_holds():
    cases = {c["id"]: c for f in ALL_CASE_FILES for c in harness.load_cases(FIXTURES / f)}
    for cid, label in LABELS.items():
        case = cases[cid]
        ledger_case = cid.startswith("L-")
        assert ("party_history" in label["required"]) == ledger_case, cid
        if case["expected_verdict"] == "SUSPICIOUS":
            assert "search_guidance" in label["required"], cid
        assert "country_risk" not in label["required"], cid
    screen_required = {cid for cid, l in LABELS.items() if "screen_party" in l["required"]}
    assert screen_required == {"N-05", "N-06", "N-12"}


# --- ledger-backed cases -----------------------------------------------------------------
def test_ledger_cases_size_balance_and_ids():
    counts = Counter(c["expected_verdict"] for c in LEDGER_CASES)
    assert len(LEDGER_CASES) == 20 and counts == {"SUSPICIOUS": 12, "CLEAR": 8}
    assert all(c["id"].startswith("L-") for c in LEDGER_CASES)
    assert harness.ledger_spec(harness.LEDGER_CASES_PATH) == {
        "generator": "scripts/generate_sample_ledger.py", "seed": 7, "hard": True}


def test_ledger_cases_are_generator_rows_with_the_generator_label():
    rows = generate(seed=7, hard=True).set_index("transaction_id")
    for case in LEDGER_CASES:
        row = rows.loc[case["ledger_transaction_id"]]
        t = case["transaction"]
        assert parse_money(row["amount"]) == Decimal(t["amount"]), case["id"]
        assert (row["country"], row["sender"], row["receiver"]) == (t["country"], t["sender"], t["receiver"])
        assert case["expected_verdict"] == ("SUSPICIOUS" if int(row["is_laundering"]) else "CLEAR")
        reasons = evaluate_row(amount=Decimal(t["amount"]), country=t["country"], sender=t["sender"],
                               receiver=t["receiver"], screener=rule_screener).reasons
        assert case["rule_reasons"] == reasons, case["id"]


def test_most_suspicious_ledger_cases_have_no_rule_hit():
    suspicious = [c for c in LEDGER_CASES if c["expected_verdict"] == "SUSPICIOUS"]
    assert sum(not c["rule_reasons"] for c in suspicious) == 9


def test_the_case_ledger_is_the_generated_batch_ready_for_the_tools():
    ledger = harness.build_case_ledger(harness.ledger_spec(harness.LEDGER_CASES_PATH))
    assert len(ledger) == 514
    assert {"transaction_id", "sender", "receiver", "amount_float", "timestamp"} <= set(ledger.columns)
    assert "is_laundering" not in ledger.columns and "pattern" not in ledger.columns


# --- the harness on the fake model ----------------------------------------------------------
def _main(tmp_path, *argv):
    out, rows = tmp_path / "report.md", tmp_path / "rows.jsonl"
    code = harness.main([*argv, "--out", str(out), "--cases-out", str(rows)])
    return code, out.read_text(encoding="utf-8"), harness.read_rows(rows)


def test_agent_engine_records_tools_and_scores_tool_selection(tmp_path):
    code, text, rows = _main(tmp_path, "--engine", "agent")
    assert code == 0 and "engine agent" in text
    assert all(r["tools_called"] == ["search_guidance", "party_history"] for r in rows)
    assert all(r["model_calls"] == 3 and r["stop_reason"] == "answered" for r in rows)
    assert all(r["passages_retrieved"] == 5 for r in rows)
    assert "| Recall (required tools called) |" in text
    assert text.rstrip().endswith(harness.PASS_THROUGH_NOTE)


def test_agent_engine_on_the_ledger_cases(tmp_path):
    code, text, rows = _main(tmp_path, "--engine", "agent", "--cases", str(harness.LEDGER_CASES_PATH))
    assert code == 0 and len(rows) == 20
    assert "| party_history on the labelled party | 17/20 = 85% |" in text  # fake asks the originator


def test_fixed_history_needs_a_ledger(tmp_path):
    with pytest.raises(SystemExit):
        harness.main(["--engine", "fixed+history"])


def test_fixed_history_adds_both_histories_to_the_record():
    seen = []

    class Spy:
        def assess_transaction(self, **kwargs):
            seen.append(kwargs)
            from utils.verdicts import AuditOutcome, Verdict
            return AuditOutcome(verdict=Verdict.CLEAR, rationale="x")

    ledger = harness.build_case_ledger(harness.ledger_spec(harness.LEDGER_CASES_PATH))
    harness.run_cases(LEDGER_CASES[:2], Spy(), None, harness.CallBudget(), engine_kind="fixed+history",
                      ledger=ledger)
    signals = seen[0]["behaviour_signals"]
    assert set(signals) == {"originator_history", "beneficiary_history"}
    assert json.loads(signals["originator_history"])["this_payment"]["forwarding"]["legs_in_forwarding_chain"] == 3
    assert seen[0]["transaction_id"] == LEDGER_CASES[0]["id"]
    assert not GENERATOR_ID.search(json.dumps(seen, default=str))


GENERATOR_ID = re.compile(r"\b(ESTP|FAN|STR|CIRS|CIRC|CIR|TXN|SAN|NAMEV|NEAR|CTRV|CTRC)-\d")


class Capturing(harness.FakeLLM):
    """The harness fake, keeping every text the model is shown."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.shown = []

    def bind_tools(self, tools):
        from langchain_core.runnables import RunnableLambda

        inner = super().bind_tools(tools)

        def respond(messages):
            self.shown.extend(str(m.content) for m in messages)
            return inner.invoke(messages)
        return RunnableLambda(respond)

    def with_structured_output(self, schema):
        from langchain_core.runnables import RunnableLambda

        inner = super().with_structured_output(schema)

        def respond(review_input):
            self.shown.append(review_input.to_string())
            return inner.invoke(review_input)
        return RunnableLambda(respond)


@pytest.mark.parametrize("engine_kind", ["agent", "fixed+history"])
def test_generator_ids_never_reach_the_model(engine_kind):
    from utils.agents import ComplianceAuditEngine
    from utils.investigation import InvestigationEngine

    budget = harness.CallBudget()
    llm = Capturing("oracle", budget)
    ledger = harness.build_case_ledger(harness.ledger_spec(harness.LEDGER_CASES_PATH))
    if engine_kind == "agent":
        engine, rag, retriever = InvestigationEngine(llm=llm), None, harness.RetrieverRecorder(harness.FakeRetriever())
    else:
        engine, rag, retriever = ComplianceAuditEngine(llm=llm), harness.FakeRetrievalQA(llm), None
    run = harness.run_cases(LEDGER_CASES, engine, rag, budget, fake=llm, engine_kind=engine_kind,
                            ledger=ledger, retriever=retriever)
    assert len(run.results) == 20 and not any(r.errored for r in run.results)
    shown = "\n".join(llm.shown)
    assert "originator_history" in shown or "party_history" in shown
    assert not GENERATOR_ID.search(shown), GENERATOR_ID.search(shown)


def test_fixed_rows_keep_their_fields(tmp_path):
    _, _, rows = _main(tmp_path)
    assert "engine" not in rows[0] and "tools_called" not in rows[0]


def test_error_mode_agent_never_clears(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LLM_MAX_RETRIES", 0)
    code, text, rows = _main(tmp_path, "--engine", "agent", "--fake", "error")
    assert code == 0
    assert all(r["returned"] == "ERROR" for r in rows)
    assert "| Errored reviews counted as CLEAR | 0 |" in text


def test_resume_skips_finished_cases_and_scores_the_whole_file(tmp_path):
    rows = tmp_path / "rows.jsonl"
    harness.main(["--engine", "agent", "--max-calls", "30", "--cases-out", str(rows)])
    first = harness.read_rows(rows)
    assert len(first) == 10
    out = tmp_path / "report.md"
    assert harness.main(["--engine", "agent", "--resume", "--cases-out", str(rows), "--out", str(out)]) == 0
    ids = [r["id"] for r in harness.read_rows(rows)]
    assert len(ids) == len(set(ids)) == 31
    assert "Cases reviewed: 31 of 31" in out.read_text(encoding="utf-8")


def test_resume_needs_cases_out():
    with pytest.raises(SystemExit):
        harness.main(["--resume"])


# --- scoring helpers ------------------------------------------------------------------------
def _row(cid, called, calls=None, expected="SUSPICIOUS", returned="SUSPICIOUS", **extra):
    return {"id": cid, "category": "x", "expected": expected, "returned": returned,
            "tools_called": called, "tool_calls": calls or [{"name": n, "args": {}} for n in called],
            **extra}


def test_tool_selection_recall_precision_and_roles():
    labels = {
        "A": {"required": ["search_guidance"], "acceptable": ["country_risk"]},
        "B": {"required": ["party_history", "search_guidance"], "acceptable": [],
              "required_role": {"party_history": "beneficiary"}},
        "C": {"required": [], "acceptable": ["country_risk"]},
    }
    rows = [
        _row("A", ["search_guidance", "country_risk", "explain_rule"]),
        _row("B", ["party_history"], [{"name": "party_history", "args": {"role": "originator"}}]),
        _row("C", []),
    ]
    result = harness.tool_selection(rows, labels)
    assert result["recall"] == (2, 3)
    assert result["precision"] == (3, 4)
    assert result["no_calls"] == 1
    assert result["required_role"] == (0, 1)
    assert result["unnecessary"] == {"explain_rule": 1}
    assert result["missed"] == {"search_guidance": 1}


def test_usage_summary_means_cost_and_p95():
    rows = [{"model_calls": 4, "tool_calls": [{}] * 2, "input_tokens": 1000, "output_tokens": 100,
             "seconds": float(s)} for s in range(1, 21)]
    summary = harness.usage_summary(rows, prices=(0.30, 2.50))
    assert summary["model_calls"] == 4 and summary["tool_calls"] == 2
    assert summary["seconds_p95"] == 19.0
    assert summary["cost_per_case"] == pytest.approx((1000 * 0.30 + 100 * 2.50) / 1e6)


def test_pause_waits_after_every_model_call():
    waits = []
    meter = harness.usage_meter(harness.CallBudget(), pause=3.0, sleep=waits.append)

    class Response:
        generations = []

    meter.on_llm_end(Response())
    meter.on_llm_end(Response())
    assert waits == [3.0, 3.0]


def test_meter_lists_each_call_and_counts_a_cut_off_reply():
    from types import SimpleNamespace

    from utils.llm_provider import OutputTruncated

    meter = harness.usage_meter(harness.CallBudget())
    answered = SimpleNamespace(
        message=SimpleNamespace(usage_metadata={"input_tokens": 100, "output_tokens": 20,
                                                "total_tokens": 150}),
        generation_info={"finish_reason": "STOP"})
    meter.on_llm_end(SimpleNamespace(generations=[[answered]]))
    meter.on_llm_error(OutputTruncated({"input_tokens": 100, "output_tokens": 0,
                                        "total_tokens": 4196}))
    meter.on_llm_error(RuntimeError("503"))  # no usage: nothing was billed
    assert meter.calls == [{"output_tokens": 50, "finish_reason": "STOP"},
                           {"output_tokens": 4096, "finish_reason": "MAX_TOKENS"}]
    assert (meter.input_tokens, meter.output_tokens) == (200, 4146)


def test_guidance_cache_serves_a_repeated_query_without_the_index(tmp_path):
    inner = harness.FakeRetriever()
    calls = []
    original = inner.invoke
    inner.invoke = lambda q, config=None: (calls.append(q), original(q))[1]
    recorder = harness.RetrieverRecorder(inner, tmp_path, "key")
    first = recorder.invoke("structuring query")
    second = harness.RetrieverRecorder(inner, tmp_path, "key").invoke("structuring query")
    assert calls == ["structuring query"]
    assert [d.page_content for d in first] == [d.page_content for d in second]
    assert harness.RetrieverRecorder(inner, tmp_path, "other index").invoke("structuring query")
    assert len(calls) == 2


def test_discordant_pairs():
    a = [_row("S1", [], returned="SUSPICIOUS"), _row("S2", [], returned="CLEAR"),
         _row("S3", [], returned="ERROR"), _row("C1", [], expected="CLEAR", returned="CLEAR"),
         _row("X", [])]
    b = [_row("S1", [], returned="CLEAR"), _row("S2", [], returned="SUSPICIOUS"),
         _row("S3", [], returned="SUSPICIOUS"), _row("C1", [], expected="CLEAR", returned="SUSPICIOUS")]
    result = compare_llm_runs.discordant(a, b)
    assert result["paired"] == 4 and result["only_a"] == ["X"]
    assert result["SUSPICIOUS"]["b"] == ["S1"] and result["SUSPICIOUS"]["c"] == ["S2", "S3"]
    assert result["CLEAR"]["b"] == ["C1"] and result["CLEAR"]["c"] == []


def test_compare_script_prints_a_table(tmp_path, capsys):
    for name in ("a.jsonl", "b.jsonl"):
        (tmp_path / name).write_text(json.dumps(_row("S1", [])) + "\n", encoding="utf-8")
    assert compare_llm_runs.main([str(tmp_path / "a.jsonl"), str(tmp_path / "b.jsonl")]) == 0
    assert "| SUSPICIOUS | 1 | 1 | 1 | 0  | 0  |" in capsys.readouterr().out
