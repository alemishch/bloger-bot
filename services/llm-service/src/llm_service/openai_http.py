"""Shared AsyncOpenAI client with configurable HTTP timeouts (embeddings/chat under proxy or load)."""

from __future__ import annotations

import httpx
from openai import AsyncOpenAI

from llm_service.config import settings


def openai_http_timeout() -> httpx.Timeout:
    return httpx.Timeout(
        settings.OPENAI_HTTP_TIMEOUT,
        connect=settings.OPENAI_HTTP_CONNECT_TIMEOUT,
    )


def async_openai_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=settings.OPENAI_API_KEY,
        timeout=openai_http_timeout(),
    )
