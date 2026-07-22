from __future__ import annotations

from datetime import date

from src.checklist.validate import Status, SubmittedDoc, load_checklist, validate
from src.config import ROOT
from src.ingest.classify import DocType

CHECKLIST = load_checklist(ROOT / "config" / "checklist_business_instalment_loan.yaml")
TODAY = date(2024, 6, 30)


def _doc(name: str, dtype: DocType, as_of: date | None = None) -> SubmittedDoc:
    return SubmittedDoc(filename=name, doc_type=dtype, as_of=as_of)


def _complete_file() -> list[SubmittedDoc]:
    fy23, fy22 = date(2023, 3, 31), date(2022, 3, 31)
    return [
        _doc("balance_sheet_FY23.pdf", DocType.BALANCE_SHEET, fy23),
        _doc("balance_sheet_FY22.pdf", DocType.BALANCE_SHEET, fy22),
        _doc("profit_and_loss_FY23.pdf", DocType.PROFIT_AND_LOSS, fy23),
        _doc("profit_and_loss_FY22.pdf", DocType.PROFIT_AND_LOSS, fy22),
        _doc("itr_ay2023_24.pdf", DocType.ITR, date(2023, 9, 28)),
        *[_doc(f"gstr3b_{i}.pdf", DocType.GST_RETURN, date(2024, 5, 20)) for i in range(4)],
        _doc("bank_statement_summary.xlsx", DocType.BANK_STATEMENT, date(2024, 5, 31)),
        _doc("kyc_pan_udyam_address.pdf", DocType.KYC),
        _doc("project_report_and_quotation.pdf", DocType.PROJECT_REPORT, date(2024, 3, 1)),
        _doc("sanction_letter_existing_facility.pdf", DocType.SANCTION_LETTER),
    ]


def test_complete_file_has_no_gaps():
    report = validate(CHECKLIST, _complete_file(), today=TODAY)
    assert report.is_complete, [(g.label, g.reason) for g in report.gaps]


def test_missing_documents_are_named_with_a_reason():
    submitted = [d for d in _complete_file() if d.doc_type is not DocType.GST_RETURN]
    report = validate(CHECKLIST, submitted, today=TODAY)

    gst = next(r for r in report.results if r.id == "gst_returns")
    assert gst.status is Status.MISSING
    assert "0 of 4" in gst.reason


def test_partially_supplied_requirement_still_counts_as_missing():
    submitted = [d for d in _complete_file() if d.doc_type is not DocType.BALANCE_SHEET]
    submitted.append(_doc("balance_sheet_FY23.pdf", DocType.BALANCE_SHEET, date(2023, 3, 31)))

    report = validate(CHECKLIST, submitted, today=TODAY)
    bs = next(r for r in report.results if r.id == "audited_financials")
    assert bs.status is Status.MISSING
    assert "1 of 2" in bs.reason


def test_stale_financials_are_present_but_flagged_stale():
    old = date(2021, 3, 31)
    submitted = [d for d in _complete_file() if d.doc_type is not DocType.BALANCE_SHEET]
    submitted += [
        _doc("balance_sheet_FY21.pdf", DocType.BALANCE_SHEET, old),
        _doc("balance_sheet_FY20.pdf", DocType.BALANCE_SHEET, date(2020, 3, 31)),
    ]

    report = validate(CHECKLIST, submitted, today=TODAY)
    bs = next(r for r in report.results if r.id == "audited_financials")
    assert bs.status is Status.STALE
    assert "2021-03-31" in bs.reason and "550 days" in bs.reason
    assert bs.matched_files  # present, just old


def test_undated_document_cannot_be_certified_fresh():
    submitted = [d for d in _complete_file() if d.doc_type is not DocType.GST_RETURN]
    submitted += [_doc(f"gstr3b_{i}.pdf", DocType.GST_RETURN, None) for i in range(4)]

    report = validate(CHECKLIST, submitted, today=TODAY)
    gst = next(r for r in report.results if r.id == "gst_returns")
    assert gst.status is Status.STALE
    assert "age cannot be verified" in gst.reason


def test_optional_item_absent_is_not_a_gap():
    submitted = [d for d in _complete_file() if d.doc_type is not DocType.SANCTION_LETTER]
    report = validate(CHECKLIST, submitted, today=TODAY)

    item = next(r for r in report.results if r.id == "existing_sanctions")
    assert item.status is Status.NOT_APPLICABLE
    assert item not in report.gaps
