"""Risk rules driven by editable YAML thresholds.

Every fired flag records the numbers that triggered it, so the officer sees the
evidence rather than a verdict. Rules never fire on missing data -- an unknown
ratio is a completeness gap, not a risk finding.
"""

from __future__ import annotations

import operator as op
from enum import Enum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from ..extract.schemas import ExtractedFinancials, val
from .ratios import AnalysisResult, Completeness, RatioResult

OPERATORS = {"lt": op.lt, "lte": op.le, "gt": op.gt, "gte": op.ge}


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Rule(BaseModel):
    id: str
    type: str  # ratio_threshold | figure_threshold | trend_direction
    label: str
    message: str
    severity: Severity
    metric: str | None = None
    figure: str | None = None
    operator: str | None = None
    threshold: float | None = None
    period: str = "latest"
    direction: str | None = None  # up | down
    min_change_pct: float | None = None


class RuleSet(BaseModel):
    version: int = 1
    rules: list[Rule]


class RiskFlag(BaseModel):
    rule_id: str
    label: str
    severity: Severity
    message: str
    period: str
    evidence: dict[str, float | str | None] = Field(default_factory=dict)


SEVERITY_ORDER = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}


def load_rules(path: Path) -> RuleSet:
    return RuleSet.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def _candidates(analysis: AnalysisResult, rule: Rule) -> list[RatioResult]:
    pool = [r for rs in analysis.by_period.values() for r in rs] + analysis.bank
    matching = [r for r in pool if r.name == rule.metric]
    if rule.period == "latest":
        latest = analysis.latest_period()
        scoped = [r for r in matching if r.period == latest]
        # Bank-conduct metrics are keyed by account, not by FY -- keep them all.
        return scoped or [r for r in analysis.bank if r.name == rule.metric]
    return matching


def evaluate_risk(
    ruleset: RuleSet, analysis: AnalysisResult, fin: ExtractedFinancials
) -> list[RiskFlag]:
    flags: list[RiskFlag] = []

    for rule in ruleset.rules:
        if rule.type == "ratio_threshold":
            fn = OPERATORS[rule.operator or "lt"]
            for r in _candidates(analysis, rule):
                if r.data_completeness is not Completeness.COMPLETE or r.value is None:
                    continue
                if fn(r.value, rule.threshold):
                    flags.append(
                        RiskFlag(
                            rule_id=rule.id,
                            label=rule.label,
                            severity=rule.severity,
                            message=rule.message.format(
                                value=r.display, threshold=rule.threshold, period=r.period
                            ).strip(),
                            period=r.period,
                            evidence={"metric": r.name, "value": r.value, "threshold": rule.threshold,
                                      "formula": r.formula},
                        )
                    )

        elif rule.type == "figure_threshold":
            fn = OPERATORS[rule.operator or "lt"]
            periods = fin.periods()
            targets = periods[-1:] if rule.period == "latest" else periods
            for fy in targets:
                bs = fin.bs_for(fy)
                if bs is None:
                    continue
                value = val(getattr(bs, rule.figure, None))
                if value is None and rule.figure == "net_worth":
                    sc, rs_ = val(bs.share_capital), val(bs.reserves_and_surplus)
                    value = None if sc is None or rs_ is None else sc + rs_
                if value is None:
                    continue
                if fn(value, rule.threshold):
                    flags.append(
                        RiskFlag(
                            rule_id=rule.id,
                            label=rule.label,
                            severity=rule.severity,
                            message=rule.message.format(
                                value=f"Rs {value:,.0f}", threshold=rule.threshold, period=fy
                            ).strip(),
                            period=fy,
                            evidence={"figure": rule.figure, "value": value, "threshold": rule.threshold},
                        )
                    )

        elif rule.type == "trend_direction":
            trend = next((t for t in analysis.trends if t.metric == rule.metric), None)
            if trend is None or trend.change_pct is None:
                continue
            moved_wrong_way = trend.change_pct < 0 if rule.direction == "down" else trend.change_pct > 0
            if moved_wrong_way and abs(trend.change_pct) >= (rule.min_change_pct or 0):
                series = ", ".join(
                    f"{p}: {v:,.1f}" if v is not None else f"{p}: n/a" for p, v in sorted(trend.series.items())
                )
                flags.append(
                    RiskFlag(
                        rule_id=rule.id,
                        label=rule.label,
                        severity=rule.severity,
                        message=rule.message.format(
                            change_pct=f"{abs(trend.change_pct):.1f}%", series=series, metric=rule.metric
                        ).strip(),
                        period=trend.note or "trend",
                        evidence={"metric": rule.metric, "change_pct": trend.change_pct, "series": series},
                    )
                )
        else:
            raise ValueError(f"Unknown rule type {rule.type!r} in rule {rule.id!r}")

    flags.sort(key=lambda f: SEVERITY_ORDER[f.severity])
    return flags
