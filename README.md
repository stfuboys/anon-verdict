# anon-verdict

Anon Verdict is an anonymous Telegram platform for sharing life situations and getting advice.

## Current architecture

- `bot.py` — Telegram UI and handlers (aiogram)
- `db.py` — SQLite persistence, reputation, ranks, demo content
- `moderation.py` — basic local PII/threat filtering
- `ai.py` — optional AI case review
- Railway volume: `/app/data`
- Production source: `main`

## Environment variables

Required:
- `BOT_TOKEN`
- `DB_PATH` (production currently points to persistent Railway storage)

Optional:
- `OWNER_TG_ID` — assigns `CEO Anon Verdict` to the matching Telegram account
- `DEVELOPER_TG_IDS` — comma-separated Telegram IDs for developer roles
- `OPENAI_API_KEY`
- `OPENAI_MODEL`

Do not commit real tokens or private Telegram IDs to this public repository.
