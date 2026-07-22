from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.extract.schemas import (  # noqa: E402
    BalanceSheet,
    BankStatementSummary,
    Citation,
    DebtService,
    ExtractedFinancials,
    Figure,
    Period,
    ProfitAndLoss,
)

LAKH = 100_000.0


def fig(value_in_lakhs: float | None, row_label: str, source: str = "balance_sheet_FY23.pdf", page: int = 1):
    """Build a cited figure in absolute rupees. `None` stays `None` -- never 0."""
    if value_in_lakhs is None:
        return None
    return Figure(
        value=value_in_lakhs * LAKH,
        citation=Citation(source_file=source, page=page, row_label=row_label),
    )


# Healthy borrower, FY22 + FY23, matching sample_data/generate.py HEALTHY.
BS = {
    "FY22": dict(
        share_capital=100, reserves_and_surplus=330, net_worth=430,
        long_term_borrowings=300, other_non_current_liabilities=35,
        short_term_borrowings=160, trade_payables=170, other_current_liabilities=50,
        total_current_liabilities=380, net_fixed_assets=545, other_non_current_assets=25,
        inventory=240, trade_receivables=265, cash_and_bank=55, other_current_assets=15,
        total_current_assets=575, total_assets=1145,
    ),
    "FY23": dict(
        share_capital=100, reserves_and_surplus=420, net_worth=520,
        long_term_borrowings=260, other_non_current_liabilities=40,
        short_term_borrowings=180, trade_payables=190, other_current_liabilities=60,
        total_current_liabilities=430, net_fixed_assets=520, other_non_current_assets=30,
        inventory=260, trade_receivables=300, cash_and_bank=90, other_current_assets=50,
        total_current_assets=700, total_assets=1250,
    ),
}

PL = {
    "FY22": dict(
        revenue=2050, cost_of_goods_sold=1435, gross_profit=615, employee_cost=215,
        other_expenses=215, ebitda=185, other_income=8, depreciation=70,
        finance_cost=66, profit_before_tax=57, tax=14, profit_after_tax=43,
    ),
    "FY23": dict(
        revenue=2400, cost_of_goods_sold=1656, gross_profit=744, employee_cost=240,
        other_expenses=264, ebitda=240, other_income=12, depreciation=75,
        finance_cost=62, profit_before_tax=115, tax=29, profit_after_tax=86,
    ),
}

PRINCIPAL = {"FY22": 58, "FY23": 55}


def make_balance_sheet(fy: str, **overrides) -> BalanceSheet:
    data = {**BS[fy], **overrides}
    src = f"balance_sheet_{fy}.pdf"
    return BalanceSheet(
        period=Period(label=fy),
        source_file=src,
        **{k: fig(v, k.replace("_", " ").title(), src) for k, v in data.items()},
    )


def make_pnl(fy: str, **overrides) -> ProfitAndLoss:
    data = {**PL[fy], **overrides}
    src = f"profit_and_loss_{fy}.pdf"
    return ProfitAndLoss(
        period=Period(label=fy),
        source_file=src,
        **{k: fig(v, k.replace("_", " ").title(), src) for k, v in data.items()},
    )


@pytest.fixture
def healthy() -> ExtractedFinancials:
    return ExtractedFinancials(
        borrower_name="Suryodaya Engineering Works Pvt Ltd",
        facility_requested="Term loan of Rs. 180.00 lakhs",
        balance_sheets=[make_balance_sheet("FY22"), make_balance_sheet("FY23")],
        profit_and_loss=[make_pnl("FY22"), make_pnl("FY23")],
        bank_statements=[
            BankStatementSummary(
                source_file="bank_statement_summary.xlsx",
                average_monthly_balance=Figure(
                    value=41.5 * LAKH,
                    citation=Citation(
                        source_file="bank_statement_summary.xlsx", sheet="Summary",
                        row_label="Average monthly balance",
                    ),
                ),
                inward_cheque_returns=Figure(
                    value=0,
                    citation=Citation(
                        source_file="bank_statement_summary.xlsx", sheet="Summary",
                        row_label="Inward cheque returns",
                    ),
                ),
                sanctioned_limit=Figure(
                    value=200 * LAKH,
                    citation=Citation(
                        source_file="bank_statement_summary.xlsx", sheet="Summary", row_label="Sanctioned limit"
                    ),
                ),
                peak_utilisation=Figure(
                    value=168 * LAKH,
                    citation=Citation(
                        source_file="bank_statement_summary.xlsx", sheet="Summary", row_label="Peak utilisation"
                    ),
                ),
            )
        ],
        debt_service=[
            DebtService(
                period_label=fy,
                principal_repayment=fig(PRINCIPAL[fy], "Principal repayment", f"profit_and_loss_{fy}.pdf"),
            )
            for fy in ("FY22", "FY23")
        ],
    )
