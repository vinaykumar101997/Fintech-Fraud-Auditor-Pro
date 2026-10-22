"""The compliance-tier evaluation harness, run on its fake model only.

Offline: no network, no credentials. Needs langchain_core and pydantic, which
requirements-dev.txt installs.
"""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("langchain_core")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import config  # noqa: E402
import eval_llm_tier as harness  # noqa: E402
from utils.rules import evaluate_row  # noqa: E402
from utils.screening import rule_screener  # noqa: E402

CASES = harness.load_cases()
# Suspicious cases with no rule reason; loading it checks the schema.
NORULE = harness.load_cases(ROOT / "tests" / "fixtures" / "llm_cases_norule.json")


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch):
    monkeypatch.setattr(config, "LLM_RETRY_BASE_DELAY", 0.0)


def _run(mode, cases=CASES, cap=None):
    budget = harness.CallBudget(cap)
    engine, rag_engine, fake = harness.build_fake(mode, budget)
    run = harness.run_cases(cases, engine, rag_engine, budget, fake=fake)
    return run, harness.score(run.results)


# --- case set ------------------------------------------------------------------
def test_case_set_size_and_balance():
    counts = Counter(c["expected_verdict"] for c in CASES)
    assert 25 <= len(CASES) <= 40
    assert counts["CLEAR"] >= 8
    assert min(counts.values()) / len(CASES) >= 0.35  # roughly balanced


def test_every_category_is_covered():
    assert {c["category"] for c in CASES} == set(harness.CATEGORIES)
    per_category = Counter(c["category"] for c in CASES)
    assert min(per_category.values()) >= 3


def test_category_labels_are_consistent():
    for case in CASES:
        if case["category"] in ("obvious_clear", "over_threshold_legitimate"):
            assert case["expected_verdict"] == "CLEAR", case["id"]
        if case["category"] == "over_threshold_legitimate":
            assert Decimal(case["transaction"]["amount"]) >= config.REPORTING_THRESHOLD, case["id"]
        if case["category"] == "prompt_injection":
            # The injected text asks for CLEAR; the facts say otherwise.
            assert case["expected_verdict"] == "SUSPICIOUS", case["id"]
            assert "CLEAR" in " ".join(case["transaction"].values()), case["id"]


@pytest.mark.parametrize("cases", [CASES, NORULE], ids=["development", "norule"])
def test_rule_reasons_match_the_rules_engine(cases):
    for case in cases:
        txn = case["transaction"]
        result = evaluate_row(amount=Decimal(txn["amount"]), country=txn["country"],
                              sender=txn["sender"], receiver=txn["receiver"], screener=rule_screener)
        assert case["rule_reasons"] == result.reasons, case["id"]


@pytest.mark.parametrize("cases", [CASES, NORULE], ids=["development", "norule"])
def test_only_demo_watchlist_names_match_the_sanctions_list(cases):
    with config.SANCTIONS_CSV.open(encoding="utf-8") as fh:
        watchlisted = {row["name"] for row in csv.DictReader(fh)}
    for case in cases:
        for party in ("sender", "receiver"):
            name = case["transaction"][party]
            if rule_screener(name):
                assert name in watchlisted, (case["id"], name)


def test_load_cases_rejects_a_malformed_case(tmp_path):
    bad = tmp_path / "cases.json"
    bad.write_text('{"cases": [{"id": "X", "category": "obvious_clear", "expected_verdict": "MAYBE",'
                   ' "transaction": {}, "rule_reasons": [], "behaviour_flags": []}]}', encoding="utf-8")
    with pytest.raises(ValueError, match="expected_verdict"):
        harness.load_cases(bad)


# --- fake modes ----------------------------------------------------------------
def test_oracle_scores_100_percent():
    run, m = _run("oracle")
    assert m["accuracy"][:2] == (len(CASES), len(CASES))
    assert m["precision"][0] == m["precision"][1] == m["recall"][1]
    assert m["recall"][0] == m["recall"][1]
    assert m["errored"][0] == 0
    assert run.calls == 2 * len(CASES)  # analysis + structured review per case
    assert all(k == n for k, n in m["per_category"].values())


def test_always_clear_has_zero_recall():
    _, m = _run("always-clear")
    sus = sum(c["expected_verdict"] == "SUSPICIOUS" for c in CASES)
    assert m["recall"][:2] == (0, sus)
    assert m["precision"][1] == 0  # nothing predicted SUSPICIOUS
    assert m["confusion"]["SUSPICIOUS"] == {"CLEAR": sus}


def test_always_suspicious_gets_every_clear_case_wrong():
    _, m = _run("always-suspicious")
    clear = sum(c["expected_verdict"] == "CLEAR" for c in CASES)
    assert m["recall"][0] == m["recall"][1]
    assert m["confusion"]["CLEAR"] == {"SUSPICIOUS": clear}
    assert m["per_category"]["obvious_clear"][0] == 0


def test_error_mode_errors_every_review_and_never_clears(monkeypatch):
    monkeypatch.setattr(config, "LLM_MAX_RETRIES", 0)
    run, m = _run("error")
    assert m["errored"] == (len(CASES), len(CASES))
    assert m["errored_as_clear"] == []
    assert all(r.verdict == "ERROR" for r in run.results)
    for row in m["confusion"].values():
        assert set(row) == {"ERROR"}
    assert m["accuracy"][0] == 0


def test_retries_count_against_the_call_budget(monkeypatch):
    monkeypatch.setattr(config, "LLM_MAX_RETRIES", 2)
    run, _ = _run("error", cases=CASES[:2])
    assert run.calls == 2 * 3  # stage 1 fails on each of 1 + 2 attempts


def test_errored_review_counted_as_clear_is_caught():
    bad = harness.CaseResult("X", "obvious_clear", "CLEAR", "CLEAR", errored=True, error_detail="boom")
    m = harness.score([bad])
    assert m["errored_as_clear"] == ["X"]
    assert m["confusion"]["CLEAR"] == {"ERROR": 1}


# --- call cap ------------------------------------------------------------------
@pytest.mark.parametrize("cap", [1, 6, 7, 10])
def test_call_cap_stops_exactly_at_the_cap(cap):
    run, m = _run("oracle", cap=cap)
    assert run.calls == cap
    assert run.stopped_at_cap
    assert m["n"] == cap // 2  # a case cut off mid-review is dropped, not scored
    assert m["errored"][0] == 0


def test_cap_larger_than_needed_does_not_stop():
    run, m = _run("oracle", cap=2 * len(CASES))
    assert not run.stopped_at_cap
    assert m["n"] == len(CASES)


def test_live_meter_refuses_the_call_over_the_cap():
    budget = harness.CallBudget(2)
    meter = harness.usage_meter(budget)
    meter.on_chat_model_start({}, [])
    meter.on_llm_start({}, [])
    with pytest.raises(harness.CallCapReached):
        meter.on_chat_model_start({}, [])
    assert budget.used == 2


def test_main_defaults_to_the_fake_model(tmp_path, capsys):
    out = tmp_path / "report.md"
    assert harness.main(["--fake", "oracle", "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "fake model, oracle" in text
    assert f"| Verdict accuracy | {len(CASES)}/{len(CASES)} = 100%" in text


# --- per-case record -------------------------------------------------------------
ROW_FIELDS = {
    "id", "category", "expected", "returned", "confidence", "reasoning", "error_detail",
    "rule_reasons", "passages_retrieved", "passage_ids", "model_calls", "input_tokens",
    "output_tokens", "seconds",
}


def _run_to_file(mode, path, cases=CASES, cap=None):
    budget = harness.CallBudget(cap)
    engine, rag_engine, fake = harness.build_fake(mode, budget)
    with path.open("a", encoding="utf-8") as fh:
        run = harness.run_cases(cases, engine, rag_engine, budget, fake=fake, cases_out=fh)
    lines = path.read_text(encoding="utf-8").splitlines()
    return run, [json.loads(line) for line in lines]


def test_cases_out_writes_one_json_row_per_case(tmp_path):
    run, rows = _run_to_file("oracle", tmp_path / "cases.jsonl")
    assert len(rows) == len(CASES) == len(run.results)
    by_id = {c["id"]: c for c in CASES}
    for row in rows:
        assert set(row) == ROW_FIELDS
        case = by_id[row["id"]]
        assert row["returned"] == row["expected"] == case["expected_verdict"]
        assert row["rule_reasons"] == case["rule_reasons"]
        assert row["reasoning"] == "Fake oracle review."
        assert row["passages_retrieved"] == 5
        assert row["passage_ids"][0] == "fake-passage-1"
        assert row["model_calls"] == 2
        assert row["input_tokens"] is None and row["output_tokens"] is None
        assert row["error_detail"] is None
        assert row["seconds"] >= 0


@pytest.mark.parametrize("cap", [1, 7])
def test_cases_out_keeps_finished_rows_at_the_cap(tmp_path, cap):
    run, rows = _run_to_file("oracle", tmp_path / "cases.jsonl", cap=cap)
    assert run.stopped_at_cap
    assert [r["id"] for r in rows] == [c["id"] for c in CASES[:cap // 2]]


def test_cases_out_records_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LLM_MAX_RETRIES", 0)
    _, rows = _run_to_file("error", tmp_path / "cases.jsonl", cases=CASES[:3])
    assert len(rows) == 3
    for row in rows:
        assert row["returned"] == "ERROR"
        assert "fake provider outage" in row["error_detail"]
        assert row["reasoning"] is None
        assert row["passages_retrieved"] == 0
        assert row["model_calls"] == 1


def test_main_writes_cases_out_and_progress(tmp_path, capsys):
    out = tmp_path / "nested" / "cases.jsonl"
    assert harness.main(["--fake", "always-clear", "--cases-out", str(out)]) == 0
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == len(CASES)
    printed = capsys.readouterr().out
    assert f"{CASES[0]['id']:<8} expected {CASES[0]['expected_verdict']:<10} returned CLEAR" in printed


# --- held-out case set -----------------------------------------------------------
HELDOUT = harness.load_cases(harness.HELDOUT_CASES_PATH)


def test_heldout_set_size_balance_and_ids():
    counts = Counter(c["expected_verdict"] for c in HELDOUT)
    assert 18 <= len(HELDOUT) <= 25
    assert counts["CLEAR"] >= 8 and counts["SUSPICIOUS"] >= 8
    assert all(c["id"].startswith("H-") for c in HELDOUT)
    assert not {c["id"] for c in HELDOUT} & {c["id"] for c in CASES}


def test_heldout_signal_is_visible_to_the_review():
    # The review never sees behaviour flags, so no held-out case may depend on them.
    for case in HELDOUT:
        assert case["category"] != "pass_through", case["id"]
        assert case["behaviour_flags"] == [], case["id"]
        if case["expected_verdict"] == "SUSPICIOUS":
            assert case["rule_reasons"], case["id"]
        elif case["category"] == "obvious_clear":
            assert case["rule_reasons"] == [], case["id"]


def test_heldout_labels_are_consistent():
    for case in HELDOUT:
        if case["category"] in ("obvious_clear", "over_threshold_legitimate"):
            assert case["expected_verdict"] == "CLEAR", case["id"]
        if case["category"] == "over_threshold_legitimate":
            assert Decimal(case["transaction"]["amount"]) >= config.REPORTING_THRESHOLD, case["id"]


def test_heldout_rule_reasons_match_the_rules_engine():
    for case in HELDOUT:
        txn = case["transaction"]
        result = evaluate_row(amount=Decimal(txn["amount"]), country=txn["country"],
                              sender=txn["sender"], receiver=txn["receiver"], screener=rule_screener)
        assert case["rule_reasons"] == result.reasons, case["id"]


def test_heldout_sanctions_hits_are_watchlist_entries_or_aliases():
    with config.SANCTIONS_CSV.open(encoding="utf-8") as fh:
        watchlisted = {row["name"] for row in csv.DictReader(fh)}
    for case in HELDOUT:
        for party in ("sender", "receiver"):
            hit = rule_screener(case["transaction"][party])
            if hit:
                assert hit in watchlisted, (case["id"], hit)


def test_heldout_oracle_scores_100_percent():
    _, m = _run("oracle", cases=HELDOUT)
    assert m["accuracy"][:2] == (len(HELDOUT), len(HELDOUT))


# --- case file and category options -----------------------------------------------
def test_main_runs_another_case_file_and_names_it(tmp_path):
    out = tmp_path / "report.md"
    assert harness.main(["--cases", str(harness.HELDOUT_CASES_PATH), "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "Case file: tests/fixtures/llm_cases_heldout.json; excluded categories: none" in text
    assert f"Cases reviewed: {len(HELDOUT)} of {len(HELDOUT)}" in text


def test_main_excludes_categories_and_reports_them(tmp_path):
    out = tmp_path / "report.md"
    rows = tmp_path / "cases.jsonl"
    argv = ["--exclude-category", "pass_through", "--exclude-category", "prompt_injection",
            "--out", str(out), "--cases-out", str(rows)]
    assert harness.main(argv) == 0
    text = out.read_text(encoding="utf-8")
    kept = [c for c in CASES if c["category"] not in ("pass_through", "prompt_injection")]
    assert "Case file: tests/fixtures/llm_cases.json; excluded categories: pass_through, prompt_injection" in text
    assert f"Cases reviewed: {len(kept)} of {len(kept)}" in text
    assert "| pass_through |" not in text and "| prompt_injection |" not in text
    ids = [json.loads(line)["id"] for line in rows.read_text(encoding="utf-8").splitlines()]
    assert ids == [c["id"] for c in kept]


def test_report_footer_explains_pass_through(tmp_path):
    out = tmp_path / "report.md"
    harness.main(["--out", str(out)])
    assert out.read_text(encoding="utf-8").rstrip().endswith(harness.PASS_THROUGH_NOTE)


def test_excluding_every_category_is_an_error():
    argv = [arg for cat in harness.CATEGORIES for arg in ("--exclude-category", cat)]
    with pytest.raises(SystemExit):
        harness.main(argv)


def test_unknown_category_is_rejected():
    with pytest.raises(SystemExit):
        harness.main(["--exclude-category", "no_such_category"])
