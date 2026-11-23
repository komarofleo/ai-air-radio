"""RSS collector with fail-soft per-source fetching."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import feedparser
import httpx

from app.db import RawItem

logger = logging.getLogger(__name__)

USER_AGENT = "aiair-news-bot/0.1 (+https://ai-air.ru; RSS collector)"
TIMEOUT_SECONDS = 15.0
MAX_BYTES = 2_000_000


def _parse_published(entry: dict[str, Any]) -> str | None:
    for key in ("published", "updated"):
        raw = entry.get(key)
        if not raw:
            continue
        try:
            dt = parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()
        except (TypeError, ValueError, IndexError, OverflowError):
            continue
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        try:
            dt = datetime(*parsed[:6], tzinfo=timezone.utc)
            return dt.replace(microsecond=0).isoformat()
        except (TypeError, ValueError, OverflowError):
            return None
    return None


def _excerpt(entry: dict[str, Any]) -> str:
    for key in ("summary", "description"):
        value = entry.get(key)
        if value:
            return str(value)
    content = entry.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and first.get("value"):
            return str(first["value"])
    return ""


def _looks_russian(text: str) -> bool:
    cyr = sum(1 for ch in text if "а" <= ch.lower() <= "я" or ch.lower() == "ё")
    latin = sum(1 for ch in text if "a" <= ch.lower() <= "z")
    return cyr > latin and cyr >= 3


async def fetch_feed(
    client: httpx.AsyncClient,
    source_name: str,
    url: str,
) -> list[RawItem]:
    """Fetch and parse one RSS feed. Returns empty list on failure.

    Args:
        client: Shared httpx client.
        source_name: Config source name.
        url: Feed URL.

    Returns:
        Normalized RawItem list (may be empty).
    """
    try:
        response = await client.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"},
            timeout=TIMEOUT_SECONDS,
            follow_redirects=True,
        )
        response.raise_for_status()
        if len(response.content) > MAX_BYTES:
            logger.warning("source=%s feed too large (%s bytes), skipping", source_name, len(response.content))
            return []
        parsed = feedparser.parse(response.content)
    except httpx.HTTPError as exc:
        logger.warning("source=%s fetch failed: %s", source_name, exc)
        return []
    except Exception as exc:  # noqa: BLE001 — fail-soft boundary
        logger.warning("source=%s unexpected parse/fetch error: %s", source_name, exc)
        return []

    items: list[RawItem] = []
    for entry in parsed.entries or []:
        link = (entry.get("link") or "").strip()
        title = (entry.get("title") or "").strip()
        if not link or not title:
            continue
        excerpt = _excerpt(entry)
        blob = f"{title} {excerpt}"
        language = "ru" if _looks_russian(blob) else "en"
        items.append(
            RawItem(
                source_name=source_name,
                url=link,
                title=title,
                excerpt=excerpt,
                published_at=_parse_published(entry),
                language_hint=language,
            )
        )
    logger.info("source=%s fetched=%s", source_name, len(items))
    return items


async def collect_all(
    sources: list[dict[str, Any]],
) -> list[RawItem]:
    """Collect items from all sources; failures are isolated.

    Args:
        sources: Enabled source dicts from YAML.

    Returns:
        Combined list of RawItem.
    """
    results: list[RawItem] = []
    async with httpx.AsyncClient() as client:
        for source in sources:
            name = str(source["name"])
            url = str(source["url"])
            batch = await fetch_feed(client, name, url)
            results.extend(batch)
    return results
