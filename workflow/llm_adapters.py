from __future__ import annotations

import os
import time
from socket import timeout as socket_timeout
from abc import ABC, abstractmethod
from typing import Any
from urllib.error import HTTPError, URLError


class LLMAdapter(ABC):
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.model = str(config.get("model") or "")
        self.api_key = str(config.get("api_key") or "")
        self.retries = max(1, int(config.get("retries") or 1))
        self.retry_base_delay = float(config.get("retry_base_delay_sec") or 2.0)
        self.retry_max_delay = float(config.get("retry_max_delay_sec") or 20.0)

    @abstractmethod
    def generate(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.3,
        max_tokens: int = 65536,
        timeout: float = 300.0,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def _should_retry(self, exc: Exception) -> bool:
        if isinstance(exc, (TimeoutError, socket_timeout, ConnectionError, URLError)):
            return True
        if isinstance(exc, HTTPError):
            return exc.code == 429 or exc.code >= 500
        status_code = getattr(exc, "status_code", None)
        if isinstance(status_code, int):
            return status_code == 429 or status_code >= 500
        response = getattr(exc, "response", None)
        if response is not None:
            resp_status = getattr(response, "status_code", None)
            if isinstance(resp_status, int):
                return resp_status == 429 or resp_status >= 500
        text = str(exc).lower()
        retry_markers = ("timeout", "temporar", "rate limit", "429", "connection reset", "service unavailable", "internal error", "bad gateway", "gateway timeout")
        return any(marker in text for marker in retry_markers)

    def _retry_delay(self, attempt_index: int) -> float:
        return min(self.retry_max_delay, self.retry_base_delay * (2 ** attempt_index))

    def _run_with_retries(self, func):
        last_exc: Exception | None = None
        for attempt in range(self.retries):
            try:
                return func()
            except Exception as exc:
                last_exc = exc
                if attempt >= self.retries - 1 or not self._should_retry(exc):
                    raise
                time.sleep(self._retry_delay(attempt))
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("LLM request failed without exception")


class GeminiAdapter(LLMAdapter):
    def generate(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.3,
        max_tokens: float = 65536,
        timeout: float = 300.0,
    ) -> dict[str, Any]:
        from google import genai as genai_new

        api_key = self.api_key or os.environ.get("GOOGLE_API_KEY", "")
        if not api_key:
            raise ValueError("Google API key not configured")

        contents = []
        system_instruction = None
        for msg in messages:
            role = msg.get("role", "user")
            text = msg.get("content", "")
            if role == "system":
                system_instruction = text
            else:
                contents.append({"role": role, "parts": [{"text": text}]})

        client = genai_new.Client(api_key=api_key)
        config: dict[str, Any] = {"temperature": temperature, "max_output_tokens": int(max_tokens)}
        if system_instruction:
            config["system_instruction"] = system_instruction

        def run_request():
            return client.models.generate_content(
                model=self.model,
                contents=contents,
                config=config,
            )

        response = self._run_with_retries(run_request)
        text = response.text if hasattr(response, "text") else str(response)
        usage = {}
        if hasattr(response, "usage_metadata"):
            metadata = response.usage_metadata
            usage = {
                "prompt_tokens": getattr(metadata, "prompt_token_count", 0),
                "completion_tokens": getattr(metadata, "candidates_token_count", 0),
            }
        return {"output": text, "usage": usage}


class OpenAIAdapter(LLMAdapter):
    def generate(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.3,
        max_tokens: int = 65536,
        timeout: float = 300.0,
    ) -> dict[str, Any]:
        import openai

        api_key = self.api_key or os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            raise ValueError("OpenAI API key not configured")

        client_kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout}
        base_url = self.config.get("base_url")
        if base_url:
            client_kwargs["base_url"] = base_url
        client = openai.OpenAI(**client_kwargs)
        def run_request():
            return client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )

        response = self._run_with_retries(run_request)
        text = response.choices[0].message.content if response.choices else ""
        usage = {}
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
            }
        return {"output": text, "usage": usage}


class AnthropicAdapter(LLMAdapter):
    def generate(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.3,
        max_tokens: int = 65536,
        timeout: float = 300.0,
    ) -> dict[str, Any]:
        import anthropic

        api_key = self.api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise ValueError("Anthropic API key not configured")

        client = anthropic.Anthropic(api_key=api_key, timeout=timeout)
        system = ""
        payload: list[dict[str, str]] = []
        for msg in messages:
            if msg.get("role") == "system":
                system = msg.get("content", "")
            else:
                payload.append(msg)

        def run_request():
            return client.messages.create(
                model=self.model,
                messages=payload,
                system=system or None,
                temperature=temperature,
                max_tokens=max_tokens,
            )

        response = self._run_with_retries(run_request)
        text = ""
        if response.content:
            first = response.content[0]
            text = first.text if hasattr(first, "text") else str(first)
        usage = {
            "prompt_tokens": getattr(response.usage, "input_tokens", 0),
            "completion_tokens": getattr(response.usage, "output_tokens", 0),
        }
        return {"output": text, "usage": usage}


def get_adapter(config: dict[str, Any]) -> LLMAdapter:
    provider = (
        os.environ.get("LLM_PROVIDER", "")
        or str(config.get("api_type") or "")
        or str(config.get("provider") or "")
    ).lower().strip()
    model = str(config.get("model") or "").lower()
    if not provider:
        if "gpt" in model or model.startswith("o1"):
            provider = "openai"
        elif "claude" in model:
            provider = "anthropic"
        else:
            provider = "google"

    if provider in {"openai", "gpt"}:
        return OpenAIAdapter(config)
    if provider in {"anthropic", "claude"}:
        return AnthropicAdapter(config)
    return GeminiAdapter(config)
