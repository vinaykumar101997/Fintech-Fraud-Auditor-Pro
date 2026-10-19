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


def test_end_to_end_on_the_fixture_is_deterministic(monkeypatch):
    monkeypatch.setattr(config, "MIN_ROWS_FOR_ML", 10)
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
    assert result["forwarded"] == 20  # fail-open: every row goes on


def test_main_prints_the_header_and_the_ranking_metrics(monkeypatch, capsys):
    monkeypatch.setattr(config, "MIN_ROWS_FOR_ML", 10)
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
