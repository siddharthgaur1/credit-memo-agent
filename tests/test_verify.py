from __future__ import annotations

from src.extract.verify import Severity, verify_balance_sheet, verify_financials, verify_pnl
from tests.conftest import make_balance_sheet, make_pnl


def test_clean_balance_sheet_produces_no_findings():
    assert verify_balance_sheet(make_balance_sheet("FY23")) == []


def test_non_balancing_sheet_is_caught_and_not_corrected():
    """The deliberate defect in the 'incomplete' sample borrower."""
    bs = make_balance_sheet("FY23", total_assets=1280)  # components still sum to 1250

    findings = verify_balance_sheet(bs)
    identity = [f for f in findings if f.check == "assets = liabilities + equity"]

    assert identity, "a balance sheet that does not balance must be flagged"
    finding = identity[0]
    assert finding.severity is Severity.CRITICAL
    assert finding.difference == 30 * 100_000
    assert "HUMAN REVIEW REQUIRED" in finding.detail

    # And nothing was silently repaired.
    assert bs.total_assets.value == 1280 * 100_000


def test_rounding_noise_is_tolerated():
    bs = make_balance_sheet("FY23", total_assets=1250.5)  # 0.04% out
    assert not [f for f in verify_balance_sheet(bs) if f.check == "assets = liabilities + equity"]


def test_subtotal_that_does_not_sum_is_flagged():
    bs = make_balance_sheet("FY23", total_current_assets=650)
    checks = {f.check for f in verify_balance_sheet(bs)}
    assert "total current assets = inventory + receivables + cash + other CA" in checks


def test_pnl_that_does_not_reconcile_to_pat_is_critical():
    pl = make_pnl("FY23", profit_after_tax=120)  # 115 - 29 = 86, not 120
    findings = [f for f in verify_pnl(pl) if f.check == "PAT = PBT - tax"]
    assert findings and findings[0].severity is Severity.CRITICAL


def test_missing_inputs_are_not_treated_as_a_verification_failure():
    """Unknown != wrong. A missing subtotal is a completeness gap, not a break."""
    bs = make_balance_sheet("FY23", total_current_liabilities=None)
    checks = {f.check for f in verify_balance_sheet(bs)}
    assert "assets = liabilities + equity" not in checks


def test_verify_financials_covers_every_statement(healthy):
    healthy.balance_sheets[1].total_assets.value = 1_000_000_000
    findings = verify_financials(healthy)
    assert any(f.statement == "balance sheet FY23" for f in findings)
