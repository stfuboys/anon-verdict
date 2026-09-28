import aiosqlite
from datetime import datetime, timezone


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

    async def _ensure_column(self, db, table, column, definition):
        cur = await db.execute(f"PRAGMA table_info({table})")
        columns = {row[1] for row in await cur.fetchall()}
        if column not in columns:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    async def init(self):
        async with aiosqlite.connect(self.path) as db:
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
                """
            )

            await self._ensure_column(db, "users", "staff_role", "TEXT DEFAULT ''")
            await self._ensure_column(db, "stories", "is_demo", "INTEGER DEFAULT 0")

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
        async with aiosqlite.connect(self.path) as db:
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

    async def get_user(self, tg_id):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,))
            return await cur.fetchone()

    async def update_profile(self, tg_id, nickname, bio):
        async with aiosqlite.connect(self.path) as db:
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
        async with aiosqlite.connect(self.path) as db:
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

    async def story(self, sid, view=False):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            if view:
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
                    (SELECT COUNT(*) FROM comments c WHERE c.story_id=s.id) AS comments_count
                FROM stories s
                JOIN users u ON u.id=s.author_id
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
                "NOT EXISTS (SELECT 1 FROM comments c0 WHERE c0.story_id=s.id)"
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
                CASE WHEN s.is_demo=0 THEN s.created_at END DESC,
                CASE WHEN s.is_demo=1 THEN s.id END ASC
            """

        return where_sql, order_sql, params

    async def feed_count(self, category=None, sort="new"):
        where_sql, _, params = self._feed_filter(category, sort)
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                f"SELECT COUNT(*) FROM stories s WHERE {where_sql}",
                params,
            )
            return (await cur.fetchone())[0]

    async def feed_item(self, offset=0, category=None, sort="new"):
        where_sql, order_sql, params = self._feed_filter(category, sort)
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                f"""
                SELECT
                    s.*,
                    u.nickname,
                    (SELECT COUNT(*) FROM comments c WHERE c.story_id=s.id) AS comments_count
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
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """
                SELECT COUNT(*)
                FROM stories s
                JOIN users u ON u.id=s.author_id
                WHERE u.tg_id=? AND s.is_demo=0
                """,
                (tg_id,),
            )
            return (await cur.fetchone())[0]

    async def user_story_item(self, tg_id, offset=0):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT
                    s.*,
                    (SELECT COUNT(*) FROM comments c WHERE c.story_id=s.id) AS comments_count
                FROM stories s
                JOIN users u ON u.id=s.author_id
                WHERE u.tg_id=? AND s.is_demo=0
                ORDER BY s.created_at DESC
                LIMIT 1 OFFSET ?
                """,
                (tg_id, max(0, offset)),
            )
            return await cur.fetchone()

    async def comment(self, tg_id, sid, body):
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT id FROM users WHERE tg_id=?", (tg_id,))
            row = await cur.fetchone()
            if not row:
                raise RuntimeError("User must be initialized before commenting")
            uid = row[0]

            await db.execute(
                """
                INSERT INTO comments(story_id, author_id, body, created_at)
                VALUES(?,?,?,?)
                """,
                (sid, uid, body[:2000], now()),
            )
            await db.execute(
                "UPDATE users SET reputation=reputation+2 WHERE id=?",
                (uid,),
            )
            await self._sync_progress(db, uid)
            await db.commit()

    async def comment_count(self, sid):
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM comments WHERE story_id=?",
                (sid,),
            )
            return (await cur.fetchone())[0]

    async def comment_item(self, sid, offset=0):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT
                    c.*,
                    u.nickname,
                    u.title,
                    u.staff_role,
                    (SELECT COUNT(*) FROM reactions r WHERE r.comment_id=c.id AND r.value=1) AS likes,
                    (SELECT COUNT(*) FROM reactions r WHERE r.comment_id=c.id AND r.value=-1) AS dislikes
                FROM comments c
                JOIN users u ON u.id=c.author_id
                WHERE c.story_id=?
                ORDER BY c.created_at ASC
                LIMIT 1 OFFSET ?
                """,
                (sid, max(0, offset)),
            )
            return await cur.fetchone()

    async def react(self, tg_id, cid, value):
        value = 1 if value > 0 else -1
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT id FROM users WHERE tg_id=?", (tg_id,))
            row = await cur.fetchone()
            if not row:
                raise RuntimeError("User must be initialized before reacting")
            uid = row[0]

            cur = await db.execute(
                "SELECT author_id FROM comments WHERE id=?",
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

            await db.commit()
            return {
                "status": "updated",
                "reputation_delta": reputation_delta,
            }

    async def leaderboard_count(self):
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT COUNT(*) FROM users WHERE tg_id != 0")
            return (await cur.fetchone())[0]

    async def leaderboard_page(self, offset=0, limit=10):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT nickname, title, reputation, level, staff_role
                FROM users
                WHERE tg_id != 0
                ORDER BY reputation DESC, created_at ASC
                LIMIT ? OFFSET ?
                """,
                (limit, max(0, offset)),
            )
            return await cur.fetchall()

    async def admin_stats(self):
        async with aiosqlite.connect(self.path) as db:
            stats = {}
            queries = {
                "users": "SELECT COUNT(*) FROM users WHERE tg_id != 0",
                "staff": "SELECT COUNT(*) FROM users WHERE tg_id != 0 AND COALESCE(staff_role, '') != ''",
                "stories": "SELECT COUNT(*) FROM stories WHERE is_demo=0",
                "open_stories": "SELECT COUNT(*) FROM stories WHERE is_demo=0 AND status='open'",
                "hidden_stories": "SELECT COUNT(*) FROM stories WHERE is_demo=0 AND status='hidden'",
                "comments": "SELECT COUNT(*) FROM comments",
                "reactions": "SELECT COUNT(*) FROM reactions",
            }
            for key, sql in queries.items():
                cur = await db.execute(sql)
                stats[key] = (await cur.fetchone())[0]
            return stats

    async def admin_user_count(self):
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT COUNT(*) FROM users WHERE tg_id != 0")
            return (await cur.fetchone())[0]

    async def admin_users_page(self, offset=0, limit=5):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT
                    u.*,
                    (SELECT COUNT(*) FROM stories s WHERE s.author_id=u.id AND s.is_demo=0) AS stories_count,
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
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT
                    u.*,
                    (SELECT COUNT(*) FROM stories s WHERE s.author_id=u.id AND s.is_demo=0) AS stories_count,
                    (SELECT COUNT(*) FROM comments c WHERE c.author_id=u.id) AS comments_count
                FROM users u
                WHERE u.tg_id=?
                """,
                (tg_id,),
            )
            return await cur.fetchone()

    async def set_staff_role(self, tg_id, role):
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "UPDATE users SET staff_role=? WHERE tg_id=? AND tg_id != 0",
                ((role or "")[:64], tg_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def admin_story_count(self):
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM stories WHERE is_demo=0"
            )
            return (await cur.fetchone())[0]

    async def admin_stories_page(self, offset=0, limit=5):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT
                    s.*,
                    u.tg_id AS author_tg_id,
                    u.nickname AS author_nickname,
                    (SELECT COUNT(*) FROM comments c WHERE c.story_id=s.id) AS comments_count
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
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT
                    s.*,
                    u.tg_id AS author_tg_id,
                    u.nickname AS author_nickname,
                    (SELECT COUNT(*) FROM comments c WHERE c.story_id=s.id) AS comments_count
                FROM stories s
                JOIN users u ON u.id=s.author_id
                WHERE s.id=? AND s.is_demo=0
                """,
                (sid,),
            )
            return await cur.fetchone()

    async def set_story_status(self, sid, status):
        if status not in {"open", "hidden"}:
            raise ValueError("Unsupported story status")
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "UPDATE stories SET status=? WHERE id=? AND is_demo=0",
                (status, sid),
            )
            await db.commit()
            return cur.rowcount > 0
