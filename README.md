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


## Latest update UX v12

- the newest author update is shown directly in the main case card
- Court feed previews updated cases with the latest author update instead of stale original text
- original case text remains visible below the latest update
- reopened cases are explicitly marked and rise in the New feed
- update history is shown only when multiple updates exist
- saved-case update notifications open the normal case card, where the latest update is immediately visible


## Best answer and case quality v13

- new case descriptions require at least 30 characters
- completing a case with replies opens a paged best-answer picker
- authors can choose a best answer or explicitly close without one
- selected advice is marked as the author's best answer in normal advice browsing
- best-answer selection is validated against the same case before closing
- stale direct-close buttons cannot bypass the best-answer picker when replies exist


## Case outcome v15

- completed cases show the selected best advice directly in the case card
- case authors can add or edit a final “what happened” outcome after completion
- outcome input survives in-memory FSM loss through the persistent pending-input route
- advisers, discussion participants, and followers can be notified when a case is completed or receives a final outcome
- the selected best-answer author keeps the dedicated +5 reputation notification and is excluded from the generic completion notification
- profiles show completed-case and best-answer counts


## Growth v16

- real cases can be shared with Telegram deep links such as `?start=case_17`
- case deep links open the advertised case immediately after bot start
- advertising links support `src_<source>_<campaign>` attribution
- first-touch acquisition source and campaign are stored per user
- CEO growth analytics show users by source/campaign and conversion into advisers and case authors
- the case card includes a share action for sending the deep link into Telegram chats

## Reliability and community v17

- Fixed case deep links and sharing: native Telegram share URL plus a copy-link button.
- Best-answer rewards are recorded once per case. Reopening/reselecting a best answer does not award another +5. Existing scores are preserved; current legacy best answers are imported into the award ledger. Previously inflated historical scores cannot be reconstructed reliably and are not reset.
- SQLite uses WAL, a busy timeout, and immediate transactions for read/modify/write operations. Repeated concurrent likes and first-advice submissions no longer multiply rewards.
- Menu buttons, commands, and navigation callbacks clear both FSM and pending input. Events for one user are serialized. Bot workflows run in private chats.
- Drafts survive restarts. Category/title/body are saved as entered; a paginated preview supports editing and explicit publication. Revision checks and a transactional publish prevent stale or duplicate publication. Open the draft from My Cases, Profile, or `/draft`.
- Long cases show bounded previews with full-text readers. Updates have their own pager, including pages inside a long update. Unicode/emoji budgets are counted conservatively in UTF-16 units.
- The anonymous case author's staff role is hidden in discussions.
- Threat-report language is no longer rejected solely for mentioning a threatening word. The local filter remains heuristic; it does not replace moderation. Phone and email detection plus reports remain available.
- AI reviews include the most recent 20 updates in chronological order and the outcome of a closed case. There are bounded requests, per-user rate limiting, a short content-based cache, and friendly error messages. A changed update invalidates the content cache. `OPENAI_API_KEY` is still required to enable AI; deployment does not purchase or provision API access.
- The first acquisition source is fixed on the first interaction, including `direct`. Existing users are locked to their known source at migration; previously missing sources are kept as direct rather than inventing historical attribution.

### Moderation and privacy

The CEO assigns `Moderator Anon Verdict` through the existing user card. Moderators open `/moderation` or Profile → Moderation. They can handle reports, hide reported content, mute an offender for 24 hours or 7 days, ban bot access, and remove restrictions they are allowed to remove. Moderators cannot grant roles, see user IDs/usernames in the moderation queue, restrict staff/CEO, or remove a CEO-imposed restriction. The Developer label alone grants no moderation permissions. The CEO can also manage restrictions from user cards and inspect the moderation audit log.

Mute prevents public submissions and ratings while preserving read access. Ban prevents access to the bot. Restrictions are checked server-side. Case content can itself contain identifying details, so authors still need to remove personal information before posting.

### Case notifications

Each real case has a bell button. This preference applies to advice, author updates, outcomes, completion, discussion replies, and helpful/best-answer notifications for that case. The global profile switch remains the master switch. Turning a bell on does not automatically add a case to Favorites; the existing subscription/participation rules determine which events are relevant.

### Backups and restore

The running process holds an exclusive database runtime lock. Before migrations, the bot makes a consistent SQLite online backup and verifies it. It also makes a verified startup snapshot and a snapshot every 6 hours, retaining the latest 28 snapshots by default. Backups have restrictive file permissions and integrity/checksum manifests.

CEO Panel → Backups can create, verify, and download a backup. Snapshots contain private user data. The default backup directory is on the same persistent volume as the database: it protects against software mistakes, not deletion/loss of the entire volume. Keep exported copies in separate private storage, or configure `BACKUP_DIR` on an independently mounted backup disk.

Optional environment settings:

- `BACKUP_DIR`: defaults to the `backups` directory beside `DB_PATH`.
- `BACKUP_INTERVAL_SECONDS`: defaults to `21600` (6 hours), minimum 300 seconds.
- `BACKUP_KEEP`: defaults to `28`, minimum 2.

Offline restore (stop the Railway service first):

```sh
python backups.py verify /app/data/backups/anon-verdict-EXAMPLE.sqlite3
python backups.py restore /app/data/backups/anon-verdict-EXAMPLE.sqlite3 --db /app/data/anon_verdict.sqlite3
```

Use the actual database path and exact snapshot filename. Restore verifies the snapshot before changing anything, refuses to run while this bot holds the runtime lock, makes a pre-restore rollback snapshot, checkpoints the old database, and atomically replaces it. Restart the service after restore. Do not overwrite a live `.sqlite3` file with `cp`.

### Regression checks

Dependencies are pinned; production uses Python 3.12. GitHub Actions runs the offline regression suite on Python 3.11 and 3.12 for PRs and changes to main/feature/fix branches. No production token, user database, Telegram request, or paid AI request is used in tests.

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```
