"""Keyword topic filter and simple relevance scoring."""

from __future__ import annotations

import re
from html import unescape

from app.db import RawItem


def strip_html(text: str) -> str:
    """Remove HTML tags and unescape entities."""
    no_tags = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", unescape(no_tags)).strip()


def score_item(item: RawItem, keywords: list[str]) -> int:
    """Score relevance by keyword hits in title + excerpt.

    Args:
        item: Collected item.
        keywords: Lowercase topic keywords.

    Returns:
        Integer score (0 = off-topic).
    """
    title = strip_html(item.title).lower()
    excerpt = strip_html(item.excerpt).lower()
    blob = f"{title} {excerpt}"
    score = 0
    for keyword in keywords:
        if not keyword:
            continue
        if keyword in title:
            score += 10
        elif keyword in blob:
            score += 4
    # Light boost for well-known product names
    for brand in ("suno", "udio", "aiva", "soundraw"):
        if brand in title:
            score += 3
    return score


def is_relevant(item: RawItem, keywords: list[str], min_score: int = 4) -> tuple[bool, int]:
    """Return (relevant, score) for an item.

    Args:
        item: Collected item.
        keywords: Topic keywords.
        min_score: Minimum score to accept.

    Returns:
        Tuple of relevance flag and score.
    """
    score = score_item(item, keywords)
    return score >= min_score, score
