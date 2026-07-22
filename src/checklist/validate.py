"""Required-document validation. Runs BEFORE extraction.

There is no point spending minutes extracting figures from an incomplete file --
the gap list alone is most of the day-one value.
"""

from __future__ import annotations

from datetime import date, timedelta
from enum import Enum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from ..ingest.classify import DocType


class Status(str, Enum):
    PRESENT = "present"
    MISSING = "missing"
    STALE = "stale"  # present, but older than the policy threshold
    NOT_APPLICABLE = "not_applicable"


class ChecklistItem(BaseModel):
    id: str
    label: str
    doc_type: DocType
    min_count: int = 1
    max_age_days: int | None = None
    mandatory: bool = True
    note: str | None = None


class Checklist(BaseModel):
    name: str
    version: int = 1
    items: list[ChecklistItem]


class SubmittedDoc(BaseModel):
    """One classified file, with the date it speaks about (not its mtime)."""

    filename: str
    doc_type: DocType
    as_of: date | None = None


class ChecklistItemResult(BaseModel):
    id: str
    label: str
    status: Status
    reason: str
    matched_files: list[str] = Field(default_factory=list)


class ChecklistReport(BaseModel):
    checklist_name: str
    results: list[ChecklistItemResult]

    @property
    def gaps(self) -> list[ChecklistItemResult]:
        return [r for r in self.results if r.status in (Status.MISSING, Status.STALE)]

    @property
    def is_complete(self) -> bool:
        return not self.gaps


def load_checklist(path: Path) -> Checklist:
    return Checklist.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def validate(
    checklist: Checklist,
    submitted: list[SubmittedDoc],
    today: date | None = None,
) -> ChecklistReport:
    today = today or date.today()
    results: list[ChecklistItemResult] = []

    for item in checklist.items:
        matches = [d for d in submitted if d.doc_type == item.doc_type]
        names = [d.filename for d in matches]

        if len(matches) < item.min_count:
            status = Status.MISSING if item.mandatory else Status.NOT_APPLICABLE
            reason = (
                f"found {len(matches)} of {item.min_count} required"
                + (f" ({', '.join(names)})" if names else "")
            )
            if not item.mandatory:
                reason += " -- optional, confirm applicability"
            results.append(
                ChecklistItemResult(id=item.id, label=item.label, status=status, reason=reason, matched_files=names)
            )
            continue

        if item.max_age_days is not None:
            # Freshness is judged on the NEWEST document only. A two-year
            # requirement necessarily includes an older year; that is the
            # requirement, not staleness.
            cutoff = today - timedelta(days=item.max_age_days)
            dated = [d for d in matches if d.as_of is not None]
            if not dated:
                results.append(
                    ChecklistItemResult(
                        id=item.id,
                        label=item.label,
                        status=Status.STALE,
                        reason="present but no period/date could be established -- age cannot be verified",
                        matched_files=names,
                    )
                )
                continue
            newest = max(d.as_of for d in dated)
            if newest < cutoff:
                age = (today - newest).days
                results.append(
                    ChecklistItemResult(
                        id=item.id,
                        label=item.label,
                        status=Status.STALE,
                        reason=(
                            f"newest is dated {newest.isoformat()} ({age} days old); "
                            f"policy allows {item.max_age_days} days"
                        ),
                        matched_files=names,
                    )
                )
                continue

        results.append(
            ChecklistItemResult(
                id=item.id,
                label=item.label,
                status=Status.PRESENT,
                reason=f"{len(matches)} document(s) found",
                matched_files=names,
            )
        )

    return ChecklistReport(checklist_name=checklist.name, results=results)
