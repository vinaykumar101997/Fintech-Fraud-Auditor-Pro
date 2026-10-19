"""scripts/eval_external.py: reading the IBM AML transaction files and scoring them.

The fixture is 20 hand-made rows in the layout of the Kaggle files
(HI-Small_Trans.csv and siblings); no real data is used.
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import config  # noqa: E402
import eval_external  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "ibm_aml_sample.csv"


def test_fixture_has_twenty_rows_in_the_ibm_layout():
    lines = FIXTURE.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 21
    assert lines[0].split(",") == eval_external.IBM_HEADER


def test_ibm_columns_map_onto_the_ledger_columns():
    frame, info = eval_external.read_ibm(FIXTURE, max_rows=100, currency="all")
    assert len(frame) == 20 and info["rows_read"] == 20
    first = frame.iloc[0]
    assert first["transaction_id"] == "L2"  # line 2 of the file
    assert first["timestamp"] == "2022-09-01 00:20:00"
    assert first["sender"] == "010-8000EBD30"
    assert first["receiver"] == "010-8000EBD30"
    assert first["amount"] == "3697.34"
    assert first["country"] == ""
    # Amount Paid in the Payment Currency, not Amount Received.
    wire = frame[frame["transaction_id"] == "L19"].iloc[0]
    assert (wire["amount"], wire["currency"]) == ("1012.50", "US Dollar")
    assert frame["is_laundering"].tolist().count(1) == 4


def test_bank_codes_are_kept_as_written():
    """'0213' and '00213' are different strings in the file. Leading zeros are not
    stripped, so the two are separate accounts."""
    frame, _ = eval_external.read_ibm(FIXTURE, max_rows=100, currency="all")
    row = frame[frame["transaction_id"] == "L18"].iloc[0]
    assert (row["sender"], row["receiver"]) == ("0213-8124D9E00", "00213-8124D9E00")


def test_currency_filter_keeps_only_that_payment_currency():
    frame, info = eval_external.read_ibm(FIXTURE, max_rows=100, currency="US Dollar")
    assert len(frame) == 17
    assert set(frame["currency"]) == {"US Dollar"}
    assert info["currency_counts"] == {"US Dollar": 17, "Euro": 2, "Yuan": 1}


def test_max_rows_counts_rows_kept_after_the_currency_filter():
    frame, _ = eval_external.read_ibm(FIXTURE, max_rows=6, currency="US Dollar")
    assert frame["transaction_id"].tolist() == ["L2", "L3", "L4", "L5", "L6", "L8"]


@pytest.mark.parametrize("currency,max_rows", [("US Dollar", 6), ("US Dollar", 100), ("all", 7), ("Euro", 1)])
def test_reading_in_small_chunks_gives_the_same_rows(currency, max_rows):
    whole, whole_info = eval_external.read_ibm(FIXTURE, max_rows=max_rows, currency=currency)
    for chunksize in (1, 3, 7):
        part, part_info = eval_external.read_ibm(FIXTURE, max_rows=max_rows, currency=currency,
                                                 chunksize=chunksize)
        pd.testing.assert_frame_equal(part.reset_index(drop=True), whole.reset_index(drop=True))
        assert part_info == whole_info


def test_rows_read_stops_at_the_row_that_completes_max_rows():
    _, info = eval_external.read_ibm(FIXTURE, max_rows=6, currency="US Dollar")
    assert info["rows_read"] == 7  # lines 2 to 8; line 7 is paid in Euro
    assert info["currency_counts"] == {"US Dollar": 6, "Euro": 1}


def _multi_day_file(tmp_path):
    """The fixture with its rows spread over 1 to 4 September, out of time order."""
    lines = FIXTURE.read_text(encoding="utf-8").splitlines()
    days = [3, 1, 4, 2, 1, 3, 2, 4, 1, 3, 2, 4, 1, 2, 3, 4, 1, 2, 3, 4]
    rows = [f"2022/09/0{day} {line[11:16]}{line[16:]}" for day, line in zip(days, lines[1:])]
    path = tmp_path / "days.csv"
    path.write_text("\n".join([lines[0]] + rows) + "\n", encoding="utf-8")
    return path, days


def test_date_range_filters_on_the_timestamp_not_on_file_order(tmp_path):
    path, days = _multi_day_file(tmp_path)
    frame, info = eval_external.read_ibm(path, max_rows=100, currency="all",
                                         start="2022-09-02", end="2022-09-03")
    expected = {f"L{i + 2}" for i, day in enumerate(days) if day in (2, 3)}
    assert set(frame["transaction_id"]) == expected
    # --end is inclusive: rows from late on 3 September are kept.
    assert frame["timestamp"].str[:10].isin(["2022-09-02", "2022-09-03"]).all()
    assert info["rows_read"] == 20
    assert info["rows_in_range"] == len(expected)


def test_date_range_rows_come_out_in_time_order(tmp_path):
    path, _ = _multi_day_file(tmp_path)
    frame, _ = eval_external.read_ibm(path, max_rows=100, currency="all", start="2022-09-01")
    assert frame["timestamp"].is_monotonic_increasing
    assert len(frame) == 20


def test_date_range_cap_keeps_the_earliest_rows_by_time(tmp_path):
    path, days = _multi_day_file(tmp_path)
    frame, info = eval_external.read_ibm(path, max_rows=5, currency="all", start="2022-09-01",
                                         end="2022-09-04")
    assert len(frame) == 5 and info["rows_in_range"] == 20
    assert frame["timestamp"].str[:10].eq("2022-09-01").all()  # 5 rows fall on 1 September


def test_date_range_and_currency_filter_combine(tmp_path):
    path, days = _multi_day_file(tmp_path)
    frame, info = eval_external.read_ibm(path, max_rows=100, currency="US Dollar",
                                         start="2022-09-04", end="2022-09-04")
    assert set(frame["currency"]) == {"US Dollar"}
    assert frame["timestamp"].str[:10].eq("2022-09-04").all()
    assert info["currency_counts"] == {"US Dollar": 4, "Yuan": 1}  # counted within the range


def test_empty_date_range_names_the_dates_in_the_file(tmp_path):
    path, _ = _multi_day_file(tmp_path)
    with pytest.raises(ValueError, match="2022-09-01.*2022-09-04"):
        eval_external.read_ibm(path, max_rows=100, currency="all", start="2022-10-01")


def test_reversed_date_range_is_refused(tmp_path):
    path, _ = _multi_day_file(tmp_path)
    with pytest.raises(ValueError, match="after"):
        eval_external.read_ibm(path, max_rows=100, currency="all", start="2022-09-03",
                               end="2022-09-02")


def test_main_passes_the_date_range_and_prints_it(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(config, "MIN_ROWS_FOR_ML", 3)
    path, _ = _multi_day_file(tmp_path)
    code = eval_external.main(["--path", str(path), "--currency", "all", "--k", "2",
                               "--start", "2022-09-02", "--end", "2022-09-03"])
    out = capsys.readouterr().out
    assert code == 0
    assert "dates 2022-09-02 to 2022-09-03" in out


def test_unknown_currency_names_the_currencies_found():
    with pytest.raises(ValueError, match="Euro, US Dollar, Yuan"):
        eval_external.read_ibm(FIXTURE, max_rows=100, currency="Dollar")


def test_a_file_in_another_layout_is_refused_with_its_header(tmp_path):
    paysim = tmp_path / "paysim.csv"
    paysim.write_text("step,type,amount,nameOrig,oldbalanceOrg,newbalanceOrig,nameDest,"
                      "oldbalanceDest,newbalanceDest,isFraud,isFlaggedFraud\n"
                      "1,PAYMENT,9839.64,C1231006815,170136.0,160296.36,M1979787155,0.0,0.0,0,0\n")
    with pytest.raises(ValueError, match="step,type,amount"):
        eval_external.read_ibm(paysim, max_rows=10, currency="all")


def test_header_case_and_spacing_do_not_matter(tmp_path):
    text = FIXTURE.read_text(encoding="utf-8").splitlines()
    text[0] = " " + text[0].upper().replace(",", " , ")
    path = tmp_path / "upper.csv"
    path.write_text("\n".join(text) + "\n", encoding="utf-8")
    frame, _ = eval_external.read_ibm(path, max_rows=100, currency="all")
    assert len(frame) == 20


def test_average_precision_and_precision_at_k_on_a_hand_worked_case():
    truth = pd.Series([1, 0, 1, 0])
    score = pd.Series([0.9, 0.8, 0.7, 0.6])
    # Positives at ranks 1 and 3: AP = (1/1 + 2/3) / 2.
    assert eval_external.average_precision(truth, score) == pytest.approx(5 / 6)
    assert eval_external.precision_at_k(truth, score, 1) == 1.0
    assert eval_external.precision_at_k(truth, score, 2) == 0.5
    assert eval_external.precision_at_k(truth, score, 3) == pytest.approx(2 / 3)
    assert eval_external.precision_at_k(truth, score, 10) is None


def test_precision_at_k_breaks_ties_by_file_order():
    truth = pd.Series([0, 1, 1])
    score = pd.Series([0.5, 0.5, 0.1])
    assert eval_external.precision_at_k(truth, score, 1) == 0.0


def test_ranking_puts_rule_flagged_rows_first():
    frame = pd.DataFrame({"ml_score": [-0.4, 0.3, 0.2], "rule_flag": [False, True, False]})
    score = eval_external.ranking_score(frame)
    assert score.idxmax() == 1
    assert score[0] > score[2]  # lower decision_function = more anomalous


def test_rows_without_an_anomaly_score_rank_last_unless_a_rule_flags_them():
    frame = pd.DataFrame({"ml_score": [0.1, float("nan"), -0.2, float("nan")],
                          "rule_flag": [False, False, False, True]})
    score = eval_external.ranking_score(frame)
    assert list(score.sort_values(ascending=False).index) == [3, 2, 0, 1]


def test_end_to_end_on_the_fixture_is_deterministic(monkeypatch):
    monkeypatch.setattr(config, "MIN_ROWS_FOR_ML", 5)  # 6 rows are not self-transfers
    first = eval_external.evaluate_external(FIXTURE, max_rows=100, currency="all", ks=[5])
    second = eval_external.evaluate_external(FIXTURE, max_rows=100, currency="all", ks=[5])
    assert first == second
    assert first["rows"] == 20 and first["laundering"] == 4
    assert first["tp"] + first["fn"] == 4
    assert first["tp"] + first["fp"] == first["forwarded"]
    assert first["ml_applied"]
    assert 0.0 <= first["pr_auc"] <= 1.0 and 0.0 <= first["pr_auc_tier0"] <= 1.0
    assert first["base_rate"] == pytest.approx(0.2)
    # The 9,400 to 9,500 USD legs sit in the structuring band, so the rules flag them.
    assert first["rule_flagged"] >= 3


def test_every_rule_reason_maps_to_exactly_one_rule_id():
    """The breakdown matches reason text, so a reworded reason must fail here
    rather than be counted under the wrong rule (the structuring-band reason
    also says "reporting threshold")."""
    from decimal import Decimal

    from utils.rules import evaluate_row

    cases = {
        "reporting_threshold": evaluate_row(Decimal("12000"), "Germany", "A", "B"),
        "structuring_band": evaluate_row(Decimal("9400"), "Germany", "A", "B"),
        "high_risk_country": evaluate_row(Decimal("50"), "RU", "A", "B"),
        "secrecy_jurisdiction": evaluate_row(Decimal("50"), "KY", "A", "B"),
        "sanctions_name": evaluate_row(Decimal("50"), "Germany", "A", "B",
                                       screener=lambda name: "Listed" if name == "A" else None),
        "unidentified_parties": evaluate_row(Decimal("50"), "Germany", "Unknown_Entity", "internal_wallet"),
        "unresolved_jurisdiction": evaluate_row(Decimal("50"), "", "A", "B"),
        "self_transfer": evaluate_row(Decimal("50"), "Germany", "A-1", "A-1"),
    }
    for rule_id, outcome in cases.items():
        assert outcome.reasons, rule_id
        assert [eval_external.rule_id(r) for r in outcome.reasons] == [rule_id], outcome.reasons
    assert eval_external.rule_id("Something new.") == "other"
    assert set(eval_external.RULE_REASONS) == set(cases)


def test_breakdown_counts_each_rule_and_the_behaviour_model(monkeypatch):
    monkeypatch.setattr(config, "MIN_ROWS_FOR_ML", 5)
    result = eval_external.evaluate_external(FIXTURE, max_rows=100, currency="all", ks=[5])
    rows = {b["source"]: b for b in result["by_source"]}
    forwarded = result["forwarded"]

    # 14,675.57 and 36,682.97 are over 10,000 but are self-transfers, so the
    # threshold does not apply; the three 9,400-9,500 legs are in the band.
    assert (rows["reporting_threshold"]["rows"], rows["reporting_threshold"]["laundering"]) == (0, 0)
    assert (rows["structuring_band"]["rows"], rows["structuring_band"]["laundering"]) == (3, 3)
    assert rows["structuring_band"]["precision"] == 1.0
    assert rows["structuring_band"]["share_of_forwarded"] == pytest.approx(3 / forwarded)
    for rule in ("high_risk_country", "secrecy_jurisdiction", "sanctions_name", "unidentified_parties"):
        assert rows[rule]["rows"] == 0 and rows[rule]["precision"] is None
    # Blank countries give every row the note, but a note does not forward a row.
    assert (rows["unresolved_jurisdiction"]["rows"], rows["unresolved_jurisdiction"]["laundering"]) == (20, 4)
    assert rows["unresolved_jurisdiction"]["note"]
    # 14 rows pay the sender's own account; one of them (line 21) is labelled laundering.
    assert (rows["self_transfer"]["rows"], rows["self_transfer"]["laundering"]) == (14, 1)
    assert rows["self_transfer"]["note"] and result["self_transfers"] == 14

    assert rows["any rule"]["rows"] == result["rule_flagged"] == 3
    assert rows["behaviour model"]["rows"] == result["ml_flagged"]
    assert (rows["behaviour model only"]["rows"]
            == forwarded - result["rule_flagged"])
    model = rows["behaviour model"]
    assert model["share_of_forwarded"] == pytest.approx(model["rows"] / forwarded)


def test_main_prints_the_breakdown(monkeypatch, capsys):
    monkeypatch.setattr(config, "MIN_ROWS_FOR_ML", 5)
    eval_external.main(["--path", str(FIXTURE), "--max-rows", "100", "--currency", "all", "--k", "5"])
    out = capsys.readouterr().out
    assert "by rule and behaviour model" in out
    lines = {line.split()[0]: line for line in out.splitlines() if line.startswith("  ")}
    assert lines["structuring_band"].split()[1:3] == ["3", "3"]
    assert "(note, does not forward)" in lines["unresolved_jurisdiction"]
    assert "self-transfers 14 not scored (1 labelled laundering)" in out


def test_labels_never_reach_the_pipeline(monkeypatch):
    seen = {}
    real = eval_external.apply_rules

    def spy(df, *args, **kwargs):
        seen["columns"] = set(df.columns)
        return real(df, *args, **kwargs)

    monkeypatch.setattr(eval_external, "apply_rules", spy)
    eval_external.evaluate_external(FIXTURE, max_rows=100, currency="all", ks=[5])
    assert not {"is_laundering", "Is Laundering", "currency"} & seen["columns"]


def test_batch_below_the_ml_minimum_reports_no_pr_auc():
    result = eval_external.evaluate_external(FIXTURE, max_rows=100, currency="all", ks=[5])
    assert not result["ml_applied"]
    assert result["pr_auc"] is None and result["pr_auc_tier0"] is None
    assert result["forwarded"] == 6  # fail-open for every row except the 14 self-transfers


def test_main_prints_the_header_and_the_ranking_metrics(monkeypatch, capsys):
    monkeypatch.setattr(config, "MIN_ROWS_FOR_ML", 5)
    code = eval_external.main(["--path", str(FIXTURE), "--max-rows", "100", "--currency", "all",
                               "--k", "5", "10"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Timestamp,From Bank,Account,To Bank,Account" in out
    assert "PR-AUC" in out and "precision@5" in out and "precision@10" in out
    assert "fewer than 30 laundering rows" in out


def test_main_rejects_a_missing_file(tmp_path, capsys):
    code = eval_external.main(["--path", str(tmp_path / "nope.csv")])
    assert code == 1
    assert "No file at" in capsys.readouterr().out
