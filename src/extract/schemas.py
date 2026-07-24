"""Typed, cited financial statements.

Two rules encoded here rather than hoped for:

1. A figure without provenance is not a figure. `Figure` cannot be constructed
   without a source file and the row label it was read from.
2. A missing line item is `None`. There is no default, no zero, no estimate.
   Downstream ratios must handle `None` by reporting insufficient data.
"""

from __future__ import annotations

import re
from datetime import date

from pydantic import BaseModel, Field, field_validator, model_validator


class Citation(BaseModel):
    source_file: str
    page: int | None = None  # PDF page, 1-indexed
    sheet: str | None = None  # workbook sheet
    row_label: str  # the label printed next to the number in the document

    @field_validator("source_file", "row_label")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("citation requires a non-empty source_file and row_label")
        return v.strip()

    @model_validator(mode="after")
    def _needs_a_location(self) -> Citation:
        if self.page is None and self.sheet is None:
            raise ValueError("citation requires a page number or a sheet name")
        return self

    def __str__(self) -> str:
        where = f"p.{self.page}" if self.page is not None else f"sheet '{self.sheet}'"
        return f"{self.source_file}, {where}, row '{self.row_label}'"


class Figure(BaseModel):
    """A number lifted from a document, in absolute INR, with its provenance."""

    value: float
    citation: Citation

    def __str__(self) -> str:
        return f"{self.value:,.0f} [{self.citation}]"


def val(f: Figure | None) -> float | None:
    """Unwrap a figure. `None` in, `None` out -- never a substitute value."""
    return None if f is None else f.value


class Period(BaseModel):
    """A reporting period, with the raw label kept for traceability."""

    label: str  # exactly as printed: "FY23", "2022-23", "March 2023"
    end_date: date | None = None

    @property
    def fy(self) -> str | None:
        """Normalise FY23 / 2022-23 / FY 2022-23 / March 2023 to 'FY23'."""
        return normalise_period(self.label, self.end_date)


def normalise_period(label: str, end_date: date | None = None) -> str | None:
    """Align inconsistent period labels so multi-year statements line up.

    Indian FY ends 31 March; FY23 means the year ended 31 March 2023.
    """
    text = (label or "").strip().lower()

    m = re.search(r"\bfy\s*[-']?\s*(\d{4})\s*[-/]\s*(\d{2,4})\b", text)  # FY 2022-23
    if m:
        return f"FY{int(m.group(2)) % 100:02d}"
    m = re.search(r"\b(\d{4})\s*[-/]\s*(\d{2,4})\b", text)  # 2022-23
    if m:
        return f"FY{int(m.group(2)) % 100:02d}"
    m = re.search(r"\bfy\s*[-']?\s*(\d{2,4})\b", text)  # FY23 / FY2023
    if m:
        return f"FY{int(m.group(1)) % 100:02d}"
    m = re.search(r"\b(mar|march)\D{0,10}(\d{4})\b", text)  # March 2023
    if m:
        return f"FY{int(m.group(2)) % 100:02d}"

    if end_date is not None:
        year = end_date.year + (1 if end_date.month > 3 else 0)
        return f"FY{year % 100:02d}"
    return None


class BalanceSheet(BaseModel):
    period: Period
    source_file: str

    # Equity
    share_capital: Figure | None = None
    reserves_and_surplus: Figure | None = None
    net_worth: Figure | None = None

    # Liabilities
    long_term_borrowings: Figure | None = None
    other_non_current_liabilities: Figure | None = None
    short_term_borrowings: Figure | None = None
    trade_payables: Figure | None = None
    other_current_liabilities: Figure | None = None
    total_current_liabilities: Figure | None = None
    total_liabilities: Figure | None = None

    # Assets
    net_fixed_assets: Figure | None = None
    other_non_current_assets: Figure | None = None
    inventory: Figure | None = None
    trade_receivables: Figure | None = None
    cash_and_bank: Figure | None = None
    other_current_assets: Figure | None = None
    total_current_assets: Figure | None = None
    total_assets: Figure | None = None

    # Intangibles are deducted when computing tangible net worth (TOL/TNW).
    intangible_assets: Figure | None = None


class ProfitAndLoss(BaseModel):
    period: Period
    source_file: str

    revenue: Figure | None = None
    other_income: Figure | None = None
    cost_of_goods_sold: Figure | None = None
    gross_profit: Figure | None = None
    employee_cost: Figure | None = None
    other_expenses: Figure | None = None
    ebitda: Figure | None = None
    depreciation: Figure | None = None
    finance_cost: Figure | None = None
    profit_before_tax: Figure | None = None
    tax: Figure | None = None
    profit_after_tax: Figure | None = None


class BankStatementSummary(BaseModel):
    source_file: str
    account_label: str = "operative account"
    months_covered: int | None = None
    period_start: date | None = None
    period_end: date | None = None

    average_monthly_balance: Figure | None = None
    minimum_balance: Figure | None = None
    total_credits: Figure | None = None
    total_debits: Figure | None = None
    inward_cheque_returns: Figure | None = None  # borrower's own cheques bounced
    outward_cheque_returns: Figure | None = None  # cheques deposited that bounced
    sanctioned_limit: Figure | None = None
    peak_utilisation: Figure | None = None


class DebtService(BaseModel):
    """Repayment obligations. Sourced from sanction letters / the proposal."""

    period_label: str = ""
    principal_repayment: Figure | None = None
    interest_expense: Figure | None = None
    proposed_emi_annual: Figure | None = None


class ExtractedFinancials(BaseModel):
    borrower_name: str | None = None
    facility_requested: str | None = None
    balance_sheets: list[BalanceSheet] = Field(default_factory=list)
    profit_and_loss: list[ProfitAndLoss] = Field(default_factory=list)
    bank_statements: list[BankStatementSummary] = Field(default_factory=list)
    debt_service: list[DebtService] = Field(default_factory=list)

    def periods(self) -> list[str]:
        """Periods present in both BS and P&L, newest last, FY-normalised."""
        labels = {bs.period.fy or bs.period.label for bs in self.balance_sheets}
        labels |= {pl.period.fy or pl.period.label for pl in self.profit_and_loss}
        return sorted(labels)

    def bs_for(self, fy: str) -> BalanceSheet | None:
        return next((b for b in self.balance_sheets if (b.period.fy or b.period.label) == fy), None)

    def pl_for(self, fy: str) -> ProfitAndLoss | None:
        return next((p for p in self.profit_and_loss if (p.period.fy or p.period.label) == fy), None)

    def debt_for(self, fy: str) -> DebtService | None:
        return next(
            (d for d in self.debt_service if normalise_period(d.period_label) == fy or d.period_label == fy),
            None,
        )
