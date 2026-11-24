# AI Air — Telegram news bot

Isolated Python service that collects AI music / Suno RSS feeds, filters by keywords, queues short posts, and publishes to a Telegram group.

**No admin UI.** Configure via `.env` + `sources.yaml` only, then restart the unit.

## Stack

- Python 3.12+, aiogram 3, feedparser, httpx, APScheduler, SQLite
- Default LLM: OpenAI `gpt-4o-mini` (`OPENAI_API_KEY`) for relevance + RU summary/translate
- Keyword prefilter before LLM; extractive fallback if OpenAI is unset/fails
- Optional `GEMINI_API_KEY` hook reserved for later

## Layout on VDS

```
/opt/aiair-news-bot/          # code, venv, .env, sources.yaml
/var/lib/aiair-news-bot/      # SQLite bot.db
/etc/systemd/system/aiair-news-bot.service
```

This unit is **standalone**. Do not edit or restart unrelated bots/services.

## Configure

```bash
cp .env.example .env
chmod 600 .env
# edit TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, schedule, batch, mix
# edit sources.yaml as needed
```

### Publish schedule

| Key | Default | Meaning |
|-----|---------|---------|
| `PUBLISH_MODE` | `cron` | `cron` or `interval` |
| `PUBLISH_CRON` | `0 11 * * mon,wed,fri` | 5-field cron (min hour dom mon dow) |
| `PUBLISH_TIMEZONE` | `Europe/Moscow` | IANA timezone for cron |
| `INTERVAL_MINUTES` | `360` | Used only when `PUBLISH_MODE=interval` |
| `BATCH_SIZE` | `4` | Posts per publish tick |
| `COLLECT_INTERVAL_MINUTES` | `30` | RSS collect cadence |

Default live schedule: **Mon/Wed/Fri 11:00 MSK**, 4 posts per tick.

### Batch mix (Suno vs other)

| Key | Default | Meaning |
|-----|---------|---------|
| `SUNO_MIN_PER_BATCH` | `1` | Preferred minimum Suno posts (best-effort) |
| `SUNO_MAX_PER_BATCH` | `2` | Cap Suno posts in a batch |
| `SUNO_KEYWORDS` | `suno` | Comma-separated markers in title/summary/url |

Fail-soft: if fewer Suno items are queued, the rest of the batch is filled from other AI-music posts (and vice versa).

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill tokens
python -m app --verify
python -m app --once
```

## systemd (VDS)

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now aiair-news-bot
sudo systemctl status aiair-news-bot
journalctl -u aiair-news-bot -f
```

Restart after config changes:

```bash
sudo systemctl restart aiair-news-bot
```

On start the log line `next_publish=...` shows the next cron fire time.

## Telegram group

Bot `@aiairbot_bot` must be a member of the target group/channel with permission to post. Chat id uses the `-100…` supergroup form.

## Ops notes

- Change schedule / batch / mix → edit `.env` → `systemctl restart aiair-news-bot`
- Add RSS → edit `sources.yaml` → restart
- Pause publishing without stopping collect → `PUBLISH_ENABLED=false` + restart
- Dry run → `DRY_RUN=true` + restart
- Backup DB → copy `/var/lib/aiair-news-bot/bot.db`
- Admin Telegram id is for future watchdog DMs only — no command panel
