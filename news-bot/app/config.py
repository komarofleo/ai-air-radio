"""Typed configuration from .env and YAML sources file."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _blank_to_none(value: Any) -> str | None:
    """Treat blank optional secrets as unset."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class Settings(BaseSettings):
    """Runtime settings loaded from environment / .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    telegram_bot_token: str = Field(..., min_length=10)
    telegram_chat_id: str = Field(..., min_length=1)
    telegram_admin_id: int = 329621295

    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"

    # Optional future hook; Gemini not required for MVP.
    gemini_api_key: str | None = None

    database_path: Path = Path("/var/lib/aiair-news-bot/bot.db")
    sources_path: Path = Path("/opt/aiair-news-bot/sources.yaml")

    batch_size: int = Field(default=2, ge=1, le=20)
    interval_minutes: int = Field(default=360, ge=5)
    collect_interval_minutes: int = Field(default=30, ge=5)
    llm_max_per_cycle: int = Field(default=8, ge=0, le=50)
    publish_enabled: bool = True
    translate_enabled: bool = True
    dry_run: bool = False

    topics: str = (
        "suno,udio,ai music,generative music,ai radio,"
        "music generation,text-to-music,ai dj"
    )

    log_level: str = "INFO"

    @field_validator("openai_api_key", "gemini_api_key", mode="before")
    @classmethod
    def empty_secret_to_none(cls, value: Any) -> str | None:
        """Treat blank API keys as unset."""
        return _blank_to_none(value)

    @property
    def topic_keywords(self) -> list[str]:
        """Parse comma-separated topic keywords."""
        return [part.strip().lower() for part in self.topics.split(",") if part.strip()]

    @property
    def openai_enabled(self) -> bool:
        """True when an OpenAI API key is configured."""
        return bool(self.openai_api_key)

    @property
    def gemini_enabled(self) -> bool:
        """True when a Gemini API key is configured (optional later hook)."""
        return bool(self.gemini_api_key)


def load_sources(path: Path) -> list[dict[str, Any]]:
    """Load enabled RSS sources from YAML.

    Args:
        path: Path to sources.yaml.

    Returns:
        List of source dicts with keys name, type, url, enabled.
    """
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    sources = data.get("sources") or []
    result: list[dict[str, Any]] = []
    for item in sources:
        if not isinstance(item, dict):
            continue
        if not item.get("enabled", True):
            continue
        if item.get("type", "rss") != "rss":
            continue
        url = item.get("url")
        name = item.get("name")
        if not url or not name:
            continue
        result.append(
            {
                "name": str(name),
                "type": "rss",
                "url": str(url),
                "enabled": True,
            }
        )
    return result
