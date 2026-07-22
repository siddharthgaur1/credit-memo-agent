"""Deterministic ratio engine.

Every number in the memo is produced here, in plain Python. The LLM explains
these numbers; it never computes one. An LLM arithmetic slip inside a credit
memo is a catastrophic failure mode, so the arithmetic simply isn't its job.

Each result carries the formula and the exact inputs used, so a credit officer
can re-derive it by hand in the memo without opening this file.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from ..extract.schemas import ExtractedFinancials, val

DAYS_IN_YEAR = 365


class Completeness(str, Enum):
    COMPLETE = "complete"
    INSUFFICIENT_DATA = "insufficient_data"
    UNDEFINED = "undefined"  # inputs present but the ratio is not defined (zero denominator)


class RatioResult(BaseModel):
    name: str
    category: str
    period: str
    value: float | None
    unit: str  # "x" | "%" | "days"
    formula: str
    inputs: dict[str, float | None]
    data_completeness: Completeness
    missing_inputs: list[str] = Field(default_factory=list)
    note: str | None = None

    @property
    def display(self) -> str:
        if self.value is None:
            return "insufficient data"
        if self.unit == "%":
            return f"{self.value:.1f}%"
        if self.unit == "days":
            return f"{self.value:.0f} days"
        if self.unit == "INR":
            return f"Rs {self.value:,.0f}"
        if self.unit == "count":
            return f"{self.value:.0f}"
        return f"{self.value:.2f}x"


def _compute(
    *,
    name: str,
    category: str,
    period: str,
    unit: str,
    formula: str,
    inputs: dict[str, float | None],
    fn,
) -> RatioResult:
    """Build a result, refusing to compute on incomplete inputs.

    `fn` receives the inputs dict only when every value is known, so no ratio can
    ever be produced from an assumed or defaulted figure.
    """
    missing = [k for k, v in inputs.items() if v is None]
    if missing:
        return RatioResult(
            name=name,
            category=category,
            period=period,
            value=None,
            unit=unit,
            formula=formula,
            inputs=inputs,
            data_completeness=Completeness.INSUFFICIENT_DATA,
            missing_inputs=missing,
            note=f"Not computed: {', '.join(missing)} not found in the submitted documents.",
        )
    try:
        value = fn(inputs)
    except ZeroDivisionError:
        return RatioResult(
            name=name,
            category=category,
            period=period,
            value=None,
            unit=unit,
            formula=formula,
            inputs=inputs,
            data_completeness=Completeness.UNDEFINED,
            note="Not computed: denominator is zero.",
        )
    return RatioResult(
        name=name,
        category=category,
        period=period,
        value=value,
        unit=unit,
        formula=formula,
        inputs=inputs,
        data_completeness=Completeness.COMPLETE,
    )


def compute_period(fin: ExtractedFinancials, fy: str) -> list[RatioResult]:
    """All ratios for one reporting period."""
    bs = fin.bs_for(fy)
    pl = fin.pl_for(fy)
    ds = fin.debt_for(fy)
    out: list[RatioResult] = []

    g = lambda obj, attr: None if obj is None else val(getattr(obj, attr))  # noqa: E731

    revenue = g(pl, "revenue")
    cogs = g(pl, "cost_of_goods_sold")
    gross_profit = g(pl, "gross_profit")
    ebitda = g(pl, "ebitda")
    depreciation = g(pl, "depreciation")
    finance_cost = g(pl, "finance_cost")
    pat = g(pl, "profit_after_tax")

    net_worth = g(bs, "net_worth")
    if net_worth is None and bs is not None:
        sc, rs = val(bs.share_capital), val(bs.reserves_and_surplus)
        net_worth = None if sc is None or rs is None else sc + rs

    ltb = g(bs, "long_term_borrowings")
    stb = g(bs, "short_term_borrowings")
    total_debt = None if ltb is None or stb is None else ltb + stb
    total_assets = g(bs, "total_assets")
    tca = g(bs, "total_current_assets")
    tcl = g(bs, "total_current_liabilities")
    inventory = g(bs, "inventory")
    receivables = g(bs, "trade_receivables")
    payables = g(bs, "trade_payables")
    intangibles = val(bs.intangible_assets) if bs is not None and bs.intangible_assets else 0.0

    principal = None if ds is None else val(ds.principal_repayment)
    proposed_emi = None if ds is None else val(ds.proposed_emi_annual)

    # --- Coverage -----------------------------------------------------------
    out.append(
        _compute(
            name="DSCR",
            category="Coverage",
            period=fy,
            unit="x",
            formula=(
                "(PAT + Depreciation + Finance cost) / (Finance cost + Principal repayment + Proposed EMI). "
                "Proposed EMI is taken as nil when the proposal does not supply one."
            ),
            inputs={
                "PAT": pat,
                "Depreciation": depreciation,
                "Finance cost": finance_cost,
                "Principal repayment": principal,
                "Proposed EMI (annual)": proposed_emi if proposed_emi is not None else 0.0,
            },
            fn=lambda i: (i["PAT"] + i["Depreciation"] + i["Finance cost"])
            / _nz(i["Finance cost"] + i["Principal repayment"] + i["Proposed EMI (annual)"]),
        )
    )
    out.append(
        _compute(
            name="Interest coverage",
            category="Coverage",
            period=fy,
            unit="x",
            formula="EBITDA / Finance cost",
            inputs={"EBITDA": ebitda, "Finance cost": finance_cost},
            fn=lambda i: i["EBITDA"] / _nz(i["Finance cost"]),
        )
    )

    # --- Leverage -----------------------------------------------------------
    out.append(
        _compute(
            name="Debt / equity",
            category="Leverage",
            period=fy,
            unit="x",
            formula="(Long-term borrowings + Short-term borrowings) / Net worth",
            inputs={"Total debt": total_debt, "Net worth": net_worth},
            fn=lambda i: i["Total debt"] / _nz(i["Net worth"]),
        )
    )
    out.append(
        _compute(
            name="TOL / TNW",
            category="Leverage",
            period=fy,
            unit="x",
            formula="(Total assets - Net worth) / (Net worth - Intangible assets)",
            inputs={"Total assets": total_assets, "Net worth": net_worth, "Intangible assets": intangibles},
            fn=lambda i: (i["Total assets"] - i["Net worth"]) / _nz(i["Net worth"] - i["Intangible assets"]),
        )
    )

    # --- Liquidity ----------------------------------------------------------
    out.append(
        _compute(
            name="Current ratio",
            category="Liquidity",
            period=fy,
            unit="x",
            formula="Total current assets / Total current liabilities",
            inputs={"Total current assets": tca, "Total current liabilities": tcl},
            fn=lambda i: i["Total current assets"] / _nz(i["Total current liabilities"]),
        )
    )
    out.append(
        _compute(
            name="Quick ratio",
            category="Liquidity",
            period=fy,
            unit="x",
            formula="(Total current assets - Inventory) / Total current liabilities",
            inputs={"Total current assets": tca, "Inventory": inventory, "Total current liabilities": tcl},
            fn=lambda i: (i["Total current assets"] - i["Inventory"]) / _nz(i["Total current liabilities"]),
        )
    )

    # --- Profitability ------------------------------------------------------
    out.append(
        _compute(
            name="Gross margin",
            category="Profitability",
            period=fy,
            unit="%",
            formula="Gross profit / Revenue x 100",
            inputs={"Gross profit": gross_profit, "Revenue": revenue},
            fn=lambda i: i["Gross profit"] / _nz(i["Revenue"]) * 100,
        )
    )
    out.append(
        _compute(
            name="Net margin",
            category="Profitability",
            period=fy,
            unit="%",
            formula="PAT / Revenue x 100",
            inputs={"PAT": pat, "Revenue": revenue},
            fn=lambda i: i["PAT"] / _nz(i["Revenue"]) * 100,
        )
    )
    out.append(
        _compute(
            name="ROCE",
            category="Profitability",
            period=fy,
            unit="%",
            formula="(EBITDA - Depreciation) / (Net worth + Long-term borrowings) x 100",
            inputs={
                "EBITDA": ebitda,
                "Depreciation": depreciation,
                "Net worth": net_worth,
                "Long-term borrowings": ltb,
            },
            fn=lambda i: (i["EBITDA"] - i["Depreciation"])
            / _nz(i["Net worth"] + i["Long-term borrowings"])
            * 100,
        )
    )

    # --- Efficiency ---------------------------------------------------------
    out.append(
        _compute(
            name="Inventory days",
            category="Efficiency",
            period=fy,
            unit="days",
            formula="Inventory / Cost of goods sold x 365",
            inputs={"Inventory": inventory, "Cost of goods sold": cogs},
            fn=lambda i: i["Inventory"] / _nz(i["Cost of goods sold"]) * DAYS_IN_YEAR,
        )
    )
    out.append(
        _compute(
            name="Receivable days",
            category="Efficiency",
            period=fy,
            unit="days",
            formula="Trade receivables / Revenue x 365",
            inputs={"Trade receivables": receivables, "Revenue": revenue},
            fn=lambda i: i["Trade receivables"] / _nz(i["Revenue"]) * DAYS_IN_YEAR,
        )
    )
    out.append(
        _compute(
            name="Payable days",
            category="Efficiency",
            period=fy,
            unit="days",
            formula="Trade payables / Cost of goods sold x 365",
            inputs={"Trade payables": payables, "Cost of goods sold": cogs},
            fn=lambda i: i["Trade payables"] / _nz(i["Cost of goods sold"]) * DAYS_IN_YEAR,
        )
    )
    out.append(
        _compute(
            name="Working capital cycle",
            category="Efficiency",
            period=fy,
            unit="days",
            formula="Inventory days + Receivable days - Payable days",
            inputs={
                "Inventory days": _lookup(out, "Inventory days"),
                "Receivable days": _lookup(out, "Receivable days"),
                "Payable days": _lookup(out, "Payable days"),
            },
            fn=lambda i: i["Inventory days"] + i["Receivable days"] - i["Payable days"],
        )
    )
    return out


def _nz(x: float) -> float:
    if x == 0:
        raise ZeroDivisionError
    return x


def _lookup(results: list[RatioResult], name: str) -> float | None:
    return next((r.value for r in results if r.name == name), None)


class Trend(BaseModel):
    metric: str
    unit: str
    series: dict[str, float | None]  # period -> value
    direction: str  # improving | deteriorating | flat | insufficient data
    change: float | None = None  # last minus first, in the metric's own unit
    change_pct: float | None = None
    note: str | None = None


# For these, a lower number is better; everything else is "higher is better".
LOWER_IS_BETTER = {
    "Debt / equity",
    "TOL / TNW",
    "Inventory days",
    "Receivable days",
    "Payable days",
    "Working capital cycle",
}
FLAT_BAND_PCT = 5.0


def trends(by_period: dict[str, list[RatioResult]], extra: dict[str, dict[str, float | None]] | None = None) -> list[Trend]:
    """Direction and magnitude for each metric across the available periods."""
    periods = sorted(by_period)
    series_by_metric: dict[str, tuple[str, dict[str, float | None]]] = {}
    for p in periods:
        for r in by_period[p]:
            unit, series = series_by_metric.setdefault(r.name, (r.unit, {}))
            series[p] = r.value
    for metric, values in (extra or {}).items():
        series_by_metric[metric] = ("INR", values)

    out: list[Trend] = []
    for metric, (unit, series) in series_by_metric.items():
        known = [(p, v) for p, v in sorted(series.items()) if v is not None]
        if len(known) < 2:
            out.append(
                Trend(
                    metric=metric,
                    unit=unit,
                    series=series,
                    direction="insufficient data",
                    note="Fewer than two periods with a computable value.",
                )
            )
            continue
        first, last = known[0][1], known[-1][1]
        change = last - first
        change_pct = None if first == 0 else change / abs(first) * 100
        if change_pct is not None and abs(change_pct) < FLAT_BAND_PCT:
            direction = "flat"
        else:
            better = change < 0 if metric in LOWER_IS_BETTER else change > 0
            direction = "improving" if better else "deteriorating"
        out.append(
            Trend(
                metric=metric,
                unit=unit,
                series=series,
                direction=direction,
                change=change,
                change_pct=change_pct,
                note=f"{known[0][0]} -> {known[-1][0]}",
            )
        )
    return out


def bank_signals(fin: ExtractedFinancials) -> list[RatioResult]:
    """Conduct signals lifted straight from the bank statement summaries."""
    out: list[RatioResult] = []
    for bsum in fin.bank_statements:
        tag = f"{bsum.account_label}"
        out.append(
            _compute(
                name="Average monthly balance",
                category="Bank conduct",
                period=tag,
                unit="INR",
                formula="As summarised from the bank statement",
                inputs={"Average monthly balance": val(bsum.average_monthly_balance)},
                fn=lambda i: i["Average monthly balance"],
            )
        )
        out.append(
            _compute(
                name="Inward cheque returns",
                category="Bank conduct",
                period=tag,
                unit="count",
                formula="Count of the borrower's own cheques/mandates returned unpaid",
                inputs={"Inward cheque returns": val(bsum.inward_cheque_returns)},
                fn=lambda i: i["Inward cheque returns"],
            )
        )
        out.append(
            _compute(
                name="Peak limit utilisation",
                category="Bank conduct",
                period=tag,
                unit="%",
                formula="Peak utilisation / Sanctioned limit x 100",
                inputs={
                    "Peak utilisation": val(bsum.peak_utilisation),
                    "Sanctioned limit": val(bsum.sanctioned_limit),
                },
                fn=lambda i: i["Peak utilisation"] / _nz(i["Sanctioned limit"]) * 100,
            )
        )
    return out


class AnalysisResult(BaseModel):
    by_period: dict[str, list[RatioResult]]
    trends: list[Trend]
    bank: list[RatioResult]

    def ratio(self, name: str, period: str) -> RatioResult | None:
        return next((r for r in self.by_period.get(period, []) if r.name == name), None)

    def latest_period(self) -> str | None:
        return sorted(self.by_period)[-1] if self.by_period else None


def compute_all(fin: ExtractedFinancials) -> AnalysisResult:
    by_period = {fy: compute_period(fin, fy) for fy in fin.periods()}
    revenue_series = {
        fy: (val(pl.revenue) if (pl := fin.pl_for(fy)) else None) for fy in by_period
    }
    pat_series = {fy: (val(pl.profit_after_tax) if (pl := fin.pl_for(fy)) else None) for fy in by_period}
    return AnalysisResult(
        by_period=by_period,
        trends=trends(by_period, extra={"Revenue": revenue_series, "PAT": pat_series}),
        bank=bank_signals(fin),
    )
