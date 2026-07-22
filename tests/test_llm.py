"""Backend interface tests. No network, no model, no key -- everything mocked."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel

from src.config import Settings, get_settings
from src.llm.backend import (
    BackendUnavailable,
    LLMBackend,
    LLMError,
    OllamaBackend,
    OpenAIBackend,
    get_backend,
)


class Answer(BaseModel):
    value: int


@pytest.fixture(autouse=True)
def _isolated_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _settings(tmp_path, **kw) -> Settings:
    return Settings(data_dir=tmp_path / "data", log_dir=tmp_path / "logs", **kw)


def _prepare(settings: Settings) -> Settings:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    return settings


def test_default_backend_is_ollama_with_no_key_present(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    get_settings.cache_clear()

    settings = get_settings()
    assert settings.llm_backend == "ollama"
    assert settings.is_local
    assert isinstance(get_backend(settings), OllamaBackend)


def test_an_openai_key_alone_never_switches_the_backend(tmp_path, monkeypatch):
    """The single worst failure mode: a cloud call on a real borrower file."""
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-not-a-real-key")
    get_settings.cache_clear()

    settings = get_settings()
    assert settings.llm_backend == "ollama"
    assert isinstance(get_backend(settings), OllamaBackend)


def test_openai_backend_requires_an_explicit_opt_in_and_a_key(tmp_path):
    settings = _prepare(_settings(tmp_path, llm_backend="openai", openai_api_key=None))
    with pytest.raises(BackendUnavailable, match="OPENAI_API_KEY"):
        get_backend(settings)


def test_unknown_backend_is_refused(tmp_path):
    settings = _prepare(_settings(tmp_path, llm_backend="anthropic"))
    with pytest.raises(LLMError, match="Unknown LLM_BACKEND"):
        get_backend(settings)


def test_both_implementations_satisfy_the_interface():
    for impl in (OllamaBackend, OpenAIBackend):
        assert issubclass(impl, LLMBackend)
        assert impl._raw_complete is not LLMBackend._raw_complete


def test_ollama_parses_structured_output_and_logs_the_call(tmp_path, monkeypatch):
    settings = _prepare(_settings(tmp_path))
    backend = OllamaBackend(settings)

    def fake_post(url, json=None, timeout=None):
        assert json["options"]["temperature"] == 0
        assert json["format"]["properties"]["value"]["type"] == "integer"
        return httpx.Response(
            200,
            json={"message": {"content": '{"value": 7}'}, "prompt_eval_count": 12, "eval_count": 3},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    assert backend.complete("anything", Answer).value == 7

    logged = [json.loads(line) for line in (settings.log_dir / "llm_calls.jsonl").read_text().splitlines()]
    assert logged[-1]["backend"] == "ollama"
    assert logged[-1]["prompt_tokens"] == 12 and logged[-1]["completion_tokens"] == 3


def test_ollama_not_running_gives_an_actionable_error(tmp_path, monkeypatch):
    settings = _prepare(_settings(tmp_path))

    def refuse(url, json=None, timeout=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", refuse)
    with pytest.raises(BackendUnavailable, match="ollama serve"):
        OllamaBackend(settings).complete("anything", Answer)


def test_missing_model_says_which_model_to_pull(tmp_path, monkeypatch):
    settings = _prepare(_settings(tmp_path, ollama_model="qwen2.5:7b"))

    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, json=None, timeout=None: httpx.Response(404, request=httpx.Request("POST", url)),
    )
    with pytest.raises(BackendUnavailable, match="ollama pull qwen2.5:7b"):
        OllamaBackend(settings).complete("anything", Answer)


def test_malformed_output_is_retried_then_reported(tmp_path, monkeypatch):
    settings = _prepare(_settings(tmp_path))
    calls = {"n": 0}

    def flaky(url, json=None, timeout=None):
        calls["n"] += 1
        body = '{"value": 5}' if calls["n"] == 3 else "not json at all"
        return httpx.Response(
            200, json={"message": {"content": body}}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx, "post", flaky)
    monkeypatch.setattr("time.sleep", lambda *_: None)

    assert OllamaBackend(settings).complete("anything", Answer).value == 5
    assert calls["n"] == 3
