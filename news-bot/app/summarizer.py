"""Summary helpers: OpenAI RU summary with extractive fallback."""

from __future__ import annotations

import logging
import re
from html import unescape

from app.config import Settings
from app.db import RawItem
from app.filter import strip_html

logger = logging.getLogger(__name__)


def _first_sentences(text: str, max_chars: int = 420) -> str:
    """Take leading sentences up to max_chars."""
    cleaned = strip_html(text)
    if not cleaned:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", cleaned)
    out: list[str] = []
    length = 0
    for part in parts:
        piece = part.strip()
        if not piece:
            continue
        if length + len(piece) + 1 > max_chars and out:
            break
        out.append(piece)
        length += len(piece) + 1
        if length >= max_chars * 0.7 and len(out) >= 2:
            break
    summary = " ".join(out).strip()
    if len(summary) > max_chars:
        summary = summary[: max_chars - 1].rstrip() + "…"
    return summary


def extractive_summary(item: RawItem) -> tuple[str, str]:
    """Build a short summary without LLM.

    Returns:
        (summary_text, language) where language is 'ru' or 'en'.
    """
    language = item.language_hint or "en"
    body = _first_sentences(item.excerpt) or _first_sentences(item.title)
    if not body:
        body = strip_html(item.title)

    if language == "ru":
        return body, "ru"

    note = "\n\n[EN] Краткое изложение без перевода (LLM недоступен)."
    return f"{body}{note}", "en"


async def build_summary(item: RawItem, settings: Settings) -> tuple[str, str]:
    """Produce summary text and language code (extractive fallback).

    OpenAI path is preferred in the pipeline via llm_process_item; this
    remains as a safe fallback when LLM is off or failed earlier.
    """
    _ = settings
    return extractive_summary(item)


def format_telegram_message(title: str, summary: str, url: str) -> str:
    """Format HTML message for Telegram sendMessage.

    Args:
        title: Post title.
        summary: Short body.
        url: Original article URL.

    Returns:
        HTML-formatted message string.
    """

    def _esc(text: str) -> str:
        return (
            unescape(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    return (
        f"<b>{_esc(title)}</b>\n\n"
        f"{_esc(summary)}\n\n"
        f'<a href="{_esc(url)}">Источник</a>'
    )
