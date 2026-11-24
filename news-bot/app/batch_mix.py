"""Select a mixed publish batch: some Suno, rest other AI-music."""

from __future__ import annotations

import logging

from app.db import QueuedPost

logger = logging.getLogger(__name__)


def is_suno_post(post: QueuedPost, keywords: list[str]) -> bool:
    """Return True if title/summary/url matches Suno keywords."""
    blob = f"{post.title} {post.summary} {post.url}".lower()
    return any(keyword in blob for keyword in keywords if keyword)


def select_mixed_batch(
    candidates: list[QueuedPost],
    *,
    batch_size: int,
    suno_min: int,
    suno_max: int,
    suno_keywords: list[str],
) -> list[QueuedPost]:
    """Pick up to batch_size posts with a Suno / non-Suno mix.

    Aims for ``suno_min``–``suno_max`` Suno items when available; fills the
    rest from non-Suno. Fail-soft: if not enough of one bucket, fill from the
    other so the batch still reaches ``batch_size`` when possible.

    Args:
        candidates: Queued posts ordered by preference (score desc).
        batch_size: Target batch size.
        suno_min: Preferred minimum Suno posts (best-effort).
        suno_max: Maximum Suno posts in the batch.
        suno_keywords: Keywords that mark Suno-focused items.

    Returns:
        Selected posts (may be shorter than batch_size if queue is thin).
    """
    if batch_size <= 0 or not candidates:
        return []

    suno_max = max(0, min(suno_max, batch_size))
    suno_min = max(0, min(suno_min, suno_max))

    suno: list[QueuedPost] = []
    other: list[QueuedPost] = []
    for post in candidates:
        if is_suno_post(post, suno_keywords):
            suno.append(post)
        else:
            other.append(post)

    # Prefer up to suno_max; if we have at least suno_min, stay in [min, max].
    if len(suno) >= suno_min:
        suno_take = min(len(suno), suno_max)
    else:
        suno_take = len(suno)  # fail-soft below preferred minimum

    selected: list[QueuedPost] = list(suno[:suno_take])
    remaining = batch_size - len(selected)
    selected.extend(other[:remaining])

    # If still short, top up from leftover Suno (beyond the preferred max).
    if len(selected) < batch_size:
        used_ids = {post.id for post in selected}
        for post in suno:
            if post.id in used_ids:
                continue
            selected.append(post)
            if len(selected) >= batch_size:
                break

    logger.info(
        "batch_mix selected=%s suno=%s other=%s candidates=%s "
        "(target suno %s-%s batch=%s)",
        len(selected),
        sum(1 for post in selected if is_suno_post(post, suno_keywords)),
        sum(1 for post in selected if not is_suno_post(post, suno_keywords)),
        len(candidates),
        suno_min,
        suno_max,
        batch_size,
    )
    return selected
