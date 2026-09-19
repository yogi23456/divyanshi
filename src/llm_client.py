"""Modular LLM client.

One interface (``LLMClient.complete_json``) in front of several providers, so the
provider can be swapped by changing ``LLM_PROVIDER`` in ``.env`` without touching
analysis code. Adding a provider means adding one ``_Provider`` subclass and one
line in ``_build_provider``.

Includes a token-bucket rate limiter and exponential-backoff retries, because a
500-resume run will hit provider rate limits.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from abc import ABC, abstractmethod
from typing import Optional

from .config import Settings, get_settings
from .utils import extract_json_object

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Any provider-side failure, normalised."""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class RateLimiter:
    """Simple thread-safe token bucket: at most N requests per minute."""

    def __init__(self, requests_per_minute: int):
        self.capacity = max(1, int(requests_per_minute))
        self._interval = 60.0 / self.capacity
        self._lock = threading.Lock()
        self._next_slot = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next_slot - now)
            self._next_slot = max(now, self._next_slot) + self._interval
        if wait > 0:
            time.sleep(wait)


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

class _Provider(ABC):
    name = "base"
    is_llm = True

    def __init__(self, settings: Settings):
        self.settings = settings
        self.model = settings.default_model()

    @abstractmethod
    def complete(self, prompt: str, system: str = "") -> str:
        """Return the raw text response for a prompt."""


class AnthropicProvider(_Provider):
    name = "anthropic"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        try:
            import anthropic  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise LLMError("The 'anthropic' package is not installed. pip install anthropic") from exc
        if not settings.anthropic_api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set. Add it to your .env file.")
        self._sdk = anthropic
        kwargs = {"api_key": settings.anthropic_api_key, "timeout": settings.timeout_seconds,
                  "max_retries": 0}  # retries are handled by LLMClient
        if settings.anthropic_base_url:
            kwargs["base_url"] = settings.anthropic_base_url
        self._client = anthropic.Anthropic(**kwargs)

    def complete(self, prompt: str, system: str = "") -> str:
        try:
            message = self._client.messages.create(
                model=self.model,
                max_tokens=self.settings.max_tokens,
                temperature=self.settings.temperature,
                system=system or "You are a precise recruitment analyst. You reply with strict JSON only.",
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001
            raise _normalise_provider_error(exc, self._sdk) from exc
        return "".join(getattr(block, "text", "") for block in message.content)


class OpenAIProvider(_Provider):
    name = "openai"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        try:
            import openai  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise LLMError("The 'openai' package is not installed. pip install openai") from exc
        if not settings.openai_api_key:
            raise LLMError("OPENAI_API_KEY is not set. Add it to your .env file.")
        self._sdk = openai
        kwargs = {"api_key": settings.openai_api_key, "timeout": settings.timeout_seconds,
                  "max_retries": 0}
        if settings.openai_base_url:
            kwargs["base_url"] = settings.openai_base_url
        self._client = openai.OpenAI(**kwargs)

    def complete(self, prompt: str, system: str = "") -> str:
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                temperature=self.settings.temperature,
                max_tokens=self.settings.max_tokens,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system",
                     "content": system or "You are a precise recruitment analyst. You reply with strict JSON only."},
                    {"role": "user", "content": prompt},
                ],
            )
        except Exception as exc:  # noqa: BLE001
            raise _normalise_provider_error(exc, self._sdk) from exc
        return response.choices[0].message.content or ""


class HeuristicProvider(_Provider):
    """Offline provider — makes no network calls.

    ``is_llm`` is False, which tells the analyzer to run its deterministic
    evidence-matching engine instead of prompting a model. This keeps the app
    fully runnable (and testable end to end) without an API key.
    """

    name = "heuristic"
    is_llm = False

    def complete(self, prompt: str, system: str = "") -> str:  # pragma: no cover
        raise LLMError("The heuristic provider does not make model calls.")


_RETRYABLE_HINTS = (
    "rate limit", "rate_limit", "429", "overloaded", "529", "timeout", "timed out",
    "connection", "temporarily unavailable", "503", "502", "500", "internal server error",
)


def _normalise_provider_error(exc: Exception, sdk) -> LLMError:
    """Turn a provider-specific exception into an LLMError with a retry flag."""
    message = f"{type(exc).__name__}: {exc}"
    lowered = message.lower()

    for attr in ("RateLimitError", "APITimeoutError", "APIConnectionError",
                 "InternalServerError", "APIStatusError"):
        klass = getattr(sdk, attr, None)
        if klass and isinstance(exc, klass):
            status = getattr(exc, "status_code", None)
            retryable = attr != "APIStatusError" or (status is not None and int(status) >= 500)
            return LLMError(message, retryable=retryable)

    return LLMError(message, retryable=any(hint in lowered for hint in _RETRYABLE_HINTS))


# ---------------------------------------------------------------------------
# Public client
# ---------------------------------------------------------------------------

def _build_provider(settings: Settings) -> _Provider:
    provider = (settings.provider or "heuristic").lower()
    if provider == "anthropic":
        return AnthropicProvider(settings)
    if provider == "openai":
        return OpenAIProvider(settings)
    if provider in {"heuristic", "none", "offline", "local"}:
        return HeuristicProvider(settings)
    raise LLMError(f"Unknown LLM_PROVIDER '{settings.provider}'. Use anthropic, openai or heuristic.")


class LLMClient:
    """Provider-agnostic client with rate limiting and retry."""

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self.rate_limiter = RateLimiter(self.settings.requests_per_minute)
        self.init_error = ""
        try:
            self.provider: Optional[_Provider] = _build_provider(self.settings)
        except LLMError as exc:
            # Degrade to the offline analyzer rather than breaking the app.
            logger.warning("LLM provider unavailable (%s) — falling back to the heuristic analyzer.", exc)
            self.init_error = str(exc)
            self.provider = HeuristicProvider(self.settings)

    # -- introspection ------------------------------------------------------
    @property
    def is_llm(self) -> bool:
        return bool(self.provider and self.provider.is_llm)

    @property
    def provider_name(self) -> str:
        return self.provider.name if self.provider else "unavailable"

    @property
    def model_name(self) -> str:
        return self.provider.model if self.provider else "-"

    def describe(self) -> str:
        if not self.is_llm:
            reason = f" ({self.init_error})" if self.init_error else ""
            return f"Offline heuristic analyzer{reason}"
        return f"{self.provider_name} · {self.model_name}"

    # -- calls --------------------------------------------------------------
    def complete_text(self, prompt: str, system: str = "", purpose: str = "") -> str:
        """Call the model with rate limiting and exponential backoff."""
        if not self.is_llm:
            raise LLMError("No LLM provider is configured.")

        last_error: Optional[LLMError] = None
        for attempt in range(self.settings.max_retries + 1):
            self.rate_limiter.acquire()
            try:
                return self.provider.complete(prompt, system=system)
            except LLMError as exc:
                last_error = exc
                if not exc.retryable or attempt >= self.settings.max_retries:
                    break
                delay = min(2 ** attempt, 30) + random.uniform(0, 0.75)
                logger.warning("LLM call failed (%s, attempt %d/%d), retrying in %.1fs: %s",
                               purpose or "call", attempt + 1, self.settings.max_retries, delay, exc)
                time.sleep(delay)
        raise last_error or LLMError("LLM call failed for an unknown reason.")

    def complete_json(self, prompt: str, system: str = "", purpose: str = "") -> Optional[dict]:
        """Call the model and parse a JSON object out of the response.

        One malformed response gets a single stricter retry before giving up.
        """
        raw = self.complete_text(prompt, system=system, purpose=purpose)
        payload = extract_json_object(raw)
        if payload is not None:
            return payload

        logger.warning("Model returned non-JSON for %s; retrying once with a stricter instruction.", purpose)
        strict = prompt + "\n\nIMPORTANT: Your previous reply was not valid JSON. Reply with a single valid JSON object and nothing else."
        raw = self.complete_text(strict, system=system, purpose=f"{purpose}:retry")
        return extract_json_object(raw)


def build_client(settings: Optional[Settings] = None) -> LLMClient:
    return LLMClient(settings)
