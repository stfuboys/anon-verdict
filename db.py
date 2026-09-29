import json
import aiosqlite
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path


class PostingRestricted(Exception):
    """A server-side publishing restriction, safe to show to its subject."""


RANKS = [
    (0, 1, "Новичок"),
    (10, 2, "Присяжный"),
    (30, 3, "Судья"),
    (75, 4, "Старший судья"),
    (150, 5, "Верховный судья"),
]


def now():
    return datetime.now(timezone.utc).isoformat()


def progress_for(reputation: int):
    reputation = max(0, int(reputation or 0))
    level = 1
    title = "Новичок"
    for threshold, rank_level, rank_title in RANKS:
        if reputation >= threshold:
            level = rank_level
            title = rank_title
    return level, title


class DB:
    def __init__(self, path, staff_roles=None):
        self.path = path
        self.staff_roles = staff_roles or {}

    @asynccontextmanager
    async def connect(self):
        async with aiosqlite.connect(self.path, timeout=20) as connection:
            await connection.execute('PRAGMA busy_timeout=20000')
            yield connection

    async def require_active(self, connection, tg_id, posting=True):
        cur = await connection.execute(
            'SELECT is_banned, muted_until FROM users WHERE tg_id=?', (tg_id,)
        )
        user = await cur.fetchone()
        if user and user[0]:
            raise PostingRestricted('Доступ к боту заблокирован модерацией.')
        if user and posting and user[1] and user[1] > now():
            until = datetime.fromisoformat(user[1]).astimezone(timezone(timedelta(hours=3)))
            raise PostingRestricted('Публикации временно ограничены до ' + until.strftime('%d.%m %H:%M МСК') + '.')

    async def _ensure_column(self, db, table, column, definition):
        cur = await db.execute(f"PRAGMA table_info({table})")
        columns = {row[1] for row in await cur.fetchall()}
        if column not in columns:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    async def init(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        async with self.connect() as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS users(
                  id INTEGER PRIMARY KEY,
                  tg_id INTEGER UNIQUE NOT NULL,
                  tg_username TEXT,
                  nickname TEXT NOT NULL,
                  bio TEXT DEFAULT '',
                  reputation INTEGER DEFAULT 0,
                  level INTEGER DEFAULT 1,
                  title TEXT DEFAULT 'Новичок',
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS stories(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  author_id INTEGER NOT NULL,
                  category TEXT NOT NULL,
                  title TEXT NOT NULL,
                  body TEXT NOT NULL,
                  status TEXT DEFAULT 'open',
                  views INTEGER DEFAULT 0,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS comments(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  story_id INTEGER NOT NULL,
                  author_id INTEGER NOT NULL,
                  body TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS story_updates(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  story_id INTEGER NOT NULL,
                  author_id INTEGER NOT NULL,
                  body TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS advice_inbox(
                  owner_id INTEGER NOT NULL,
                  story_id INTEGER NOT NULL,
                  message_id INTEGER,
                  last_seen_count INTEGER DEFAULT 0,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY(owner_id, story_id)
                );

                CREATE TABLE IF NOT EXISTS pending_inputs(
                  user_id INTEGER PRIMARY KEY,
                  action TEXT NOT NULL,
                  payload TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS reactions(
                  user_id INTEGER NOT NULL,
                  comment_id INTEGER NOT NULL,
                  value INTEGER NOT NULL,
                  PRIMARY KEY(user_id, comment_id)
                );

                CREATE TABLE IF NOT EXISTS follows(
                  follower_id INTEGER NOT NULL,
                  following_id INTEGER NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(follower_id, following_id)
                );

                CREATE TABLE IF NOT EXISTS app_meta(
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS favorites(
                  user_id INTEGER NOT NULL,
                  story_id INTEGER NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(user_id, story_id)
                );

                CREATE TABLE IF NOT EXISTS reports(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  reporter_id INTEGER NOT NULL,
                  target_type TEXT NOT NULL,
                  target_id INTEGER NOT NULL,
                  reason TEXT NOT NULL,
                  status TEXT DEFAULT 'open',
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS story_views(
                  user_id INTEGER NOT NULL,
                  story_id INTEGER NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(user_id, story_id)
                );

                CREATE TABLE IF NOT EXISTS discussion_messages(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  story_id INTEGER NOT NULL,
                  author_id INTEGER NOT NULL,
                  body TEXT NOT NULL,
                  reply_to_id INTEGER,
                  status TEXT DEFAULT 'open',
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS discussion_reactions(
                  user_id INTEGER NOT NULL,
                  message_id INTEGER NOT NULL,
                  value INTEGER NOT NULL DEFAULT 1,
                  PRIMARY KEY(user_id, message_id)
                );

                CREATE TABLE IF NOT EXISTS growth_events(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER NOT NULL,
                  event_type TEXT NOT NULL,
                  source TEXT DEFAULT '',
                  campaign TEXT DEFAULT '',
                  story_id INTEGER,
                  created_at TEXT NOT NULL
                );
                """
            )

            await self._ensure_column(db, "users", "staff_role", "TEXT DEFAULT ''")
            await self._ensure_column(db, "users", "notifications_enabled", "INTEGER DEFAULT 1")
            await self._ensure_column(db, "users", "acquisition_source", "TEXT DEFAULT ''")
            await self._ensure_column(db, "users", "acquisition_campaign", "TEXT DEFAULT ''")
            await self._ensure_column(db, "users", "acquisition_case_id", "INTEGER")
            await self._ensure_column(db, "stories", "is_demo", "INTEGER DEFAULT 0")
            await self._ensure_column(db, "stories", "status_before_hidden", "TEXT DEFAULT ''")
            await self._ensure_column(db, "stories", "reopened_at", "TEXT DEFAULT ''")
            await self._ensure_column(db, "stories", "best_comment_id", "INTEGER")
            await self._ensure_column(db, "stories", "outcome_body", "TEXT DEFAULT ''")
            await self._ensure_column(db, "stories", "outcome_created_at", "TEXT DEFAULT ''")
            await self._ensure_column(db, "comments", "status", "TEXT DEFAULT 'open'")
            await self._ensure_column(db, "users", "is_banned", "INTEGER DEFAULT 0")
            await self._ensure_column(db, "users", "muted_until", "TEXT DEFAULT ''")
            await self._ensure_column(db, "users", "acquisition_locked", "INTEGER DEFAULT 0")

            await db.executescript('''
                CREATE TABLE IF NOT EXISTS best_answer_awards(
                  story_id INTEGER PRIMARY KEY,
                  comment_id INTEGER NOT NULL,
                  user_id INTEGER NOT NULL,
                  points INTEGER NOT NULL DEFAULT 5,
                  created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS story_drafts(
                  user_id INTEGER PRIMARY KEY,
                  category TEXT DEFAULT '', title TEXT DEFAULT '', body TEXT DEFAULT '',
                  stage TEXT NOT NULL DEFAULT 'category',
                  revision INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS case_notifications(
                  user_id INTEGER NOT NULL, story_id INTEGER NOT NULL,
                  enabled INTEGER NOT NULL DEFAULT 1,
                  PRIMARY KEY(user_id, story_id)
                );
                CREATE TABLE IF NOT EXISTS moderation_audit(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  actor_tg_id INTEGER NOT NULL, target_tg_id INTEGER,
                  action TEXT NOT NULL, reason TEXT DEFAULT '', created_at TEXT NOT NULL
                );
            ''')
            cur = await db.execute("SELECT 1 FROM app_meta WHERE key='reliability_v17'")
            if not await cur.fetchone():
                # Keep existing reputation; previously selected answers have
                # already received their award. Do not issue it again.
                await db.execute('''
                    INSERT OR IGNORE INTO best_answer_awards(story_id, comment_id, user_id, created_at)
                    SELECT s.id, c.id, c.author_id, ?
                    FROM stories s JOIN comments c ON c.id=s.best_comment_id
                ''', (now(),))
                await db.execute('''
                    UPDATE users SET acquisition_locked=1,
                    acquisition_source=COALESCE(NULLIF(acquisition_source, ''), 'direct')
                ''')
                await db.execute("INSERT INTO app_meta(key,value) VALUES('reliability_v17',?)", (now(),))

            await db.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_stories_feed
                  ON stories(status, is_demo, created_at);
                CREATE INDEX IF NOT EXISTS idx_stories_feed_category
                  ON stories(status, category, is_demo, created_at);
                CREATE INDEX IF NOT EXISTS idx_stories_author
                  ON stories(author_id, is_demo, created_at);
                CREATE INDEX IF NOT EXISTS idx_comments_story
                  ON comments(story_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_users_reputation
                  ON users(reputation DESC, created_at);
                CREATE INDEX IF NOT EXISTS idx_reactions_comment
                  ON reactions(comment_id, value);
                CREATE INDEX IF NOT EXISTS idx_favorites_user
                  ON favorites(user_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_favorites_story
                  ON favorites(story_id);
                CREATE INDEX IF NOT EXISTS idx_reports_status
                  ON reports(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_story_views_story
                  ON story_views(story_id);
                CREATE INDEX IF NOT EXISTS idx_discussion_story
                  ON discussion_messages(story_id, status, created_at);
                CREATE INDEX IF NOT EXISTS idx_discussion_reply
                  ON discussion_messages(reply_to_id);
                CREATE INDEX IF NOT EXISTS idx_discussion_reactions_message
                  ON discussion_reactions(message_id, value);
                CREATE INDEX IF NOT EXISTS idx_growth_events_type
                  ON growth_events(event_type, created_at);
                CREATE INDEX IF NOT EXISTS idx_growth_events_source
                  ON growth_events(source, campaign, created_at);
                """
            )

            for tg_id, role in self.staff_roles.items():
                await db.execute(
                    "UPDATE users SET staff_role=? WHERE tg_id=?",
                    (role, tg_id),
                )

            cur = await db.execute("SELECT id, reputation FROM users WHERE tg_id != 0")
            for user_id, reputation in await cur.fetchall():
                level, title = progress_for(reputation)
                await db.execute(
                    "UPDATE users SET level=?, title=? WHERE id=?",
                    (level, title, user_id),
                )

            await self._seed_demo_content(db)
            await self._migrate_reaction_reputation(db)
            await db.commit()

    async def _seed_demo_content(self, db):
        cur = await db.execute("SELECT COUNT(*) FROM stories WHERE is_demo=1")
        if (await cur.fetchone())[0] > 0:
            return

        cur = await db.execute("SELECT id FROM users WHERE tg_id=0")
        row = await cur.fetchone()
        if row:
            system_user_id = row[0]
        else:
            cur = await db.execute(
                """
                INSERT INTO users(
                    tg_id, tg_username, nickname, bio, reputation,
                    level, title, staff_role, created_at
                )
                VALUES(0, NULL, 'Anon Verdict', 'Системные примеры дел.', 0, 1, 'Новичок', 'SYSTEM', ?)
                """,
                (now(),),
            )
            system_user_id = cur.lastrowid

        demo_stories = [
            (
                "❤️ Отношения",
                "Мы постоянно миримся и снова ссоримся",
                "Мы вместе почти год. После каждой серьёзной ссоры обещаем разговаривать спокойнее, "
                "но через пару недель всё повторяется. Я уже не понимаю: это обычный кризис или мы просто "
                "не умеем быть вместе? Что бы вы посоветовали обсудить в первую очередь?",
            ),
            (
                "💼 Работа",
                "Оставаться на стабильной работе или пробовать новое?",
                "Работа стабильная, зарплата приходит вовремя, но я перестал чувствовать, что развиваюсь. "
                "Есть возможность попробовать другое направление, но там меньше определённости. "
                "Как вы обычно отличаете разумный риск от импульсивного решения?",
            ),
            (
                "🧑‍🤝‍🧑 Дружба",
                "Друг появляется только когда ему что-то нужно",
                "Мы давно знакомы, но в последнее время он пишет почти только когда нужна помощь. "
                "Когда инициирую встречу я, постоянно находятся причины отказаться. "
                "Стоит сказать об этом напрямую или просто перестать тянуть общение на себе?",
            ),
            (
                "💰 Деньги",
                "Как перестать тратить всё сразу после зарплаты?",
                "Каждый месяц обещаю себе начать откладывать, но после зарплаты быстро появляются покупки, "
                "которые в моменте кажутся нужными. К концу месяца снова почти ничего не остаётся. "
                "Какие простые правила реально помогают держать себя в рамках?",
            ),
            (
                "🎓 Учёба",
                "Потерял мотивацию в середине обучения",
                "Начинал с интересом, а сейчас делаю задания только потому, что надо. "
                "Не могу понять, мне не подходит направление или я просто выгорел от режима. "
                "Как бы вы проверили это до того, как всё бросать?",
            ),
        ]

        for category, title, body in demo_stories:
            await db.execute(
                """
                INSERT INTO stories(
                    author_id, category, title, body, status,
                    views, created_at, is_demo
                )
                VALUES(?,?,?,?, 'open', 0, ?, 1)
                """,
                (system_user_id, category, title, body, now()),
            )

    async def _migrate_reaction_reputation(self, db):
        cur = await db.execute(
            "SELECT value FROM app_meta WHERE key='reaction_reputation_v1'"
        )
        if await cur.fetchone():
            return

        cur = await db.execute(
            """
            SELECT c.author_id, COUNT(*)
            FROM reactions r
            JOIN comments c ON c.id=r.comment_id
            WHERE r.value=1 AND r.user_id != c.author_id
            GROUP BY c.author_id
            """
        )
        for author_id, likes_count in await cur.fetchall():
            if likes_count:
                await db.execute(
                    "UPDATE users SET reputation=reputation+? WHERE id=?",
                    (likes_count, author_id),
                )
                await self._sync_progress(db, author_id)

        await db.execute(
            """
            INSERT INTO app_meta(key, value)
            VALUES('reaction_reputation_v1', ?)
            """,
            (now(),),
        )

    async def ensure_user(self, tg_id, tg_username=None):
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute("SELECT id FROM users WHERE tg_id=?", (tg_id,))
            row = await cur.fetchone()
            staff_role = self.staff_roles.get(tg_id)

            if row:
                if staff_role:
                    await db.execute(
                        "UPDATE users SET tg_username=?, staff_role=? WHERE tg_id=?",
                        (tg_username, staff_role, tg_id),
                    )
                else:
                    await db.execute(
                        "UPDATE users SET tg_username=? WHERE tg_id=?",
                        (tg_username, tg_id),
                    )
                await db.commit()
                return row[0]

            nick = (tg_username or f"Пользователь{tg_id % 10000}")[:32]
            cur = await db.execute(
                """
                INSERT INTO users(
                    tg_id, tg_username, nickname, staff_role, created_at
                )
                VALUES(?,?,?,?,?)
                """,
                (tg_id, tg_username, nick, staff_role or "", now()),
            )
            await db.commit()
            return cur.lastrowid

    async def record_acquisition(self, tg_id, source="", campaign="", case_id=None):
        source = (source or "").strip().lower()[:32]
        campaign = (campaign or "").strip().lower()[:64]
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, acquisition_locked FROM users WHERE tg_id=?",
                (tg_id,),
            )
            user = await cur.fetchone()
            if not user:
                return False

            if not user["acquisition_locked"]:
                await db.execute(
                    """
                    UPDATE users
                    SET acquisition_source=?, acquisition_campaign=?, acquisition_case_id=?, acquisition_locked=1
                    WHERE id=? AND acquisition_locked=0
                    """,
                    (source or 'direct', campaign, case_id, user["id"]),
                )

            await db.execute(
                """
                INSERT INTO growth_events(user_id, event_type, source, campaign, story_id, created_at)
                VALUES(?, 'start', ?, ?, ?, ?)
                """,
                (user["id"], source, campaign, case_id, now()),
            )
            await db.commit()
            return True

    async def growth_stats(self):
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT
                    COALESCE(NULLIF(acquisition_source, ''), 'direct') AS source,
                    COUNT(*) AS users,
                    SUM(CASE WHEN EXISTS(
                        SELECT 1 FROM comments c WHERE c.author_id=u.id
                    ) THEN 1 ELSE 0 END) AS advisers,
                    SUM(CASE WHEN EXISTS(
                        SELECT 1 FROM stories s WHERE s.author_id=u.id AND s.is_demo=0
                    ) THEN 1 ELSE 0 END) AS authors
                FROM users u
                WHERE u.tg_id != 0
                GROUP BY COALESCE(NULLIF(acquisition_source, ''), 'direct')
                ORDER BY users DESC
                """
            )
            by_source = await cur.fetchall()

            cur = await db.execute(
                """
                SELECT
                    COALESCE(NULLIF(acquisition_campaign, ''), '—') AS campaign,
                    COALESCE(NULLIF(acquisition_source, ''), 'direct') AS source,
                    COUNT(*) AS users,
                    SUM(CASE WHEN EXISTS(
                        SELECT 1 FROM comments c WHERE c.author_id=u.id
                    ) THEN 1 ELSE 0 END) AS advisers,
                    SUM(CASE WHEN EXISTS(
                        SELECT 1 FROM stories s WHERE s.author_id=u.id AND s.is_demo=0
                    ) THEN 1 ELSE 0 END) AS authors
                FROM users u
                WHERE u.tg_id != 0 AND acquisition_campaign != ''
                GROUP BY acquisition_source, acquisition_campaign
                ORDER BY users DESC
                LIMIT 10
                """
            )
            campaigns = await cur.fetchall()

            cur = await db.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN acquisition_source NOT IN ('', 'direct') THEN 1 ELSE 0 END) AS attributed
                FROM users WHERE tg_id != 0
                """
            )
            totals = await cur.fetchone()
            return {"totals": totals, "sources": by_source, "campaigns": campaigns}

    async def set_pending_input(self, tg_id, action, payload):
        async with self.connect() as db:
            cur = await db.execute("SELECT id FROM users WHERE tg_id=?", (tg_id,))
            row = await cur.fetchone()
            if not row:
                return False
            await db.execute(
                """
                INSERT INTO pending_inputs(user_id, action, payload, updated_at)
                VALUES(?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                  action=excluded.action,
                  payload=excluded.payload,
                  updated_at=excluded.updated_at
                """,
                (row[0], action, json.dumps(payload, ensure_ascii=False), now()),
            )
            await db.commit()
            return True

    async def pending_input(self, tg_id, max_age_seconds=1800):
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT p.action, p.payload, p.updated_at
                FROM pending_inputs p
                JOIN users u ON u.id=p.user_id
                WHERE u.tg_id=?
                """,
                (tg_id,),
            )
            row = await cur.fetchone()
            if not row:
                return None

            try:
                updated_at = datetime.fromisoformat(row["updated_at"])
                age = (datetime.now(timezone.utc) - updated_at).total_seconds()
            except (TypeError, ValueError):
                age = max_age_seconds + 1

            if age > max_age_seconds:
                await db.execute(
                    """
                    DELETE FROM pending_inputs
                    WHERE user_id=(SELECT id FROM users WHERE tg_id=?)
                    """,
                    (tg_id,),
                )
                await db.commit()
                return None

            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, json.JSONDecodeError):
                payload = {}
            return {
                "action": row["action"],
                "payload": payload,
                "updated_at": row["updated_at"],
            }

    async def clear_pending_input(self, tg_id):
        async with self.connect() as db:
            cur = await db.execute(
                """
                DELETE FROM pending_inputs
                WHERE user_id=(SELECT id FROM users WHERE tg_id=?)
                """,
                (tg_id,),
            )
            await db.commit()
            return cur.rowcount > 0

    async def get_user(self, tg_id):
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,))
            return await cur.fetchone()

    async def profile_stats(self, tg_id):
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT
                    u.id,
                    (SELECT COUNT(*) FROM stories s
                     WHERE s.author_id=u.id AND s.is_demo=0 AND s.status!='deleted') AS stories_count,
                    (SELECT COUNT(*) FROM comments c
                     WHERE c.author_id=u.id AND c.status='open') AS advice_count,
                    (
                        SELECT COUNT(*)
                        FROM reactions r
                        JOIN comments c2 ON c2.id=r.comment_id
                        WHERE c2.author_id=u.id
                          AND c2.status='open'
                          AND r.value=1
                          AND r.user_id != u.id
                    ) AS helpful_likes,
                    (
                        SELECT COUNT(*)
                        FROM stories s3
                        JOIN comments c3 ON c3.id=s3.best_comment_id
                        WHERE c3.author_id=u.id
                          AND s3.best_comment_id IS NOT NULL
                          AND s3.status!='deleted'
                    ) AS best_answers,
                    (SELECT COUNT(*) FROM stories s4
                     WHERE s4.author_id=u.id AND s4.is_demo=0 AND s4.status='closed') AS closed_stories
                FROM users u
                WHERE u.tg_id=?
                """,
                (tg_id,),
            )
            return await cur.fetchone()

    async def update_profile(self, tg_id, nickname, bio):
        async with self.connect() as db:
            await db.execute(
                "UPDATE users SET nickname=?, bio=? WHERE tg_id=?",
                (nickname[:32], bio[:160], tg_id),
            )
            await db.commit()

    async def _sync_progress(self, db, user_id):
        cur = await db.execute("SELECT reputation FROM users WHERE id=?", (user_id,))
        row = await cur.fetchone()
        if not row:
            return
        level, title = progress_for(row[0])
        await db.execute(
            "UPDATE users SET level=?, title=? WHERE id=?",
            (level, title, user_id),
        )

    async def create_story(self, tg_id, category, title, body):
        body = (body or "").strip()
        if len(body) < 30:
            raise ValueError("Story body must be at least 30 characters")

        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            await self.require_active(db, tg_id)
            cur = await db.execute("SELECT id FROM users WHERE tg_id=?", (tg_id,))
            row = await cur.fetchone()
            if not row:
                raise RuntimeError("User must be initialized before creating a story")
            uid = row[0]
            cur = await db.execute(
                """
                INSERT INTO stories(author_id, category, title, body, created_at, is_demo)
                VALUES(?,?,?,?,?,0)
                """,
                (uid, category, title[:100], body[:4000], now()),
            )
            await db.commit()
            return cur.lastrowid

    async def story(self, sid, view=False, viewer_tg_id=None):
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row

            if view and viewer_tg_id is not None:
                cur = await db.execute(
                    """
                    SELECT viewer.id, s.author_id
                    FROM users viewer
                    JOIN stories s ON s.id=?
                    WHERE viewer.tg_id=?
                      AND s.status IN ('open','closed')
                    """,
                    (sid, viewer_tg_id),
                )
                view_row = await cur.fetchone()

                if view_row and view_row["id"] != view_row["author_id"]:
                    cur = await db.execute(
                        """
                        INSERT OR IGNORE INTO story_views(user_id, story_id, created_at)
                        VALUES(?,?,?)
                        """,
                        (view_row["id"], sid, now()),
                    )
                    if cur.rowcount > 0:
                        await db.execute(
                            "UPDATE stories SET views=views+1 WHERE id=?",
                            (sid,),
                        )
                    await db.commit()

            cur = await db.execute(
                """
                SELECT
                    s.*,
                    u.nickname,
                    u.tg_id AS author_tg_id,
                    (SELECT COUNT(*) FROM comments c WHERE c.story_id=s.id AND c.status='open') AS comments_count,
                    (SELECT COUNT(*) FROM favorites f WHERE f.story_id=s.id) AS favorites_count,
                    (SELECT COUNT(*) FROM discussion_messages d WHERE d.story_id=s.id AND d.status='open') AS discussion_count,
                    (SELECT COUNT(*) FROM story_updates su WHERE su.story_id=s.id) AS update_count,
                    (SELECT su.body FROM story_updates su WHERE su.story_id=s.id ORDER BY su.created_at DESC, su.id DESC LIMIT 1) AS latest_update_body,
                    (SELECT su.created_at FROM story_updates su WHERE su.story_id=s.id ORDER BY su.created_at DESC, su.id DESC LIMIT 1) AS latest_update_created_at,
                    bc.body AS best_comment_body,
                    bu.nickname AS best_comment_nickname,
                    bu.title AS best_comment_title
                FROM stories s
                JOIN users u ON u.id=s.author_id
                LEFT JOIN comments bc ON bc.id=s.best_comment_id AND bc.status='open'
                LEFT JOIN users bu ON bu.id=bc.author_id
                WHERE s.id=?
                """,
                (sid,),
            )
            return await cur.fetchone()

    def _feed_filter(self, category=None, sort="new"):
        clauses = ["s.status='open'"]
        params = []

        if category:
            clauses.append("s.category=?")
            params.append(category)

        if sort == "unanswered":
            clauses.append(
                "NOT EXISTS (SELECT 1 FROM comments c0 WHERE c0.story_id=s.id AND c0.status='open')"
            )

        where_sql = " AND ".join(clauses)

        if sort == "popular":
            order_sql = """
                s.is_demo ASC,
                comments_count DESC,
                s.views DESC,
                s.created_at DESC
            """
        else:
            order_sql = """
                s.is_demo ASC,
                CASE WHEN s.is_demo=0 THEN
                    MAX(
                        s.created_at,
                        COALESCE(NULLIF(s.reopened_at, ''), s.created_at),
                        COALESCE(
                            (SELECT MAX(su.created_at) FROM story_updates su WHERE su.story_id=s.id),
                            s.created_at
                        )
                    )
                END DESC,
                CASE WHEN s.is_demo=1 THEN s.id END ASC
            """

        return where_sql, order_sql, params

    async def feed_count(self, category=None, sort="new"):
        where_sql, _, params = self._feed_filter(category, sort)
        async with self.connect() as db:
            cur = await db.execute(
                f"SELECT COUNT(*) FROM stories s WHERE {where_sql}",
                params,
            )
            return (await cur.fetchone())[0]

    async def feed_item(self, offset=0, category=None, sort="new"):
        where_sql, order_sql, params = self._feed_filter(category, sort)
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                f"""
                SELECT
                    s.*,
                    u.nickname,
                    (SELECT COUNT(*) FROM comments c WHERE c.story_id=s.id AND c.status='open') AS comments_count,
                    (SELECT COUNT(*) FROM favorites f WHERE f.story_id=s.id) AS favorites_count,
                    (SELECT COUNT(*) FROM discussion_messages d WHERE d.story_id=s.id AND d.status='open') AS discussion_count,
                    (SELECT COUNT(*) FROM story_updates su WHERE su.story_id=s.id) AS update_count,
                    (SELECT su.body FROM story_updates su WHERE su.story_id=s.id ORDER BY su.created_at DESC, su.id DESC LIMIT 1) AS latest_update_body,
                    (SELECT su.created_at FROM story_updates su WHERE su.story_id=s.id ORDER BY su.created_at DESC, su.id DESC LIMIT 1) AS latest_update_created_at
                FROM stories s
                JOIN users u ON u.id=s.author_id
                WHERE {where_sql}
                ORDER BY {order_sql}
                LIMIT 1 OFFSET ?
                """,
                (*params, max(0, offset)),
            )
            return await cur.fetchone()

    async def user_story_count(self, tg_id):
        async with self.connect() as db:
            cur = await db.execute(
                """
                SELECT COUNT(*)
                FROM stories s
                JOIN users u ON u.id=s.author_id
                WHERE u.tg_id=? AND s.is_demo=0 AND s.status!='deleted'
                """,
                (tg_id,),
            )
            return (await cur.fetchone())[0]

    async def user_story_item(self, tg_id, offset=0):
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT
                    s.*,
                    (SELECT COUNT(*) FROM comments c WHERE c.story_id=s.id AND c.status='open') AS comments_count,
                    (SELECT COUNT(*) FROM favorites f WHERE f.story_id=s.id) AS favorites_count,
                    (SELECT COUNT(*) FROM discussion_messages d WHERE d.story_id=s.id AND d.status='open') AS discussion_count,
                    (SELECT COUNT(*) FROM story_updates su WHERE su.story_id=s.id) AS update_count,
                    (SELECT su.body FROM story_updates su WHERE su.story_id=s.id ORDER BY su.created_at DESC, su.id DESC LIMIT 1) AS latest_update_body,
                    (SELECT su.created_at FROM story_updates su WHERE su.story_id=s.id ORDER BY su.created_at DESC, su.id DESC LIMIT 1) AS latest_update_created_at
                FROM stories s
                JOIN users u ON u.id=s.author_id
                WHERE u.tg_id=? AND s.is_demo=0 AND s.status!='deleted'
                ORDER BY s.created_at DESC
                LIMIT 1 OFFSET ?
                """,
                (tg_id, max(0, offset)),
            )
            return await cur.fetchone()

    async def comment(self, tg_id, sid, body):
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            await self.require_active(db, tg_id)
            cur = await db.execute(
                "SELECT id, nickname FROM users WHERE tg_id=?",
                (tg_id,),
            )
            row = await cur.fetchone()
            if not row:
                raise RuntimeError("User must be initialized before commenting")
            uid, nickname = row

            cur = await db.execute(
                """
                SELECT u.tg_id, u.notifications_enabled, s.title, s.status, s.is_demo
                FROM stories s
                JOIN users u ON u.id=s.author_id
                WHERE s.id=?
                """,
                (sid,),
            )
            story_row = await cur.fetchone()
            if not story_row:
                return {"status": "story_not_found"}
            if story_row[3] == "closed":
                return {"status": "story_closed"}
            if story_row[3] != "open":
                return {"status": "story_unavailable"}
            if story_row[4] or story_row[0] == tg_id:
                return {"status": "self_or_demo"}

            cur = await db.execute(
                """
                SELECT COUNT(*)
                FROM comments
                WHERE story_id=? AND author_id=?
                """,
                (sid, uid),
            )
            previous_comments = (await cur.fetchone())[0]
            reputation_reward = 2 if previous_comments == 0 else 0

            cur = await db.execute(
                """
                INSERT INTO comments(story_id, author_id, body, created_at, status)
                VALUES(?,?,?,?, 'open')
                """,
                (sid, uid, body[:2000], now()),
            )
            comment_id = cur.lastrowid

            if reputation_reward:
                await db.execute(
                    "UPDATE users SET reputation=reputation+? WHERE id=?",
                    (reputation_reward, uid),
                )
                await self._sync_progress(db, uid)
            await db.commit()

            owner_tg_id, owner_notifications, story_title, _story_status, _is_demo = story_row
            return {
                "status": "created",
                "comment_id": comment_id,
                "commenter_id": uid,
                "commenter_tg_id": tg_id,
                "commenter_nickname": nickname,
                "story_owner_tg_id": owner_tg_id,
                "story_owner_notifications": bool(owner_notifications),
                "story_title": story_title,
                "reputation_reward": reputation_reward,
            }

    async def add_story_update(self, tg_id, sid, body):
        body = (body or "").strip()
        if not body:
            return {"status": "empty"}

        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            await self.require_active(db, tg_id)
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT s.id, s.status, s.author_id
                FROM stories s
                JOIN users u ON u.id=s.author_id
                WHERE s.id=? AND u.tg_id=? AND s.is_demo=0
                """,
                (sid, tg_id),
            )
            story = await cur.fetchone()
            if not story:
                return {"status": "not_found"}
            if story["status"] in {"hidden", "deleted"}:
                return {"status": "unavailable"}

            created_at = now()
            cur = await db.execute(
                """
                INSERT INTO story_updates(story_id, author_id, body, created_at)
                VALUES(?,?,?,?)
                """,
                (sid, story["author_id"], body, created_at),
            )
            update_id = cur.lastrowid
            await db.commit()
            return {
                "status": "created",
                "update_id": update_id,
                "story_status": story["status"],
            }

    async def story_updates(self, sid, limit=5):
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT id, body, created_at
                FROM story_updates
                WHERE story_id=?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (sid, max(1, int(limit))),
            )
            return await cur.fetchall()

    async def story_update_count(self, sid):
        async with self.connect() as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM story_updates WHERE story_id=?",
                (sid,),
            )
            return (await cur.fetchone())[0]

    async def advice_inbox(self, owner_tg_id, sid):
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT ai.message_id, ai.last_seen_count
                FROM advice_inbox ai
                JOIN users u ON u.id=ai.owner_id
                WHERE u.tg_id=? AND ai.story_id=?
                """,
                (owner_tg_id, sid),
            )
            return await cur.fetchone()

    async def set_advice_inbox(self, owner_tg_id, sid, message_id, last_seen_count):
        async with self.connect() as db:
            cur = await db.execute("SELECT id FROM users WHERE tg_id=?", (owner_tg_id,))
            row = await cur.fetchone()
            if not row:
                return False
            owner_id = row[0]
            await db.execute(
                """
                INSERT INTO advice_inbox(owner_id, story_id, message_id, last_seen_count, updated_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(owner_id, story_id) DO UPDATE SET
                  message_id=excluded.message_id,
                  last_seen_count=excluded.last_seen_count,
                  updated_at=excluded.updated_at
                """,
                (owner_id, sid, message_id, max(0, int(last_seen_count)), now()),
            )
            await db.commit()
            return True

    async def mark_advice_seen(self, owner_tg_id, sid, seen_count):
        async with self.connect() as db:
            cur = await db.execute("SELECT id FROM users WHERE tg_id=?", (owner_tg_id,))
            row = await cur.fetchone()
            if not row:
                return False
            owner_id = row[0]
            await db.execute(
                """
                INSERT INTO advice_inbox(owner_id, story_id, message_id, last_seen_count, updated_at)
                VALUES(?,?,NULL,?,?)
                ON CONFLICT(owner_id, story_id) DO UPDATE SET
                  last_seen_count=MAX(advice_inbox.last_seen_count, excluded.last_seen_count),
                  updated_at=excluded.updated_at
                """,
                (owner_id, sid, max(0, int(seen_count)), now()),
            )
            await db.commit()
            return True

    async def comment_count(self, sid):
        async with self.connect() as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM comments WHERE story_id=? AND status='open'",
                (sid,),
            )
            return (await cur.fetchone())[0]

    async def comment_item(self, sid, offset=0):
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT
                    c.*,
                    u.nickname,
                    u.title,
                    u.staff_role,
                    CASE WHEN s.best_comment_id=c.id THEN 1 ELSE 0 END AS is_best,
                    (SELECT COUNT(*) FROM reactions r WHERE r.comment_id=c.id AND r.value=1) AS likes,
                    (SELECT COUNT(*) FROM reactions r WHERE r.comment_id=c.id AND r.value=-1) AS dislikes
                FROM comments c
                JOIN users u ON u.id=c.author_id
                JOIN stories s ON s.id=c.story_id
                WHERE c.story_id=? AND c.status='open'
                ORDER BY c.created_at ASC
                LIMIT 1 OFFSET ?
                """,
                (sid, max(0, offset)),
            )
            return await cur.fetchone()

    async def react(self, tg_id, cid, value):
        value = 1 if value > 0 else -1
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            await self.require_active(db, tg_id)
            cur = await db.execute("SELECT id FROM users WHERE tg_id=?", (tg_id,))
            row = await cur.fetchone()
            if not row:
                raise RuntimeError("User must be initialized before reacting")
            uid = row[0]

            cur = await db.execute(
                """SELECT c.author_id FROM comments c JOIN stories s ON s.id=c.story_id
                   WHERE c.id=? AND c.status='open' AND s.status IN ('open','closed')""",
                (cid,),
            )
            comment_row = await cur.fetchone()
            if not comment_row:
                return {"status": "not_found", "reputation_delta": 0}

            author_id = comment_row[0]
            if author_id == uid:
                return {"status": "self", "reputation_delta": 0}

            cur = await db.execute(
                "SELECT value FROM reactions WHERE user_id=? AND comment_id=?",
                (uid, cid),
            )
            old_row = await cur.fetchone()
            old_value = old_row[0] if old_row else None

            if old_value == value:
                return {"status": "unchanged", "reputation_delta": 0}

            await db.execute(
                """
                INSERT INTO reactions(user_id, comment_id, value)
                VALUES(?,?,?)
                ON CONFLICT(user_id, comment_id)
                DO UPDATE SET value=excluded.value
                """,
                (uid, cid, value),
            )

            # Only likes affect reputation. A like is +1; changing a like
            # to a dislike removes that +1. Dislikes themselves do not
            # push reputation below zero or punish controversial advice.
            reputation_delta = 0
            if old_value != 1 and value == 1:
                reputation_delta = 1
            elif old_value == 1 and value == -1:
                reputation_delta = -1

            if reputation_delta:
                await db.execute(
                    """
                    UPDATE users
                    SET reputation=MAX(0, reputation+?)
                    WHERE id=?
                    """,
                    (reputation_delta, author_id),
                )
                await self._sync_progress(db, author_id)

            cur = await db.execute(
                """
                SELECT u.tg_id, u.notifications_enabled, c.story_id
                FROM comments c
                JOIN users u ON u.id=c.author_id
                WHERE c.id=?
                """,
                (cid,),
            )
            author_row = await cur.fetchone()

            await db.commit()
            return {
                "status": "updated",
                "reputation_delta": reputation_delta,
                "author_tg_id": author_row[0] if author_row else None,
                "author_notifications": bool(author_row[1]) if author_row else False,
                "story_id": author_row[2] if author_row else None,
            }

    async def leaderboard_count(self):
        async with self.connect() as db:
            cur = await db.execute("SELECT COUNT(*) FROM users WHERE tg_id != 0")
            return (await cur.fetchone())[0]

    async def leaderboard_page(self, offset=0, limit=10):
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT
                    u.nickname,
                    u.title,
                    u.reputation,
                    u.level,
                    u.staff_role,
                    (SELECT COUNT(*) FROM comments c WHERE c.author_id=u.id AND c.status='open') AS advice_count,
                    (
                        SELECT COUNT(*)
                        FROM reactions r
                        JOIN comments c2 ON c2.id=r.comment_id
                        WHERE c2.author_id=u.id
                          AND c2.status='open'
                          AND r.value=1
                          AND r.user_id != u.id
                    ) AS helpful_likes
                FROM users u
                WHERE u.tg_id != 0
                ORDER BY u.reputation DESC, helpful_likes DESC, u.created_at ASC
                LIMIT ? OFFSET ?
                """,
                (limit, max(0, offset)),
            )
            return await cur.fetchall()

    async def discussion_count(self, sid):
        async with self.connect() as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM discussion_messages WHERE story_id=? AND status='open'",
                (sid,),
            )
            return (await cur.fetchone())[0]

    async def discussion_item(self, sid, offset=0):
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT
                    d.*,
                    u.nickname,
                    u.staff_role,
                    u.tg_id AS author_tg_id,
                    s.author_id AS story_author_id,
                    parent.body AS reply_body,
                    parent.author_id AS reply_author_id,
                    parent_u.nickname AS reply_nickname,
                    (SELECT COUNT(*) FROM discussion_reactions r
                     WHERE r.message_id=d.id AND r.value=1) AS likes
                FROM discussion_messages d
                JOIN users u ON u.id=d.author_id
                JOIN stories s ON s.id=d.story_id
                LEFT JOIN discussion_messages parent
                  ON parent.id=d.reply_to_id AND parent.status='open'
                LEFT JOIN users parent_u ON parent_u.id=parent.author_id
                WHERE d.story_id=? AND d.status='open'
                ORDER BY d.created_at ASC, d.id ASC
                LIMIT 1 OFFSET ?
                """,
                (sid, max(0, offset)),
            )
            return await cur.fetchone()

    async def discussion_page(self, sid, offset=0, limit=4):
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT
                    d.*,
                    u.nickname,
                    u.staff_role,
                    u.tg_id AS author_tg_id,
                    s.author_id AS story_author_id,
                    parent.body AS reply_body,
                    parent.author_id AS reply_author_id,
                    parent_u.nickname AS reply_nickname,
                    (SELECT COUNT(*) FROM discussion_reactions r
                     WHERE r.message_id=d.id AND r.value=1) AS likes
                FROM discussion_messages d
                JOIN users u ON u.id=d.author_id
                JOIN stories s ON s.id=d.story_id
                LEFT JOIN discussion_messages parent
                  ON parent.id=d.reply_to_id AND parent.status='open'
                LEFT JOIN users parent_u ON parent_u.id=parent.author_id
                WHERE d.story_id=? AND d.status='open'
                ORDER BY d.created_at ASC, d.id ASC
                LIMIT ? OFFSET ?
                """,
                (sid, max(1, limit), max(0, offset)),
            )
            return await cur.fetchall()

    async def discussion_message(self, message_id):
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT
                    d.*,
                    u.nickname,
                    u.tg_id AS author_tg_id,
                    u.notifications_enabled,
                    s.title AS story_title,
                    s.author_id AS story_author_id
                FROM discussion_messages d
                JOIN users u ON u.id=d.author_id
                JOIN stories s ON s.id=d.story_id
                WHERE d.id=?
                """,
                (message_id,),
            )
            return await cur.fetchone()

    async def add_discussion_message(self, tg_id, sid, body, reply_to_id=None):
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            await self.require_active(db, tg_id)
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, nickname, notifications_enabled FROM users WHERE tg_id=?",
                (tg_id,),
            )
            user = await cur.fetchone()
            if not user:
                raise RuntimeError("User must be initialized before discussion")

            cur = await db.execute(
                """
                SELECT s.author_id, s.title, u.tg_id AS owner_tg_id, u.notifications_enabled AS owner_notifications
                FROM stories s
                JOIN users u ON u.id=s.author_id
                WHERE s.id=? AND s.status='open' AND s.is_demo=0
                """,
                (sid,),
            )
            story = await cur.fetchone()
            if not story:
                return {"status": "story_not_found"}

            reply = None
            if reply_to_id is not None:
                cur = await db.execute(
                    """
                    SELECT d.id, d.author_id, u.tg_id AS reply_tg_id,
                           u.notifications_enabled AS reply_notifications
                    FROM discussion_messages d
                    JOIN users u ON u.id=d.author_id
                    WHERE d.id=? AND d.story_id=? AND d.status='open'
                    """,
                    (reply_to_id, sid),
                )
                reply = await cur.fetchone()
                if not reply:
                    return {"status": "reply_not_found"}

            cur = await db.execute(
                """
                INSERT INTO discussion_messages(
                    story_id, author_id, body, reply_to_id, status, created_at
                )
                VALUES(?,?,?,?, 'open', ?)
                """,
                (sid, user["id"], body[:1200], reply_to_id, now()),
            )
            message_id = cur.lastrowid
            await db.commit()

            return {
                "status": "created",
                "message_id": message_id,
                "author_id": user["id"],
                "author_tg_id": tg_id,
                "author_nickname": user["nickname"],
                "is_story_author": user["id"] == story["author_id"],
                "story_title": story["title"],
                "owner_tg_id": story["owner_tg_id"],
                "owner_notifications": bool(story["owner_notifications"]),
                "reply_tg_id": reply["reply_tg_id"] if reply else None,
                "reply_notifications": bool(reply["reply_notifications"]) if reply else False,
            }

    async def toggle_discussion_like(self, tg_id, message_id):
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            await self.require_active(db, tg_id)
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT id FROM users WHERE tg_id=?", (tg_id,))
            user = await cur.fetchone()
            if not user:
                return {"status": "user_not_found"}

            cur = await db.execute(
                """
                SELECT d.author_id, d.story_id
                FROM discussion_messages d
                WHERE d.id=? AND d.status='open'
                """,
                (message_id,),
            )
            msg = await cur.fetchone()
            if not msg:
                return {"status": "not_found"}
            if msg["author_id"] == user["id"]:
                return {"status": "self"}

            cur = await db.execute(
                "SELECT 1 FROM discussion_reactions WHERE user_id=? AND message_id=?",
                (user["id"], message_id),
            )
            exists = await cur.fetchone()
            if exists:
                await db.execute(
                    "DELETE FROM discussion_reactions WHERE user_id=? AND message_id=?",
                    (user["id"], message_id),
                )
                liked = False
            else:
                await db.execute(
                    """
                    INSERT INTO discussion_reactions(user_id, message_id, value)
                    VALUES(?,?,1)
                    """,
                    (user["id"], message_id),
                )
                liked = True
            await db.commit()
            return {"status": "updated", "liked": liked, "story_id": msg["story_id"]}

    async def discussion_cooldown_remaining(self, tg_id, cooldown_seconds=8):
        async with self.connect() as db:
            cur = await db.execute(
                """
                SELECT d.created_at
                FROM discussion_messages d
                JOIN users u ON u.id=d.author_id
                WHERE u.tg_id=?
                ORDER BY d.created_at DESC
                LIMIT 1
                """,
                (tg_id,),
            )
            row = await cur.fetchone()
            return self._cooldown_remaining(row[0] if row else None, cooldown_seconds)

    async def set_discussion_status(self, message_id, status):
        if status not in {"open", "hidden"}:
            raise ValueError("Unsupported discussion status")
        async with self.connect() as db:
            cur = await db.execute(
                "UPDATE discussion_messages SET status=? WHERE id=?",
                (status, message_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def admin_discussion_message(self, message_id):
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT
                    d.*,
                    u.tg_id AS author_tg_id,
                    u.nickname AS author_nickname,
                    s.title AS story_title
                FROM discussion_messages d
                JOIN users u ON u.id=d.author_id
                JOIN stories s ON s.id=d.story_id
                WHERE d.id=?
                """,
                (message_id,),
            )
            return await cur.fetchone()

    async def set_notifications(self, tg_id, enabled):
        async with self.connect() as db:
            await db.execute(
                "UPDATE users SET notifications_enabled=? WHERE tg_id=?",
                (1 if enabled else 0, tg_id),
            )
            await db.commit()

    async def toggle_notifications(self, tg_id):
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                "SELECT notifications_enabled FROM users WHERE tg_id=?",
                (tg_id,),
            )
            row = await cur.fetchone()
            current = bool(row[0]) if row else True
            new_value = not current
            await db.execute(
                "UPDATE users SET notifications_enabled=? WHERE tg_id=?",
                (1 if new_value else 0, tg_id),
            )
            await db.commit()
            return new_value

    async def favorite_state(self, tg_id, sid):
        async with self.connect() as db:
            cur = await db.execute("SELECT id FROM users WHERE tg_id=?", (tg_id,))
            row = await cur.fetchone()
            if not row:
                return False
            cur = await db.execute(
                "SELECT 1 FROM favorites WHERE user_id=? AND story_id=?",
                (row[0], sid),
            )
            return bool(await cur.fetchone())

    async def toggle_favorite(self, tg_id, sid):
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute("SELECT id FROM users WHERE tg_id=?", (tg_id,))
            row = await cur.fetchone()
            if not row:
                return False
            uid = row[0]

            cur = await db.execute(
                "SELECT 1 FROM favorites WHERE user_id=? AND story_id=?",
                (uid, sid),
            )
            exists = await cur.fetchone()
            if exists:
                await db.execute(
                    "DELETE FROM favorites WHERE user_id=? AND story_id=?",
                    (uid, sid),
                )
                await db.commit()
                return False

            cur = await db.execute(
                "SELECT 1 FROM stories WHERE id=? AND status IN ('open','closed')",
                (sid,),
            )
            if not await cur.fetchone():
                return False

            await db.execute(
                """
                INSERT OR IGNORE INTO favorites(user_id, story_id, created_at)
                VALUES(?,?,?)
                """,
                (uid, sid, now()),
            )
            await db.commit()
            return True

    async def favorite_count(self, tg_id):
        async with self.connect() as db:
            cur = await db.execute(
                """
                SELECT COUNT(*)
                FROM favorites f
                JOIN users u ON u.id=f.user_id
                JOIN stories s ON s.id=f.story_id
                WHERE u.tg_id=? AND s.status IN ('open','closed')
                """,
                (tg_id,),
            )
            return (await cur.fetchone())[0]

    async def favorite_item(self, tg_id, offset=0):
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT
                    s.*,
                    u2.nickname,
                    (SELECT COUNT(*) FROM comments c WHERE c.story_id=s.id AND c.status='open') AS comments_count,
                    (SELECT COUNT(*) FROM favorites f2 WHERE f2.story_id=s.id) AS favorites_count,
                    (SELECT COUNT(*) FROM discussion_messages d WHERE d.story_id=s.id AND d.status='open') AS discussion_count,
                    (SELECT COUNT(*) FROM story_updates su WHERE su.story_id=s.id) AS update_count,
                    (SELECT su.body FROM story_updates su WHERE su.story_id=s.id ORDER BY su.created_at DESC, su.id DESC LIMIT 1) AS latest_update_body,
                    (SELECT su.created_at FROM story_updates su WHERE su.story_id=s.id ORDER BY su.created_at DESC, su.id DESC LIMIT 1) AS latest_update_created_at
                FROM favorites f
                JOIN users u ON u.id=f.user_id
                JOIN stories s ON s.id=f.story_id
                JOIN users u2 ON u2.id=s.author_id
                WHERE u.tg_id=? AND s.status IN ('open','closed')
                ORDER BY f.created_at DESC
                LIMIT 1 OFFSET ?
                """,
                (tg_id, max(0, offset)),
            )
            return await cur.fetchone()

    async def favorite_subscribers(self, sid, exclude_tg_ids=None):
        exclude_tg_ids = set(exclude_tg_ids or [])
        async with self.connect() as db:
            cur = await db.execute(
                """
                SELECT DISTINCT u.tg_id
                FROM favorites f
                JOIN users u ON u.id=f.user_id
                WHERE f.story_id=? AND u.notifications_enabled=1 AND u.is_banned=0
                  AND NOT EXISTS(SELECT 1 FROM case_notifications cn
                    WHERE cn.user_id=u.id AND cn.story_id=f.story_id AND cn.enabled=0)
                """,
                (sid,),
            )
            rows = await cur.fetchall()
            return [row[0] for row in rows if row[0] not in exclude_tg_ids]

    async def report_target(self, reporter_tg_id, target_type, target_id, reason):
        if target_type not in {"story", "comment", "discussion"}:
            raise ValueError("Unsupported report target")

        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            await self.require_active(db, reporter_tg_id)
            cur = await db.execute("SELECT id FROM users WHERE tg_id=?", (reporter_tg_id,))
            row = await cur.fetchone()
            if not row:
                return {"status": "user_not_found"}
            reporter_id = row[0]

            table = {
                "story": "stories",
                "comment": "comments",
                "discussion": "discussion_messages",
            }[target_type]
            cur = await db.execute(
                f"SELECT author_id FROM {table} WHERE id=?",
                (target_id,),
            )
            target_row = await cur.fetchone()
            if not target_row:
                return {"status": "target_not_found"}
            if target_row[0] == reporter_id:
                return {"status": "self"}

            cur = await db.execute(
                """
                SELECT id
                FROM reports
                WHERE reporter_id=? AND target_type=? AND target_id=? AND status='open'
                """,
                (reporter_id, target_type, target_id),
            )
            if await cur.fetchone():
                return {"status": "duplicate"}

            cur = await db.execute(
                """
                INSERT INTO reports(reporter_id, target_type, target_id, reason, created_at)
                VALUES(?,?,?,?,?)
                """,
                (reporter_id, target_type, target_id, reason[:80], now()),
            )
            await db.commit()
            return {"status": "created", "report_id": cur.lastrowid}

    async def story_cooldown_remaining(self, tg_id, cooldown_seconds=60):
        async with self.connect() as db:
            cur = await db.execute(
                """
                SELECT s.created_at
                FROM stories s
                JOIN users u ON u.id=s.author_id
                WHERE u.tg_id=? AND s.is_demo=0
                ORDER BY s.created_at DESC
                LIMIT 1
                """,
                (tg_id,),
            )
            row = await cur.fetchone()
            return self._cooldown_remaining(row[0] if row else None, cooldown_seconds)

    async def comment_cooldown_remaining(self, tg_id, cooldown_seconds=15):
        async with self.connect() as db:
            cur = await db.execute(
                """
                SELECT c.created_at
                FROM comments c
                JOIN users u ON u.id=c.author_id
                WHERE u.tg_id=?
                ORDER BY c.created_at DESC
                LIMIT 1
                """,
                (tg_id,),
            )
            row = await cur.fetchone()
            return self._cooldown_remaining(row[0] if row else None, cooldown_seconds)

    def _cooldown_remaining(self, timestamp, cooldown_seconds):
        if not timestamp:
            return 0
        try:
            created = datetime.fromisoformat(timestamp)
        except (TypeError, ValueError):
            return 0
        delta = (datetime.now(timezone.utc) - created).total_seconds()
        return max(0, int(cooldown_seconds - delta + 0.999))

    async def admin_report_count(self):
        async with self.connect() as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM reports WHERE status='open'"
            )
            return (await cur.fetchone())[0]

    async def admin_reports_page(self, offset=0, limit=5):
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT
                    r.*,
                    u.tg_id AS reporter_tg_id,
                    u.nickname AS reporter_nickname
                FROM reports r
                JOIN users u ON u.id=r.reporter_id
                WHERE r.status='open'
                ORDER BY r.created_at ASC
                LIMIT ? OFFSET ?
                """,
                (limit, max(0, offset)),
            )
            return await cur.fetchall()

    async def admin_report(self, report_id):
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT
                    r.*,
                    u.tg_id AS reporter_tg_id,
                    u.nickname AS reporter_nickname
                FROM reports r
                JOIN users u ON u.id=r.reporter_id
                WHERE r.id=?
                """,
                (report_id,),
            )
            return await cur.fetchone()

    async def resolve_report(self, report_id, resolution="resolved"):
        if resolution not in {"resolved", "dismissed"}:
            raise ValueError("Unsupported report resolution")
        async with self.connect() as db:
            cur = await db.execute(
                "UPDATE reports SET status=? WHERE id=? AND status='open'",
                (resolution, report_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def set_comment_status(self, cid, status):
        if status not in {"open", "hidden"}:
            raise ValueError("Unsupported comment status")
        async with self.connect() as db:
            cur = await db.execute(
                "UPDATE comments SET status=? WHERE id=?",
                (status, cid),
            )
            await db.commit()
            return cur.rowcount > 0

    async def admin_comment(self, cid):
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT
                    c.*,
                    u.tg_id AS author_tg_id,
                    u.nickname AS author_nickname,
                    s.title AS story_title
                FROM comments c
                JOIN users u ON u.id=c.author_id
                JOIN stories s ON s.id=c.story_id
                WHERE c.id=?
                """,
                (cid,),
            )
            return await cur.fetchone()

    async def admin_stats(self):
        async with self.connect() as db:
            stats = {}
            queries = {
                "users": "SELECT COUNT(*) FROM users WHERE tg_id != 0",
                "staff": "SELECT COUNT(*) FROM users WHERE tg_id != 0 AND COALESCE(staff_role, '') != ''",
                "stories": "SELECT COUNT(*) FROM stories WHERE is_demo=0",
                "open_stories": "SELECT COUNT(*) FROM stories WHERE is_demo=0 AND status='open'",
                "closed_stories": "SELECT COUNT(*) FROM stories WHERE is_demo=0 AND status='closed'",
                "deleted_stories": "SELECT COUNT(*) FROM stories WHERE is_demo=0 AND status='deleted'",
                "hidden_stories": "SELECT COUNT(*) FROM stories WHERE is_demo=0 AND status='hidden'",
                "comments": "SELECT COUNT(*) FROM comments",
                "reactions": "SELECT COUNT(*) FROM reactions",
                "favorites": "SELECT COUNT(*) FROM favorites",
                "reports": "SELECT COUNT(*) FROM reports WHERE status='open'",
                "discussion_messages": "SELECT COUNT(*) FROM discussion_messages WHERE status='open'",
            }
            for key, sql in queries.items():
                cur = await db.execute(sql)
                stats[key] = (await cur.fetchone())[0]
            return stats

    async def admin_user_count(self):
        async with self.connect() as db:
            cur = await db.execute("SELECT COUNT(*) FROM users WHERE tg_id != 0")
            return (await cur.fetchone())[0]

    async def admin_users_page(self, offset=0, limit=5):
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT
                    u.*,
                    (SELECT COUNT(*) FROM stories s WHERE s.author_id=u.id AND s.is_demo=0 AND s.status!='deleted') AS stories_count,
                    (SELECT COUNT(*) FROM comments c WHERE c.author_id=u.id) AS comments_count
                FROM users u
                WHERE u.tg_id != 0
                ORDER BY u.created_at DESC
                LIMIT ? OFFSET ?
                """,
                (limit, max(0, offset)),
            )
            return await cur.fetchall()

    async def admin_user(self, tg_id):
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT
                    u.*,
                    (SELECT COUNT(*) FROM stories s WHERE s.author_id=u.id AND s.is_demo=0 AND s.status!='deleted') AS stories_count,
                    (SELECT COUNT(*) FROM comments c WHERE c.author_id=u.id) AS comments_count
                FROM users u
                WHERE u.tg_id=?
                """,
                (tg_id,),
            )
            return await cur.fetchone()

    async def set_staff_role(self, tg_id, role):
        async with self.connect() as db:
            cur = await db.execute(
                "UPDATE users SET staff_role=? WHERE tg_id=? AND tg_id != 0",
                ((role or "")[:64], tg_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def admin_story_count(self):
        async with self.connect() as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM stories WHERE is_demo=0"
            )
            return (await cur.fetchone())[0]

    async def admin_stories_page(self, offset=0, limit=5):
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT
                    s.*,
                    u.tg_id AS author_tg_id,
                    u.nickname AS author_nickname,
                    (SELECT COUNT(*) FROM comments c WHERE c.story_id=s.id AND c.status='open') AS comments_count
                FROM stories s
                JOIN users u ON u.id=s.author_id
                WHERE s.is_demo=0
                ORDER BY s.created_at DESC
                LIMIT ? OFFSET ?
                """,
                (limit, max(0, offset)),
            )
            return await cur.fetchall()

    async def admin_story(self, sid):
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT
                    s.*,
                    u.tg_id AS author_tg_id,
                    u.nickname AS author_nickname,
                    (SELECT COUNT(*) FROM comments c WHERE c.story_id=s.id AND c.status='open') AS comments_count
                FROM stories s
                JOIN users u ON u.id=s.author_id
                WHERE s.id=? AND s.is_demo=0
                """,
                (sid,),
            )
            return await cur.fetchone()

    async def set_story_outcome(self, tg_id, sid, body):
        body = (body or "").strip()
        if len(body) < 10:
            return {"status": "too_short"}

        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            await self.require_active(db, tg_id)
            cur = await db.execute(
                """
                UPDATE stories
                SET outcome_body=?, outcome_created_at=?
                WHERE id=?
                  AND is_demo=0
                  AND status='closed'
                  AND author_id=(SELECT id FROM users WHERE tg_id=?)
                """,
                (body[:2000], now(), sid, tg_id),
            )
            await db.commit()
            return {"status": "updated" if cur.rowcount else "unavailable"}

    async def story_participant_subscribers(self, sid, exclude_tg_ids=None):
        exclude_tg_ids = set(int(x) for x in (exclude_tg_ids or []) if x)
        async with self.connect() as db:
            cur = await db.execute(
                """
                SELECT DISTINCT u.tg_id
                FROM users u
                WHERE u.notifications_enabled=1 AND u.is_banned=0
                  AND NOT EXISTS(SELECT 1 FROM case_notifications cn
                    WHERE cn.user_id=u.id AND cn.story_id=? AND cn.enabled=0)
                  AND (
                    u.id IN (
                      SELECT c.author_id FROM comments c
                      WHERE c.story_id=? AND c.status='open'
                    )
                    OR u.id IN (
                      SELECT d.author_id FROM discussion_messages d
                      WHERE d.story_id=? AND d.status='open'
                    )
                    OR u.id IN (
                      SELECT f.user_id FROM favorites f WHERE f.story_id=?
                    )
                  )
                """,
                (sid, sid, sid, sid),
            )
            rows = await cur.fetchall()
            return [row[0] for row in rows if int(row[0]) not in exclude_tg_ids]

    async def close_story_with_best(self, tg_id, sid, comment_id=None):
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT s.id, s.status, s.best_comment_id
                FROM stories s
                JOIN users u ON u.id=s.author_id
                WHERE s.id=? AND u.tg_id=? AND s.is_demo=0
                """,
                (sid, tg_id),
            )
            story = await cur.fetchone()
            if not story:
                return {"status": "not_found"}
            if story["status"] == "hidden":
                return {"status": "moderated"}
            if story["status"] == "deleted":
                return {"status": "deleted"}
            if story["status"] != "open":
                return {"status": "not_open"}

            selected = None
            if comment_id is not None:
                cur = await db.execute(
                    """
                    SELECT
                        c.id,
                        c.author_id,
                        u.tg_id AS author_tg_id,
                        u.nickname AS author_nickname,
                        u.notifications_enabled AS author_notifications
                    FROM comments c
                    JOIN users u ON u.id=c.author_id
                    WHERE c.id=? AND c.story_id=? AND c.status='open'
                    """,
                    (comment_id, sid),
                )
                selected = await cur.fetchone()
                if not selected:
                    return {"status": "invalid_comment"}

                await db.execute(
                    "UPDATE stories SET status='closed', best_comment_id=? WHERE id=?",
                    (comment_id, sid),
                )
                cur = await db.execute(
                    """INSERT OR IGNORE INTO best_answer_awards
                       (story_id, comment_id, user_id, points, created_at) VALUES(?,?,?,5,?)""",
                    (sid, comment_id, selected["author_id"], now()),
                )
                reward = 5 if cur.rowcount else 0
                if reward:
                    await db.execute("UPDATE users SET reputation=reputation+? WHERE id=?",
                                     (reward, selected["author_id"]))
                    await self._sync_progress(db, selected["author_id"])
            else:
                await db.execute(
                    "UPDATE stories SET status='closed', best_comment_id=NULL WHERE id=?",
                    (sid,),
                )

            await db.commit()
            result = {
                "status": "updated",
                "best_comment_id": comment_id,
            }
            if selected:
                result["best_author_tg_id"] = selected["author_tg_id"]
                result["best_author_nickname"] = selected["author_nickname"]
                result["best_author_notifications"] = bool(selected["author_notifications"])
                result["best_reputation_reward"] = reward
            return result

    async def change_own_story_status(self, tg_id, sid, target_status):
        if target_status not in {"open", "closed", "deleted"}:
            raise ValueError("Unsupported lifecycle status")

        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT s.id, s.status
                FROM stories s
                JOIN users u ON u.id=s.author_id
                WHERE s.id=? AND u.tg_id=? AND s.is_demo=0
                """,
                (sid, tg_id),
            )
            story = await cur.fetchone()
            if not story:
                return {"status": "not_found"}

            current = story["status"]
            if current == "hidden":
                return {"status": "moderated"}
            if current == "deleted":
                return {"status": "deleted"}

            allowed = {
                ("open", "closed"),
                ("closed", "open"),
                ("open", "deleted"),
                ("closed", "deleted"),
            }
            if (current, target_status) not in allowed:
                if current == target_status:
                    return {"status": "unchanged", "current": current}
                return {"status": "invalid_transition", "current": current}

            if current == "closed" and target_status == "open":
                await db.execute(
                    "UPDATE stories SET status=?, reopened_at=? WHERE id=?",
                    (target_status, now(), sid),
                )
            else:
                await db.execute(
                    "UPDATE stories SET status=? WHERE id=?",
                    (target_status, sid),
                )
            await db.commit()
            return {"status": "updated", "previous": current, "current": target_status}

    async def set_story_status(self, sid, status):
        if status not in {"open", "hidden"}:
            raise ValueError("Unsupported moderation story status")

        async with self.connect() as db:
            if status == "hidden":
                cur = await db.execute(
                    """
                    UPDATE stories
                    SET status_before_hidden=CASE
                          WHEN status!='hidden' THEN status
                          ELSE status_before_hidden
                        END,
                        status='hidden'
                    WHERE id=? AND is_demo=0
                    """,
                    (sid,),
                )
            else:
                cur = await db.execute(
                    """
                    UPDATE stories
                    SET status=CASE
                          WHEN status='hidden'
                            THEN COALESCE(NULLIF(status_before_hidden, ''), 'open')
                          ELSE status
                        END,
                        status_before_hidden=''
                    WHERE id=? AND is_demo=0
                    """,
                    (sid,),
                )
            await db.commit()
            return cur.rowcount > 0

    async def draft(self, tg_id):
        async with self.connect() as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute('''SELECT d.* FROM story_drafts d JOIN users u ON u.id=d.user_id
                                       WHERE u.tg_id=?''', (tg_id,))
            return await cur.fetchone()

    async def save_draft(self, tg_id, **fields):
        allowed = {'category', 'title', 'body', 'stage'}
        if not fields.keys() <= allowed:
            raise ValueError('Invalid draft fields')
        async with self.connect() as conn:
            await conn.execute('BEGIN IMMEDIATE')
            await self.require_active(conn, tg_id)
            cur = await conn.execute('SELECT id FROM users WHERE tg_id=?', (tg_id,))
            user = await cur.fetchone()
            if not user:
                raise ValueError('Unknown user')
            await conn.execute('INSERT OR IGNORE INTO story_drafts(user_id,updated_at) VALUES(?,?)', (user[0], now()))
            if fields:
                setters = ', '.join(f'{key}=?' for key in fields)
                await conn.execute(f'UPDATE story_drafts SET {setters}, revision=revision+1, updated_at=? WHERE user_id=?',
                                   (*fields.values(), now(), user[0]))
            await conn.commit()
        return await self.draft(tg_id)

    async def delete_draft(self, tg_id):
        async with self.connect() as conn:
            await conn.execute('DELETE FROM story_drafts WHERE user_id=(SELECT id FROM users WHERE tg_id=?)', (tg_id,))
            await conn.commit()

    async def publish_draft(self, tg_id, revision, bypass_cooldown=False):
        async with self.connect() as conn:
            await conn.execute('BEGIN IMMEDIATE')
            conn.row_factory = aiosqlite.Row
            await self.require_active(conn, tg_id)
            cur = await conn.execute('''SELECT d.* FROM story_drafts d JOIN users u ON u.id=d.user_id
                                       WHERE u.tg_id=?''', (tg_id,))
            draft = await cur.fetchone()
            if not draft or draft['revision'] != revision or draft['stage'] != 'preview':
                return {'status': 'stale'}
            if not draft['category'] or not 1 <= len(draft['title']) <= 100 or not 30 <= len(draft['body'].strip()) <= 4000:
                return {'status': 'invalid'}
            if not bypass_cooldown:
                cur = await conn.execute('SELECT MAX(created_at) FROM stories WHERE author_id=? AND is_demo=0', (draft['user_id'],))
                remaining = self._cooldown_remaining((await cur.fetchone())[0], 60)
                if remaining:
                    return {'status': 'cooldown', 'remaining': remaining}
            cur = await conn.execute('''INSERT INTO stories(author_id,category,title,body,created_at,is_demo)
                                        VALUES(?,?,?,?,?,0)''',
                                     (draft['user_id'], draft['category'], draft['title'], draft['body'].strip(), now()))
            sid = cur.lastrowid
            await conn.execute('DELETE FROM story_drafts WHERE user_id=?', (draft['user_id'],))
            await conn.execute('DELETE FROM pending_inputs WHERE user_id=?', (draft['user_id'],))
            await conn.commit()
            return {'status': 'created', 'story_id': sid}

    async def story_update_item(self, sid, offset=0):
        async with self.connect() as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute('SELECT * FROM story_updates WHERE story_id=? ORDER BY created_at DESC,id DESC LIMIT 1 OFFSET ?',
                                     (sid, max(0, offset)))
            return await cur.fetchone()

    async def case_notification_state(self, tg_id, sid):
        async with self.connect() as conn:
            cur = await conn.execute('''SELECT cn.enabled FROM case_notifications cn JOIN users u ON u.id=cn.user_id
                                       WHERE u.tg_id=? AND cn.story_id=?''', (tg_id, sid))
            row = await cur.fetchone()
            return bool(row[0]) if row else True

    async def toggle_case_notifications(self, tg_id, sid):
        async with self.connect() as conn:
            await conn.execute('BEGIN IMMEDIATE')
            cur = await conn.execute('SELECT id FROM users WHERE tg_id=?', (tg_id,))
            user = await cur.fetchone()
            cur = await conn.execute("SELECT 1 FROM stories WHERE id=? AND status IN ('open','closed')", (sid,))
            if not user or not await cur.fetchone():
                return None
            cur = await conn.execute('SELECT enabled FROM case_notifications WHERE user_id=? AND story_id=?', (user[0], sid))
            current = await cur.fetchone()
            enabled = not (bool(current[0]) if current else True)
            await conn.execute('''INSERT INTO case_notifications(user_id,story_id,enabled) VALUES(?,?,?)
                                  ON CONFLICT(user_id,story_id) DO UPDATE SET enabled=excluded.enabled''',
                               (user[0], sid, int(enabled)))
            await conn.commit()
            return enabled

    async def notifications_allowed(self, tg_id, sid=None):
        async with self.connect() as conn:
            cur = await conn.execute('''SELECT u.notifications_enabled AND NOT u.is_banned
                                       AND NOT EXISTS(SELECT 1 FROM case_notifications cn
                                         WHERE cn.user_id=u.id AND cn.story_id=? AND cn.enabled=0)
                                       FROM users u WHERE u.tg_id=?''', (sid, tg_id))
            row = await cur.fetchone()
            return bool(row and row[0])

    async def can_moderate(self, tg_id, owner_tg_id):
        if tg_id and owner_tg_id and int(tg_id) == int(owner_tg_id):
            return True
        user = await self.get_user(tg_id)
        return bool(user and not user['is_banned'] and user['staff_role'] == 'Moderator Anon Verdict')

    async def moderators(self, owner_tg_id):
        async with self.connect() as conn:
            cur = await conn.execute("SELECT tg_id FROM users WHERE staff_role='Moderator Anon Verdict' AND is_banned=0")
            ids = [row[0] for row in await cur.fetchall()]
            return list(dict.fromkeys(([owner_tg_id] if owner_tg_id else []) + ids))

    async def restrict_user(self, actor_tg_id, target_tg_id, action, owner_tg_id, reason=''):
        if action not in {'mute24', 'mute7', 'ban', 'clear'}:
            return False
        async with self.connect() as conn:
            await conn.execute('BEGIN IMMEDIATE')
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute('SELECT * FROM users WHERE tg_id=?', (actor_tg_id,))
            actor = await cur.fetchone()
            cur = await conn.execute('SELECT * FROM users WHERE tg_id=?', (target_tg_id,))
            target = await cur.fetchone()
            owner = actor_tg_id == owner_tg_id
            if not actor or actor['is_banned'] or (not owner and actor['staff_role'] != 'Moderator Anon Verdict'):
                return False
            if not target or target_tg_id in {0, owner_tg_id, actor_tg_id}:
                return False
            if not owner and target['staff_role']:
                return False
            # Protect every active CEO restriction, including attempts to replace
            # its audit actor before clearing it in a second operation.
            active_restriction = target['is_banned'] or (target['muted_until'] or '') > now()
            if not owner and active_restriction:
                cur = await conn.execute('''SELECT actor_tg_id FROM moderation_audit WHERE target_tg_id=?
                                           AND action IN ('mute24','mute7','ban','clear') ORDER BY id DESC LIMIT 1''', (target_tg_id,))
                previous = await cur.fetchone()
                if previous and previous[0] == owner_tg_id:
                    return False
            if action == 'ban':
                await conn.execute('UPDATE users SET is_banned=1 WHERE tg_id=?', (target_tg_id,))
            elif action == 'clear':
                await conn.execute("UPDATE users SET is_banned=0, muted_until='' WHERE tg_id=?", (target_tg_id,))
            else:
                until = (datetime.now(timezone.utc) + timedelta(days=7 if action == 'mute7' else 1)).isoformat()
                await conn.execute('UPDATE users SET muted_until=MAX(COALESCE(muted_until,\'\'), ?) WHERE tg_id=?', (until, target_tg_id))
            await conn.execute('''INSERT INTO moderation_audit(actor_tg_id,target_tg_id,action,reason,created_at)
                                  VALUES(?,?,?,?,?)''', (actor_tg_id, target_tg_id, action, reason[:200], now()))
            await conn.execute('DELETE FROM pending_inputs WHERE user_id=?', (target['id'],))
            await conn.commit()
            return True

    async def audit_moderation(self, actor_tg_id, action, reason):
        async with self.connect() as conn:
            await conn.execute('INSERT INTO moderation_audit(actor_tg_id,action,reason,created_at) VALUES(?,?,?,?)',
                               (actor_tg_id, action, reason[:200], now()))
            await conn.commit()

    async def moderation_history(self, limit=10):
        async with self.connect() as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute('SELECT * FROM moderation_audit ORDER BY id DESC LIMIT ?', (limit,))
            return await cur.fetchall()
