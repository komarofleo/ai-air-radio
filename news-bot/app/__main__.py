"""Entrypoint: `python -m app` — scheduled collect + publish loops."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from aiogram.exceptions import TelegramRetryAfter
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import Settings
from app.db import Database
from app.pipeline import run_collect_cycle, run_once, run_publish_cycle
from app.publisher import create_bot, verify_bot


def setup_logging(level: str) -> None:
    """Configure root logger."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )


async def async_main(argv: list[str] | None = None) -> int:
    """Run bot in once / verify / daemon mode."""
    parser = argparse.ArgumentParser(description="AI Air Telegram news bot")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one collect+publish cycle and exit",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Call Telegram getMe and exit (no publish)",
    )
    parser.add_argument(
        "--publish-only",
        action="store_true",
        help="Publish one batch from queue and exit",
    )
    parser.add_argument(
        "--collect-only",
        action="store_true",
        help="Collect/filter/enqueue only and exit",
    )
    args = parser.parse_args(argv)

    settings = Settings()
    setup_logging(settings.log_level)
    logger = logging.getLogger("app")

    db = Database(settings.database_path)
    bot = create_bot(settings)

    try:
        if args.verify:
            identity = await _verify_with_retry(bot, logger, attempts=8)
            logger.info(
                "telegram ok username=@%s id=%s",
                identity.get("username"),
                identity.get("id"),
            )
            return 0

        # Skip getMe for collect/publish-only to avoid Telegram flood loops.
        if args.collect_only:
            logger.info(
                "collect-only openai=%s model=%s",
                settings.openai_enabled,
                settings.openai_model if settings.openai_enabled else "-",
            )
            await run_collect_cycle(db, settings)
            return 0

        if args.publish_only:
            logger.info("publish-only starting (no getMe)")
            sent = await run_publish_cycle(bot, db, settings)
            logger.info("publish-only sent=%s", sent)
            return 0

        # Daemon: skip getMe to avoid Telegram flood control loops.
        logger.info(
            "daemon starting openai=%s model=%s batch=%s interval=%sm",
            settings.openai_enabled,
            settings.openai_model if settings.openai_enabled else "-",
            settings.batch_size,
            settings.interval_minutes,
        )

        if args.once:
            await run_once(bot, db, settings)
            return 0

        # Daemon: schedule collect and publish on separate intervals.
        scheduler = AsyncIOScheduler()
        scheduler.add_job(
            run_collect_cycle,
            "interval",
            minutes=settings.collect_interval_minutes,
            args=[db, settings],
            id="collect",
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            run_publish_cycle,
            "interval",
            minutes=settings.interval_minutes,
            args=[bot, db, settings],
            id="publish",
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
        logger.info(
            "scheduler started collect_every=%sm publish_every=%sm batch=%s",
            settings.collect_interval_minutes,
            settings.interval_minutes,
            settings.batch_size,
        )
        # Warm-up collect only. First publish waits for the scheduled interval
        # (avoids Telegram flood loops on rapid restarts).
        await run_collect_cycle(db, settings)

        stop = asyncio.Event()
        await stop.wait()
        return 0
    finally:
        await bot.session.close()
        db.close()


async def _verify_with_retry(
    bot: object,
    logger: logging.Logger,
    attempts: int = 6,
) -> dict[str, object]:
    """Call getMe with RetryAfter backoff so flood control does not crash-loop."""
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await verify_bot(bot)  # type: ignore[arg-type]
        except TelegramRetryAfter as exc:
            last_exc = exc
            wait_for = float(getattr(exc, "retry_after", 30)) + 1.0
            logger.warning(
                "getMe flood control attempt=%s/%s sleep=%.0fs",
                attempt,
                attempts,
                wait_for,
            )
            await asyncio.sleep(wait_for)
    assert last_exc is not None
    raise last_exc


def main() -> None:
    """Sync wrapper for console_scripts / python -m."""
    try:
        raise SystemExit(asyncio.run(async_main()))
    except KeyboardInterrupt:
        raise SystemExit(0) from None


if __name__ == "__main__":
    main()
