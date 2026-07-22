"""Full LangGraph pipeline over the synthetic files, with a stubbed LLM.

The stub reads the same document text a real model would be given and returns
schema-shaped output with citations. That keeps the test hermetic (no Ollama, no
key, no network) while still exercising ingestion, extraction, provenance
checking, verification, the ratio engine, risk rules, memo and reviewer.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sample_data"))

from generate import HEALTHY, INCOMPLETE, STRESSED, generate  # noqa: E402

from src.agents.graph import run_case  # noqa: E402
from src.config import Settings  # noqa: E402
from src.extract.schemas import normalise_period  # noqa: E402
from src.llm.backend import LLMBackend  # noqa: E402

LAKH = 100_000.0

BS_LABELS = {
    "share_capital": "Share capital",
    "reserves_and_surplus": "Reserves and surplus",
    "net_worth": "Net worth",
    "long_term_borrowings": "Long-term borrowings",
    "other_non_current_liabilities": "Other non-current liabilities",
    "short_term_borrowings": "Short-term borrowings",
    "trade_payables": "Trade payables",
    "other_current_liabilities": "Other current liabilities",
    "total_current_liabilities": "Total current liabilities",
    "net_fixed_assets": "Net fixed assets",
    "other_non_current_assets": "Other non-current assets",
    "inventory": "Inventories",
    "trade_receivables": "Trade receivables",
    "cash_and_bank": "Cash and bank balances",
    "other_current_assets": "Other current assets",
    "total_current_assets": "Total current assets",
    "total_assets": "Total assets",
}

PL_LABELS = {
    "revenue": "Revenue from operations",
    "cost_of_goods_sold": "Cost of goods sold",
    "gross_profit": "Gross profit",
    "employee_cost": "Employee benefit expense",
    "other_expenses": "Other expenses",
    "ebitda": "EBITDA",
    "other_income": "Other income",
    "depreciation": "Depreciation and amortisation",
    "finance_cost": "Finance costs",
    "profit_before_tax": "Profit before tax",
    "tax": "Tax expense",
    "profit_after_tax": "Profit after tax",
}

BANK_LABELS = {
    "average_monthly_balance": "Average monthly balance",
    "minimum_balance": "Minimum balance during period",
    "total_credits": "Total credits",
    "total_debits": "Total debits",
    "inward_cheque_returns": "Inward cheque returns (own cheques returned unpaid)",
    "outward_cheque_returns": "Outward cheque returns (deposited cheques returned)",
    "sanctioned_limit": "Sanctioned limit",
    "peak_utilisation": "Peak utilisation",
}

DEBT_LABELS = {"principal_repayment": "Principal repayment of term loans"}

SCALED = set(BS_LABELS) | set(PL_LABELS) | set(DEBT_LABELS) | {
    "average_monthly_balance", "minimum_balance", "total_credits",
    "total_debits", "sanctioned_limit", "peak_utilisation",
}


def _to_number(raw: str) -> float | None:
    text = raw.strip().replace(",", "")
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    try:
        value = float(text)
    except ValueError:
        return None
    return -value if negative else value


class StubBackend(LLMBackend):
    """Reads the prompt the way a model would, and cites what it actually found."""

    name = "stub"

    def _raw_complete(self, prompt: str, schema):
        if schema.__name__ == "Narrative":
            return json.dumps(
                {"risk_commentary": "Commentary on the flags above.", "trend_commentary": "Trend note."}
            ), {}

        source = re.search(r"Source file: (.+)", prompt).group(1).strip()
        labels = {
            "BalanceSheet": BS_LABELS,
            "ProfitAndLoss": PL_LABELS,
            "BankStatementSummary": BANK_LABELS,
            "DebtService": DEBT_LABELS,
        }[schema.__name__]

        # Headings wrap across lines in a real PDF, so \s (not a literal space).
        period = re.search(r"(?:AS AT|YEAR ENDED)\s+(\d{1,2}\s+\w+\s+\d{4})", prompt)
        label = " ".join(period.group(1).split()) if period else "unknown"

        if schema.__name__ == "DebtService":
            payload: dict = {"period_label": label}
        else:
            payload = {"source_file": source}
            if schema.__name__ != "BankStatementSummary":
                payload["period"] = {"label": label}

        for field, label in labels.items():
            match = re.search(rf"^{re.escape(label)}\t([^\t\n]*)", prompt, re.M)
            if not match:
                continue
            value = _to_number(match.group(1))
            if value is None:
                continue
            payload[field] = {
                "value": value * (LAKH if field in SCALED else 1),
                "citation": self._cite(prompt, label, source),
            }
        return json.dumps(payload), {}

    @staticmethod
    def _cite(prompt: str, label: str, source: str) -> dict:
        """Locate the block the label sits in, so the citation actually resolves."""
        before = prompt[: prompt.index(label)]
        page = re.findall(r"--- page (\d+) ---", before)
        sheet = re.findall(r"--- sheet '([^']+)' ---", before)
        return {
            "source_file": source,
            "page": int(page[-1]) if page else None,
            "sheet": sheet[-1] if sheet and not page else None,
            "row_label": label,
        }


@pytest.fixture(scope="module")
def cases(tmp_path_factory):
    root = tmp_path_factory.mktemp("pipeline")
    settings = Settings(data_dir=root / "data", log_dir=root / "logs")
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    backend = StubBackend(settings)

    out = {}
    for borrower in (HEALTHY, STRESSED, INCOMPLETE):
        folder = generate(borrower, root / "files")
        out[borrower["slug"]] = run_case(
            borrower["slug"],
            sorted(folder.iterdir()),
            borrower_name=borrower["name"],
            facility_requested=borrower["facility"],
            settings=settings,
            backend=backend,
        )
    return out


def test_healthy_case_produces_a_complete_memo_with_every_figure_cited(cases):
    state = cases["suryodaya_engineering"]
    memo = state["memo"]

    assert Path(state["memo_path"]).exists()
    assert memo.citations
    for citation in memo.citations:
        assert re.search(r"(p\.\d+|sheet ')", citation), citation
        assert "row '" in citation

    assert state["review_findings"] == [], state["review_findings"]
    assert not state["verification"], [f.check for f in state["verification"]]

    ratios = {r.name: r for r in state["analysis"].by_period[normalise_period(HEALTHY["years"][-1])]}
    assert ratios["Current ratio"].value == pytest.approx(700 / 430, rel=1e-6)
    assert ratios["Gross margin"].value == pytest.approx(31.0, rel=1e-6)


def test_stressed_case_raises_the_expected_flags(cases):
    flags = {f.rule_id for f in cases["vaishnavi_polymers"]["flags"]}

    assert {"dscr_thin", "interest_cover_thin", "high_leverage", "revenue_declining"} <= flags
    assert "cheque_returns" in flags


def test_incomplete_case_catches_the_broken_balance_sheet(cases):
    state = cases["meenakshi_traders"]

    identity = [f for f in state["verification"] if f.check == "assets = liabilities + equity"]
    assert identity, [f.check for f in state["verification"]]
    assert identity[0].difference == pytest.approx(30 * LAKH)
    assert any("assets = liabilities + equity" in e for e in state["escalations"])


def test_incomplete_case_reports_insufficient_data_not_an_invented_figure(cases):
    state = cases["meenakshi_traders"]
    latest = normalise_period(INCOMPLETE["years"][-1])
    ratios = {r.name: r for r in state["analysis"].by_period[latest]}

    assert ratios["DSCR"].value is None
    assert "Depreciation" in ratios["DSCR"].missing_inputs
    assert any("DSCR" in q for q in state["memo"].open_questions)

    # Nothing anywhere in the memo silently stands in for the missing figure.
    summary = next(s for s in state["memo"].sections if s.heading == "Financial summary")
    assert summary.table


def test_incomplete_case_lists_the_missing_documents(cases):
    gaps = {g.id for g in cases["meenakshi_traders"]["checklist_report"].gaps}
    assert {"gst_returns", "audited_financials"} <= gaps


def test_no_case_produces_an_approve_or_reject_verdict(cases):
    for state in cases.values():
        prose = " ".join(s.body.lower() for s in state["memo"].sections)
        for phrase in ("we recommend", "approve the facility", "credit score", "reject the"):
            assert phrase not in prose
        assert state["memo"].backend == "stub"
