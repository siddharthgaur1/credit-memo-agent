from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.extract.extractor import reject_uncited
from src.extract.schemas import BalanceSheet, Citation, Figure, Period, normalise_period
from src.ingest.parsers import Page, ParsedDoc

DOC = ParsedDoc(
    path="balance_sheet_FY23.pdf",
    kind="pdf",
    pages=[
        Page(
            number=1,
            text="BALANCE SHEET AS AT 31 MARCH 2023",
            tables=[[["Particulars", "Amount"], ["Trade receivables", "300.00"], ["Inventories", "260.00"]]],
        )
    ],
)


def _bs(**figures) -> BalanceSheet:
    return BalanceSheet(period=Period(label="FY23"), source_file="balance_sheet_FY23.pdf", **figures)


def _fig(value: float, row_label: str, source="balance_sheet_FY23.pdf", page=1, sheet=None) -> Figure:
    return Figure(
        value=value,
        citation=Citation(source_file=source, page=page, sheet=sheet, row_label=row_label),
    )


def test_a_figure_cannot_be_built_without_provenance():
    with pytest.raises(ValidationError):
        Citation(source_file="", page=1, row_label="Inventories")
    with pytest.raises(ValidationError):
        Citation(source_file="bs.pdf", page=1, row_label="   ")
    with pytest.raises(ValidationError):
        Citation(source_file="bs.pdf", row_label="Inventories")  # neither page nor sheet


def test_figure_with_a_row_label_absent_from_the_page_is_rejected():
    bs = _bs(
        trade_receivables=_fig(30_000_000, "Trade receivables"),
        inventory=_fig(26_000_000, "Sundry debtors ageing"),  # not printed anywhere
    )

    cleaned, rejections = reject_uncited(bs, DOC)

    assert cleaned.trade_receivables is not None
    assert cleaned.inventory is None, "an unverifiable figure must be dropped, not caveated"
    assert any("Sundry debtors ageing" in r for r in rejections)


def test_figure_citing_another_file_is_rejected():
    bs = _bs(inventory=_fig(26_000_000, "Inventories", source="some_other_file.pdf"))
    cleaned, rejections = reject_uncited(bs, DOC)

    assert cleaned.inventory is None
    assert any("some_other_file.pdf" in r for r in rejections)


def test_figure_citing_a_nonexistent_page_is_rejected():
    bs = _bs(inventory=_fig(26_000_000, "Inventories", page=9))
    cleaned, rejections = reject_uncited(bs, DOC)

    assert cleaned.inventory is None
    assert any("does not exist" in r for r in rejections)


def test_verified_figures_survive_untouched():
    bs = _bs(inventory=_fig(26_000_000, "Inventories"))
    cleaned, rejections = reject_uncited(bs, DOC)

    assert rejections == []
    assert cleaned.inventory.value == 26_000_000


@pytest.mark.parametrize(
    "label,expected",
    [
        ("FY23", "FY23"),
        ("FY 2022-23", "FY23"),
        ("2022-23", "FY23"),
        ("FY2023", "FY23"),
        ("March 2023", "FY23"),
        ("Year ended 31 March 2023", "FY23"),
        ("some prose with no period", None),
    ],
)
def test_inconsistent_period_labels_align(label, expected):
    assert normalise_period(label) == expected


def test_period_end_date_is_used_when_the_label_is_useless():
    from datetime import date

    assert Period(label="statement", end_date=date(2023, 3, 31)).fy == "FY23"
    assert Period(label="statement", end_date=date(2023, 6, 30)).fy == "FY24"
