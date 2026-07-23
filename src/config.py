"""Configuration + the data firewall.

The single most important rule in this file: the LLM backend defaults to Ollama
(local) and the OpenAI path is *opt-in only*. Having an OPENAI_API_KEY present
must never, on its own, cause document content to leave the machine.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]

load_dotenv(ROOT / ".env")

log = logging.getLogger("credit_memo")


class Settings(BaseModel):
    llm_backend: str = "ollama"

    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"

    openai_api_key: str | None = None
    openai_base_url: str | None = None  # OpenAI-compatible endpoint; None = OpenAI
    openai_model: str = "gpt-4o"

    data_dir: Path = ROOT / "data" / "local"
    config_dir: Path = ROOT / "config"
    log_dir: Path = ROOT / "logs"

    @property
    def is_local(self) -> bool:
        return self.llm_backend == "ollama"

    @property
    def checklist_path(self) -> Path:
        return self.config_dir / "checklist_business_instalment_loan.yaml"

    @property
    def risk_rules_path(self) -> Path:
        return self.config_dir / "risk_rules.yaml"


@lru_cache
def get_settings() -> Settings:
    s = Settings(
        llm_backend=os.getenv("LLM_BACKEND", "ollama").strip().lower(),
        ollama_host=os.getenv("OLLAMA_HOST", "http://localhost:11434"),
        ollama_model=os.getenv("OLLAMA_MODEL", "llama3.1:8b"),
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        openai_base_url=os.getenv("OPENAI_BASE_URL") or None,
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o"),
        data_dir=Path(os.getenv("DATA_DIR", str(ROOT / "data" / "local"))),
    )
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.log_dir.mkdir(parents=True, exist_ok=True)
    warn_if_cloud(s)
    return s


def warn_if_cloud(s: Settings) -> None:
    """Shout, at startup, if we are configured to ship documents off-machine."""
    if s.is_local:
        return
    banner = (
        "\n" + "!" * 78 + "\n"
        "!!  LLM_BACKEND=openai -- BORROWER DOCUMENT CONTENT WILL BE SENT TO AN\n"
        "!!  EXTERNAL API (OpenAI). This mode exists for the synthetic demo files\n"
        "!!  in sample_data/ ONLY. Do not point it at a real borrower file.\n"
        "!!  Set LLM_BACKEND=ollama to run fully offline.\n" + "!" * 78 + "\n"
    )
    log.warning(banner)
    print(banner, flush=True)
