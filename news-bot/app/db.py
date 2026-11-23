"""SQLite persistence for raw items, queue, and publish log."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


def utc_now() -> str:
    """Return current UTC time as ISO-8601 string."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_url(url: str) -> str:
    """Strip tracking params and trailing slash for stable dedup."""
    parsed = urlparse(url.strip())
    drop_prefixes = ("utm_", "fbclid", "gclid", "mc_")
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not any(key.lower().startswith(prefix) for prefix in drop_prefixes)
    ]
    cleaned = parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
        path=parsed.path.rstrip("/") or "/",
        query=urlencode(query_pairs),
        fragment="",
    )
    return urlunparse(cleaned)


def normalize_title(title: str) -> str:
    """Lowercase and collapse whitespace for title dedup."""
    return re.sub(r"\s+", " ", title.strip().lower())


def content_hash(normalized_url: str) -> str:
    """SHA-256 of normalized URL."""
    return hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()


@dataclass
class RawItem:
    """Collected RSS item ready for filter/queue."""

    source_name: str
    url: str
    title: str
    excerpt: str
    published_at: str | None
    language_hint: str | None = None


@dataclass
class QueuedPost:
    """Post waiting for (or having completed) publish."""

    id: int
    url: str
    title: str
    summary: str
    score: int
    status: str
    language: str


class Database:
    """Thin SQLite wrapper for the news bot."""

    def __init__(self, path: Path) -> None:
        """Open (or create) the database file.

        Args:
            path: SQLite file path.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS raw_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_name TEXT NOT NULL,
                url TEXT NOT NULL,
                normalized_url TEXT NOT NULL,
                title TEXT NOT NULL,
                title_norm TEXT NOT NULL,
                excerpt TEXT NOT NULL DEFAULT '',
                published_at TEXT,
                content_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS processed_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                raw_item_id INTEGER NOT NULL UNIQUE,
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                score INTEGER NOT NULL DEFAULT 0,
                language TEXT NOT NULL DEFAULT 'en',
                status TEXT NOT NULL DEFAULT 'queued',
                retry_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (raw_item_id) REFERENCES raw_items(id)
            );

            CREATE TABLE IF NOT EXISTS publish_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER NOT NULL,
                tg_message_id INTEGER,
                sent_at TEXT,
                error TEXT,
                FOREIGN KEY (post_id) REFERENCES processed_posts(id)
            );

            CREATE INDEX IF NOT EXISTS idx_raw_title_norm
                ON raw_items(title_norm);
            CREATE INDEX IF NOT EXISTS idx_posts_status_score
                ON processed_posts(status, score DESC, id ASC);
            """
        )
        self._conn.commit()

    def close(self) -> None:
        """Close the connection."""
        self._conn.close()

    def is_duplicate(self, url: str, title: str) -> bool:
        """Return True if URL hash or recent similar title already exists."""
        norm_url = normalize_url(url)
        digest = content_hash(norm_url)
        title_norm = normalize_title(title)
        row = self._conn.execute(
            "SELECT 1 FROM raw_items WHERE content_hash = ? LIMIT 1",
            (digest,),
        ).fetchone()
        if row:
            return True
        row = self._conn.execute(
            """
            SELECT 1 FROM raw_items
            WHERE title_norm = ?
              AND created_at >= datetime('now', '-14 days')
            LIMIT 1
            """,
            (title_norm,),
        ).fetchone()
        return row is not None

    def insert_raw_item(self, item: RawItem) -> int | None:
        """Insert a raw item if not duplicate. Returns new id or None."""
        if self.is_duplicate(item.url, item.title):
            return None
        norm_url = normalize_url(item.url)
        digest = content_hash(norm_url)
        title_norm = normalize_title(item.title)
        cur = self._conn.execute(
            """
            INSERT INTO raw_items (
                source_name, url, normalized_url, title, title_norm,
                excerpt, published_at, content_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.source_name,
                item.url,
                norm_url,
                item.title,
                title_norm,
                item.excerpt or "",
                item.published_at,
                digest,
                utc_now(),
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def enqueue_post(
        self,
        raw_item_id: int,
        url: str,
        title: str,
        summary: str,
        score: int,
        language: str,
    ) -> int:
        """Create a queued processed post linked to a raw item."""
        now = utc_now()
        cur = self._conn.execute(
            """
            INSERT INTO processed_posts (
                raw_item_id, url, title, summary, score, language,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)
            """,
            (raw_item_id, url, title, summary, score, language, now, now),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def fetch_queued(self, limit: int) -> list[QueuedPost]:
        """Fetch up to `limit` queued posts ordered by score then id."""
        rows = self._conn.execute(
            """
            SELECT id, url, title, summary, score, status, language
            FROM processed_posts
            WHERE status = 'queued'
            ORDER BY score DESC, id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            QueuedPost(
                id=int(row["id"]),
                url=row["url"],
                title=row["title"],
                summary=row["summary"],
                score=int(row["score"]),
                status=row["status"],
                language=row["language"],
            )
            for row in rows
        ]

    def mark_sent(self, post_id: int, tg_message_id: int | None) -> None:
        """Mark post as sent and write publish log."""
        now = utc_now()
        self._conn.execute(
            """
            UPDATE processed_posts
            SET status = 'sent', updated_at = ?
            WHERE id = ?
            """,
            (now, post_id),
        )
        self._conn.execute(
            """
            INSERT INTO publish_log (post_id, tg_message_id, sent_at, error)
            VALUES (?, ?, ?, NULL)
            """,
            (post_id, tg_message_id, now),
        )
        self._conn.commit()

    def mark_failed(self, post_id: int, error: str, *, requeue: bool = False) -> None:
        """Increment retry; optionally keep post queued for later retry."""
        now = utc_now()
        status = "queued" if requeue else "failed"
        self._conn.execute(
            """
            UPDATE processed_posts
            SET status = ?,
                retry_count = retry_count + 1,
                updated_at = ?
            WHERE id = ?
            """,
            (status, now, post_id),
        )
        self._conn.execute(
            """
            INSERT INTO publish_log (post_id, tg_message_id, sent_at, error)
            VALUES (?, NULL, ?, ?)
            """,
            (post_id, now, error[:1000]),
        )
        self._conn.commit()

    def mark_skipped(self, post_id: int, reason: str) -> None:
        """Mark post skipped (e.g. dry-run or off-topic late)."""
        now = utc_now()
        self._conn.execute(
            """
            UPDATE processed_posts
            SET status = 'skipped', updated_at = ?
            WHERE id = ?
            """,
            (now, post_id),
        )
        self._conn.execute(
            """
            INSERT INTO publish_log (post_id, tg_message_id, sent_at, error)
            VALUES (?, NULL, ?, ?)
            """,
            (post_id, now, reason[:1000]),
        )
        self._conn.commit()

    def counts(self) -> dict[str, int]:
        """Return simple status counts for logging."""
        raw = self._conn.execute("SELECT COUNT(*) AS c FROM raw_items").fetchone()
        queued = self._conn.execute(
            "SELECT COUNT(*) AS c FROM processed_posts WHERE status = 'queued'"
        ).fetchone()
        sent = self._conn.execute(
            "SELECT COUNT(*) AS c FROM processed_posts WHERE status = 'sent'"
        ).fetchone()
        return {
            "raw": int(raw["c"]),
            "queued": int(queued["c"]),
            "sent": int(sent["c"]),
        }
