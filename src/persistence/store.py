"""Local SQLite case history. Never leaves the machine; gitignored."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    borrower    TEXT,
    backend     TEXT,
    memo_path   TEXT,
    payload     TEXT NOT NULL
);
"""


class CaseRecord(BaseModel):
    case_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    borrower_name: str | None = None
    facility_requested: str | None = None
    backend: str = "ollama"
    classifications: list[dict] = Field(default_factory=list)
    checklist: dict = Field(default_factory=dict)
    financials: dict = Field(default_factory=dict)
    verification: list[dict] = Field(default_factory=list)
    analysis: dict = Field(default_factory=dict)
    flags: list[dict] = Field(default_factory=list)
    narrative: str = ""
    trend_commentary: str = ""
    escalations: list[str] = Field(default_factory=list)
    review_findings: list[str] = Field(default_factory=list)
    progress: list[str] = Field(default_factory=list)
    memo_path: str | None = None


def _dump(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, list):
        return [_dump(o) for o in obj]
    if isinstance(obj, dict):
        return {k: _dump(v) for k, v in obj.items()}
    return obj


def to_record(state: dict, backend_name: str) -> CaseRecord:
    fin = state.get("financials")
    return CaseRecord(
        case_id=state["case_id"],
        borrower_name=getattr(fin, "borrower_name", None) or state.get("borrower_name"),
        facility_requested=getattr(fin, "facility_requested", None) or state.get("facility_requested"),
        backend=backend_name,
        classifications=state.get("classifications", []),
        checklist=_dump(state.get("checklist_report")) or {},
        financials=_dump(fin) or {},
        verification=_dump(state.get("verification", [])) or [],
        analysis=_dump(state.get("analysis")) or {},
        flags=_dump(state.get("flags", [])) or [],
        narrative=state.get("narrative", ""),
        trend_commentary=state.get("trend_commentary", ""),
        escalations=state.get("escalations", []),
        review_findings=state.get("review_findings", []),
        progress=state.get("progress", []),
        memo_path=state.get("memo_path"),
    )


class Store:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def save(self, record: CaseRecord) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO cases (id, created_at, borrower, backend, memo_path, payload)"
                " VALUES (?,?,?,?,?,?)",
                (
                    record.case_id,
                    record.created_at.isoformat(),
                    record.borrower_name,
                    record.backend,
                    record.memo_path,
                    json.dumps(record.model_dump(mode="json")),
                ),
            )

    def get(self, case_id: str) -> CaseRecord | None:
        with self._conn() as c:
            row = c.execute("SELECT payload FROM cases WHERE id = ?", (case_id,)).fetchone()
        return CaseRecord.model_validate_json(row["payload"]) if row else None

    def list_cases(self, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, created_at, borrower, backend FROM cases ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
