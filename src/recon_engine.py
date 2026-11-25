"""Two-tier bank-to-ledger reconciliation engine and QBO/Xero AJE generator."""
from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

CENT = Decimal("0.01")


def _q(value: Decimal) -> Decimal:
    return value.quantize(CENT)


def _parse_amount(raw: str) -> Decimal:
    text = (raw or "").strip().replace(",", "")
    if not text:
        return Decimal("0.00")
    return _q(Decimal(text))


def _is_digit_transposition(a: Decimal, b: Decimal) -> bool:
    """Return True when two non-equal amounts differ by a multiple of 9 and share digits."""
    diff = abs(_q(a) - _q(b))
    if diff == Decimal("0.00"):
        return False
    cents_diff = int((diff * 100).to_integral_value())
    if cents_diff % 9 != 0:
        return False
    digits_a = sorted(ch for ch in f"{abs(_q(a)):.2f}" if ch.isdigit())
    digits_b = sorted(ch for ch in f"{abs(_q(b)):.2f}" if ch.isdigit())
    return digits_a == digits_b


class BankLine(BaseModel):
    """Normalized bank statement transaction line."""

    date: date
    reference: str
    description: str
    debit: Decimal = Field(ge=0)
    credit: Decimal = Field(ge=0)
    balance: Decimal

    @property
    def net_amount(self) -> Decimal:
        return _q(self.credit - self.debit)


class LedgerLine(BaseModel):
    """Normalized General Ledger cash book voucher line."""

    date: date
    voucher_no: str
    reference: str
    description: str
    cash_in: Decimal = Field(ge=0)
    cash_out: Decimal = Field(ge=0)
    balance: Decimal

    @property
    def net_amount(self) -> Decimal:
        return _q(self.cash_in - self.cash_out)


class ReconException(BaseModel):
    """Classified discrepancy item across timing or book-adjustment categories."""

    exception_id: str
    category: str
    source: str
    date: date
    reference: str
    voucher_no: Optional[str] = None
    description: str
    amount: Decimal
    impact_side: str
    signed_impact: Decimal


class AdjustingJournalLine(BaseModel):
    """Balanced double-entry line ready for QBO or Xero journal import."""

    entry_no: str
    date: date
    account_code: str
    account_name: str
    debit: Decimal
    credit: Decimal
    reference: str
    memo: str


class ReconciliationResult(BaseModel):
    """Complete two-tier reconciliation report with mathematical tie-out proof."""

    bank_rows: int
    ledger_rows: int
    matched_groups: int
    unadjusted_bank_balance: Decimal
    deposits_in_transit_total: Decimal
    outstanding_cheques_total: Decimal
    adjusted_bank_balance: Decimal
    unadjusted_book_balance: Decimal
    unknown_receipts_total: Decimal
    bank_charges_total: Decimal
    duplicate_reversals_total: Decimal
    transposition_adjustment_total: Decimal
    adjusted_book_balance: Decimal
    net_variance: Decimal
    passed: bool
    exceptions: List[ReconException]
    adjusting_journals: List[AdjustingJournalLine]
    aje_total_debits: Decimal
    aje_total_credits: Decimal
    aje_net_cash_impact: Decimal


def _verify_running_chain(amounts: List[Decimal], balances: List[Decimal], label: str) -> None:
    if not amounts:
        raise ValueError(f"{label} contains zero rows")
    implied_open = _q(balances[0] - amounts[0])
    running = implied_open
    for idx, (amt, bal) in enumerate(zip(amounts, balances), start=1):
        running = _q(running + amt)
        if running != _q(bal):
            raise ValueError(f"{label} running balance break at row {idx}: expected {running}, got {bal}")


def load_bank_statement(path: Path) -> List[BankLine]:
    """Load bank statement CSV and verify continuous running balance integrity."""
    lines: List[BankLine] = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        for raw in csv.DictReader(fh):
            lines.append(
                BankLine(
                    date=date.fromisoformat(raw["Date"].strip()),
                    reference=raw["Reference"].strip(),
                    description=raw["Description"].strip(),
                    debit=_parse_amount(raw["Debit"]),
                    credit=_parse_amount(raw["Credit"]),
                    balance=_parse_amount(raw["Balance"]),
                )
            )
    _verify_running_chain([ln.net_amount for ln in lines], [ln.balance for ln in lines], "Bank statement")
    return lines


def load_cash_ledger(path: Path) -> List[LedgerLine]:
    """Load General Ledger cash book CSV and verify continuous running balance integrity."""
    lines: List[LedgerLine] = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        for raw in csv.DictReader(fh):
            lines.append(
                LedgerLine(
                    date=date.fromisoformat(raw["Date"].strip()),
                    voucher_no=raw["Voucher_No"].strip(),
                    reference=raw["Reference"].strip(),
                    description=raw["Description"].strip(),
                    cash_in=_parse_amount(raw["Cash_In"]),
                    cash_out=_parse_amount(raw["Cash_Out"]),
                    balance=_parse_amount(raw["Balance"]),
                )
            )
    _verify_running_chain([ln.net_amount for ln in lines], [ln.balance for ln in lines], "Cash ledger")
    return lines


def _resolve_base_reference(ref: str, bank_refs: set[str]) -> str:
    """Collapse split voucher suffixes (e.g. CHK-10001-1 -> CHK-10001) when parent exists."""
    if ref not in bank_refs and "-" in ref:
        candidate = ref.rsplit("-", 1)[0]
        if candidate in bank_refs:
            return candidate
    return ref


def _classify_bank_charge(line: BankLine) -> Tuple[str, str]:
    desc = line.description.lower()
    if "merchant" in desc or "discount" in desc:
        return "6045", "Merchant Card Processing Fees"
    if "wire" in desc:
        return "6050", "Wire Transfer Expense"
    return "6040", "Bank Service Charges Expense"


def _classify_expense_account(desc: str) -> Tuple[str, str]:
    low = desc.lower()
    if "electricity" in low or "power" in low:
        return "6010", "Electricity Utility Expense"
    if "stationery" in low or "office supplies" in low:
        return "6020", "Office Supplies Expense"
    return "6090", "General Operating Expense"


def reconcile_accounts(
    bank_lines: List[BankLine],
    ledger_lines: List[LedgerLine],
    max_clearance_days: int = 5,
) -> ReconciliationResult:
    """Execute deterministic matching, classify exceptions, and build balanced AJEs."""
    bank_by_ref: Dict[str, List[BankLine]] = defaultdict(list)
    for b in bank_lines:
        bank_by_ref[b.reference].append(b)

    bank_refs = set(bank_by_ref.keys())
    ledger_by_ref: Dict[str, List[LedgerLine]] = defaultdict(list)
    for l in ledger_lines:
        base_ref = _resolve_base_reference(l.reference, bank_refs)
        ledger_by_ref[base_ref].append(l)

    matched_groups = 0
    exceptions: List[ReconException] = []
    aj_lines: List[AdjustingJournalLine] = []
    exc_seq = 1
    aje_seq = 1

    def next_exc_id() -> str:
        nonlocal exc_seq
        val = f"EXC-{exc_seq:02d}"
        exc_seq += 1
        return val

    def add_aje(entry_date: date, dr_acct: Tuple[str, str], cr_acct: Tuple[str, str], amt: Decimal, ref: str, memo: str) -> None:
        nonlocal aje_seq
        code = f"AJE-{aje_seq:02d}"
        aje_seq += 1
        amt_q = _q(amt)
        aj_lines.append(
            AdjustingJournalLine(
                entry_no=code,
                date=entry_date,
                account_code=dr_acct[0],
                account_name=dr_acct[1],
                debit=amt_q,
                credit=Decimal("0.00"),
                reference=ref,
                memo=memo,
            )
        )
        aj_lines.append(
            AdjustingJournalLine(
                entry_no=code,
                date=entry_date,
                account_code=cr_acct[0],
                account_name=cr_acct[1],
                debit=Decimal("0.00"),
                credit=amt_q,
                reference=ref,
                memo=memo,
            )
        )

    all_refs = sorted(set(bank_by_ref.keys()) | set(ledger_by_ref.keys()))
    for ref in all_refs:
        bs = bank_by_ref.get(ref, [])
        ls = ledger_by_ref.get(ref, [])

        if bs and ls:
            b_net = _q(sum((x.net_amount for x in bs), Decimal("0.00")))
            l_net = _q(sum((x.net_amount for x in ls), Decimal("0.00")))

            # Exact 1-to-1 or split-voucher many-to-1 match within clearance window
            if len(bs) == 1 and b_net == l_net:
                day_gap = max(abs((bs[0].date - x.date).days) for x in ls)
                if day_gap <= max_clearance_days:
                    matched_groups += 1
                    continue

            # Duplicate ledger voucher against a single bank item
            if len(bs) == 1 and len(ls) > 1 and all(x.net_amount == bs[0].net_amount for x in ls):
                matched_groups += 1
                for dup in ls[1:]:
                    amt = abs(dup.net_amount)
                    signed = -dup.net_amount  # reversing cash_out adds positive cash; reversing cash_in subtracts
                    exceptions.append(
                        ReconException(
                            exception_id=next_exc_id(),
                            category="Duplicate Entry",
                            source="Cash Ledger",
                            date=dup.date,
                            reference=dup.reference,
                            voucher_no=dup.voucher_no,
                            description=dup.description,
                            amount=amt,
                            impact_side="Book (+)" if signed > 0 else "Book (-)",
                            signed_impact=signed,
                        )
                    )
                    exp_acct = _classify_expense_account(dup.description)
                    if signed > 0:
                        add_aje(dup.date, ("1010", "Operating Checking Account"), exp_acct, amt, dup.voucher_no, f"Reverse duplicate {dup.voucher_no}")
                    else:
                        add_aje(dup.date, exp_acct, ("1010", "Operating Checking Account"), amt, dup.voucher_no, f"Reverse duplicate {dup.voucher_no}")
                continue

            # Digit transposition / book amount mismatch on matching reference
            if len(bs) == 1 and len(ls) == 1 and b_net != l_net:
                diff = _q(b_net - l_net)
                category = "Transposition Error" if _is_digit_transposition(abs(b_net), abs(l_net)) else "Amount Mismatch"
                exceptions.append(
                    ReconException(
                        exception_id=next_exc_id(),
                        category=category,
                        source="Both Sides",
                        date=ls[0].date,
                        reference=ref,
                        voucher_no=ls[0].voucher_no,
                        description=f"{ls[0].description} (Cleared {abs(b_net):.2f} vs Book {abs(l_net):.2f})",
                        amount=abs(diff),
                        impact_side="Book (+)" if diff > 0 else "Book (-)",
                        signed_impact=diff,
                    )
                )
                exp_acct = _classify_expense_account(ls[0].description)
                if diff < 0:
                    add_aje(ls[0].date, exp_acct, ("1010", "Operating Checking Account"), abs(diff), ls[0].voucher_no, f"Correct {category.lower()} on {ref}")
                else:
                    add_aje(ls[0].date, ("1010", "Operating Checking Account"), exp_acct, abs(diff), ls[0].voucher_no, f"Correct {category.lower()} on {ref}")
                continue

            raise ValueError(
                f"Unresolved multi-row or clearance-window mismatch on reference {ref}: "
                f"bank_rows={len(bs)} (net={b_net}), ledger_rows={len(ls)} (net={l_net})"
            )

        # Bank-only items -> Bank Service Charges or Unknown Receipts
        for b in bs:
            if not ls:
                if b.debit > 0:
                    exceptions.append(
                        ReconException(
                            exception_id=next_exc_id(),
                            category="Bank Service Charge",
                            source="Bank Statement",
                            date=b.date,
                            reference=b.reference,
                            description=b.description,
                            amount=b.debit,
                            impact_side="Book (-)",
                            signed_impact=-b.debit,
                        )
                    )
                    fee_acct = _classify_bank_charge(b)
                    add_aje(b.date, fee_acct, ("1010", "Operating Checking Account"), b.debit, b.reference, b.description)
                elif b.credit > 0:
                    exceptions.append(
                        ReconException(
                            exception_id=next_exc_id(),
                            category="Unknown Receipt",
                            source="Bank Statement",
                            date=b.date,
                            reference=b.reference,
                            description=b.description,
                            amount=b.credit,
                            impact_side="Book (+)",
                            signed_impact=b.credit,
                        )
                    )
                    add_aje(b.date, ("1010", "Operating Checking Account"), ("2050", "Unallocated Customer Receipts"), b.credit, b.reference, b.description)

        # Ledger-only items -> Deposits in Transit or Outstanding Cheques
        for l in ls:
            if not bs:
                if l.cash_in > 0:
                    exceptions.append(
                        ReconException(
                            exception_id=next_exc_id(),
                            category="Deposit in Transit",
                            source="Cash Ledger",
                            date=l.date,
                            reference=l.reference,
                            voucher_no=l.voucher_no,
                            description=l.description,
                            amount=l.cash_in,
                            impact_side="Bank (+)",
                            signed_impact=l.cash_in,
                        )
                    )
                elif l.cash_out > 0:
                    exceptions.append(
                        ReconException(
                            exception_id=next_exc_id(),
                            category="Outstanding Cheque",
                            source="Cash Ledger",
                            date=l.date,
                            reference=l.reference,
                            voucher_no=l.voucher_no,
                            description=l.description,
                            amount=l.cash_out,
                            impact_side="Bank (-)",
                            signed_impact=-l.cash_out,
                        )
                    )

    unadj_bank = _q(bank_lines[-1].balance)
    unadj_book = _q(ledger_lines[-1].balance)

    dit_total = _q(sum((e.amount for e in exceptions if e.category == "Deposit in Transit"), Decimal("0.00")))
    osc_total = _q(sum((e.amount for e in exceptions if e.category == "Outstanding Cheque"), Decimal("0.00")))
    adj_bank = _q(unadj_bank + dit_total - osc_total)

    unk_total = _q(sum((e.amount for e in exceptions if e.category == "Unknown Receipt"), Decimal("0.00")))
    chg_total = _q(sum((e.amount for e in exceptions if e.category == "Bank Service Charge"), Decimal("0.00")))
    dup_total = _q(sum((e.signed_impact for e in exceptions if e.category == "Duplicate Entry"), Decimal("0.00")))
    trans_total = _q(sum((e.signed_impact for e in exceptions if e.category in {"Transposition Error", "Amount Mismatch"}), Decimal("0.00")))
    adj_book = _q(unadj_book + unk_total - chg_total + dup_total + trans_total)

    net_variance = _q(adj_bank - adj_book)
    aje_debits = _q(sum((j.debit for j in aj_lines), Decimal("0.00")))
    aje_credits = _q(sum((j.credit for j in aj_lines), Decimal("0.00")))
    aje_cash_impact = _q(sum((j.debit - j.credit for j in aj_lines if j.account_code == "1010"), Decimal("0.00")))

    return ReconciliationResult(
        bank_rows=len(bank_lines),
        ledger_rows=len(ledger_lines),
        matched_groups=matched_groups,
        unadjusted_bank_balance=unadj_bank,
        deposits_in_transit_total=dit_total,
        outstanding_cheques_total=osc_total,
        adjusted_bank_balance=adj_bank,
        unadjusted_book_balance=unadj_book,
        unknown_receipts_total=unk_total,
        bank_charges_total=chg_total,
        duplicate_reversals_total=dup_total,
        transposition_adjustment_total=trans_total,
        adjusted_book_balance=adj_book,
        net_variance=net_variance,
        passed=(net_variance == Decimal("0.00") and aje_debits == aje_credits),
        exceptions=exceptions,
        adjusting_journals=aj_lines,
        aje_total_debits=aje_debits,
        aje_total_credits=aje_credits,
        aje_net_cash_impact=aje_cash_impact,
    )


def export_aje_csv(aj_lines: List[AdjustingJournalLine], out_path: Path) -> None:
    """Export QBO/Xero-compatible balanced adjusting journal entries to CSV."""
    cols = ["entry_no", "date", "account_code", "account_name", "debit", "credit", "reference", "memo"]
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols)
        writer.writeheader()
        for line in aj_lines:
            writer.writerow(
                {
                    "entry_no": line.entry_no,
                    "date": line.date.isoformat(),
                    "account_code": line.account_code,
                    "account_name": line.account_name,
                    "debit": f"{line.debit:.2f}",
                    "credit": f"{line.credit:.2f}",
                    "reference": line.reference,
                    "memo": line.memo,
                }
            )


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entrypoint printing reconciliation JSON and optionally exporting AJEs."""
    ap = argparse.ArgumentParser(description="Two-Tier Bank-to-Book Reconciliation Engine")
    ap.add_argument("--bank", type=Path, required=True, help="Path to bank statement CSV")
    ap.add_argument("--ledger", type=Path, required=True, help="Path to cash ledger CSV")
    ap.add_argument("--aje-csv", type=Path, default=None, help="Optional CSV output path for AJEs")
    ap.add_argument("--json-out", type=Path, default=None, help="Optional JSON output path")
    args = ap.parse_args(argv)

    result = reconcile_accounts(load_bank_statement(args.bank), load_cash_ledger(args.ledger))
    if args.aje_csv is not None:
        export_aje_csv(result.adjusting_journals, args.aje_csv)
    payload = result.model_dump_json(indent=2)
    if args.json_out is not None:
        args.json_out.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
