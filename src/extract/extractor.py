"""LLM-driven extraction into the typed, cited schemas.

The LLM reads documents; it does not do arithmetic and it does not get to invent
provenance. Every figure it returns is checked against the actual document text
before it is accepted -- a citation that doesn't resolve is treated exactly like
a figure that was never found.
"""

from __future__ import annotations

import logging
import re
from datetime import date

from pydantic import BaseModel

from ..ingest.classify import DocType
from ..ingest.parsers import ParsedDoc
from ..llm.backend import LLMBackend
from .schemas import (
    BalanceSheet,
    BankStatementSummary,
    DebtService,
    ExtractedFinancials,
    Figure,
    ProfitAndLoss,
)

log = logging.getLogger("credit_memo.extract")

CORE_BS_FIELDS = ("total_assets", "total_current_assets", "total_current_liabilities", "net_worth")
CORE_PL_FIELDS = ("revenue", "ebitda", "profit_after_tax", "finance_cost")

RULES = """
Rules you must follow exactly:
1. Report every amount in ABSOLUTE RUPEES. If the statement is printed in lakhs,
   multiply by 100000; if in crores, multiply by 10000000. State nothing else.
2. Every figure needs a citation: the source file name, the page number (PDFs) or
   sheet name (workbooks), and the row label EXACTLY as printed in the document.
3. If a line item is not present in the document, return null for it. Do NOT
   estimate it, do NOT derive it, do NOT carry it over from another year.
4. Do not compute totals or subtotals yourself. Only report numbers that are
   printed in the document.
5. Copy the period label exactly as printed (e.g. "FY23", "2022-23", "March 2023").
"""


def _prompt(doc: ParsedDoc, what: str, focus: str = "") -> str:
    return (
        f"You are extracting a {what} from a business loan file.\n"
        f"Source file: {doc.filename}\n{RULES}\n{focus}\n"
        f"--- DOCUMENT START ---\n{doc.as_prompt()}\n--- DOCUMENT END ---\n"
        "Return JSON matching the schema."
    )


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def reject_uncited(model: BaseModel, doc: ParsedDoc) -> tuple[BaseModel, list[str]]:
    """Drop any Figure whose citation does not resolve against the document.

    A figure with unverifiable provenance is worse than a missing figure: it
    looks trustworthy. So it is removed and reported, never kept with a caveat.
    """
    pages_by_num = {p.number: p for p in doc.pages if p.number is not None}
    pages_by_sheet = {p.sheet: p for p in doc.pages if p.sheet is not None}
    rejections: list[str] = []

    for name, value in list(model):
        if not isinstance(value, Figure):
            continue
        cite = value.citation
        reason = None

        if _normalise(cite.source_file) != _normalise(doc.filename):
            reason = f"cites '{cite.source_file}' but was extracted from '{doc.filename}'"
        else:
            page = pages_by_num.get(cite.page) if cite.page is not None else pages_by_sheet.get(cite.sheet)
            if page is None:
                reason = f"cites a {'page' if cite.page else 'sheet'} that does not exist in the document"
            elif _normalise(cite.row_label) not in _normalise(page.as_prompt_block()):
                reason = f"row label '{cite.row_label}' does not appear on {page.label}"

        if reason:
            rejections.append(f"{name}: {reason} -- figure dropped")
            setattr(model, name, None)

    return model, rejections


def _completeness(model: BaseModel, fields: tuple[str, ...]) -> float:
    return sum(1 for f in fields if getattr(model, f, None) is not None) / len(fields)


def _extract_one(
    backend: LLMBackend, doc: ParsedDoc, schema: type[BaseModel], what: str, core: tuple[str, ...]
) -> tuple[BaseModel, list[str]]:
    """Extract, verify provenance, and retry once with a targeted prompt."""
    result = backend.complete(_prompt(doc, what), schema)
    result, rejections = reject_uncited(result, doc)

    if _completeness(result, core) < 0.5:
        missing = [f for f in core if getattr(result, f, None) is None]
        log.info("%s: low completeness, retrying for %s", doc.filename, missing)
        focus = (
            "Your previous attempt could not locate these line items: "
            + ", ".join(missing)
            + ". Look again, including inside tables and continuation pages. "
              "If a line item is genuinely absent, leave it null."
        )
        retry = backend.complete(_prompt(doc, what, focus), schema)
        retry, retry_rejections = reject_uncited(retry, doc)
        if _completeness(retry, core) > _completeness(result, core):
            result, rejections = retry, retry_rejections

    return result, rejections


class ExtractionOutcome(BaseModel):
    financials: ExtractedFinancials
    rejected_figures: list[str] = []
    escalations: list[str] = []


def extract_documents(
    backend: LLMBackend,
    docs: list[tuple[ParsedDoc, DocType]],
    borrower_name: str | None = None,
    facility_requested: str | None = None,
) -> ExtractionOutcome:
    fin = ExtractedFinancials(borrower_name=borrower_name, facility_requested=facility_requested)
    rejected: list[str] = []
    escalations: list[str] = []

    for doc, dtype in docs:
        try:
            if dtype is DocType.BALANCE_SHEET:
                bs, rej = _extract_one(backend, doc, BalanceSheet, "balance sheet", CORE_BS_FIELDS)
                fin.balance_sheets.append(bs)
            elif dtype is DocType.PROFIT_AND_LOSS:
                pl, rej = _extract_one(backend, doc, ProfitAndLoss, "profit & loss statement", CORE_PL_FIELDS)
                fin.profit_and_loss.append(pl)
                # Repayment obligations are printed alongside the P&L, and DSCR
                # is meaningless without them -- so pull them from the same doc.
                debt, debt_rej = _extract_one(
                    backend,
                    doc,
                    DebtService,
                    "debt service note (principal repayment of term loans during the year)",
                    ("principal_repayment",),
                )
                debt.period_label = debt.period_label or pl.period.label
                fin.debt_service.append(debt)
                rej = rej + debt_rej
            elif dtype is DocType.BANK_STATEMENT:
                bsum, rej = _extract_one(
                    backend,
                    doc,
                    BankStatementSummary,
                    "bank statement summary (balances, credits, debits, cheque returns)",
                    ("average_monthly_balance", "total_credits"),
                )
                fin.bank_statements.append(bsum)
            else:
                continue
        except Exception as exc:  # noqa: BLE001 - extraction failed -- escalate, don't guess
            escalations.append(f"{doc.filename}: extraction failed ({exc}). Human review required.")
            continue
        rejected.extend(f"{doc.filename} -> {r}" for r in rej)

    if not fin.balance_sheets:
        escalations.append("No balance sheet could be extracted -- leverage and liquidity ratios are unavailable.")
    if not fin.profit_and_loss:
        escalations.append("No profit & loss could be extracted -- coverage and profitability ratios are unavailable.")

    return ExtractionOutcome(financials=fin, rejected_figures=rejected, escalations=escalations)


def statement_date(model: BalanceSheet | ProfitAndLoss) -> date | None:
    """Period end date, from the explicit date or inferred from an FY label."""
    if model.period.end_date:
        return model.period.end_date
    fy = model.period.fy
    if fy and fy.startswith("FY"):
        return date(2000 + int(fy[2:]), 3, 31)
    return None
