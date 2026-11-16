"""Replit OpenAI integration client and bounded retries."""
from __future__ import annotations

from functools import lru_cache

import httpx
from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.core.config import get_settings

_RETRIABLE = (APIConnectionError, APITimeoutError, InternalServerError, RateLimitError)


def _is_retriable(error: BaseException) -> bool:
    if isinstance(error, _RETRIABLE) or isinstance(error, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code == 429 or error.response.status_code >= 500
    return False


external_retry = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception(_is_retriable),
)


def ai_configured() -> bool:
    settings = get_settings()
    return bool(settings.openai_api_key or (settings.ai_integrations_openai_api_key and settings.ai_integrations_openai_base_url))


@lru_cache(maxsize=1)
def get_openai_client() -> AsyncOpenAI:
    settings = get_settings()
    api_key = settings.openai_api_key or settings.ai_integrations_openai_api_key
    if not api_key:
        raise RuntimeError("OpenAI is not configured. Connect Replit AI Integrations or set OPENAI_API_KEY.")
    return AsyncOpenAI(
        api_key=api_key,
        base_url=settings.ai_integrations_openai_base_url or None,
        timeout=settings.ai_timeout_seconds,
        max_retries=0,
    )


def speech_model() -> str:
    # whisper-1 is available only when the user supplies their own OpenAI key.
    return "whisper-1" if get_settings().openai_api_key else "gpt-4o-mini-transcribe"
