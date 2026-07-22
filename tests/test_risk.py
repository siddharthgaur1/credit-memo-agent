from __future__ import annotations

from src.analysis.ratios import compute_all
from src.analysis.risk import Severity, evaluate_risk, load_rules
from src.config import ROOT
from tests.conftest import make_balance_sheet, make_pnl

RULES = load_rules(ROOT / "config" / "risk_rules.yaml")


def _flags(fin):
    return evaluate_risk(RULES, compute_all(fin), fin)


def test_healthy_borrower_fires_no_high_severity_flags(healthy):
    high = [f for f in _flags(healthy) if f.severity is Severity.HIGH]
    assert high == [], [f.label for f in high]


def test_thin_dscr_fires_with_the_numbers_that_triggered_it(healthy):
    healthy.profit_and_loss = [make_pnl("FY22"), make_pnl("FY23", profit_after_tax=-115, depreciation=62)]

    dscr = next(f for f in _flags(healthy) if f.rule_id == "dscr_thin")
    assert dscr.severity is Severity.HIGH
    assert dscr.evidence["threshold"] == 1.25
    assert dscr.evidence["value"] < 1.25
    assert "formula" in dscr.evidence


def test_negative_net_worth_fires_on_the_raw_figure(healthy):
    healthy.balance_sheets = [
        make_balance_sheet("FY22"),
        make_balance_sheet("FY23", net_worth=-40, reserves_and_surplus=-140),
    ]
    flags = _flags(healthy)
    assert any(f.rule_id == "negative_net_worth" and f.severity is Severity.HIGH for f in flags)


def test_declining_revenue_fires_from_the_trend_not_a_single_year(healthy):
    healthy.profit_and_loss = [make_pnl("FY22"), make_pnl("FY23", revenue=1420)]

    flag = next(f for f in _flags(healthy) if f.rule_id == "revenue_declining")
    assert flag.evidence["change_pct"] < -10
    assert "FY22" in str(flag.evidence["series"])


def test_cheque_returns_fire_from_bank_conduct(healthy):
    healthy.bank_statements[0].inward_cheque_returns.value = 4
    assert any(f.rule_id == "cheque_returns" for f in _flags(healthy))


def test_a_rule_never_fires_on_a_ratio_that_could_not_be_computed(healthy):
    """No depreciation -> no DSCR -> no DSCR flag. Silence, not a guess."""
    healthy.profit_and_loss = [make_pnl("FY22"), make_pnl("FY23", depreciation=None)]

    assert not any(f.rule_id == "dscr_thin" for f in _flags(healthy))


def test_flags_are_ordered_most_severe_first(healthy):
    healthy.profit_and_loss = [make_pnl("FY22"), make_pnl("FY23", revenue=1420, profit_after_tax=-115)]
    healthy.bank_statements[0].inward_cheque_returns.value = 4

    severities = [f.severity for f in _flags(healthy)]
    assert severities == sorted(severities, key=lambda s: {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}[s])
