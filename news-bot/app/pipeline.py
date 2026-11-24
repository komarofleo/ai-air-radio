"""Collect → filter → summarize → enqueue → publish cycle."""

from __future__ import annotations

import logging

from aiogram import Bot

from app.collector import collect_all
from app.config import Settings, load_sources
from app.db import Database, RawItem
from app.filter import is_relevant, score_item
from app.llm import llm_process_item
from app.publisher import publish_batch
from app.summarizer import build_summary

logger = logging.getLogger(__name__)


async def _enqueue_item(
    db: Database,
    item: RawItem,
    *,
    title: str,
    summary: str,
    score: int,
    language: str,
) -> bool:
    """Insert raw + queue post. Returns True if newly queued."""
    raw_id = db.insert_raw_item(item)
    if raw_id is None:
        return False
    db.enqueue_post(
        raw_item_id=raw_id,
        url=item.url,
        title=title,
        summary=summary,
        score=score,
        language=language,
    )
    logger.info(
        "queued score=%s lang=%s title=%s",
        score,
        language,
        title[:80],
    )
    return True


async def run_collect_cycle(db: Database, settings: Settings) -> int:
    """Fetch feeds, filter, summarize, and enqueue new posts.

    Returns:
        Number of newly queued posts.
    """
    sources = load_sources(settings.sources_path)
    if not sources:
        logger.warning("no enabled sources in %s", settings.sources_path)
        return 0

    items = await collect_all(sources)

    # Keyword prefilter + score; process highest scores first.
    candidates: list[tuple[int, RawItem]] = []
    for item in items:
        if db.is_duplicate(item.url, item.title):
            continue
        keyword_ok, keyword_score = is_relevant(item, settings.topic_keywords)
        if not keyword_ok:
            db.insert_raw_item(item)
            continue
        candidates.append((keyword_score, item))

    candidates.sort(key=lambda pair: pair[0], reverse=True)

    queued = 0
    llm_used = 0
    llm_disabled_this_cycle = False
    consecutive_llm_fail = 0

    for keyword_score, item in candidates:
        title = item.title
        score = keyword_score
        use_llm = (
            settings.openai_enabled
            and not llm_disabled_this_cycle
            and llm_used < settings.llm_max_per_cycle
        )

        if use_llm:
            llm = await llm_process_item(item, settings)
            llm_used += 1
            if llm is None:
                consecutive_llm_fail += 1
                logger.warning(
                    "llm failed; extractive fallback title=%s",
                    item.title[:80],
                )
                if consecutive_llm_fail >= 2:
                    llm_disabled_this_cycle = True
                    logger.warning(
                        "disabling llm for rest of cycle after consecutive failures"
                    )
                summary, language = await build_summary(item, settings)
            elif not llm["relevant"] or int(llm["score"]) < 40:
                db.insert_raw_item(item)
                logger.info(
                    "skipped llm-offtopic score=%s title=%s",
                    llm["score"],
                    item.title[:80],
                )
                consecutive_llm_fail = 0
                continue
            else:
                consecutive_llm_fail = 0
                score = max(keyword_score, int(llm["score"]))
                title = llm["title_ru"] or item.title
                summary = llm["summary_ru"] or (await build_summary(item, settings))[0]
                language = "ru"
        else:
            # Still score with keywords; optional extractive body.
            summary, language = await build_summary(item, settings)
            score = score_item(item, settings.topic_keywords)

        if await _enqueue_item(
            db,
            item,
            title=title,
            summary=summary,
            score=score,
            language=language,
        ):
            queued += 1

    counts = db.counts()
    logger.info(
        "collect done new_queued=%s llm_used=%s totals raw=%s queued=%s sent=%s",
        queued,
        llm_used,
        counts["raw"],
        counts["queued"],
        counts["sent"],
    )
    return queued


async def run_publish_cycle(
    bot: Bot,
    db: Database,
    settings: Settings,
) -> int:
    """Publish one batch from the queue."""
    return await publish_batch(bot, db, settings)


async def run_once(bot: Bot, db: Database, settings: Settings) -> None:
    """Single full cycle: collect then publish."""
    await run_collect_cycle(db, settings)
    await run_publish_cycle(bot, db, settings)
