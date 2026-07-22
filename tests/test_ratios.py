"""Ratio engine against hand-computed fixtures.

The expected values below were computed by hand from the FY23 healthy fixture.
If a number here changes, the memo's arithmetic changed -- that is the point.
"""

from __future__ import annotations

import pytest

from src.analysis.ratios import Completeness, compute_all, compute_period
from tests.conftest import make_balance_sheet, make_pnl

# FY23 healthy borrower, hand-computed:
#   DSCR              (86 + 75 + 62) / (62 + 55)      = 223 / 117
#   Interest coverage 240 / 62
#   Debt / equity     (260 + 180) / 520               = 440 / 520
#   TOL / TNW         (1250 - 520) / 520              = 730 / 520
#   Current ratio     700 / 430
#   Quick ratio       (700 - 260) / 430               = 440 / 430
#   Gross margin      744 / 2400 x 100
#   Net margin        86 / 2400 x 100
#   ROCE              (240 - 75) / (520 + 260) x 100  = 165 / 780 x 100
#   Inventory days    260 / 1656 x 365
#   Receivable days   300 / 2400 x 365
#   Payable days      190 / 1656 x 365
#   WC cycle          inventory + receivable - payable days
EXPECTED_FY23 = {
    "DSCR": 223 / 117,
    "Interest coverage": 240 / 62,
    "Debt / equity": 440 / 520,
    "TOL / TNW": 730 / 520,
    "Current ratio": 700 / 430,
    "Quick ratio": 440 / 430,
    "Gross margin": 31.0,
    "Net margin": 86 / 2400 * 100,
    "ROCE": 165 / 780 * 100,
    "Inventory days": 260 / 1656 * 365,
    "Receivable days": 45.625,
    "Payable days": 190 / 1656 * 365,
    "Working capital cycle": (260 / 1656 * 365) + 45.625 - (190 / 1656 * 365),
}


@pytest.mark.parametrize("name,expected", sorted(EXPECTED_FY23.items()))
def test_ratio_matches_hand_computed_value(healthy, name, expected):
    result = next(r for r in compute_period(healthy, "FY23") if r.name == name)
    assert result.data_completeness is Completeness.COMPLETE
    assert result.value == pytest.approx(expected, rel=1e-12)


def test_every_ratio_shows_its_formula_and_inputs(healthy):
    for result in compute_period(healthy, "FY23"):
        assert result.formula, f"{result.name} has no formula"
        assert result.inputs, f"{result.name} does not record its inputs"


def test_missing_line_item_yields_insufficient_data_not_a_number(healthy):
    """Depreciation absent -> DSCR and ROCE must refuse to compute."""
    healthy.profit_and_loss = [make_pnl("FY22"), make_pnl("FY23", depreciation=None)]

    results = {r.name: r for r in compute_period(healthy, "FY23")}

    for name in ("DSCR", "ROCE"):
        assert results[name].value is None
        assert results[name].data_completeness is Completeness.INSUFFICIENT_DATA
        assert "Depreciation" in results[name].missing_inputs
        assert results[name].display == "insufficient data"

    # Ratios that don't need depreciation are unaffected.
    assert results["Current ratio"].value == pytest.approx(700 / 430)


def test_zero_denominator_is_undefined_not_infinity(healthy):
    healthy.profit_and_loss = [make_pnl("FY23", finance_cost=0)]
    healthy.balance_sheets = [make_balance_sheet("FY23")]

    cover = next(r for r in compute_period(healthy, "FY23") if r.name == "Interest coverage")
    assert cover.value is None
    assert cover.data_completeness is Completeness.UNDEFINED


def test_trends_report_direction_and_magnitude(healthy):
    analysis = compute_all(healthy)
    revenue = next(t for t in analysis.trends if t.metric == "Revenue")

    assert revenue.direction == "improving"
    assert revenue.change_pct == pytest.approx((2400 - 2050) / 2050 * 100)

    net_margin = next(t for t in analysis.trends if t.metric == "Net margin")
    assert net_margin.direction == "improving"


def test_single_period_trend_reports_insufficient_data(healthy):
    healthy.balance_sheets = [make_balance_sheet("FY23")]
    healthy.profit_and_loss = [make_pnl("FY23")]

    analysis = compute_all(healthy)
    assert all(t.direction == "insufficient data" for t in analysis.trends)
