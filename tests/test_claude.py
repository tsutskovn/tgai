"""Tests for tgai.claude — LLM client logic (no API calls)."""

import time

import pytest

from tgai.claude import (
    _truncate_msg,
    _call_with_timeout,
    MAX_MSG_CHARS,
    CONTEXT_LEVELS,
    DEFAULT_CONTEXT_LEVEL,
    LLMClient,
    InsufficientCreditsError,
    validate_yandex_credentials,
    YandexGPTClient,
)


# ------------------------------------------------------------------
# Truncation
# ------------------------------------------------------------------

class TestTruncateMsg:
    def test_short_message_unchanged(self):
        assert _truncate_msg("hello") == "hello"

    def test_exact_limit_unchanged(self):
        msg = "x" * MAX_MSG_CHARS
        assert _truncate_msg(msg) == msg

    def test_long_message_truncated(self):
        msg = "x" * (MAX_MSG_CHARS + 100)
        result = _truncate_msg(msg)
        assert len(result) < len(msg)
        assert result.endswith("… [обрезано]")
        assert result.startswith("x" * MAX_MSG_CHARS)

    def test_empty_string(self):
        assert _truncate_msg("") == ""


class TestTimeoutHelper:
    def test_call_with_timeout_returns_value(self):
        assert _call_with_timeout(lambda: "ok", 0.1) == "ok"

    def test_call_with_timeout_raises_on_timeout(self):
        with pytest.raises(TimeoutError):
            _call_with_timeout(lambda: time.sleep(0.2), 0.01)


# ------------------------------------------------------------------
# Context levels
# ------------------------------------------------------------------

class TestContextLevels:
    def test_all_levels_defined(self):
        assert "base" in CONTEXT_LEVELS
        assert "medium" in CONTEXT_LEVELS
        assert "extended" in CONTEXT_LEVELS

    def test_levels_ascending(self):
        assert CONTEXT_LEVELS["base"] < CONTEXT_LEVELS["medium"]
        assert CONTEXT_LEVELS["medium"] < CONTEXT_LEVELS["extended"]

    def test_default_exists(self):
        assert DEFAULT_CONTEXT_LEVEL in CONTEXT_LEVELS


# ------------------------------------------------------------------
# LLMClient — history management (no API needed)
# ------------------------------------------------------------------

class _FakeLLM(LLMClient):
    """Concrete subclass for testing — _complete returns canned response."""
    provider_name = "fake"

    def __init__(self, context_level="base"):
        super().__init__(model="fake-model", context_level=context_level)

    def _complete(self, messages, system="", max_tokens=1024):
        return "fake reply"


class TestLLMHistory:
    def test_initial_history_empty(self):
        llm = _FakeLLM()
        assert llm.get_history(1) == []

    def test_ask_stores_history(self):
        llm = _FakeLLM()
        llm.ask(1, "hello")
        h = llm.get_history(1)
        assert len(h) == 2  # user + assistant
        assert h[0]["role"] == "user"
        assert h[1]["role"] == "assistant"

    def test_history_trimmed_to_max(self):
        llm = _FakeLLM(context_level="base")  # max_history=5
        for i in range(20):
            llm.ask(1, f"msg {i}")
        h = llm.get_history(1)
        assert len(h) <= llm.max_history

    def test_clear_history(self):
        llm = _FakeLLM()
        llm.ask(1, "hi")
        llm.clear_history(1)
        assert llm.get_history(1) == []

    def test_set_history(self):
        llm = _FakeLLM()
        custom = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
        llm.set_history(1, custom)
        assert llm.get_history(1) == custom

    def test_context_level_sets_max_history(self):
        for level, expected in CONTEXT_LEVELS.items():
            llm = _FakeLLM(context_level=level)
            assert llm.max_history == expected

    def test_unknown_context_level_uses_default(self):
        llm = _FakeLLM(context_level="nonexistent")
        assert llm.max_history == CONTEXT_LEVELS[DEFAULT_CONTEXT_LEVEL]


class TestLLMPropose:
    def test_propose_reply_returns_string(self):
        llm = _FakeLLM()
        result = llm.propose_reply("hi", [], "")
        assert isinstance(result, str)

    def test_draft_reply_returns_string(self):
        llm = _FakeLLM()
        result = llm.draft_reply("draft text", [], "")
        assert isinstance(result, str)

    def test_summarize_empty(self):
        llm = _FakeLLM()
        result = llm.summarize_messages([])
        assert result == "Нет сообщений."

    def test_summarize_non_empty(self):
        llm = _FakeLLM()
        msgs = [{"sender": "A", "text": "hello", "date": "12:00"}]
        result = llm.summarize_messages(msgs)
        assert isinstance(result, str)


class TestYandexValidation:
    def test_validate_yandex_credentials_success(self, monkeypatch):
        monkeypatch.setattr(
            YandexGPTClient,
            "_complete",
            lambda self, messages, system="", max_tokens=1024: "ok",
        )
        ok, error = validate_yandex_credentials("key", "folder", "yandexgpt-lite", timeout_seconds=0.1)
        assert ok is True
        assert error == ""

    def test_validate_yandex_credentials_unknown_api_key(self, monkeypatch):
        def _raise(self, messages, system="", max_tokens=1024):
            raise InsufficientCreditsError("YandexGPT 401: Unknown api key")

        monkeypatch.setattr(YandexGPTClient, "_complete", _raise)
        ok, error = validate_yandex_credentials("bad", "folder", "yandexgpt-lite", timeout_seconds=0.1)
        assert ok is False
        assert "неверный API ключ" in error

    def test_validate_yandex_credentials_missing_folder(self):
        ok, error = validate_yandex_credentials("key", "", "yandexgpt-lite", timeout_seconds=0.1)
        assert ok is False
        assert "folder_id" in error
