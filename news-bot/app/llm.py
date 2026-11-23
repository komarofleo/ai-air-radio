"""OpenAI gpt-4o-mini helpers for relevance, summary, and EN→RU.

Never log API keys or full Authorization headers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx

from app.config import Settings
from app.db import RawItem
from app.filter import strip_html

logger = logging.getLogger(__name__)

OPENAI_URL = "https://api.openai.com/v1/chat/completions"


def _topics_blob(settings: Settings) -> str:
    return ", ".join(settings.topic_keywords)


async def openai_chat(
    settings: Settings,
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.2,
    max_tokens: int = 400,
) -> str | None:
    """Call OpenAI chat completions. Returns content or None on failure.

    Args:
        settings: App settings (must have openai_api_key).
        messages: Chat messages.
        temperature: Sampling temperature.
        max_tokens: Max completion tokens.

    Returns:
        Assistant text, or None if the call fails.
    """
    if not settings.openai_enabled:
        return None

    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "model": settings.openai_model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": messages,
        "response_format": {"type": "json_object"},
    }

    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            response = None
            for attempt in range(1, 4):
                response = await client.post(
                    OPENAI_URL, headers=headers, json=payload
                )
                if response.status_code != 429:
                    break
                retry_after = response.headers.get("retry-after", "20")
                try:
                    wait_for = min(60.0, float(retry_after) + 1.0)
                except ValueError:
                    wait_for = 20.0
                logger.warning(
                    "openai rate-limited attempt=%s sleep=%.0fs model=%s",
                    attempt,
                    wait_for,
                    settings.openai_model,
                )
                await asyncio.sleep(wait_for)
            assert response is not None
            if response.status_code >= 400:
                # Never log response bodies (may include request echoes).
                logger.warning(
                    "openai http_status=%s model=%s",
                    response.status_code,
                    settings.openai_model,
                )
                return None
            data = response.json()
    except httpx.HTTPError as exc:
        logger.warning("openai request failed: %s", type(exc).__name__)
        return None
    except (ValueError, KeyError) as exc:
        logger.warning("openai response parse failed: %s", type(exc).__name__)
        return None

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.warning("openai unexpected response shape")
        return None
    if not isinstance(content, str) or not content.strip():
        return None
    return content.strip()


def _parse_json_object(text: str) -> dict[str, Any] | None:
    """Parse a JSON object from model output."""
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        if isinstance(data, dict):
            return data
    return None


async def llm_process_item(
    item: RawItem,
    settings: Settings,
) -> dict[str, Any] | None:
    """One-shot relevance + RU title/summary via OpenAI.

    Returns:
        Dict with keys relevant, score, title_ru, summary_ru — or None on failure.
    """
    title = strip_html(item.title)[:300]
    excerpt = strip_html(item.excerpt)[:1200]
    topics = _topics_blob(settings)

    system = (
        "You are a news editor for an AI music / AI radio Telegram channel. "
        "Decide if the item is on-topic for: AI music generation, Suno, Udio, "
        "generative music tools, AI radio/DJ. "
        "Reply with JSON only: "
        '{"relevant": bool, "score": 0-100, "title_ru": string, "summary_ru": string}. '
        "summary_ru: 2-4 short sentences in Russian, facts only from the input, "
        "no inventing. Keep product names (Suno, Udio, etc.) untranslated. "
        "If already Russian, polish briefly. If off-topic, relevant=false and "
        "empty title_ru/summary_ru."
    )
    user = (
        f"Topics: {topics}\n\n"
        f"Title: {title}\n\n"
        f"Excerpt: {excerpt}\n\n"
        f"Source URL: {item.url}"
    )

    raw = await openai_chat(
        settings,
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=450,
    )
    if not raw:
        return None

    data = _parse_json_object(raw)
    if not data:
        logger.warning("openai json parse failed for item title=%s", title[:60])
        return None

    relevant = bool(data.get("relevant"))
    try:
        score = int(data.get("score", 0))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(100, score))
    title_ru = str(data.get("title_ru") or "").strip()
    summary_ru = str(data.get("summary_ru") or "").strip()
    return {
        "relevant": relevant,
        "score": score,
        "title_ru": title_ru,
        "summary_ru": summary_ru,
    }
