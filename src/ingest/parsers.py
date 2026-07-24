"""Digital PDF + Excel/CSV parsing.

Financial statements are mostly tables, so table fidelity matters more than
prose. Every page/sheet keeps its number/name so extracted figures can cite it.
"""

from __future__ import annotations

import calendar
import re
from datetime import date
from pathlib import Path

from pydantic import BaseModel, Field

# A digital PDF page carries hundreds of characters. Anything under this across
# the whole document means there is no text layer worth reading.
MIN_TEXT_CHARS_PER_PAGE = 40


class ScannedPDFError(RuntimeError):
    """No extractable text layer. OCR is out of scope -- say so, don't return empty."""


class Page(BaseModel):
    """One page of a PDF or one sheet of a workbook."""

    number: int | None = None  # PDF page, 1-indexed
    sheet: str | None = None  # workbook sheet name
    text: str = ""
    tables: list[list[list[str | None]]] = Field(default_factory=list)

    @property
    def label(self) -> str:
        return f"page {self.number}" if self.number else f"sheet '{self.sheet}'"

    def as_prompt_block(self) -> str:
        parts = [f"--- {self.label} ---", self.text.strip()]
        for i, table in enumerate(self.tables, start=1):
            rows = ["\t".join("" if c is None else str(c).strip() for c in row) for row in table]
            parts.append(f"[table {i} on {self.label}]\n" + "\n".join(rows))
        return "\n".join(p for p in parts if p.strip())


class ParsedDoc(BaseModel):
    path: Path
    kind: str  # "pdf" | "excel" | "csv"
    pages: list[Page]

    @property
    def filename(self) -> str:
        return self.path.name

    def as_prompt(self, max_chars: int = 60_000) -> str:
        body = "\n\n".join(p.as_prompt_block() for p in self.pages)
        return body[:max_chars]


def parse_pdf(path: Path) -> ParsedDoc:
    import pdfplumber

    pages: list[Page] = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            pages.append(
                Page(
                    number=i,
                    text=page.extract_text() or "",
                    tables=[t for t in (page.extract_tables() or []) if t],
                )
            )

    total_chars = sum(len(p.text.strip()) for p in pages)
    has_tables = any(p.tables for p in pages)
    if not has_tables and total_chars < MIN_TEXT_CHARS_PER_PAGE * max(len(pages), 1):
        raise ScannedPDFError(
            f"{path.name}: no extractable text layer ({total_chars} chars over {len(pages)} pages). "
            "This looks like a scanned/image PDF -- OCR is not supported. "
            "Please supply a digital PDF or the source spreadsheet."
        )
    return ParsedDoc(path=path, kind="pdf", pages=pages)


def _find_header_row(rows: list[list]) -> int:
    """Locate the real header in sheets that open with a title block.

    Heuristic: first row with >= 2 non-empty cells that is followed by a row of
    the same or greater width. Title blocks are typically 1 populated cell wide.
    """
    for i, row in enumerate(rows):
        filled = sum(1 for c in row if c is not None and str(c).strip() != "")
        if filled < 2:
            continue
        nxt = rows[i + 1] if i + 1 < len(rows) else []
        nxt_filled = sum(1 for c in nxt if c is not None and str(c).strip() != "")
        if nxt_filled >= 2:
            return i
    return 0


def parse_tabular(path: Path) -> ParsedDoc:
    """Excel (all sheets) or CSV, tolerating title blocks and merged headers."""
    import pandas as pd

    if path.suffix.lower() == ".csv":
        raw = {path.stem: pd.read_csv(path, header=None, dtype=object)}
        kind = "csv"
    else:
        raw = pd.read_excel(path, sheet_name=None, header=None, dtype=object)
        kind = "excel"

    pages: list[Page] = []
    for name, df in raw.items():
        # Merged header cells arrive as NaN in the trailing columns -- carry the
        # label forward so "FY23 | Amount" doesn't become "| Amount".
        rows = df.where(df.notna(), None).values.tolist()
        header_idx = _find_header_row(rows)
        header = list(rows[header_idx]) if rows else []
        for j in range(1, len(header)):
            if header[j] is None and header[j - 1] is not None:
                header[j] = header[j - 1]
        table = [[None if c is None else str(c).strip() for c in r] for r in rows[header_idx:]]
        if table:
            table[0] = [None if c is None else str(c).strip() for c in header]
        title = "\n".join(
            str(c).strip() for r in rows[:header_idx] for c in r if c is not None and str(c).strip()
        )
        pages.append(Page(sheet=str(name), text=title, tables=[table] if table else []))

    return ParsedDoc(path=path, kind=kind, pages=pages)


MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_MONTH_YEAR = re.compile(r"\b(" + "|".join(MONTHS) + r")[a-z]*\.?\s+(\d{4})\b", re.IGNORECASE)
_DMY = re.compile(r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{4})\b")


def latest_date_in(text: str) -> date | None:
    """Newest date a document mentions -- what it speaks about, not its mtime.

    Used for staleness: a GST return filed for "Jun 2026" is fresh regardless of
    when the file was copied onto the machine.
    """
    found: list[date] = []
    for month, year in _MONTH_YEAR.findall(text or ""):
        m = MONTHS[month.lower()[:3]]
        found.append(date(int(year), m, calendar.monthrange(int(year), m)[1]))
    for d, m, y in _DMY.findall(text or ""):
        if 1 <= int(m) <= 12 and 1 <= int(d) <= 31:
            found.append(date(int(y), int(m), int(d)))
    return max(found) if found else None


def parse_file(path: Path) -> ParsedDoc:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return parse_pdf(path)
    if suffix in {".xlsx", ".xlsm", ".xls", ".csv"}:
        return parse_tabular(path)
    raise ValueError(f"{path.name}: unsupported file type '{suffix}'. Supported: .pdf .xlsx .xls .csv")
