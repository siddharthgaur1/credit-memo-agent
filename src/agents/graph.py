"""LangGraph pipeline.

Intake -> Checklist -> Extraction -> Analysis -> Risk narrative -> Memo -> Review.

Two things the graph deliberately does NOT do: compute a ratio (that is the
deterministic engine in src/analysis) and paper over a failed check (it stops and
asks a specific question instead).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Any, TypedDict

from pydantic import BaseModel

from ..analysis.ratios import Completeness, compute_all
from ..analysis.risk import RiskFlag, evaluate_risk, load_rules
from ..checklist.validate import SubmittedDoc, load_checklist, validate
from ..config import Settings, get_settings
from ..extract.extractor import extract_documents
from ..extract.verify import Severity, verify_financials
from ..ingest.classify import DocType, classify
from ..ingest.parsers import ScannedPDFError, parse_file
from ..llm.backend import LLMBackend, get_backend
from ..memo.compose import build_memo, write_docx

log = logging.getLogger("credit_memo.agents")


def _extend(a: list, b: list) -> list:
    return (a or []) + (b or [])


class CaseState(TypedDict, total=False):
    case_id: str
    files: list[str]
    borrower_name: str | None
    facility_requested: str | None
    out_dir: str

    parsed: list[Any]
    classifications: list[dict]
    checklist_report: Any
    financials: Any
    verification: list[Any]
    analysis: Any
    flags: list[RiskFlag]
    narrative: str
    trend_commentary: str
    memo: Any
    memo_path: str | None

    settings: Any  # Settings
    backend: Any  # LLMBackend

    escalations: Annotated[list[str], _extend]
    review_findings: Annotated[list[str], _extend]
    progress: Annotated[list[str], _extend]


class Narrative(BaseModel):
    """What the LLM is allowed to produce: prose about numbers it was given."""

    risk_commentary: str
    trend_commentary: str


NARRATIVE_PROMPT = """You are drafting commentary for a bank credit memo.

You are given risk flags and trends that have ALREADY been computed
deterministically. Your job is to explain them in plain English for a credit
officer, citing the exact figures you were given.

Hard rules:
- Do not compute, re-derive, round or restate any number differently from how it
  is given to you. Quote figures exactly as supplied.
- Do not introduce any figure that is not listed below.
- Do not recommend approval or rejection, do not assign a rating or score, and do
  not speculate about the borrower's intentions.
- If nothing was flagged, say so plainly rather than manufacturing concern.

RISK FLAGS:
{flags}

TRENDS:
{trends}

Write two things: risk_commentary (one short paragraph per flag, grouped by
severity) and trend_commentary (one paragraph covering direction and magnitude
of the key metrics)."""


def _settings_and_backend(state: CaseState) -> tuple[Settings, LLMBackend]:
    """Settings and backend travel in the state, not in a module-level singleton.

    That is what lets the tests drive the whole graph against a stub backend with
    no model installed, and what keeps a case pinned to the backend it started on.
    """
    settings: Settings = state.get("settings") or get_settings()
    backend: LLMBackend = state.get("backend") or get_backend(settings)
    return settings, backend


# --------------------------------------------------------------------------- #
# Nodes
# --------------------------------------------------------------------------- #


def intake_agent(state: CaseState) -> CaseState:
    parsed, classifications, escalations = [], [], []
    for raw in state["files"]:
        path = Path(raw)
        try:
            doc = parse_file(path)
        except ScannedPDFError as exc:
            escalations.append(str(exc))
            continue
        except Exception as exc:  # noqa: BLE001 - an unreadable doc is escalated, not fatal
            escalations.append(f"{path.name}: could not be read ({exc}).")
            continue

        result = classify(doc)
        if result.question:
            escalations.append(result.question)
        parsed.append((doc, result.doc_type))
        classifications.append(
            {
                "filename": doc.filename,
                "doc_type": result.doc_type.value,
                "confidence": round(result.confidence, 2),
                "evidence": result.evidence[:4],
            }
        )

    return {
        "parsed": parsed,
        "classifications": classifications,
        "escalations": escalations,
        "progress": [f"Intake: read {len(parsed)} file(s), {len(escalations)} issue(s)."],
    }


def checklist_agent(state: CaseState) -> CaseState:
    settings, _ = _settings_and_backend(state)
    checklist = load_checklist(settings.checklist_path)
    submitted = [
        SubmittedDoc(filename=doc.filename, doc_type=dtype, as_of=_doc_date(doc, dtype))
        for doc, dtype in state.get("parsed", [])
    ]
    report = validate(checklist, submitted)
    return {
        "checklist_report": report,
        "progress": [
            f"Checklist: {len(report.gaps)} gap(s) of {len(report.results)} requirement(s)."
        ],
    }


def _doc_date(doc, dtype: DocType):
    """The period the document speaks about, for staleness.

    Financial statements are dated by their financial year; everything else by
    the newest date printed on it. Never the file's mtime -- copying a file does
    not make it fresh.
    """
    from datetime import date as _date

    from ..extract.schemas import normalise_period
    from ..ingest.parsers import latest_date_in

    text = "\n".join(p.as_prompt_block() for p in doc.pages)[:4000]

    if dtype in (DocType.BALANCE_SHEET, DocType.PROFIT_AND_LOSS):
        fy = normalise_period(text) or normalise_period(doc.filename)
        if fy:
            return _date(2000 + int(fy[2:]), 3, 31)

    return latest_date_in(text)


def extraction_agent(state: CaseState) -> CaseState:
    _, backend = _settings_and_backend(state)
    outcome = extract_documents(
        backend,
        state.get("parsed", []),
        borrower_name=state.get("borrower_name"),
        facility_requested=state.get("facility_requested"),
    )
    findings = verify_financials(outcome.financials)
    critical = [f for f in findings if f.severity is Severity.CRITICAL]
    escalations = list(outcome.escalations)
    escalations += [f"{f.check} failed on {f.statement}: {f.detail}" for f in critical]
    escalations += [f"Uncited figure rejected -- {r}" for r in outcome.rejected_figures]

    return {
        "financials": outcome.financials,
        "verification": findings,
        "escalations": escalations,
        "progress": [
            (f"Extraction: {len(outcome.financials.balance_sheets)} balance sheet(s), "
            f"{len(outcome.financials.profit_and_loss)} P&L(s), "
            f"{len(findings)} verification finding(s).")
        ],
    }


def analysis_agent(state: CaseState) -> CaseState:
    """Invokes the deterministic engine. Computes nothing itself."""
    settings, _ = _settings_and_backend(state)
    fin = state["financials"]
    analysis = compute_all(fin)
    flags = evaluate_risk(load_rules(settings.risk_rules_path), analysis, fin)
    return {
        "analysis": analysis,
        "flags": flags,
        "progress": [f"Analysis: {sum(len(v) for v in analysis.by_period.values())} ratios, {len(flags)} flag(s)."],
    }


def risk_narrative_agent(state: CaseState) -> CaseState:
    _, backend = _settings_and_backend(state)
    flags = state.get("flags", [])
    analysis = state["analysis"]

    flag_text = "\n".join(
        f"- [{f.severity.value.upper()}] {f.label} ({f.period}): {f.message} | evidence: {f.evidence}"
        for f in flags
    ) or "None fired."
    trend_text = "\n".join(
        f"- {t.metric}: {t.direction}"
        + (f", change {t.change_pct:.1f}% ({t.note})" if t.change_pct is not None else " (insufficient data)")
        for t in analysis.trends
    ) or "No trends computable."

    try:
        narrative = backend.complete(
            NARRATIVE_PROMPT.format(flags=flag_text, trends=trend_text), Narrative
        )
        return {
            "narrative": narrative.risk_commentary,
            "trend_commentary": narrative.trend_commentary,
            "progress": ["Narrative: drafted."],
        }
    except Exception as exc:  # noqa: BLE001
        # Prose is the only thing the LLM owns here, so losing it degrades the
        # memo rather than invalidating it. Fall back to the raw evidence.
        log.warning("narrative generation failed: %s", exc)
        return {
            "narrative": flag_text,
            "trend_commentary": trend_text,
            "escalations": [f"Narrative generation unavailable ({exc}); raw findings shown instead."],
            "progress": ["Narrative: LLM unavailable, using raw findings."],
        }


def memo_agent(state: CaseState) -> CaseState:
    settings, backend = _settings_and_backend(state)
    memo = build_memo(
        fin=state["financials"],
        checklist=state["checklist_report"],
        analysis=state["analysis"],
        flags=state.get("flags", []),
        verification=state.get("verification", []),
        narrative=state.get("narrative", ""),
        trend_commentary=state.get("trend_commentary", ""),
        backend=backend.name,
        open_questions=state.get("escalations", []),
    )
    out_dir = Path(state.get("out_dir") or (settings.data_dir / state["case_id"]))
    path = write_docx(memo, out_dir / "credit_memo.docx")
    return {"memo": memo, "memo_path": str(path), "progress": [f"Memo: written to {path.name}."]}


def reviewer_agent(state: CaseState) -> CaseState:
    """Deterministic pre-delivery validation. Checks, not opinions."""
    findings: list[str] = []
    fin = state["financials"]
    memo = state["memo"]
    analysis = state["analysis"]

    from ..extract.schemas import Figure

    for group in (fin.balance_sheets, fin.profit_and_loss, fin.bank_statements):
        for stmt in group:
            for name, value in stmt:
                if isinstance(value, Figure) and not value.citation.row_label:
                    findings.append(f"{stmt.source_file}: {name} has no row label -- must not be presented.")

    incomplete = [
        r for rs in analysis.by_period.values() for r in rs if r.data_completeness is not Completeness.COMPLETE
    ]
    ratio_section = next((s for s in memo.sections if s.heading == "Ratio analysis"), None)
    if incomplete and ratio_section and "insufficient data" not in ratio_section.body:
        findings.append("Ratios computed on incomplete data are presented without their caveat.")

    if state.get("verification") and not any(s.heading.startswith("Arithmetic") for s in memo.sections):
        findings.append("Verification findings exist but are absent from the memo.")

    banned = ("we recommend", "approve the facility", "reject the", "credit score", "rating of")
    prose = " ".join(s.body.lower() for s in memo.sections)
    for phrase in banned:
        if phrase in prose:
            findings.append(f"Memo prose contains a decision-like phrase: '{phrase}'.")

    if not memo.citations:
        findings.append("Memo carries no source citations.")

    return {
        "review_findings": findings,
        "progress": [f"Review: {len(findings)} issue(s) before delivery."],
    }


def human_escalation(state: CaseState) -> CaseState:
    """Nothing analysable came out of the file. Stop and ask, don't produce a memo.

    A memo built on no statements would look like a result. It isn't one.
    """
    report = state.get("checklist_report")
    questions = [
        ("No balance sheet or profit & loss statement could be extracted from this file, "
        "so no ratio can be computed. Please confirm which submitted file contains the "
        "audited financials, or supply them.")
    ]
    if report is not None:
        questions += [f"Still outstanding: {r.label} -- {r.reason}" for r in report.gaps]
    return {
        "escalations": questions,
        "progress": ["Escalated to human: nothing analysable in the submitted file."],
    }


def _has_statements(state: CaseState) -> str:
    fin = state.get("financials")
    if fin is not None and (fin.balance_sheets or fin.profit_and_loss):
        return "analysis"
    return "escalate"


def build_graph():
    from langgraph.graph import END, START, StateGraph

    g = StateGraph(CaseState)
    g.add_node("intake", intake_agent)
    g.add_node("checklist", checklist_agent)
    g.add_node("extraction", extraction_agent)
    g.add_node("analysis", analysis_agent)
    g.add_node("narrative", risk_narrative_agent)
    g.add_node("memo", memo_agent)
    g.add_node("review", reviewer_agent)
    g.add_node("escalate", human_escalation)

    g.add_edge(START, "intake")
    g.add_edge("intake", "checklist")
    g.add_edge("checklist", "extraction")
    g.add_conditional_edges("extraction", _has_statements, {"analysis": "analysis", "escalate": "escalate"})
    g.add_edge("escalate", END)
    g.add_edge("analysis", "narrative")
    g.add_edge("narrative", "memo")
    g.add_edge("memo", "review")
    g.add_edge("review", END)
    return g.compile()


def run_case(
    case_id: str,
    files: list[str | Path],
    *,
    borrower_name: str | None = None,
    facility_requested: str | None = None,
    out_dir: str | Path | None = None,
    settings: Settings | None = None,
    backend: LLMBackend | None = None,
) -> CaseState:
    settings = settings or get_settings()
    backend = backend or get_backend(settings)
    graph = build_graph()
    initial: CaseState = {
        "case_id": case_id,
        "files": [str(f) for f in files],
        "borrower_name": borrower_name,
        "facility_requested": facility_requested,
        "out_dir": str(out_dir or (settings.data_dir / case_id)),
        "settings": settings,
        "backend": backend,
    }
    return graph.invoke(initial)
