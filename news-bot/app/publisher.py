"""Telegram publisher via aiogram 3 (send only; no admin commands)."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter

from app.config import Settings
from app.db import Database, QueuedPost
from app.summarizer import format_telegram_message

logger = logging.getLogger(__name__)


def create_bot(settings: Settings) -> Bot:
    """Create an aiogram Bot instance."""
    return Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


async def verify_bot(bot: Bot) -> dict[str, object]:
    """Call getMe and return bot identity (no secrets)."""
    me = await bot.get_me()
    return {
        "id": me.id,
        "username": me.username,
        "first_name": me.first_name,
        "can_join_groups": me.can_join_groups,
        "can_read_all_group_messages": me.can_read_all_group_messages,
    }


async def publish_batch(
    bot: Bot,
    db: Database,
    settings: Settings,
) -> int:
    """Publish up to batch_size queued posts.

    Args:
        bot: aiogram Bot.
        db: Database.
        settings: Settings.

    Returns:
        Number of successfully sent messages.
    """
    if not settings.publish_enabled:
        logger.info("publish disabled via PUBLISH_ENABLED=false")
        return 0

    posts = db.fetch_queued(settings.batch_size)
    if not posts:
        logger.info("publish queue empty")
        return 0

    sent = 0
    for post in posts:
        ok = await publish_one(bot, db, settings, post)
        if ok:
            sent += 1
        await asyncio.sleep(1.5)
    return sent


async def publish_one(
    bot: Bot,
    db: Database,
    settings: Settings,
    post: QueuedPost,
) -> bool:
    """Publish a single queued post (or dry-run skip)."""
    text = format_telegram_message(post.title, post.summary, post.url)

    if settings.dry_run:
        logger.info("DRY_RUN skip send post_id=%s title=%s", post.id, post.title[:80])
        db.mark_skipped(post.id, "dry_run")
        return False

    try:
        message = await bot.send_message(
            chat_id=settings.telegram_chat_id,
            text=text,
            disable_web_page_preview=False,
        )
    except TelegramRetryAfter as exc:
        wait_for = float(getattr(exc, "retry_after", 30)) + 1.0
        logger.warning(
            "telegram flood post_id=%s sleep=%.0fs",
            post.id,
            wait_for,
        )
        await asyncio.sleep(wait_for)
        try:
            message = await bot.send_message(
                chat_id=settings.telegram_chat_id,
                text=text,
                disable_web_page_preview=False,
            )
        except TelegramRetryAfter as retry_exc:
            logger.error(
                "telegram still flooded post_id=%s; requeue error=%s",
                post.id,
                retry_exc,
            )
            db.mark_failed(post.id, str(retry_exc), requeue=True)
            return False
        except TelegramAPIError as retry_exc:
            logger.error(
                "telegram send failed post_id=%s error=%s",
                post.id,
                retry_exc,
            )
            db.mark_failed(post.id, str(retry_exc))
            return False
    except TelegramAPIError as exc:
        logger.error(
            "telegram send failed post_id=%s error=%s",
            post.id,
            exc,
        )
        db.mark_failed(post.id, str(exc))
        return False

    db.mark_sent(post.id, message.message_id)
    logger.info(
        "sent post_id=%s tg_message_id=%s",
        post.id,
        message.message_id,
    )
    return True
