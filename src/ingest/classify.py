"""Document classification from filename + content signals.

Deterministic keyword scoring, not an LLM call: it is cheap, auditable, and a
misclassification here silently poisons everything downstream. When the signal
is weak we return UNKNOWN and let the caller ask the user.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from .parsers import ParsedDoc


class DocType(str, Enum):
    BALANCE_SHEET = "balance_sheet"
    PROFIT_AND_LOSS = "profit_and_loss"
    BANK_STATEMENT = "bank_statement"
    GST_RETURN = "gst_return"
    ITR = "itr"
    SANCTION_LETTER = "sanction_letter"
    KYC = "kyc"
    PROJECT_REPORT = "project_report"
    OTHER = "other"
    UNKNOWN = "unknown"


# (doc type, filename keywords, content keywords). Content keywords are the
# phrases these documents actually print, not synonyms we wish they used.
SIGNALS: list[tuple[DocType, tuple[str, ...], tuple[str, ...]]] = [
    (
        DocType.BALANCE_SHEET,
        ("balance", "bs", "balancesheet"),
        ("balance sheet", "shareholders funds", "shareholder's funds", "total equity and liabilities",
         "non-current assets", "current liabilities", "reserves and surplus"),
    ),
    (
        DocType.PROFIT_AND_LOSS,
        ("p&l", "pnl", "profit", "loss", "income"),
        ("profit and loss", "statement of profit", "revenue from operations", "profit before tax",
         "profit after tax", "other income", "finance cost"),
    ),
    (
        DocType.BANK_STATEMENT,
        ("bank", "statement", "account"),
        ("account statement", "closing balance", "withdrawal", "deposit", "ifsc", "cheque return",
         "narration", "value date"),
    ),
    (
        DocType.GST_RETURN,
        ("gst", "gstr", "3b", "gstr1"),
        ("gstr", "gstin", "goods and services tax", "outward supplies", "taxable value"),
    ),
    (
        DocType.ITR,
        ("itr", "return", "acknowledgement"),
        ("income tax return", "acknowledgement number", "assessment year", "pan of the assessee",
         "gross total income"),
    ),
    (
        DocType.SANCTION_LETTER,
        ("sanction", "facility", "loan"),
        ("sanction letter", "sanctioned amount", "terms and conditions of sanction", "emi",
         "repayment schedule", "rate of interest"),
    ),
    (
        DocType.KYC,
        ("kyc", "pan", "aadhaar", "aadhar", "udyam", "incorporation"),
        ("permanent account number", "aadhaar", "certificate of incorporation", "udyam registration",
         "proof of address"),
    ),
    (
        DocType.PROJECT_REPORT,
        ("project", "quotation", "quote", "proforma", "invoice", "cma"),
        ("project report", "proforma invoice", "quotation", "machinery", "means of finance",
         "cost of project"),
    ),
]

FILENAME_WEIGHT = 2
CONTENT_WEIGHT = 3
MIN_SCORE = 5  # below this the evidence is too thin to act on


class Classification(BaseModel):
    doc_type: DocType
    confidence: float  # 0..1, share of the winning score over all scores
    evidence: list[str]
    question: str | None = None  # set when we need the user to disambiguate


def classify(doc: ParsedDoc) -> Classification:
    name = doc.filename.lower()
    body = "\n".join(p.as_prompt_block() for p in doc.pages).lower()[:200_000]

    scores: dict[DocType, int] = {}
    evidence: dict[DocType, list[str]] = {}
    for dtype, name_kws, content_kws in SIGNALS:
        score = 0
        hits: list[str] = []
        for kw in name_kws:
            if kw in name:
                score += FILENAME_WEIGHT
                hits.append(f"filename contains '{kw}'")
        for kw in content_kws:
            if kw in body:
                score += CONTENT_WEIGHT
                hits.append(f"content contains '{kw}'")
        if score:
            scores[dtype] = score
            evidence[dtype] = hits

    if not scores:
        return Classification(
            doc_type=DocType.UNKNOWN,
            confidence=0.0,
            evidence=[],
            question=f"What kind of document is '{doc.filename}'? No recognisable signals found.",
        )

    best = max(scores, key=lambda d: scores[d])
    total = sum(scores.values())
    confidence = scores[best] / total

    if scores[best] < MIN_SCORE:
        runners = ", ".join(d.value for d in sorted(scores, key=lambda d: -scores[d])[:3])
        return Classification(
            doc_type=DocType.UNKNOWN,
            confidence=confidence,
            evidence=evidence[best],
            question=f"'{doc.filename}' is ambiguous (weak match; candidates: {runners}). What is it?",
        )

    return Classification(doc_type=best, confidence=confidence, evidence=evidence[best])
