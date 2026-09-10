"""Minimal OpenAI-compatible chat client (Ollama, Groq, Gemini, OpenAI) returning JSON objects."""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

log = logging.getLogger(__name__)

# provider -> (default base URL, default writer model)
PROVIDERS: dict[str, tuple[str, str]] = {
    "ollama": ("http://127.0.0.1:11434/v1", "llama3.1"),
    "groq": ("https://api.groq.com/openai/v1", "openai/gpt-oss-20b"),  # own free quota; research uses gpt-oss-120b
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.1-pro-preview"),
    "openai": ("https://api.openai.com/v1", "gpt-5.5"),
}
RETRIABLE_STATUS = {408, 429, 500, 502, 503, 504}
RATE_LIMIT_HEADERS = ("retry-after", "x-ratelimit-reset-tokens", "x-ratelimit-reset-requests")


class LLMError(RuntimeError):
    """Retriable: transient HTTP errors, timeouts, malformed JSON."""

    def __init__(self, message: str, wait_s: float | None = None):
        super().__init__(message)
        self.wait_s = wait_s


class LLMFatalError(LLMError):
    """Not retriable: bad key, unknown model, bad request."""


def ollama_base_url(ollama_host: str) -> str:
    """OLLAMA_HOST in .env targets Docker; the orchestrator runs on the host itself."""
    host = (ollama_host or "").strip().rstrip("/")
    if not host:
        return PROVIDERS["ollama"][0]
    host = host.replace("host.docker.internal", "127.0.0.1")
    if not host.startswith(("http://", "https://")):
        host = "http://" + host
    return host if host.endswith("/v1") else host + "/v1"


def parse_duration(value: str) -> float | None:
    """'7', '7.66s', '1m2.5s', '250ms' -> seconds."""
    value = (value or "").strip()
    try:
        return float(value)
    except ValueError:
        pass
    parts = re.findall(r"([\d.]+)(ms|h|m|s)", value)
    if not parts:
        return None
    return sum(float(n) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[u] for n, u in parts)


def rate_limit_wait(resp: Any) -> float | None:
    headers = getattr(resp, "headers", None) or {}
    for name in RATE_LIMIT_HEADERS:
        seconds = parse_duration(headers.get(name, ""))
        if seconds is not None:
            return seconds
    return None


def parse_json_reply(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise LLMError("model reply contained no JSON object")
        text = text[start:end + 1]
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise LLMError(f"invalid JSON from model: {exc}") from exc
    if not isinstance(data, dict):
        raise LLMError("model reply must be a JSON object")
    return data


@dataclass
class LLMClient:
    provider: str
    model: str
    base_url: str
    api_key: str = ""
    timeout_s: float = 300.0
    max_attempts: int = 5
    max_wait_s: float = 90.0
    json_mode: bool = True
    max_completion_tokens: int | None = 4096  # explicit cap: reasoning can't eat the whole reply, fits 8k TPM
    extra_body: dict[str, Any] = field(default_factory=dict)
    session: requests.Session | None = None
    sleep: Callable[[float], None] = time.sleep

    def __post_init__(self) -> None:
        if self.model.startswith("groq/compound"):
            self.json_mode = False  # compound systems don't accept response_format
        if self.model.startswith("openai/gpt-oss") and "reasoning_effort" not in self.extra_body:
            self.extra_body["reasoning_effort"] = "low"  # medium/high reasoning can use the entire output budget

    def chat_json(self, system: str, user: str, temperature: float = 0.8) -> dict[str, Any]:
        session = self.session or requests.Session()
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        body: dict[str, Any] = {
            "model": self.model,
            "temperature": temperature,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            **self.extra_body,
        }
        if self.max_completion_tokens:
            body["max_completion_tokens"] = self.max_completion_tokens
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = session.post(url, json=body, headers=headers, timeout=self.timeout_s)
                if resp.status_code == 429 and ("request too large" in resp.text.lower()
                                                or "tokens per day" in resp.text.lower()):
                    # request exceeds a per-minute cap, or the daily budget is spent: waiting minutes won't help
                    raise LLMFatalError(f"{self.provider} HTTP 429: {resp.text[:300]}")
                if resp.status_code in RETRIABLE_STATUS:
                    raise LLMError(f"{self.provider} HTTP {resp.status_code}: {resp.text[:200]}",
                                   wait_s=rate_limit_wait(resp))
                if resp.status_code == 400 and "json_validate_failed" in resp.text and "response_format" in body:
                    # Groq's JSON-mode validator rejected the generation; retry and parse the raw reply instead
                    body.pop("response_format")
                    raise LLMError(f"{self.provider} JSON-mode validation failed; retrying without JSON mode", wait_s=1)
                if resp.status_code != 200:
                    raise LLMFatalError(f"{self.provider} HTTP {resp.status_code}: {resp.text[:300]}")
                try:
                    choice = resp.json()["choices"][0]
                    content = choice["message"]["content"] or ""
                except (ValueError, KeyError, IndexError, TypeError) as exc:
                    raise LLMError(f"unexpected {self.provider} response shape") from exc
                try:
                    return parse_json_reply(content)
                except LLMError as exc:
                    raise LLMError(f"{exc} (finish_reason={choice.get('finish_reason')}, "
                                   f"{len(content)} chars: {content[:80]!r})") from exc
            except LLMFatalError:
                raise
            except (LLMError, requests.RequestException) as exc:
                if attempt == self.max_attempts:
                    raise LLMError(f"{self.provider}/{self.model} failed after {attempt} attempts: {exc}") from exc
                hinted = getattr(exc, "wait_s", None)
                delay = min(self.max_wait_s, (hinted + 1) if hinted is not None else 2 ** attempt)
                log.warning("%s/%s: %s (attempt %d/%d); waiting %.0fs", self.provider, self.model, exc,
                            attempt, self.max_attempts, delay)
                self.sleep(delay)
        raise AssertionError("unreachable")
