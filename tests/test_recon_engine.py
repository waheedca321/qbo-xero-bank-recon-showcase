from decimal import Decimal
from pathlib import Path
import json

import pytest

from src.recon_engine import (
    load_bank_statement,
    load_cash_ledger,
    main,
    reconcile_accounts,
)

FIX = Path(__file__).resolve().parent.parent / "fixtures"


def test_full_october_reconciliation_ties_to_zero_variance():
    bank = load_bank_statement(FIX / "bank_statement_oct2026.csv")
    ledger = load_cash_ledger(FIX / "cash_ledger_oct2026.csv")
    res = reconcile_accounts(bank, ledger)

    assert res.bank_rows == 300
    assert res.ledger_rows == 302
    assert res.matched_groups == 294
    assert res.unadjusted_bank_balance == Decimal("43506.16")
    assert res.deposits_in_transit_total == Decimal("2790.50")
    assert res.outstanding_cheques_total == Decimal("2455.25")
    assert res.adjusted_bank_balance == Decimal("43841.41")

    assert res.unadjusted_book_balance == Decimal("42137.31")
    assert res.unknown_receipts_total == Decimal("1930.00")
    assert res.bank_charges_total == Decimal("392.40")
    assert res.duplicate_reversals_total == Decimal("184.50")
    assert res.transposition_adjustment_total == Decimal("-18.00")
    assert res.adjusted_book_balance == Decimal("43841.41")

    assert res.net_variance == Decimal("0.00")
    assert res.passed is True
    assert len(res.exceptions) == 11


def test_exception_categories_and_balanced_adjusting_journals():
    res = reconcile_accounts(
        load_bank_statement(FIX / "bank_statement_oct2026.csv"),
        load_cash_ledger(FIX / "cash_ledger_oct2026.csv"),
    )
    counts = {}
    for exc in res.exceptions:
        counts[exc.category] = counts.get(exc.category, 0) + 1

    assert counts == {
        "Duplicate Entry": 1,
        "Transposition Error": 1,
        "Outstanding Cheque": 2,
        "Unknown Receipt": 2,
        "Deposit in Transit": 2,
        "Bank Service Charge": 3,
    }
    assert len(res.adjusting_journals) == 14  # 7 balanced two-line entries
    assert res.aje_total_debits == Decimal("2524.90")
    assert res.aje_total_credits == Decimal("2524.90")
    assert res.aje_net_cash_impact == Decimal("1704.10")


def test_broken_bank_running_balance_rejected(tmp_path: Path):
    bad_bank = tmp_path / "bad_bank.csv"
    bad_bank.write_text(
        "Date,Reference,Description,Debit,Credit,Balance\n"
        "2026-10-01,DEP-1,OPENING,,100.00,1100.00\n"
        "2026-10-02,CHK-1,PAYMENT,25.00,,1050.00\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="running balance break"):
        load_bank_statement(bad_bank)


def test_split_ledger_vouchers_match_single_bank_clearing(tmp_path: Path):
    bank_csv = tmp_path / "bank.csv"
    bank_csv.write_text(
        "Date,Reference,Description,Debit,Credit,Balance\n"
        "2026-10-03,CHK-10001,CLEARED CHEQUE #10001,947.64,,9052.36\n",
        encoding="utf-8",
    )
    ledger_csv = tmp_path / "ledger.csv"
    ledger_csv.write_text(
        "Date,Voucher_No,Reference,Description,Cash_In,Cash_Out,Balance\n"
        "2026-10-01,PV-1A,CHK-10001-1,Vendor Split 1,,568.58,9431.42\n"
        "2026-10-01,PV-1B,CHK-10001-2,Vendor Split 2,,379.06,9052.36\n",
        encoding="utf-8",
    )
    res = reconcile_accounts(load_bank_statement(bank_csv), load_cash_ledger(ledger_csv))
    assert res.passed is True
    assert res.matched_groups == 1
    assert res.exceptions == []


def test_cli_exports_aje_csv_and_json(tmp_path: Path):
    aje_csv = tmp_path / "ajes.csv"
    json_out = tmp_path / "recon.json"
    code = main(
        [
            "--bank",
            str(FIX / "bank_statement_oct2026.csv"),
            "--ledger",
            str(FIX / "cash_ledger_oct2026.csv"),
            "--aje-csv",
            str(aje_csv),
            "--json-out",
            str(json_out),
        ]
    )
    assert code == 0
    assert aje_csv.exists()
    data = json.loads(json_out.read_text(encoding="utf-8"))
    assert data["net_variance"] == "0.00"
    assert data["passed"] is True
