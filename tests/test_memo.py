from __future__ import annotations

from datetime import date

from src.agents.graph import reviewer_agent
from src.analysis.ratios import compute_all
from src.analysis.risk import evaluate_risk, load_rules
from src.checklist.validate import SubmittedDoc, load_checklist, validate
from src.config import ROOT
from src.extract.verify import verify_financials
from src.ingest.classify import DocType
from src.memo.compose import DISCLAIMER, build_memo, write_docx
from tests.conftest import make_balance_sheet, make_pnl

CHECKLIST = load_checklist(ROOT / "config" / "checklist_business_instalment_loan.yaml")
RULES = load_rules(ROOT / "config" / "risk_rules.yaml")


def _memo(fin, narrative="Commentary.", trend="Trends."):
    analysis = compute_all(fin)
    report = validate(
        CHECKLIST,
        [SubmittedDoc(filename="balance_sheet_FY23.pdf", doc_type=DocType.BALANCE_SHEET, as_of=date(2023, 3, 31))],
        today=date(2023, 6, 30),
    )
    return build_memo(
        fin=fin,
        checklist=report,
        analysis=analysis,
        flags=evaluate_risk(RULES, analysis, fin),
        verification=verify_financials(fin),
        narrative=narrative,
        trend_commentary=trend,
        backend="ollama",
    )


def test_memo_carries_the_machine_generated_header_and_no_verdict(healthy):
    memo = _memo(healthy)

    assert "NOT A CREDIT DECISION" in DISCLAIMER
    assert "requires" not in memo.borrower_name  # borrower name came from the file
    prose = " ".join(s.body.lower() for s in memo.sections)
    for phrase in ("we recommend", "approve the facility", "credit score"):
        assert phrase not in prose


def test_every_figure_in_the_memo_is_cited(healthy):
    memo = _memo(healthy)

    assert memo.citations
    for citation in memo.citations:
        assert ".pdf" in citation or ".xlsx" in citation
        assert "row '" in citation


def test_ratio_table_shows_the_formula(healthy):
    ratios = next(s for s in _memo(healthy).sections if s.heading == "Ratio analysis")
    header, *rows = ratios.table

    assert "Formula" in header
    dscr = next(r for r in rows if r[0] == "DSCR")
    assert "PAT + Depreciation + Finance cost" in dscr[-2]


def test_incomplete_ratio_is_labelled_and_raised_as_an_open_question(healthy):
    healthy.profit_and_loss = [make_pnl("FY22"), make_pnl("FY23", depreciation=None)]
    memo = _memo(healthy)

    ratios = next(s for s in memo.sections if s.heading == "Ratio analysis")
    assert "insufficient data" in ratios.body
    dscr_row = next(r for r in ratios.table[1:] if r[0] == "DSCR")
    assert "insufficient data" in dscr_row
    assert any("DSCR" in q for q in memo.open_questions)


def test_non_balancing_sheet_reaches_the_memo_as_a_finding(healthy):
    healthy.balance_sheets = [make_balance_sheet("FY22"), make_balance_sheet("FY23", total_assets=1280)]
    memo = _memo(healthy)

    findings = next(s for s in memo.sections if s.heading == "Arithmetic verification findings")
    checks = [row[0] for row in findings.table[1:]]
    assert "assets = liabilities + equity" in checks
    assert any("clarify" in q.lower() for q in memo.open_questions)


def test_missing_line_item_never_becomes_a_number_in_the_summary(healthy):
    healthy.balance_sheets = [make_balance_sheet("FY22"), make_balance_sheet("FY23", inventory=None)]
    summary = next(s for s in _memo(healthy).sections if s.heading == "Financial summary")

    inventory = next(r for r in summary.table[1:] if r[0] == "Inventory")
    assert inventory[-1] == "not found"


def test_checklist_gaps_become_questions_for_the_borrower(healthy):
    memo = _memo(healthy)
    assert any("GST returns" in q for q in memo.open_questions)


def test_reviewer_passes_a_clean_memo_and_catches_a_decision_phrase(healthy):
    state = {
        "financials": healthy,
        "analysis": compute_all(healthy),
        "memo": _memo(healthy),
        "verification": [],
    }
    assert reviewer_agent(state)["review_findings"] == []

    state["memo"] = _memo(healthy, narrative="On balance we recommend approval of this facility.")
    findings = reviewer_agent(state)["review_findings"]
    assert any("decision-like phrase" in f for f in findings)


def test_docx_is_written(tmp_path, healthy):
    path = write_docx(_memo(healthy), tmp_path / "memo" / "credit_memo.docx")
    assert path.exists() and path.stat().st_size > 5_000
