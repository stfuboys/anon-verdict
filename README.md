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


## CEO panel

When `OWNER_TG_ID` matches the current Telegram user, the owner sees a private `🛡️ CEO Панель` button and can also use `/admin`.

Current owner-only controls:
- project statistics
- recent users
- user lookup by Telegram ID
- assign/remove team labels (Developer / Moderator)
- recent real cases
- hide a case from the public court or restore it

All admin callbacks verify the owner Telegram ID server-side. Team labels do not grant CEO permissions.


## Community v3

The bot now includes:
- paginated favorites that also act as lightweight subscriptions
- notifications for new advice on your case, updates to saved cases, and useful likes
- user-controlled notification toggle in profile
- reports for cases and advice with a private CEO moderation queue
- anti-spam cooldowns for new cases and advice
- anti-farming rules: users cannot advise their own case, and only the first advice to the same case earns the +2 posting reward
- richer leaderboard context with advice counts and helpful likes

Moderation actions are restricted to the configured `OWNER_TG_ID`.


## Discussions v5

Each real case now has its own discussion area, separate from formal advice.

- discussion messages do not grant reputation
- the case owner appears as `👑 Автор` without exposing Telegram identity
- participants can reply to a specific discussion message
- discussion messages support lightweight likes
- discussion messages can be reported and moderated from the CEO queue
- direct replies can notify the recipient
- followers of a saved case are notified when the case author posts in the discussion
- discussion posting has its own cooldown to reduce spam
