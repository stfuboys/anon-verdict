import aiosqlite
from datetime import datetime, timezone

def now():
    return datetime.now(timezone.utc).isoformat()

class DB:
    def __init__(self, path): self.path = path

    async def init(self):
        async with aiosqlite.connect(self.path) as db:
            await db.executescript('''
            CREATE TABLE IF NOT EXISTS users(
              id INTEGER PRIMARY KEY, tg_id INTEGER UNIQUE NOT NULL,
              tg_username TEXT, nickname TEXT NOT NULL, bio TEXT DEFAULT '',
              reputation INTEGER DEFAULT 0, level INTEGER DEFAULT 1,
              title TEXT DEFAULT 'Новичок', created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS stories(
              id INTEGER PRIMARY KEY AUTOINCREMENT, author_id INTEGER NOT NULL,
              category TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
              status TEXT DEFAULT 'open', views INTEGER DEFAULT 0, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS comments(
              id INTEGER PRIMARY KEY AUTOINCREMENT, story_id INTEGER NOT NULL,
              author_id INTEGER NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reactions(
              user_id INTEGER NOT NULL, comment_id INTEGER NOT NULL, value INTEGER NOT NULL,
              PRIMARY KEY(user_id,comment_id));
            CREATE TABLE IF NOT EXISTS follows(
              follower_id INTEGER NOT NULL, following_id INTEGER NOT NULL,
              created_at TEXT NOT NULL, PRIMARY KEY(follower_id,following_id));
            ''')
            await db.commit()

    async def ensure_user(self,tg_id,tg_username=None):
        async with aiosqlite.connect(self.path) as db:
            cur=await db.execute("SELECT id FROM users WHERE tg_id=?",(tg_id,))
            row=await cur.fetchone()
            if row:
                await db.execute("UPDATE users SET tg_username=? WHERE tg_id=?",(tg_username,tg_id))
                await db.commit(); return row[0]
            nick=(tg_username or f"Пользователь{tg_id%10000}")[:32]
            cur=await db.execute("INSERT INTO users(tg_id,tg_username,nickname,created_at) VALUES(?,?,?,?)",
                                 (tg_id,tg_username,nick,now()))
            await db.commit(); return cur.lastrowid

    async def get_user(self,tg_id):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory=aiosqlite.Row
            cur=await db.execute("SELECT * FROM users WHERE tg_id=?",(tg_id,))
            return await cur.fetchone()

    async def update_profile(self,tg_id,nickname,bio):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE users SET nickname=?,bio=? WHERE tg_id=?",
                             (nickname[:32],bio[:160],tg_id)); await db.commit()

    async def create_story(self,tg_id,category,title,body):
        async with aiosqlite.connect(self.path) as db:
            cur=await db.execute("SELECT id FROM users WHERE tg_id=?",(tg_id,))
            uid=(await cur.fetchone())[0]
            cur=await db.execute("INSERT INTO stories(author_id,category,title,body,created_at) VALUES(?,?,?,?,?)",
                                 (uid,category,title[:100],body[:4000],now()))
            await db.commit(); return cur.lastrowid

    async def story(self,sid,view=False):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory=aiosqlite.Row
            if view:
                await db.execute("UPDATE stories SET views=views+1 WHERE id=?",(sid,)); await db.commit()
            cur=await db.execute('''SELECT s.*,u.nickname FROM stories s JOIN users u ON u.id=s.author_id WHERE s.id=?''',(sid,))
            return await cur.fetchone()

    async def latest(self,limit=10):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory=aiosqlite.Row
            cur=await db.execute('''SELECT s.*,u.nickname FROM stories s JOIN users u ON u.id=s.author_id
                                    WHERE s.status='open' ORDER BY s.created_at DESC LIMIT ?''',(limit,))
            return await cur.fetchall()

    async def comment(self,tg_id,sid,body):
        async with aiosqlite.connect(self.path) as db:
            cur=await db.execute("SELECT id FROM users WHERE tg_id=?",(tg_id,)); uid=(await cur.fetchone())[0]
            await db.execute("INSERT INTO comments(story_id,author_id,body,created_at) VALUES(?,?,?,?)",
                              (sid,uid,body[:2000],now()))
            await db.execute("UPDATE users SET reputation=reputation+2 WHERE id=?",(uid,))
            await db.commit()

    async def comments(self,sid):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory=aiosqlite.Row
            cur=await db.execute('''SELECT c.*,u.nickname,u.title,
              (SELECT COUNT(*) FROM reactions r WHERE r.comment_id=c.id AND r.value=1) likes,
              (SELECT COUNT(*) FROM reactions r WHERE r.comment_id=c.id AND r.value=-1) dislikes
              FROM comments c JOIN users u ON u.id=c.author_id
              WHERE c.story_id=? ORDER BY c.created_at ASC''',(sid,))
            return await cur.fetchall()

    async def react(self,tg_id,cid,value):
        async with aiosqlite.connect(self.path) as db:
            uid=(await (await db.execute("SELECT id FROM users WHERE tg_id=?",(tg_id,))).fetchone())[0]
            await db.execute('''INSERT INTO reactions(user_id,comment_id,value) VALUES(?,?,?)
                                ON CONFLICT(user_id,comment_id) DO UPDATE SET value=excluded.value''',
                             (uid,cid,value)); await db.commit()

    async def leaderboard(self,limit=10):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory=aiosqlite.Row
            cur=await db.execute("SELECT nickname,title,reputation,level FROM users ORDER BY reputation DESC LIMIT ?",(limit,))
            return await cur.fetchall()
