"""OpenAI Chat Completions quirks per model family (JSON mode, temperature)."""

from __future__ import annotations

import os
import time
from typing import Any

import structlog
from openai import APIStatusError, AsyncOpenAI
from openai.types.chat import ChatCompletion

from llm_service.config import settings

logger = structlog.get_logger()


def pipeline_stage_model() -> str:
    """Model for multi-agent stages (analysis, hypothesis, rerank, judge)."""
    r = (os.getenv("OPENAI_REASONING_MODEL") or settings.OPENAI_REASONING_MODEL or "").strip()
    if r:
        return r
    return (os.getenv("OPENAI_STAGE_MODEL") or settings.OPENAI_STAGE_MODEL or "").strip() or "gpt-4o-mini"


def is_reasoning_style_model(model: str) -> bool:
    m = (model or "").lower()
    if m.startswith("o1") or m.startswith("o3"):
        return True
    if m.startswith("gpt-5"):
        return True
    return False


def _add_usage(acc: dict[str, int], resp: ChatCompletion | None) -> None:
    if not resp or not resp.usage:
        return
    acc["prompt_tokens"] = acc.get("prompt_tokens", 0) + (resp.usage.prompt_tokens or 0)
    acc["completion_tokens"] = acc.get("completion_tokens", 0) + (resp.usage.completion_tokens or 0)


async def chat_completion_json(
    client: AsyncOpenAI,
    *,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
    usage_acc: dict[str, int],
    stage_timings_ms: dict[str, float],
    timing_key: str,
) -> ChatCompletion:
    """Prefer JSON object mode; retry without it on 400 (e.g. some reasoning models)."""
    t0 = time.perf_counter()
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if not is_reasoning_style_model(model):
        kwargs["temperature"] = temperature
        kwargs["response_format"] = {"type": "json_object"}

    try:
        resp = await client.chat.completions.create(**kwargs)
    except APIStatusError as e:
        if getattr(e, "status_code", None) == 400 and "response_format" in kwargs:
            logger.warning("chat_json_mode_retry", timing_key=timing_key, detail=str(e)[:300])
            kwargs.pop("response_format", None)
            if is_reasoning_style_model(model):
                kwargs.pop("temperature", None)
            resp = await client.chat.completions.create(**kwargs)
        else:
            raise
    _add_usage(usage_acc, resp)
    stage_timings_ms[timing_key] = (time.perf_counter() - t0) * 1000
    return resp


async def chat_completion_text(
    client: AsyncOpenAI,
    *,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
    usage_acc: dict[str, int],
    stage_timings_ms: dict[str, float],
    timing_key: str,
) -> ChatCompletion:
    t0 = time.perf_counter()
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if not is_reasoning_style_model(model):
        kwargs["temperature"] = temperature
    resp = await client.chat.completions.create(**kwargs)
    _add_usage(usage_acc, resp)
    stage_timings_ms[timing_key] = (time.perf_counter() - t0) * 1000
    return resp
