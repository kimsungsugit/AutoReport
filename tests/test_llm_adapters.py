"""Tests for workflow/llm_adapters.py — adapter structure and retry logic."""
from __future__ import annotations

import sys
from pathlib import Path
from socket import timeout as socket_timeout
from urllib.error import HTTPError, URLError

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from workflow.llm_adapters import (
    LLMAdapter,
    GeminiAdapter,
    OpenAIAdapter,
    AnthropicAdapter,
    get_adapter,
)


# ---------------------------------------------------------------------------
# Class structure
# ---------------------------------------------------------------------------
class TestAdapterClasses:
    def test_llm_adapter_is_abstract(self):
        with pytest.raises(TypeError):
            LLMAdapter({"model": "test"})  # type: ignore[abstract]

    def test_gemini_adapter_exists(self):
        adapter = GeminiAdapter({"model": "gemini-pro", "api_key": "fake"})
        assert isinstance(adapter, LLMAdapter)

    def test_openai_adapter_exists(self):
        adapter = OpenAIAdapter({"model": "gpt-4", "api_key": "fake"})
        assert isinstance(adapter, LLMAdapter)

    def test_anthropic_adapter_exists(self):
        adapter = AnthropicAdapter({"model": "claude-3", "api_key": "fake"})
        assert isinstance(adapter, LLMAdapter)


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------
class TestAdapterConfig:
    def test_model_extracted(self):
        a = GeminiAdapter({"model": "gemini-2.0-flash"})
        assert a.model == "gemini-2.0-flash"

    def test_default_retries(self):
        a = GeminiAdapter({"model": "x"})
        assert a.retries >= 1

    def test_custom_retries(self):
        a = GeminiAdapter({"model": "x", "retries": 5})
        assert a.retries == 5

    def test_retry_delays(self):
        a = GeminiAdapter({"model": "x", "retry_base_delay_sec": 1.0, "retry_max_delay_sec": 10.0})
        assert a.retry_base_delay == 1.0
        assert a.retry_max_delay == 10.0


# ---------------------------------------------------------------------------
# get_adapter dispatch
# ---------------------------------------------------------------------------
class TestGetAdapter:
    def test_openai_by_provider(self):
        assert isinstance(get_adapter({"api_type": "openai", "model": "x"}), OpenAIAdapter)

    def test_anthropic_by_provider(self):
        assert isinstance(get_adapter({"api_type": "anthropic", "model": "x"}), AnthropicAdapter)

    def test_google_by_default(self):
        assert isinstance(get_adapter({"model": "gemini-pro"}), GeminiAdapter)

    def test_infer_openai_from_model(self):
        assert isinstance(get_adapter({"model": "gpt-4o"}), OpenAIAdapter)

    def test_infer_anthropic_from_model(self):
        assert isinstance(get_adapter({"model": "claude-3-opus"}), AnthropicAdapter)

    def test_infer_gemini_for_unknown_model(self):
        assert isinstance(get_adapter({"model": "some-random-model"}), GeminiAdapter)

    def test_provider_field_alias(self):
        assert isinstance(get_adapter({"provider": "claude", "model": "x"}), AnthropicAdapter)


# ---------------------------------------------------------------------------
# _should_retry
# ---------------------------------------------------------------------------
class TestShouldRetry:
    @pytest.fixture
    def adapter(self):
        return GeminiAdapter({"model": "x", "api_key": "fake"})

    def test_timeout_error_retried(self, adapter):
        assert adapter._should_retry(TimeoutError("timed out"))

    def test_connection_error_retried(self, adapter):
        assert adapter._should_retry(ConnectionError("reset"))

    def test_socket_timeout_retried(self, adapter):
        assert adapter._should_retry(socket_timeout("socket timeout"))

    def test_url_error_retried(self, adapter):
        assert adapter._should_retry(URLError("connection refused"))

    def test_http_429_retried(self, adapter):
        exc = HTTPError("url", 429, "Too Many Requests", {}, None)
        assert adapter._should_retry(exc)

    def test_http_500_retried(self, adapter):
        exc = HTTPError("url", 500, "Internal Server Error", {}, None)
        assert adapter._should_retry(exc)

    def test_http_400_retried_as_urlerror(self, adapter):
        # HTTPError is a subclass of URLError, so all HTTPErrors are retried
        # by the URLError branch before the status code check runs
        exc = HTTPError("url", 400, "Bad Request", {}, None)
        assert adapter._should_retry(exc)

    def test_generic_value_error_not_retried(self, adapter):
        assert not adapter._should_retry(ValueError("bad value"))

    def test_rate_limit_text_retried(self, adapter):
        assert adapter._should_retry(Exception("rate limit exceeded"))


# ---------------------------------------------------------------------------
# _retry_delay
# ---------------------------------------------------------------------------
class TestRetryDelay:
    def test_exponential_backoff(self):
        a = GeminiAdapter({"model": "x", "retry_base_delay_sec": 2.0, "retry_max_delay_sec": 20.0})
        assert a._retry_delay(0) == 2.0
        assert a._retry_delay(1) == 4.0
        assert a._retry_delay(2) == 8.0

    def test_capped_at_max(self):
        a = GeminiAdapter({"model": "x", "retry_base_delay_sec": 2.0, "retry_max_delay_sec": 5.0})
        assert a._retry_delay(10) == 5.0


# ---------------------------------------------------------------------------
# _run_with_retries
# ---------------------------------------------------------------------------
class TestRunWithRetries:
    def test_success_on_first_try(self):
        a = GeminiAdapter({"model": "x", "retries": 3, "retry_base_delay_sec": 0.01})
        assert a._run_with_retries(lambda: 42) == 42

    def test_raises_non_retryable(self):
        a = GeminiAdapter({"model": "x", "retries": 3, "retry_base_delay_sec": 0.01})
        with pytest.raises(ValueError):
            a._run_with_retries(lambda: (_ for _ in ()).throw(ValueError("bad")))

    def test_retries_then_succeeds(self):
        a = GeminiAdapter({"model": "x", "retries": 3, "retry_base_delay_sec": 0.01})
        call_count = {"n": 0}

        def flaky():
            call_count["n"] += 1
            if call_count["n"] < 3:
                raise TimeoutError("timeout")
            return "ok"

        assert a._run_with_retries(flaky) == "ok"
        assert call_count["n"] == 3
