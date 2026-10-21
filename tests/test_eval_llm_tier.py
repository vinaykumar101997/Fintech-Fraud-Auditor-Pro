"""The compliance-tier evaluation harness, run on its fake model only.

Offline: no network, no credentials. Needs langchain_core and pydantic, which
requirements-dev.txt installs.
"""

from __future__ import annotations

import csv
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


def test_rule_reasons_match_the_rules_engine():
    for case in CASES:
        txn = case["transaction"]
        result = evaluate_row(amount=Decimal(txn["amount"]), country=txn["country"],
                              sender=txn["sender"], receiver=txn["receiver"], screener=rule_screener)
        assert case["rule_reasons"] == result.reasons, case["id"]


def test_only_demo_watchlist_names_match_the_sanctions_list():
    with config.SANCTIONS_CSV.open(encoding="utf-8") as fh:
        watchlisted = {row["name"] for row in csv.DictReader(fh)}
    for case in CASES:
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
