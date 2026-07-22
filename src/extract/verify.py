"""Arithmetic verification of extracted statements.

A balance sheet that does not balance is a FINDING, not a bug to paper over.
Nothing in this module ever writes a corrected value back -- it only reports.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from .schemas import BalanceSheet, ExtractedFinancials, ProfitAndLoss, val

# Statements are usually printed in lakhs/thousands with rounding. Tolerate
# rounding noise, not a real break: 0.5% of the larger side, floor 1000 INR.
REL_TOLERANCE = 0.005
ABS_TOLERANCE = 1_000.0


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class VerificationFinding(BaseModel):
    check: str
    statement: str  # e.g. "balance_sheet FY23"
    expected: float
    actual: float
    difference: float
    severity: Severity
    detail: str

    @property
    def passed(self) -> bool:
        return False  # findings only exist for failures


def _within_tolerance(a: float, b: float) -> bool:
    return abs(a - b) <= max(ABS_TOLERANCE, REL_TOLERANCE * max(abs(a), abs(b)))


def _check(
    findings: list[VerificationFinding],
    *,
    name: str,
    statement: str,
    expected: float | None,
    actual: float | None,
    severity: Severity,
    detail: str,
) -> None:
    """Record a finding only when both sides are known and they disagree.

    Unknown inputs are not a failure -- they are a completeness issue reported
    elsewhere. Inventing a side to force a check is exactly what we must not do.
    """
    if expected is None or actual is None:
        return
    if _within_tolerance(expected, actual):
        return
    findings.append(
        VerificationFinding(
            check=name,
            statement=statement,
            expected=expected,
            actual=actual,
            difference=actual - expected,
            severity=severity,
            detail=detail,
        )
    )


def _sum_or_none(*parts: float | None) -> float | None:
    """Sum only if every part is known. A partial sum is a fabricated total."""
    return None if any(p is None for p in parts) else sum(p for p in parts)  # type: ignore[misc]


def verify_balance_sheet(bs: BalanceSheet) -> list[VerificationFinding]:
    f: list[VerificationFinding] = []
    label = f"balance sheet {bs.period.label}"

    equity = val(bs.net_worth)
    if equity is None:
        equity = _sum_or_none(val(bs.share_capital), val(bs.reserves_and_surplus))
    elif (composed := _sum_or_none(val(bs.share_capital), val(bs.reserves_and_surplus))) is not None:
        _check(
            f,
            name="net worth = share capital + reserves & surplus",
            statement=label,
            expected=composed,
            actual=equity,
            severity=Severity.WARNING,
            detail="Reported net worth differs from its components.",
        )

    liabilities = _sum_or_none(
        val(bs.long_term_borrowings),
        val(bs.other_non_current_liabilities),
        val(bs.total_current_liabilities),
    )

    # The headline identity.
    if equity is not None and liabilities is not None:
        _check(
            f,
            name="assets = liabilities + equity",
            statement=label,
            expected=equity + liabilities,
            actual=val(bs.total_assets),
            severity=Severity.CRITICAL,
            detail=(
                "The balance sheet does not balance. Either the extraction missed a line "
                "item or the submitted statement itself is inconsistent. HUMAN REVIEW REQUIRED "
                "-- no figure has been adjusted."
            ),
        )

    _check(
        f,
        name="total current assets = inventory + receivables + cash + other CA",
        statement=label,
        expected=_sum_or_none(
            val(bs.inventory), val(bs.trade_receivables), val(bs.cash_and_bank), val(bs.other_current_assets)
        ),
        actual=val(bs.total_current_assets),
        severity=Severity.WARNING,
        detail="Current-asset subtotal does not equal its line items.",
    )

    _check(
        f,
        name="total current liabilities = short-term borrowings + payables + other CL",
        statement=label,
        expected=_sum_or_none(
            val(bs.short_term_borrowings), val(bs.trade_payables), val(bs.other_current_liabilities)
        ),
        actual=val(bs.total_current_liabilities),
        severity=Severity.WARNING,
        detail="Current-liability subtotal does not equal its line items.",
    )

    _check(
        f,
        name="total assets = non-current + current assets",
        statement=label,
        expected=_sum_or_none(
            val(bs.net_fixed_assets), val(bs.other_non_current_assets), val(bs.total_current_assets)
        ),
        actual=val(bs.total_assets),
        severity=Severity.WARNING,
        detail="Total assets does not equal non-current plus current assets.",
    )
    return f


def verify_pnl(pl: ProfitAndLoss) -> list[VerificationFinding]:
    f: list[VerificationFinding] = []
    label = f"profit & loss {pl.period.label}"

    _check(
        f,
        name="gross profit = revenue - cost of goods sold",
        statement=label,
        expected=(None if val(pl.revenue) is None or val(pl.cost_of_goods_sold) is None
                  else val(pl.revenue) - val(pl.cost_of_goods_sold)),
        actual=val(pl.gross_profit),
        severity=Severity.WARNING,
        detail="Gross profit does not reconcile to revenue less COGS.",
    )

    if val(pl.gross_profit) is not None:
        _check(
            f,
            name="EBITDA = gross profit - employee cost - other expenses",
            statement=label,
            expected=_sum_or_none(
                val(pl.gross_profit),
                None if val(pl.employee_cost) is None else -val(pl.employee_cost),
                None if val(pl.other_expenses) is None else -val(pl.other_expenses),
            ),
            actual=val(pl.ebitda),
            severity=Severity.WARNING,
            detail="EBITDA does not reconcile to gross profit less operating costs.",
        )

    # Other income absent from a statement genuinely means nil, unlike a missing
    # subtotal -- statements print it only when non-zero.
    other_income = val(pl.other_income) or 0.0
    _check(
        f,
        name="PBT = EBITDA + other income - depreciation - finance cost",
        statement=label,
        expected=_sum_or_none(
            val(pl.ebitda),
            other_income,
            None if val(pl.depreciation) is None else -val(pl.depreciation),
            None if val(pl.finance_cost) is None else -val(pl.finance_cost),
        ),
        actual=val(pl.profit_before_tax),
        severity=Severity.WARNING,
        detail="Profit before tax does not reconcile from EBITDA.",
    )

    _check(
        f,
        name="PAT = PBT - tax",
        statement=label,
        expected=(None if val(pl.profit_before_tax) is None or val(pl.tax) is None
                  else val(pl.profit_before_tax) - val(pl.tax)),
        actual=val(pl.profit_after_tax),
        severity=Severity.CRITICAL,
        detail="Profit after tax does not reconcile to PBT less tax.",
    )
    return f


def verify_financials(fin: ExtractedFinancials) -> list[VerificationFinding]:
    findings: list[VerificationFinding] = []
    for bs in fin.balance_sheets:
        findings.extend(verify_balance_sheet(bs))
    for pl in fin.profit_and_loss:
        findings.extend(verify_pnl(pl))
    return findings
