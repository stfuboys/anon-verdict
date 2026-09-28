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


## UX v6

The Telegram interface is streamlined around a smaller number of primary actions.

- persistent keyboard now contains only the six core destinations
- ranks and help live inside the profile instead of the global keyboard
- case cards prioritize advice and discussion; reports are moved under secondary actions
- advice, AI analysis, discussion, favorites, and personal cases preserve return context
- discussions show compact windows of recent messages instead of one message per screen
- profile shows case/advice/helpful-like stats and progress toward the next rank
- primary reply-keyboard navigation renders the destination directly without temporary “opening…” messages
- back-button wording and empty states are standardized


## Court card v7

The court feed card is now more expressive and less repetitive.

- feed cards show a simple status such as "needs advice", "discussion active", or "open"
- the duplicate page counter was removed from the card body; navigation keeps the page position
- metadata is visually separated from the story excerpt
- pending advice/discussion input has an explicit cancel action
- navigating away from a text-entry flow clears the stale FSM state, preventing later messages from being submitted to the wrong action
- canceling returns to the relevant case or discussion context


## Case lifecycle v9

Case authors can manage the lifecycle of their own real cases.

- open cases accept new advice and discussion messages
- completed cases leave the active Court feed but remain readable from My Cases and Favorites
- completed cases can be reopened by their author
- deleted cases are soft-deleted: they disappear from user-facing lists while historical advice, reactions, moderation data, and reputation remain intact
- destructive deletion requires confirmation
- owner controls are exposed through a dedicated case management screen
- moderation hiding preserves the case's previous open/completed state for safe restoration


## Case updates and advice inbox v10

- case authors can append situation updates without overwriting the original story
- saved-case subscribers are notified when the author posts an update
- owner advice notifications are aggregated into one editable Telegram message per case
- opening the advice pager marks current answers as read and refreshes the aggregate card
- advice remains a one-card pager with previous/current/next navigation
