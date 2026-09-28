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
                """
            )

            await self._ensure_column(db, "users", "staff_role", "TEXT DEFAULT ''")
            await self._ensure_column(db, "stories", "is_demo", "INTEGER DEFAULT 0")

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
        cur = await db.execute(
            "SELECT reputation FROM users WHERE id=?",
            (user_id,),
        )
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
                SELECT s.*, u.nickname
                FROM stories s
                JOIN users u ON u.id=s.author_id
                WHERE s.id=?
                """,
                (sid,),
            )
            return await cur.fetchone()

    async def latest(self, limit=10):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row

            cur = await db.execute(
                """
                SELECT s.*, u.nickname
                FROM stories s
                JOIN users u ON u.id=s.author_id
                WHERE s.status='open' AND s.is_demo=0
                ORDER BY s.created_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            real = list(await cur.fetchall())
            if len(real) >= limit:
                return real

            cur = await db.execute(
                """
                SELECT s.*, u.nickname
                FROM stories s
                JOIN users u ON u.id=s.author_id
                WHERE s.status='open' AND s.is_demo=1
                ORDER BY s.id ASC
                LIMIT ?
                """,
                (limit - len(real),),
            )
            demos = list(await cur.fetchall())
            return real + demos

    async def user_stories(self, tg_id, limit=10):
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
                LIMIT ?
                """,
                (tg_id, limit),
            )
            return await cur.fetchall()

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

    async def comments(self, sid):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT
                    c.*,
                    u.nickname,
                    u.title,
                    u.staff_role,
                    (SELECT COUNT(*) FROM reactions r WHERE r.comment_id=c.id AND r.value=1) likes,
                    (SELECT COUNT(*) FROM reactions r WHERE r.comment_id=c.id AND r.value=-1) dislikes
                FROM comments c
                JOIN users u ON u.id=c.author_id
                WHERE c.story_id=?
                ORDER BY c.created_at ASC
                """,
                (sid,),
            )
            return await cur.fetchall()

    async def react(self, tg_id, cid, value):
        value = 1 if value > 0 else -1
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT id FROM users WHERE tg_id=?", (tg_id,))
            row = await cur.fetchone()
            if not row:
                raise RuntimeError("User must be initialized before reacting")
            uid = row[0]

            await db.execute(
                """
                INSERT INTO reactions(user_id, comment_id, value)
                VALUES(?,?,?)
                ON CONFLICT(user_id, comment_id)
                DO UPDATE SET value=excluded.value
                """,
                (uid, cid, value),
            )
            await db.commit()

    async def leaderboard(self, limit=10):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT nickname, title, reputation, level, staff_role
                FROM users
                WHERE tg_id != 0
                ORDER BY reputation DESC, created_at ASC
                LIMIT ?
                """,
                (limit,),
            )
            return await cur.fetchall()
