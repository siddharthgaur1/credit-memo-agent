"""FastAPI service. Binds to localhost by default -- see __main__ at the bottom.

Uploaded files are written under DATA_DIR, which is gitignored. Nothing is sent
anywhere unless LLM_BACKEND=openai, and that is warned about loudly at startup.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..agents.graph import run_case
from ..config import get_settings
from ..llm.backend import get_backend
from ..persistence.store import Store, to_record
from ..qa import ask

logging.basicConfig(level=logging.INFO)

settings = get_settings()
app = FastAPI(title="Credit Memo Agent", version="0.1.0")
store = Store(settings.data_dir / "cases.db")

# In-memory status for cases still running. Case history itself lives in SQLite.
_running: dict[str, str] = {}

ALLOWED_SUFFIXES = {".pdf", ".xlsx", ".xlsm", ".xls", ".csv"}


class AskRequest(BaseModel):
    question: str


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "llm_backend": settings.llm_backend,
        "local_only": settings.is_local,
        "model": settings.ollama_model if settings.is_local else settings.openai_model,
    }


@app.post("/cases")
async def create_case(
    background: BackgroundTasks,
    files: list[UploadFile],
    borrower_name: str | None = None,
    facility_requested: str | None = None,
) -> dict:
    case_id = uuid.uuid4().hex[:12]
    case_dir = settings.data_dir / case_id / "uploads"
    case_dir.mkdir(parents=True, exist_ok=True)

    saved: list[Path] = []
    for upload in files:
        name = Path(upload.filename or "unnamed").name  # strip any path components
        if Path(name).suffix.lower() not in ALLOWED_SUFFIXES:
            raise HTTPException(400, f"{name}: unsupported file type. Allowed: {sorted(ALLOWED_SUFFIXES)}")
        target = case_dir / name
        target.write_bytes(await upload.read())
        saved.append(target)

    if not saved:
        raise HTTPException(400, "No files supplied.")

    _running[case_id] = "queued"
    background.add_task(_process, case_id, saved, borrower_name, facility_requested)
    return {"case_id": case_id, "status": "queued", "files": [p.name for p in saved]}


def _process(case_id: str, files: list[Path], borrower: str | None, facility: str | None) -> None:
    _running[case_id] = "running"
    try:
        backend = get_backend(settings)
        state = run_case(
            case_id,
            files,
            borrower_name=borrower,
            facility_requested=facility,
            out_dir=settings.data_dir / case_id,
            settings=settings,
            backend=backend,
        )
        store.save(to_record(state, backend.name))
        _running[case_id] = "done"
    except Exception as exc:  # surfaced verbatim -- a silent failure is worse
        logging.exception("case %s failed", case_id)
        _running[case_id] = f"failed: {exc}"


@app.get("/cases")
def list_cases() -> dict:
    return {"cases": store.list_cases(), "running": _running}


@app.get("/cases/{case_id}")
def get_case(case_id: str) -> dict:
    record = store.get(case_id)
    if record is None:
        status = _running.get(case_id)
        if status is None:
            raise HTTPException(404, f"No case {case_id}")
        return {"case_id": case_id, "status": status}
    return {"case_id": case_id, "status": "done", "record": record.model_dump(mode="json")}


@app.post("/cases/{case_id}/ask")
def ask_case(case_id: str, body: AskRequest) -> dict:
    record = store.get(case_id)
    if record is None:
        raise HTTPException(404, f"No completed case {case_id}")
    answer = ask(get_backend(settings), record, body.question)
    return answer.model_dump(mode="json")


@app.get("/cases/{case_id}/memo.docx")
def get_memo(case_id: str) -> FileResponse:
    record = store.get(case_id)
    if record is None or not record.memo_path or not Path(record.memo_path).exists():
        raise HTTPException(404, f"No memo for case {case_id}")
    return FileResponse(
        record.memo_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=f"credit_memo_{case_id}.docx",
    )


if __name__ == "__main__":
    import uvicorn

    # localhost by default. Change this deliberately, not by accident.
    uvicorn.run(app, host="127.0.0.1", port=8000)
