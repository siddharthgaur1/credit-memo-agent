"""Follow-up Q&A over a processed case.

The model only ever sees the extracted, cited state -- never the raw documents
and never anything it could confuse for general knowledge. If the answer is not
in that state, the correct answer is "not in the submitted documents".
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from .llm.backend import LLMBackend
from .persistence.store import CaseRecord


class Answer(BaseModel):
    answer: str
    citations: list[str] = Field(default_factory=list)
    answered_from_documents: bool


QA_PROMPT = """You are answering a credit officer's question about a loan file.

You may use ONLY the case state below. It contains the documents' extracted
figures with citations, the checklist status, the computed ratios with their
formulas, and the risk flags.

Hard rules:
- If the answer is not present in the case state, set answered_from_documents to
  false and answer exactly: "Not in the submitted documents."
- Never compute a new ratio or total. Quote the computed values as given.
- Cite the source for every figure you quote, using the citation strings present
  in the case state.
- Do not recommend approval or rejection.

CASE STATE (JSON):
{state}

QUESTION: {question}
"""

# Trim the state so a large case still fits a local model's context window.
MAX_STATE_CHARS = 40_000


def _context(record: CaseRecord) -> str:
    payload = {
        "borrower_name": record.borrower_name,
        "facility_requested": record.facility_requested,
        "checklist": record.checklist,
        "financials": record.financials,
        "ratios": record.analysis,
        "risk_flags": record.flags,
        "verification_findings": record.verification,
        "open_escalations": record.escalations,
    }
    return json.dumps(payload, default=str)[:MAX_STATE_CHARS]


def ask(backend: LLMBackend, record: CaseRecord, question: str) -> Answer:
    return backend.complete(QA_PROMPT.format(state=_context(record), question=question), Answer)
