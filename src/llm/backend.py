"""LLM backend interface: one method, two implementations, local by default.

`complete(prompt, schema)` returns a validated instance of `schema` (a Pydantic
model) or raises. Callers never see raw text, so a backend swap can't change the
shape of anything downstream.
"""

from __future__ import annotations

import abc
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from ..config import Settings, get_settings

log = logging.getLogger("credit_memo.llm")

T = TypeVar("T", bound=BaseModel)

MAX_ATTEMPTS = 3


class LLMError(RuntimeError):
    pass


class BackendUnavailable(LLMError):
    """The backend is not reachable -- message must tell the user what to do."""


class LLMBackend(abc.ABC):
    name: str = "base"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._log_path = self.settings.log_dir / "llm_calls.jsonl"

    @abc.abstractmethod
    def _raw_complete(self, prompt: str, schema: type[BaseModel]) -> tuple[str, dict]:
        """Return (json_text, usage_dict)."""

    def complete(self, prompt: str, schema: type[T]) -> T:
        last: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            started = time.time()
            try:
                text, usage = self._raw_complete(prompt, schema)
                parsed = schema.model_validate_json(text)
            except BackendUnavailable:
                raise
            except (ValidationError, json.JSONDecodeError) as exc:
                last = exc
                log.warning("%s: invalid structured output (attempt %d): %s", self.name, attempt, exc)
                prompt = f"{prompt}\n\nYour previous reply did not match the schema ({exc}). Reply with valid JSON only."
            except Exception as exc:  # network / transient
                last = exc
                log.warning("%s: call failed (attempt %d): %s", self.name, attempt, exc)
            else:
                self._log_call(schema, usage, time.time() - started, attempt, ok=True)
                return parsed
            time.sleep(2 ** (attempt - 1))
        self._log_call(schema, {}, 0.0, MAX_ATTEMPTS, ok=False)
        raise LLMError(f"{self.name}: failed after {MAX_ATTEMPTS} attempts: {last}")

    def _log_call(self, schema: type[BaseModel], usage: dict, secs: float, attempts: int, ok: bool) -> None:
        # Log metadata only. Never the prompt -- it contains document content.
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "backend": self.name,
            "schema": schema.__name__,
            "attempts": attempts,
            "seconds": round(secs, 2),
            "ok": ok,
            **{k: v for k, v in usage.items() if v is not None},
        }
        with self._log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")


class OllamaBackend(LLMBackend):
    """Default. Fully local -- no document content leaves the machine."""

    name = "ollama"

    def _raw_complete(self, prompt: str, schema: type[BaseModel]) -> tuple[str, dict]:
        url = f"{self.settings.ollama_host.rstrip('/')}/api/chat"
        payload = {
            "model": self.settings.ollama_model,
            "messages": [{"role": "user", "content": prompt}],
            "format": schema.model_json_schema(),
            "stream": False,
            "options": {"temperature": 0},
        }
        try:
            resp = httpx.post(url, json=payload, timeout=300)
        except httpx.ConnectError as exc:
            raise BackendUnavailable(
                f"Cannot reach Ollama at {self.settings.ollama_host}. "
                f"Start it with `ollama serve`, then `ollama pull {self.settings.ollama_model}`."
            ) from exc
        if resp.status_code == 404:
            raise BackendUnavailable(
                f"Ollama has no model '{self.settings.ollama_model}'. "
                f"Run `ollama pull {self.settings.ollama_model}` (or set OLLAMA_MODEL)."
            )
        resp.raise_for_status()
        data = resp.json()
        usage = {
            "prompt_tokens": data.get("prompt_eval_count"),
            "completion_tokens": data.get("eval_count"),
        }
        return data["message"]["content"], usage


class OpenAIBackend(LLMBackend):
    """Opt-in only. Sends document content to OpenAI. Synthetic data only."""

    name = "openai"

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__(settings)
        if not self.settings.openai_api_key:
            raise BackendUnavailable("LLM_BACKEND=openai but OPENAI_API_KEY is not set.")
        from openai import OpenAI

        self._client = OpenAI(api_key=self.settings.openai_api_key)

    def _raw_complete(self, prompt: str, schema: type[BaseModel]) -> tuple[str, dict]:
        completion = self._client.beta.chat.completions.parse(
            model=self.settings.openai_model,
            messages=[{"role": "user", "content": prompt}],
            response_format=schema,
            temperature=0,
        )
        u = completion.usage
        usage = {
            "prompt_tokens": getattr(u, "prompt_tokens", None),
            "completion_tokens": getattr(u, "completion_tokens", None),
        }
        return completion.choices[0].message.content, usage


def get_backend(settings: Settings | None = None) -> LLMBackend:
    """Explicit selection only. An OpenAI key present is NOT a signal to use it."""
    s = settings or get_settings()
    if s.llm_backend == "openai":
        return OpenAIBackend(s)
    if s.llm_backend == "ollama":
        return OllamaBackend(s)
    raise LLMError(f"Unknown LLM_BACKEND={s.llm_backend!r}. Use 'ollama' (local) or 'openai' (demo).")
