"""End-to-end over the generated synthetic files, up to the point an LLM is needed.

Intake and checklist are deterministic, so they can be exercised for real against
real generated PDFs and workbooks -- no model, no mocking.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sample_data"))

from generate import HEALTHY, INCOMPLETE, RECENT_MONTHS, generate  # noqa: E402

LATEST_FY = HEALTHY["years"][-1]
STALE_FY = INCOMPLETE["years"][-1]

from src.agents.graph import checklist_agent, intake_agent  # noqa: E402
from src.checklist.validate import Status  # noqa: E402
from src.config import Settings  # noqa: E402
from src.ingest.classify import DocType  # noqa: E402


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    root = tmp_path_factory.mktemp("sample")
    return {b["slug"]: generate(b, root) for b in (HEALTHY, INCOMPLETE)}


def _intake(folder: Path) -> dict:
    return intake_agent({"files": [str(p) for p in sorted(folder.iterdir())]})


def _with_settings(state: dict, tmp_path) -> dict:
    return {**state, "settings": Settings(data_dir=tmp_path / "data", log_dir=tmp_path / "logs")}


def test_generated_documents_are_classified_correctly(generated):
    state = _intake(generated["suryodaya_engineering"])
    found = {c["filename"]: c["doc_type"] for c in state["classifications"]}

    assert found[f"balance_sheet_{LATEST_FY}.pdf"] == DocType.BALANCE_SHEET.value
    assert found[f"profit_and_loss_{LATEST_FY}.pdf"] == DocType.PROFIT_AND_LOSS.value
    assert found["bank_statement_summary.xlsx"] == DocType.BANK_STATEMENT.value
    assert found[f"gstr3b_{RECENT_MONTHS[0].replace(' ', '_').lower()}.pdf"] == DocType.GST_RETURN.value
    assert found[next(f for f in found if f.startswith("itr_"))] == DocType.ITR.value
    assert found["kyc_pan_udyam_address.pdf"] == DocType.KYC.value
    assert found["project_report_and_quotation.pdf"] == DocType.PROJECT_REPORT.value
    assert found["sanction_letter_existing_facility.pdf"] == DocType.SANCTION_LETTER.value
    assert state["escalations"] == []


def test_healthy_file_passes_the_checklist(generated, tmp_path):
    state = _intake(generated["suryodaya_engineering"])
    report = checklist_agent(_with_settings(state, tmp_path))["checklist_report"]

    assert report.is_complete, [(g.label, g.reason) for g in report.gaps]
    statuses = {r.id: r.status for r in report.results}
    assert statuses["audited_financials"] is Status.PRESENT
    assert statuses["gst_returns"] is Status.PRESENT
    assert statuses["bank_statements"] is Status.PRESENT
    assert statuses["project_report"] is Status.PRESENT
    assert statuses["kyc"] is Status.PRESENT


def test_incomplete_file_lists_missing_and_stale_documents(generated, tmp_path):
    state = _intake(generated["meenakshi_traders"])
    report = checklist_agent(_with_settings(state, tmp_path))["checklist_report"]
    results = {r.id: r for r in report.results}

    assert results["gst_returns"].status is Status.MISSING
    assert "0 of 4" in results["gst_returns"].reason

    # Financials are FY20/FY21 -- years past the policy window.
    assert results["audited_financials"].status is Status.STALE
    assert "days old" in results["audited_financials"].reason
    assert results["audited_financials"].matched_files  # present, just old

    assert not report.is_complete
    assert {"gst_returns", "audited_financials"} <= {g.id for g in report.gaps}


def test_pdf_tables_survive_extraction(generated):
    from src.ingest.parsers import parse_file

    doc = parse_file(generated["suryodaya_engineering"] / f"balance_sheet_{LATEST_FY}.pdf")
    text = doc.as_prompt()

    assert "BALANCE SHEET AS AT 31 MARCH" in text
    assert "1,250.00" in text  # total assets, from the ruled table
    assert doc.pages[0].tables, "the statement table must be extracted as a table"


def test_bank_workbook_summary_is_readable(generated):
    from src.ingest.parsers import parse_file

    doc = parse_file(generated["suryodaya_engineering"] / "bank_statement_summary.xlsx")
    summary = next(p for p in doc.pages if p.sheet == "Summary")

    assert summary.tables[0][0] == ["Particulars", "Value"]
    labels = [row[0] for row in summary.tables[0]]
    assert "Inward cheque returns (own cheques returned unpaid)" in labels


def test_incomplete_borrower_balance_sheet_is_the_deliberate_defect():
    """FY21 components sum to 830 but the statement prints 860."""
    bs = INCOMPLETE["bs"]
    components = bs["Net fixed assets"][1] + bs["Other non-current assets"][1] + bs["Total current assets"][1]
    equity_and_liabilities = (
        bs["Net worth"][1] + bs["Long-term borrowings"][1]
        + bs["Other non-current liabilities"][1] + bs["Total current liabilities"][1]
    )

    assert components == equity_and_liabilities == 830
    assert bs["Total assets"][1] == 860, "the sample must stay broken -- it proves verification works"
    assert INCOMPLETE["pl"]["Depreciation and amortisation"][1] is None


def test_dates_used_by_the_checklist_come_from_the_documents(generated):
    """Staleness must key off the reporting period, not the file's mtime."""
    from src.agents.graph import _doc_date
    from src.ingest.parsers import parse_file

    doc = parse_file(generated["meenakshi_traders"] / f"balance_sheet_{STALE_FY}.pdf")
    assert _doc_date(doc, DocType.BALANCE_SHEET) == date(2000 + int(STALE_FY[2:]), 3, 31)
