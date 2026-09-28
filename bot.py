import asyncio
import html
import os

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from db import DB
from moderation import moderate
from ai import review


load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")


def parse_int_env(name):
    value = (os.getenv(name) or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def parse_int_list_env(name):
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return []
    result = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            result.append(int(item))
        except ValueError as exc:
            raise RuntimeError(f"{name} must contain comma-separated integers") from exc
    return result


staff_roles = {}
owner_id = parse_int_env("OWNER_TG_ID")
if owner_id:
    staff_roles[owner_id] = "CEO Anon Verdict"

for developer_id in parse_int_list_env("DEVELOPER_TG_IDS"):
    staff_roles.setdefault(developer_id, "Developer Anon Verdict")


bot = Bot(TOKEN)
dp = Dispatcher()
db = DB(
    os.getenv("DB_PATH")
    or os.path.join(
        os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "/app/data"),
        "anon_verdict.sqlite3",
    ),
    staff_roles=staff_roles,
)

CATS = [
    "❤️ Отношения",
    "💼 Работа",
    "💰 Деньги",
    "👨‍👩‍👦 Семья",
    "🧑‍🤝‍🧑 Дружба",
    "🎓 Учёба",
    "🚀 Другое",
]


class Story(StatesGroup):
    category = State()
    title = State()
    body = State()


class Profile(StatesGroup):
    nickname = State()
    bio = State()


class Comment(StatesGroup):
    body = State()


class Admin(StatesGroup):
    user_lookup = State()


def h(value):
    return html.escape(str(value or ""), quote=False)


def main_keyboard(user_id=None):
    rows = [
        [KeyboardButton(text="📝 Подать дело"), KeyboardButton(text="🏛️ Зал суда")],
        [KeyboardButton(text="👤 Мой профиль"), KeyboardButton(text="🏆 Рейтинг")],
        [KeyboardButton(text="⚖️ Мои дела"), KeyboardButton(text="🎖️ Звания")],
        [KeyboardButton(text="ℹ️ Как это работает")],
    ]
    if owner_id is not None and user_id == owner_id:
        rows.append([KeyboardButton(text="🛡️ CEO Панель")])

    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выбери действие…",
    )


def menu():
    b = InlineKeyboardBuilder()
    b.button(text="📝 Подать дело", callback_data="new")
    b.button(text="🏛️ Зал суда", callback_data="feed")
    b.button(text="👤 Профиль", callback_data="profile")
    b.button(text="🏆 Рейтинг", callback_data="rating")
    b.button(text="🎖️ Звания", callback_data="ranks")
    b.button(text="ℹ️ Как это работает", callback_data="help")
    b.adjust(2, 2, 2)
    return b.as_markup()


def home_button():
    b = InlineKeyboardBuilder()
    b.button(text="🏠 Главное меню", callback_data="home")
    return b.as_markup()


def is_owner(user_id):
    return owner_id is not None and int(user_id) == owner_id


def admin_menu():
    b = InlineKeyboardBuilder()
    b.button(text="📊 Статистика", callback_data="admin:stats")
    b.button(text="👥 Пользователи", callback_data="admin:users")
    b.button(text="🔎 Найти по Telegram ID", callback_data="admin:find")
    b.button(text="⚖️ Дела", callback_data="admin:cases")
    b.button(text="🏠 Выйти", callback_data="home")
    b.adjust(2, 1, 1, 1)
    return b.as_markup()


def admin_back():
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ CEO-панель", callback_data="admin:home")
    return b.as_markup()


def demo_badge(story):
    return "\n🧪 <i>Пример от Anon Verdict</i>" if story["is_demo"] else ""


def role_line(user):
    role = (user["staff_role"] or "").strip()
    if role and role != "SYSTEM":
        return f"🛡️ <b>{h(role)}</b>\n🎖️ {h(user['title'])}"
    return f"🎖️ {h(user['title'])}"


async def ensure_message_user(m: Message):
    await db.ensure_user(m.from_user.id, m.from_user.username)


async def ensure_callback_user(c: CallbackQuery):
    await db.ensure_user(c.from_user.id, c.from_user.username)


async def send_feed(target, user_id=None):
    rows = await db.latest()
    if not rows:
        await target.answer(
            "🏛️ <b>Зал суда пока пуст</b>\n\nПодай первое дело — оно появится здесь анонимно.",
            parse_mode="HTML",
            reply_markup=main_keyboard(user_id),
        )
        return

    real_count = sum(1 for s in rows if not s["is_demo"])
    if real_count == 0:
        await target.answer(
            "🏛️ <b>ЗАЛ СУДА</b>\n\n"
            "Сообщество только запускается. Ниже — несколько демонстрационных дел, "
            "чтобы сразу было понятно, как работает Anon Verdict.\n\n"
            "🧪 Примеры всегда помечены и не выдаются за истории реальных людей.",
            parse_mode="HTML",
        )
    else:
        await target.answer(
            "🏛️ <b>ЗАЛ СУДА</b>\n\nВыбери дело, которое хочешь разобрать.",
            parse_mode="HTML",
        )

    for s in rows:
        b = InlineKeyboardBuilder()
        b.button(text="💬 Открыть дело", callback_data=f"s:{s['id']}")
        await target.answer(
            f"⚖️ <b>Дело №{s['id']}</b>\n"
            f"🏷️ {h(s['category'])}"
            f"{demo_badge(s)}\n\n"
            f"<b>{h(s['title'])}</b>\n"
            f"{h(s['body'][:700])}",
            parse_mode="HTML",
            reply_markup=b.as_markup(),
        )


@dp.message(CommandStart())
async def start(m: Message):
    await ensure_message_user(m)
    u = await db.get_user(m.from_user.id)

    role_text = ""
    if u and u["staff_role"] and u["staff_role"] != "SYSTEM":
        role_text = f"\n\n🛡️ Твоя роль: <b>{h(u['staff_role'])}</b>."

    await m.answer(
        "⚖️ <b>ANON VERDICT</b>\n\n"
        "Анонимный зал жизненных ситуаций.\n"
        "Рассказывай о том, что происходит, получай мнения со стороны "
        "и помогай другим своими советами.\n\n"
        "🔒 Автор дела скрыт от других пользователей.\n"
        "⭐ За полезную активность растёт репутация.\n"
        "🎖️ Репутация открывает судебные звания."
        f"{role_text}\n\n"
        "Начни с <b>🏛️ Зал суда</b>, чтобы посмотреть, как всё устроено, "
        "или нажми <b>📝 Подать дело</b>.",
        parse_mode="HTML",
        reply_markup=main_keyboard(m.from_user.id),
    )


@dp.callback_query(F.data == "new")
async def new(c: CallbackQuery, state: FSMContext):
    await ensure_callback_user(c)
    await state.set_state(Story.category)
    b = InlineKeyboardBuilder()
    for x in CATS:
        b.button(text=x, callback_data="cat:" + x)
    b.adjust(2)
    await c.message.answer(
        "⚖️ <b>Новое дело</b>\n\nВыбери категорию ситуации:",
        parse_mode="HTML",
        reply_markup=b.as_markup(),
    )
    await c.answer()


@dp.callback_query(F.data.startswith("cat:"))
async def cat(c: CallbackQuery, state: FSMContext):
    await ensure_callback_user(c)
    await state.update_data(category=c.data[4:])
    await state.set_state(Story.title)
    await c.message.answer("Коротко назови ситуацию:")
    await c.answer()


@dp.message(Story.title)
async def title(m: Message, state: FSMContext):
    await ensure_message_user(m)
    text, reasons = moderate(m.text or "")
    text = text.strip()
    if not text:
        await m.answer("Напиши название текстом.")
        return
    if reasons:
        await m.answer("Удали из текста персональные данные или угрозы.")
        return
    await state.update_data(title=text)
    await state.set_state(Story.body)
    await m.answer(
        "Теперь расскажи подробно. Не указывай телефоны, адреса, документы "
        "и другие данные, по которым можно определить человека."
    )


@dp.message(Story.body)
async def body(m: Message, state: FSMContext):
    await ensure_message_user(m)
    text, reasons = moderate(m.text or "")
    text = text.strip()
    if not text:
        await m.answer("Расскажи ситуацию текстом.")
        return
    if reasons:
        await m.answer("Удали из текста персональные данные или угрозы.")
        return
    d = await state.get_data()
    sid = await db.create_story(m.from_user.id, d["category"], d["title"], text)
    await state.clear()
    await m.answer(
        f"📜 <b>Дело №{sid} опубликовано анонимно.</b>\n\n"
        "Теперь его смогут увидеть в Зале суда.",
        parse_mode="HTML",
        reply_markup=main_keyboard(m.from_user.id),
    )


@dp.callback_query(F.data == "home")
async def home(c: CallbackQuery):
    await ensure_callback_user(c)
    await c.message.answer(
        "⚖️ <b>Главное меню</b>\n\nВыбирай действие на клавиатуре ниже:",
        parse_mode="HTML",
        reply_markup=main_keyboard(c.from_user.id),
    )
    await c.answer()


@dp.callback_query(F.data == "help")
async def help_menu(c: CallbackQuery):
    await ensure_callback_user(c)
    await c.message.answer(
        "⚖️ <b>КАК РАБОТАЕТ ANON VERDICT</b>\n\n"
        "1️⃣ <b>Подай дело</b> — анонимно расскажи свою ситуацию.\n"
        "2️⃣ <b>Зал суда</b> — читай дела и мнения других.\n"
        "3️⃣ <b>Дай совет</b> — помоги автору своим взглядом.\n"
        "4️⃣ <b>Решение за автором</b> — советы не заменяют его собственное решение.\n\n"
        "⭐ За опубликованный совет начисляется репутация.\n"
        "🎖️ Репутация повышает судебное звание.\n"
        "🧪 Демонстрационные дела всегда отмечены отдельно.",
        parse_mode="HTML",
        reply_markup=home_button(),
    )
    await c.answer()


@dp.callback_query(F.data == "ranks")
async def ranks(c: CallbackQuery):
    await ensure_callback_user(c)
    await c.message.answer(
        "🎖️ <b>СУДЕБНЫЕ ЗВАНИЯ</b>\n\n"
        "🌱 <b>Новичок</b> — 0+ репутации\n"
        "⚔️ <b>Присяжный</b> — 10+\n"
        "⚖️ <b>Судья</b> — 30+\n"
        "🏛️ <b>Старший судья</b> — 75+\n"
        "👑 <b>Верховный судья</b> — 150+\n\n"
        "Один опубликованный совет сейчас даёт <b>+2</b> репутации.\n"
        "🛡️ Роли команды проекта существуют отдельно от судебных званий.",
        parse_mode="HTML",
        reply_markup=home_button(),
    )
    await c.answer()


@dp.callback_query(F.data == "feed")
async def feed(c: CallbackQuery):
    await ensure_callback_user(c)
    await send_feed(c.message, c.from_user.id)
    await c.answer()


@dp.callback_query(F.data.startswith("s:"))
async def show(c: CallbackQuery):
    await ensure_callback_user(c)
    sid = int(c.data[2:])
    s = await db.story(sid, True)
    if not s:
        await c.answer("Дело не найдено", show_alert=True)
        return

    b = InlineKeyboardBuilder()
    if not s["is_demo"]:
        b.button(text="💬 Дать совет", callback_data=f"c:{sid}")
    b.button(text="🧠 Разбор", callback_data=f"a:{sid}")
    b.adjust(2)

    demo_note = ""
    if s["is_demo"]:
        demo_note = (
            "\n\n🧪 <i>Это демонстрационное дело от Anon Verdict. "
            "Оно показывает механику сервиса и не является историей реального пользователя.</i>"
        )

    await c.message.answer(
        f"⚖️ <b>Дело №{sid}</b>\n"
        f"🏷️ {h(s['category'])}\n\n"
        f"<b>{h(s['title'])}</b>\n"
        f"{h(s['body'])}\n\n"
        f"👁 Просмотров: {s['views']}"
        f"{demo_note}",
        parse_mode="HTML",
        reply_markup=b.as_markup(),
    )

    comments = await db.comments(sid)
    for x in comments:
        rb = InlineKeyboardBuilder()
        rb.button(text="👍", callback_data=f"r:1:{x['id']}:{sid}")
        rb.button(text="👎", callback_data=f"r:-1:{x['id']}:{sid}")

        role = ""
        if x["staff_role"] and x["staff_role"] != "SYSTEM":
            role = f" · 🛡️ {h(x['staff_role'])}"

        await c.message.answer(
            f"🧠 <b>{h(x['nickname'])}</b>{role} · {h(x['title'])}\n"
            f"{h(x['body'])}\n\n"
            f"👍 {x['likes']}  👎 {x['dislikes']}",
            parse_mode="HTML",
            reply_markup=rb.as_markup(),
        )

    if not comments and not s["is_demo"]:
        await c.message.answer("💬 Пока никто не дал совет. Можно стать первым.")

    await c.answer()


@dp.callback_query(F.data.startswith("c:"))
async def cstart(c: CallbackQuery, state: FSMContext):
    await ensure_callback_user(c)
    sid = int(c.data[2:])
    s = await db.story(sid)
    if not s or s["is_demo"]:
        await c.answer(
            "К демонстрационным делам советы не добавляются.",
            show_alert=True,
        )
        return
    await state.update_data(sid=sid)
    await state.set_state(Comment.body)
    await c.message.answer(
        "Напиши совет или мнение.\n\n"
        "Лучше объяснить свою мысль, а не просто вынести вердикт."
    )
    await c.answer()


@dp.message(Comment.body)
async def comment(m: Message, state: FSMContext):
    await ensure_message_user(m)
    text, reasons = moderate(m.text or "")
    text = text.strip()
    if not text:
        await m.answer("Напиши совет текстом.")
        return
    if reasons:
        await m.answer("Удали персональные данные или угрозы.")
        return

    d = await state.get_data()
    await db.comment(m.from_user.id, d["sid"], text)
    await state.clear()

    u = await db.get_user(m.from_user.id)
    await m.answer(
        "✅ Совет опубликован. <b>+2 репутации.</b>\n\n"
        f"🎖️ Текущее звание: <b>{h(u['title'])}</b>\n"
        f"⭐ Репутация: <b>{u['reputation']}</b>",
        parse_mode="HTML",
        reply_markup=main_keyboard(m.from_user.id),
    )


@dp.callback_query(F.data.startswith("r:"))
async def react(c: CallbackQuery):
    await ensure_callback_user(c)
    _, v, cid, _ = c.data.split(":")
    await db.react(c.from_user.id, int(cid), int(v))
    await c.answer("Оценка сохранена")


@dp.callback_query(F.data.startswith("a:"))
async def ai(c: CallbackQuery):
    await ensure_callback_user(c)
    s = await db.story(int(c.data[2:]))
    if not s:
        await c.answer("Дело не найдено", show_alert=True)
        return
    await c.message.answer("🧠 Советник разбирает ситуацию...")
    result = await review(s["title"], s["body"])
    await c.message.answer(h(result), parse_mode="HTML")
    await c.answer()


@dp.callback_query(F.data == "profile")
async def profile(c: CallbackQuery):
    await ensure_callback_user(c)
    u = await db.get_user(c.from_user.id)
    b = InlineKeyboardBuilder()
    b.button(text="✏️ Изменить", callback_data="edit")
    await c.message.answer(
        f"👤 <b>{h(u['nickname'])}</b>\n"
        f"{role_line(u)}\n"
        f"⭐ Репутация: {u['reputation']}\n"
        f"📈 Уровень: {u['level']}\n\n"
        f"📝 {h(u['bio'] or 'Описание пока не добавлено.')}",
        parse_mode="HTML",
        reply_markup=b.as_markup(),
    )
    await c.answer()


@dp.callback_query(F.data == "edit")
async def edit(c: CallbackQuery, state: FSMContext):
    await ensure_callback_user(c)
    await state.set_state(Profile.nickname)
    await c.message.answer("Новый ник (до 32 символов):")
    await c.answer()


@dp.message(Profile.nickname)
async def nick(m: Message, state: FSMContext):
    await ensure_message_user(m)
    text, reasons = moderate(m.text or "")
    text = text.strip()
    if not text:
        await m.answer("Ник не может быть пустым.")
        return
    if reasons:
        await m.answer("В нике не должно быть персональных данных или угроз.")
        return
    await state.update_data(nick=text[:32])
    await state.set_state(Profile.bio)
    await m.answer("Описание профиля (до 160 символов):")


@dp.message(Profile.bio)
async def bio(m: Message, state: FSMContext):
    await ensure_message_user(m)
    text, reasons = moderate(m.text or "")
    if reasons:
        await m.answer("Удали из описания персональные данные или угрозы.")
        return
    d = await state.get_data()
    await db.update_profile(m.from_user.id, d["nick"], (text or "")[:160])
    await state.clear()
    await m.answer("✅ Профиль обновлён.", reply_markup=main_keyboard(m.from_user.id))


def rating_text(rows):
    if not rows:
        return "🏆 <b>Рейтинг пока пуст.</b>"

    lines = []
    for i, x in enumerate(rows, 1):
        staff = f" · 🛡️ {h(x['staff_role'])}" if x["staff_role"] else ""
        lines.append(
            f"<b>{i}.</b> {h(x['nickname'])} — ⭐ {x['reputation']} "
            f"· {h(x['title'])}{staff}"
        )
    return "🏆 <b>РЕЙТИНГ ЗАЛА</b>\n\n" + "\n".join(lines)


@dp.callback_query(F.data == "rating")
async def rating(c: CallbackQuery):
    await ensure_callback_user(c)
    rows = await db.leaderboard()
    await c.message.answer(
        rating_text(rows),
        parse_mode="HTML",
        reply_markup=home_button(),
    )
    await c.answer()


@dp.message(F.text == "📝 Подать дело")
async def menu_new(m: Message, state: FSMContext):
    await ensure_message_user(m)
    await state.set_state(Story.category)
    b = InlineKeyboardBuilder()
    for x in CATS:
        b.button(text=x, callback_data="cat:" + x)
    b.adjust(2)
    await m.answer(
        "⚖️ <b>Новое дело</b>\n\nВыбери категорию:",
        parse_mode="HTML",
        reply_markup=b.as_markup(),
    )


@dp.message(F.text == "🏛️ Зал суда")
async def menu_feed(m: Message):
    await ensure_message_user(m)
    await send_feed(m, m.from_user.id)


@dp.message(F.text == "👤 Мой профиль")
async def menu_profile(m: Message):
    await ensure_message_user(m)
    u = await db.get_user(m.from_user.id)
    b = InlineKeyboardBuilder()
    b.button(text="✏️ Изменить профиль", callback_data="edit")
    b.button(text="🎖️ Мои звания", callback_data="ranks")
    b.adjust(1)
    await m.answer(
        f"👤 <b>{h(u['nickname'])}</b>\n"
        f"{role_line(u)}\n"
        f"⭐ Репутация: {u['reputation']}\n"
        f"📈 Уровень: {u['level']}\n\n"
        f"📝 {h(u['bio'] or 'Описание пока не добавлено.')}",
        parse_mode="HTML",
        reply_markup=b.as_markup(),
    )


@dp.message(F.text == "🏆 Рейтинг")
async def menu_rating(m: Message):
    await ensure_message_user(m)
    rows = await db.leaderboard()
    await m.answer(
        rating_text(rows),
        parse_mode="HTML",
        reply_markup=main_keyboard(m.from_user.id),
    )


@dp.message(F.text == "⚖️ Мои дела")
async def my_cases(m: Message):
    await ensure_message_user(m)
    rows = await db.user_stories(m.from_user.id)

    if not rows:
        b = InlineKeyboardBuilder()
        b.button(text="📝 Подать первое дело", callback_data="new")
        await m.answer(
            "⚖️ <b>МОИ ДЕЛА</b>\n\n"
            "Ты ещё ничего не публиковал.\n"
            "Когда подашь дело, здесь появятся его просмотры и количество советов.",
            parse_mode="HTML",
            reply_markup=b.as_markup(),
        )
        return

    await m.answer(
        "⚖️ <b>МОИ ДЕЛА</b>\n\nПоследние опубликованные тобой ситуации:",
        parse_mode="HTML",
    )
    for s in rows:
        b = InlineKeyboardBuilder()
        b.button(text="Открыть", callback_data=f"s:{s['id']}")
        await m.answer(
            f"📜 <b>Дело №{s['id']}</b> · {h(s['category'])}\n"
            f"<b>{h(s['title'])}</b>\n\n"
            f"👁 {s['views']} просмотров · 💬 {s['comments_count']} советов",
            parse_mode="HTML",
            reply_markup=b.as_markup(),
        )


@dp.message(F.text == "🎖️ Звания")
async def menu_ranks(m: Message):
    await ensure_message_user(m)
    await m.answer(
        "🎖️ <b>СУДЕБНЫЕ ЗВАНИЯ</b>\n\n"
        "🌱 Новичок — 0+\n"
        "⚔️ Присяжный — 10+\n"
        "⚖️ Судья — 30+\n"
        "🏛️ Старший судья — 75+\n"
        "👑 Верховный судья — 150+\n\n"
        "⭐ Один опубликованный совет сейчас даёт +2 репутации.\n"
        "🛡️ Роли команды Anon Verdict отображаются отдельно.",
        parse_mode="HTML",
        reply_markup=main_keyboard(m.from_user.id),
    )


@dp.message(F.text == "ℹ️ Как это работает")
async def menu_help(m: Message):
    await ensure_message_user(m)
    await m.answer(
        "ℹ️ <b>КАК ЭТО РАБОТАЕТ</b>\n\n"
        "📝 <b>Подай дело</b> → расскажи ситуацию анонимно.\n"
        "🏛️ <b>Зал суда</b> → читай реальные и демонстрационные дела.\n"
        "💬 <b>Дай совет</b> → помоги автору и получи репутацию.\n"
        "⚖️ <b>Мои дела</b> → следи за своими публикациями.\n"
        "⭐ <b>Репутация</b> → повышает судебное звание.\n\n"
        "🧪 Примеры от Anon Verdict всегда помечены и не выдаются за реальные истории.",
        parse_mode="HTML",
        reply_markup=main_keyboard(m.from_user.id),
    )



@dp.message(F.text == "🛡️ CEO Панель")
async def admin_button(m: Message, state: FSMContext):
    await ensure_message_user(m)
    if not is_owner(m.from_user.id):
        await m.answer("Раздел доступен только владельцу проекта.")
        return
    await state.clear()
    await m.answer(
        "🛡️ <b>CEO ANON VERDICT</b>\n\nВыбери раздел:",
        parse_mode="HTML",
        reply_markup=admin_menu(),
    )


@dp.message(Command("admin"))
async def admin_command(m: Message, state: FSMContext):
    await ensure_message_user(m)
    if not is_owner(m.from_user.id):
        await m.answer("Команда недоступна.")
        return
    await state.clear()
    await m.answer(
        "🛡️ <b>CEO ANON VERDICT</b>\n\n"
        "Панель управления проектом. Здесь можно смотреть статистику, "
        "пользователей и дела, а также назначать роли команде.",
        parse_mode="HTML",
        reply_markup=admin_menu(),
    )


@dp.callback_query(F.data == "admin:home")
async def admin_home(c: CallbackQuery, state: FSMContext):
    if not is_owner(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await c.message.answer(
        "🛡️ <b>CEO ANON VERDICT</b>\n\nВыбери раздел:",
        parse_mode="HTML",
        reply_markup=admin_menu(),
    )
    await c.answer()


@dp.callback_query(F.data == "admin:stats")
async def admin_stats(c: CallbackQuery):
    if not is_owner(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    s = await db.admin_stats()
    await c.message.answer(
        "📊 <b>СТАТИСТИКА ANON VERDICT</b>\n\n"
        f"👥 Пользователи: <b>{s['users']}</b>\n"
        f"🛡️ Команда: <b>{s['staff']}</b>\n"
        f"⚖️ Реальные дела: <b>{s['stories']}</b>\n"
        f"🟢 Открытые: <b>{s['open_stories']}</b>\n"
        f"🙈 Скрытые: <b>{s['hidden_stories']}</b>\n"
        f"💬 Советы: <b>{s['comments']}</b>\n"
        f"👍👎 Реакции: <b>{s['reactions']}</b>",
        parse_mode="HTML",
        reply_markup=admin_back(),
    )
    await c.answer()


async def send_admin_user(target, tg_id):
    u = await db.admin_user(tg_id)
    if not u:
        await target.answer(
            "Пользователь с таким Telegram ID не найден.",
            reply_markup=admin_back(),
        )
        return

    role = h(u["staff_role"] or "обычный пользователь")
    username = f"@{h(u['tg_username'])}" if u["tg_username"] else "не указан"
    text = (
        "👤 <b>КАРТОЧКА ПОЛЬЗОВАТЕЛЯ</b>\n\n"
        f"Telegram ID: <code>{u['tg_id']}</code>\n"
        f"Username: {username}\n"
        f"Ник в Anon Verdict: <b>{h(u['nickname'])}</b>\n"
        f"Роль: <b>{role}</b>\n"
        f"Звание: <b>{h(u['title'])}</b>\n"
        f"⭐ Репутация: <b>{u['reputation']}</b>\n"
        f"⚖️ Дел: <b>{u['stories_count']}</b>\n"
        f"💬 Советов: <b>{u['comments_count']}</b>"
    )

    b = InlineKeyboardBuilder()
    if int(u["tg_id"]) != owner_id:
        b.button(
            text="💻 Developer",
            callback_data=f"admin:role:{u['tg_id']}:developer",
        )
        b.button(
            text="🛡️ Moderator",
            callback_data=f"admin:role:{u['tg_id']}:moderator",
        )
        b.button(
            text="👤 Снять роль",
            callback_data=f"admin:role:{u['tg_id']}:clear",
        )
    b.button(text="⬅️ CEO-панель", callback_data="admin:home")
    b.adjust(2, 1, 1)
    await target.answer(text, parse_mode="HTML", reply_markup=b.as_markup())


@dp.callback_query(F.data == "admin:users")
async def admin_users(c: CallbackQuery):
    if not is_owner(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return

    rows = await db.recent_users(10)
    if not rows:
        await c.message.answer(
            "👥 Пользователей пока нет.",
            reply_markup=admin_back(),
        )
        await c.answer()
        return

    b = InlineKeyboardBuilder()
    for u in rows:
        icon = "🛡️" if u["staff_role"] else "👤"
        b.button(
            text=f"{icon} {u['nickname'][:24]} · {u['tg_id']}",
            callback_data=f"admin:user:{u['tg_id']}",
        )
    b.button(text="🔎 Найти по ID", callback_data="admin:find")
    b.button(text="⬅️ CEO-панель", callback_data="admin:home")
    b.adjust(1)
    await c.message.answer(
        "👥 <b>ПОСЛЕДНИЕ ПОЛЬЗОВАТЕЛИ</b>\n\n"
        "Нажми на пользователя, чтобы открыть карточку.",
        parse_mode="HTML",
        reply_markup=b.as_markup(),
    )
    await c.answer()


@dp.callback_query(F.data == "admin:find")
async def admin_find(c: CallbackQuery, state: FSMContext):
    if not is_owner(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(Admin.user_lookup)
    await c.message.answer(
        "🔎 Отправь Telegram ID пользователя числом.\n\n"
        "Например: <code>123456789</code>",
        parse_mode="HTML",
        reply_markup=admin_back(),
    )
    await c.answer()


@dp.message(Admin.user_lookup)
async def admin_find_result(m: Message, state: FSMContext):
    if not is_owner(m.from_user.id):
        await state.clear()
        return
    raw = (m.text or "").strip()
    if not raw.isdigit():
        await m.answer("Нужен Telegram ID — только цифры.")
        return
    await state.clear()
    await send_admin_user(m, int(raw))


@dp.callback_query(F.data.startswith("admin:user:"))
async def admin_user_card(c: CallbackQuery):
    if not is_owner(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    try:
        tg_id = int(c.data.split(":")[2])
    except (ValueError, IndexError):
        await c.answer("Некорректный ID", show_alert=True)
        return
    await send_admin_user(c.message, tg_id)
    await c.answer()


@dp.callback_query(F.data.startswith("admin:role:"))
async def admin_set_role(c: CallbackQuery):
    if not is_owner(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return

    try:
        _, _, raw_tg_id, role_key = c.data.split(":")
        tg_id = int(raw_tg_id)
    except (ValueError, IndexError):
        await c.answer("Некорректная команда", show_alert=True)
        return

    if tg_id == owner_id:
        await c.answer("Роль CEO защищена.", show_alert=True)
        return

    roles = {
        "developer": "Developer Anon Verdict",
        "moderator": "Moderator Anon Verdict",
        "clear": "",
    }
    if role_key not in roles:
        await c.answer("Неизвестная роль", show_alert=True)
        return

    ok = await db.set_staff_role(tg_id, roles[role_key])
    if not ok:
        await c.answer("Пользователь не найден", show_alert=True)
        return

    label = roles[role_key] or "роль снята"
    await c.answer(f"Готово: {label}", show_alert=True)
    await send_admin_user(c.message, tg_id)


@dp.callback_query(F.data == "admin:cases")
async def admin_cases(c: CallbackQuery):
    if not is_owner(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return

    rows = await db.recent_stories_admin(10)
    if not rows:
        await c.message.answer(
            "⚖️ Реальных дел пока нет.",
            reply_markup=admin_back(),
        )
        await c.answer()
        return

    b = InlineKeyboardBuilder()
    for s in rows:
        icon = "🟢" if s["status"] == "open" else "🙈"
        title = str(s["title"])[:28]
        b.button(
            text=f"{icon} #{s['id']} · {title}",
            callback_data=f"admin:story:{s['id']}",
        )
    b.button(text="⬅️ CEO-панель", callback_data="admin:home")
    b.adjust(1)

    await c.message.answer(
        "⚖️ <b>ПОСЛЕДНИЕ ДЕЛА</b>\n\n"
        "🟢 открыто · 🙈 скрыто",
        parse_mode="HTML",
        reply_markup=b.as_markup(),
    )
    await c.answer()


@dp.callback_query(F.data.startswith("admin:story:"))
async def admin_story_card(c: CallbackQuery):
    if not is_owner(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return

    try:
        sid = int(c.data.split(":")[2])
    except (ValueError, IndexError):
        await c.answer("Некорректный номер дела", show_alert=True)
        return

    s = await db.admin_story(sid)
    if not s:
        await c.answer("Дело не найдено", show_alert=True)
        return

    status = "🟢 открыто" if s["status"] == "open" else "🙈 скрыто"
    username = h(s["author_nickname"])
    await c.message.answer(
        f"⚖️ <b>ДЕЛО №{s['id']}</b>\n\n"
        f"Статус: <b>{status}</b>\n"
        f"Категория: {h(s['category'])}\n"
        f"Автор в сервисе: <b>{username}</b>\n"
        f"Telegram ID автора: <code>{s['author_tg_id']}</code>\n"
        f"👁 {s['views']} · 💬 {s['comments_count']}\n\n"
        f"<b>{h(s['title'])}</b>\n"
        f"{h(s['body'])}",
        parse_mode="HTML",
    )

    b = InlineKeyboardBuilder()
    if s["status"] == "open":
        b.button(
            text="🙈 Скрыть дело",
            callback_data=f"admin:story-status:{sid}:hidden",
        )
    else:
        b.button(
            text="🟢 Вернуть в зал",
            callback_data=f"admin:story-status:{sid}:open",
        )
    b.button(text="⬅️ К делам", callback_data="admin:cases")
    b.adjust(1)
    await c.message.answer("Управление делом:", reply_markup=b.as_markup())
    await c.answer()


@dp.callback_query(F.data.startswith("admin:story-status:"))
async def admin_story_status(c: CallbackQuery):
    if not is_owner(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return

    try:
        _, _, raw_sid, status = c.data.split(":")
        sid = int(raw_sid)
    except (ValueError, IndexError):
        await c.answer("Некорректная команда", show_alert=True)
        return

    if status not in {"open", "hidden"}:
        await c.answer("Некорректный статус", show_alert=True)
        return

    ok = await db.set_story_status(sid, status)
    if not ok:
        await c.answer("Дело не найдено", show_alert=True)
        return

    message = "Дело возвращено в Зал суда." if status == "open" else "Дело скрыто из Зала суда."
    await c.answer(message, show_alert=True)


@dp.message(Command("profile"))
async def p(m: Message):
    await ensure_message_user(m)
    u = await db.get_user(m.from_user.id)
    await m.answer(
        f"👤 <b>{h(u['nickname'])}</b>\n"
        f"{role_line(u)}\n"
        f"⭐ {u['reputation']}\n"
        f"📈 Уровень {u['level']}",
        parse_mode="HTML",
    )


async def main():
    await db.init()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
