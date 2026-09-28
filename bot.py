import asyncio
import html
import os
import secrets

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
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

RATING_PAGE_SIZE = 10
ADMIN_PAGE_SIZE = 5


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


def clamp(value, low, high):
    return max(low, min(value, high))


def is_owner(user_id):
    return owner_id is not None and user_id is not None and int(user_id) == owner_id


def main_keyboard(user_id=None):
    rows = [
        [KeyboardButton(text="📝 Подать дело"), KeyboardButton(text="🏛️ Зал суда")],
        [KeyboardButton(text="👤 Мой профиль"), KeyboardButton(text="🏆 Рейтинг")],
        [KeyboardButton(text="⚖️ Мои дела"), KeyboardButton(text="⭐ Избранное")],
        [KeyboardButton(text="🎖️ Звания"), KeyboardButton(text="ℹ️ Как это работает")],
    ]
    if is_owner(user_id):
        rows.append([KeyboardButton(text="🛡️ CEO Панель")])

    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выбери действие…",
    )


def home_inline(user_id=None):
    b = InlineKeyboardBuilder()
    b.button(text="🏛️ Зал суда", callback_data="feed:0")
    b.button(text="⚖️ Мои дела", callback_data="my:0")
    b.button(text="⭐ Избранное", callback_data="favorites:0")
    b.button(text="👤 Профиль", callback_data="profile")
    b.button(text="🏆 Рейтинг", callback_data="rating:0")
    b.button(text="🎖️ Звания", callback_data="ranks")
    b.button(text="ℹ️ Как это работает", callback_data="help")
    if is_owner(user_id):
        b.button(text="🛡️ CEO Панель", callback_data="admin:home")
        b.adjust(2, 2, 2, 1, 1)
    else:
        b.adjust(2, 2, 2, 1)
    return b.as_markup()


def home_text(user=None):
    role_text = ""
    if user and user["staff_role"] and user["staff_role"] != "SYSTEM":
        role_text = f"\n🛡️ Роль: <b>{h(user['staff_role'])}</b>"
    return (
        "⚖️ <b>ANON VERDICT</b>\n\n"
        "Анонимный зал жизненных ситуаций.\n"
        "Рассказывай о том, что происходит, получай мнения со стороны "
        "и помогай другим своими советами.\n\n"
        "🔒 Автор дела скрыт от других пользователей.\n"
        "⭐ За полезную активность растёт репутация."
        f"{role_text}\n\n"
        "Быстрые кнопки снизу остаются — ими удобно мгновенно открывать нужный раздел."
    )


async def safe_edit(message, text, reply_markup=None):
    try:
        await message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def safe_notify(tg_id, text, reply_markup=None):
    if not tg_id:
        return False
    try:
        await bot.send_message(
            tg_id,
            text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
        return True
    except (TelegramForbiddenError, TelegramBadRequest):
        return False


async def notify_many(tg_ids, text, reply_markup=None):
    unique_ids = list(dict.fromkeys(int(x) for x in tg_ids if x))
    for start in range(0, len(unique_ids), 20):
        batch = unique_ids[start:start + 20]
        await asyncio.gather(
            *(safe_notify(tg_id, text, reply_markup) for tg_id in batch),
            return_exceptions=True,
        )
        if start + 20 < len(unique_ids):
            await asyncio.sleep(1)


async def ensure_message_user(m: Message):
    await db.ensure_user(m.from_user.id, m.from_user.username)


async def ensure_callback_user(c: CallbackQuery):
    await db.ensure_user(c.from_user.id, c.from_user.username)


def nav_row(builder, prev_data, page_text, next_data):
    builder.button(text="⬅️", callback_data=prev_data)
    builder.button(text=page_text, callback_data="noop")
    builder.button(text="➡️", callback_data=next_data)


FEED_CATEGORIES = {
    "all": ("Все категории", None),
    "rel": ("❤️ Отношения", "❤️ Отношения"),
    "work": ("💼 Работа", "💼 Работа"),
    "money": ("💰 Деньги", "💰 Деньги"),
    "family": ("👨‍👩‍👦 Семья", "👨‍👩‍👦 Семья"),
    "friends": ("🧑‍🤝‍🧑 Дружба", "🧑‍🤝‍🧑 Дружба"),
    "study": ("🎓 Учёба", "🎓 Учёба"),
    "other": ("🚀 Другое", "🚀 Другое"),
}

FEED_SORTS = {
    "new": "🆕 Новые",
    "popular": "🔥 Популярные",
    "unanswered": "🆘 Без советов",
}

REPORT_REASONS = {
    "spam": "📨 Спам",
    "abuse": "🤬 Оскорбления",
    "personal": "🔐 Персональные данные",
    "danger": "⚠️ Опасный контент",
    "other": "🚩 Другое",
}


def feed_options(cat_key="all", sort="new"):
    if cat_key not in FEED_CATEGORIES:
        cat_key = "all"
    if sort not in FEED_SORTS:
        sort = "new"
    return cat_key, sort, FEED_CATEGORIES[cat_key][1]


def feed_card_text(story, index, total, cat_key="all", sort="new"):
    cat_key, sort, _ = feed_options(cat_key, sort)
    badge = "\n🧪 <i>Пример от Anon Verdict</i>" if story["is_demo"] else ""

    excerpt = str(story["body"]).strip()
    if len(excerpt) > 320:
        excerpt = excerpt[:320].rstrip() + "…"

    filter_label = FEED_CATEGORIES[cat_key][0]
    sort_label = FEED_SORTS[sort]

    return (
        "🏛️ <b>ЗАЛ СУДА</b>\n"
        f"🏷️ {h(filter_label)} · {h(sort_label)}\n\n"
        f"⚖️ <b>Дело №{story['id']}</b> · {h(story['category'])}"
        f"{badge}\n\n"
        f"<b>{h(story['title'])}</b>\n\n"
        f"{h(excerpt)}\n\n"
        f"👁 {story['views']} · 💬 {story['comments_count']} · ⭐ {story['favorites_count']}\n"
        f"📄 {index + 1} из {total}"
    )


def feed_keyboard(index, total, sid, cat_key="all", sort="new"):
    cat_key, sort, _ = feed_options(cat_key, sort)
    b = InlineKeyboardBuilder()

    b.button(
        text="📖 Открыть дело",
        callback_data=f"case:{sid}:{index}:{cat_key}:{sort}",
    )

    prev_i = (index - 1) % total
    next_i = (index + 1) % total
    nav_row(
        b,
        f"feed:{prev_i}:{cat_key}:{sort}",
        f"{index + 1}/{total}",
        f"feed:{next_i}:{cat_key}:{sort}",
    )

    b.button(
        text="🏷 Фильтр",
        callback_data=f"feedcat:{cat_key}:{sort}",
    )
    b.button(
        text="↕️ Сортировка",
        callback_data=f"feedsort:{cat_key}:{sort}",
    )
    b.button(
        text="🎲 Случайное",
        callback_data=f"random:{index}:{cat_key}:{sort}",
    )

    b.adjust(1, 3, 2, 1)
    return b.as_markup()


async def render_feed(message, index=0, cat_key="all", sort="new"):
    cat_key, sort, category = feed_options(cat_key, sort)
    total = await db.feed_count(category=category, sort=sort)
    if total == 0:
        b = InlineKeyboardBuilder()
        b.button(
            text="🏷️ Сменить категорию",
            callback_data=f"feedcat:{cat_key}:{sort}",
        )
        b.button(
            text="↕️ Сменить сортировку",
            callback_data=f"feedsort:{cat_key}:{sort}",
        )
        b.adjust(1)
        await safe_edit(
            message,
            "🏛️ <b>ЗАЛ СУДА</b>\n\n"
            "По выбранным фильтрам дел пока нет.",
            b.as_markup(),
        )
        return

    index = clamp(index, 0, total - 1)
    story = await db.feed_item(index, category=category, sort=sort)
    if not story:
        index = 0
        story = await db.feed_item(0, category=category, sort=sort)

    await safe_edit(
        message,
        feed_card_text(story, index, total, cat_key, sort),
        feed_keyboard(index, total, story["id"], cat_key, sort),
    )


async def render_feed_categories(message, current_cat="all", sort="new"):
    current_cat, sort, _ = feed_options(current_cat, sort)
    b = InlineKeyboardBuilder()
    for key, (label, _) in FEED_CATEGORIES.items():
        prefix = "✅ " if key == current_cat else ""
        b.button(
            text=prefix + label,
            callback_data=f"feed:0:{key}:{sort}",
        )
    b.button(
        text="⬅️ Назад к ленте",
        callback_data=f"feed:0:{current_cat}:{sort}",
    )
    b.adjust(2, 2, 2, 2, 1)
    await safe_edit(
        message,
        "🏷️ <b>КАТЕГОРИЯ</b>\n\nВыбери, какие дела показывать:",
        b.as_markup(),
    )


async def render_feed_sorts(message, cat_key="all", current_sort="new"):
    cat_key, current_sort, _ = feed_options(cat_key, current_sort)
    b = InlineKeyboardBuilder()
    for key, label in FEED_SORTS.items():
        prefix = "✅ " if key == current_sort else ""
        b.button(
            text=prefix + label,
            callback_data=f"feed:0:{cat_key}:{key}",
        )
    b.button(
        text="⬅️ Назад к ленте",
        callback_data=f"feed:0:{cat_key}:{current_sort}",
    )
    b.adjust(1)
    await safe_edit(
        message,
        "↕️ <b>СОРТИРОВКА</b>\n\n"
        "🆕 Новые — свежие дела первыми.\n"
        "🔥 Популярные — больше обсуждений и просмотров.\n"
        "🆘 Без советов — дела, которым ещё никто не ответил.",
        b.as_markup(),
    )


def case_text(story):
    demo_note = ""
    if story["is_demo"]:
        demo_note = (
            "\n\n🧪 <i>Демонстрационный пример от Anon Verdict. "
            "Это не история реального пользователя.</i>"
        )
    body = h(story["body"][:3000])
    if len(story["body"]) > 3000:
        body += "…"
    return (
        f"⚖️ <b>Дело №{story['id']}</b>\n"
        f"🏷️ {h(story['category'])}\n\n"
        f"<b>{h(story['title'])}</b>\n\n"
        f"{body}\n\n"
        f"👁 Просмотров: {story['views']}\n"
        f"💬 Советов: {story['comments_count']}"
        f"{demo_note}"
    )


def case_keyboard(
    story,
    feed_index=0,
    back_to="feed",
    cat_key="all",
    sort="new",
    favorite=False,
    own_story=False,
):
    cat_key, sort, _ = feed_options(cat_key, sort)
    b = InlineKeyboardBuilder()

    if not story["is_demo"]:
        b.button(
            text="💬 Дать совет",
            callback_data=f"advice:{story['id']}:{feed_index}:{cat_key}:{sort}",
        )

    if story["comments_count"]:
        b.button(
            text=f"💬 Советы ({story['comments_count']})",
            callback_data=f"comments:{story['id']}:0:{feed_index}:{cat_key}:{sort}",
        )

    b.button(
        text="🧠 Разбор",
        callback_data=f"ai:{story['id']}:{feed_index}:{cat_key}:{sort}",
    )

    b.button(
        text="✅ В избранном" if favorite else "⭐ В избранное",
        callback_data=f"fav:{story['id']}:{feed_index}:{back_to}:{cat_key}:{sort}",
    )

    if not story["is_demo"] and not own_story:
        b.button(
            text="🚩 Пожаловаться",
            callback_data=f"report:story:{story['id']}",
        )

    if back_to == "my":
        b.button(text="⬅️ К моим делам", callback_data=f"my:{feed_index}")
    elif back_to == "favorites":
        b.button(text="⬅️ К избранному", callback_data=f"favorites:{feed_index}")
    else:
        b.button(
            text="⬅️ В зал суда",
            callback_data=f"feed:{feed_index}:{cat_key}:{sort}",
        )

    b.button(text="🏠 Главное меню", callback_data="home")
    b.adjust(2, 2, 1, 1, 1)
    return b.as_markup()


async def render_case(
    message,
    sid,
    feed_index=0,
    back_to="feed",
    count_view=False,
    cat_key="all",
    sort="new",
):
    story = await db.story(sid, count_view)
    if not story:
        await safe_edit(
            message,
            "Дело не найдено.",
            home_inline(message.chat.id),
        )
        return

    viewer_id = message.chat.id
    favorite = await db.favorite_state(viewer_id, sid)
    own_story = int(story["author_tg_id"]) == int(viewer_id)

    await safe_edit(
        message,
        case_text(story),
        case_keyboard(
            story,
            feed_index,
            back_to,
            cat_key,
            sort,
            favorite=favorite,
            own_story=own_story,
        ),
    )


def comment_text(comment, index, total):
    role = ""
    if comment["staff_role"] and comment["staff_role"] != "SYSTEM":
        role = f"\n🛡️ {h(comment['staff_role'])}"
    return (
        f"💬 <b>СОВЕТЫ К ДЕЛУ</b>\n\n"
        f"<b>{h(comment['nickname'])}</b> · {h(comment['title'])}"
        f"{role}\n\n"
        f"{h(comment['body'])}\n\n"
        f"👍 {comment['likes']} · 👎 {comment['dislikes']}\n"
        f"📄 {index + 1} из {total}"
    )


def comment_keyboard(
    comment,
    sid,
    index,
    total,
    feed_index,
    cat_key="all",
    sort="new",
):
    cat_key, sort, _ = feed_options(cat_key, sort)
    b = InlineKeyboardBuilder()
    b.button(
        text=f"👍 {comment['likes']}",
        callback_data=f"react:1:{comment['id']}:{sid}:{index}:{feed_index}:{cat_key}:{sort}",
    )
    b.button(
        text=f"👎 {comment['dislikes']}",
        callback_data=f"react:-1:{comment['id']}:{sid}:{index}:{feed_index}:{cat_key}:{sort}",
    )
    b.button(
        text="🚩 Жалоба",
        callback_data=f"report:comment:{comment['id']}:{sid}",
    )
    prev_i = (index - 1) % total
    next_i = (index + 1) % total
    nav_row(
        b,
        f"comments:{sid}:{prev_i}:{feed_index}:{cat_key}:{sort}",
        f"{index + 1}/{total}",
        f"comments:{sid}:{next_i}:{feed_index}:{cat_key}:{sort}",
    )
    b.button(
        text="⬅️ К делу",
        callback_data=f"case:{sid}:{feed_index}:{cat_key}:{sort}",
    )
    b.adjust(2, 1, 3, 1)
    return b.as_markup()


async def render_comments(
    message,
    sid,
    index,
    feed_index,
    cat_key="all",
    sort="new",
):
    total = await db.comment_count(sid)
    if total == 0:
        story = await db.story(sid)
        await safe_edit(
            message,
            "💬 <b>СОВЕТЫ</b>\n\nПока никто не высказался. Можно стать первым.",
            case_keyboard(story, feed_index, "feed", cat_key, sort)
            if story
            else home_inline(),
        )
        return

    index = clamp(index, 0, total - 1)
    comment = await db.comment_item(sid, index)
    await safe_edit(
        message,
        comment_text(comment, index, total),
        comment_keyboard(
            comment,
            sid,
            index,
            total,
            feed_index,
            cat_key,
            sort,
        ),
    )


async def render_profile(message, user_id):
    u = await db.get_user(user_id)
    role = ""
    if u["staff_role"] and u["staff_role"] != "SYSTEM":
        role = f"🛡️ <b>{h(u['staff_role'])}</b>\n"

    notifications_on = bool(u["notifications_enabled"])
    notif_text = "🔔 Уведомления включены" if notifications_on else "🔕 Уведомления выключены"

    b = InlineKeyboardBuilder()
    b.button(text="✏️ Изменить профиль", callback_data="edit")
    b.button(text="⭐ Избранное", callback_data="favorites:0")
    b.button(
        text="🔕 Выключить уведомления" if notifications_on else "🔔 Включить уведомления",
        callback_data="notifications:toggle",
    )
    b.button(text="🎖️ Звания", callback_data="ranks")
    b.button(text="🏠 Главное меню", callback_data="home")
    b.adjust(2, 1, 1, 1)

    await safe_edit(
        message,
        f"👤 <b>{h(u['nickname'])}</b>\n"
        f"{role}"
        f"🎖️ {h(u['title'])}\n"
        f"⭐ Репутация: {u['reputation']}\n"
        f"📈 Уровень: {u['level']}\n"
        f"{notif_text}\n\n"
        f"📝 {h(u['bio'] or 'Описание пока не добавлено.')}",
        b.as_markup(),
    )


async def render_my_cases(message, user_id, index=0):
    total = await db.user_story_count(user_id)
    if total == 0:
        b = InlineKeyboardBuilder()
        b.button(text="📝 Подать первое дело", callback_data="new")
        b.button(text="🏠 Главное меню", callback_data="home")
        b.adjust(1)
        await safe_edit(
            message,
            "⚖️ <b>МОИ ДЕЛА</b>\n\nТы ещё ничего не публиковал.",
            b.as_markup(),
        )
        return

    index = clamp(index, 0, total - 1)
    story = await db.user_story_item(user_id, index)
    text = (
        f"⚖️ <b>МОИ ДЕЛА</b>\n\n"
        f"📜 <b>Дело №{story['id']}</b>\n"
        f"🏷️ {h(story['category'])}\n\n"
        f"<b>{h(story['title'])}</b>\n\n"
        f"👁 {story['views']} просмотров · 💬 {story['comments_count']} советов\n"
        f"📄 {index + 1} из {total}"
    )
    b = InlineKeyboardBuilder()
    b.button(text="📖 Открыть", callback_data=f"mycase:{story['id']}:{index}")
    prev_i = (index - 1) % total
    next_i = (index + 1) % total
    nav_row(b, f"my:{prev_i}", f"{index + 1}/{total}", f"my:{next_i}")
    b.button(text="🏠 Главное меню", callback_data="home")
    b.adjust(1, 3, 1)
    await safe_edit(message, text, b.as_markup())


async def render_favorites(message, user_id, index=0):
    total = await db.favorite_count(user_id)
    if total == 0:
        b = InlineKeyboardBuilder()
        b.button(text="🏛️ Найти дела", callback_data="feed:0")
        b.button(text="🏠 Главное меню", callback_data="home")
        b.adjust(1)
        await safe_edit(
            message,
            "⭐ <b>ИЗБРАННОЕ</b>\n\n"
            "Здесь будут дела, которые ты сохранил. "
            "По ним также можно получать уведомления о новых советах.",
            b.as_markup(),
        )
        return

    index = clamp(index, 0, total - 1)
    story = await db.favorite_item(user_id, index)
    if not story:
        await safe_edit(
            message,
            "⭐ <b>ИЗБРАННОЕ</b>\n\nСписок изменился. Открой его ещё раз.",
            home_inline(user_id),
        )
        return

    text = (
        "⭐ <b>ИЗБРАННОЕ</b>\n\n"
        f"⚖️ <b>Дело №{story['id']}</b>\n"
        f"🏷️ {h(story['category'])}\n\n"
        f"<b>{h(story['title'])}</b>\n\n"
        f"👁 {story['views']} · 💬 {story['comments_count']}\n"
        f"📄 {index + 1} из {total}"
    )

    b = InlineKeyboardBuilder()
    b.button(
        text="📖 Открыть",
        callback_data=f"favcase:{story['id']}:{index}",
    )
    prev_i = (index - 1) % total
    next_i = (index + 1) % total
    nav_row(
        b,
        f"favorites:{prev_i}",
        f"{index + 1}/{total}",
        f"favorites:{next_i}",
    )
    b.button(text="🏠 Главное меню", callback_data="home")
    b.adjust(1, 3, 1)
    await safe_edit(message, text, b.as_markup())


async def render_rating(message, page=0):
    total = await db.leaderboard_count()
    if total == 0:
        await safe_edit(
            message,
            "🏆 <b>РЕЙТИНГ</b>\n\nПока здесь пусто.",
            home_inline(),
        )
        return

    pages = max(1, (total + RATING_PAGE_SIZE - 1) // RATING_PAGE_SIZE)
    page = clamp(page, 0, pages - 1)
    offset = page * RATING_PAGE_SIZE
    rows = await db.leaderboard_page(offset, RATING_PAGE_SIZE)

    lines = []
    for i, x in enumerate(rows, offset + 1):
        staff = f" · 🛡️ {h(x['staff_role'])}" if x["staff_role"] else ""
        lines.append(
            f"<b>{i}.</b> {h(x['nickname'])} — ⭐ {x['reputation']} · {h(x['title'])}{staff}\n"
            f"   💬 {x['advice_count']} советов · 👍 {x['helpful_likes']} полезных оценок"
        )

    b = InlineKeyboardBuilder()
    if pages > 1:
        prev_p = (page - 1) % pages
        next_p = (page + 1) % pages
        nav_row(b, f"rating:{prev_p}", f"{page + 1}/{pages}", f"rating:{next_p}")
        b.adjust(3)
    b.button(text="🏠 Главное меню", callback_data="home")
    await safe_edit(
        message,
        "🏆 <b>РЕЙТИНГ ЗАЛА</b>\n\n" + "\n".join(lines),
        b.as_markup(),
    )


def admin_menu():
    b = InlineKeyboardBuilder()
    b.button(text="📊 Статистика", callback_data="admin:stats")
    b.button(text="🚩 Жалобы", callback_data="admin:reports:0")
    b.button(text="👥 Пользователи", callback_data="admin:users:0")
    b.button(text="🔎 Найти по ID", callback_data="admin:find")
    b.button(text="⚖️ Дела", callback_data="admin:cases:0")
    b.button(text="🏠 Главное меню", callback_data="home")
    b.adjust(2, 1, 1, 1, 1)
    return b.as_markup()


async def render_admin_home(message):
    reports = await db.admin_report_count()
    await safe_edit(
        message,
        "🛡️ <b>CEO ANON VERDICT</b>\n\n"
        "Управление проектом: статистика, пользователи, роли и модерация.\n\n"
        f"🚩 Открытых жалоб: <b>{reports}</b>",
        admin_menu(),
    )


async def render_admin_reports(message, page=0):
    total = await db.admin_report_count()
    if total == 0:
        await safe_edit(
            message,
            "🚩 <b>ЖАЛОБЫ</b>\n\nОткрытых жалоб нет.",
            admin_menu(),
        )
        return

    pages = max(1, (total + ADMIN_PAGE_SIZE - 1) // ADMIN_PAGE_SIZE)
    page = clamp(page, 0, pages - 1)
    rows = await db.admin_reports_page(page * ADMIN_PAGE_SIZE, ADMIN_PAGE_SIZE)

    b = InlineKeyboardBuilder()
    for report in rows:
        icon = "⚖️" if report["target_type"] == "story" else "💬"
        reason = str(report["reason"])[:22]
        b.button(
            text=f"🚩 #{report['id']} · {icon} {report['target_id']} · {reason}",
            callback_data=f"admin:report:{report['id']}:{page}",
        )

    if pages > 1:
        prev_p = (page - 1) % pages
        next_p = (page + 1) % pages
        nav_row(
            b,
            f"admin:reports:{prev_p}",
            f"{page + 1}/{pages}",
            f"admin:reports:{next_p}",
        )

    b.button(text="⬅️ CEO Панель", callback_data="admin:home")
    b.adjust(*([1] * len(rows)), 3 if pages > 1 else 1, 1)

    await safe_edit(
        message,
        "🚩 <b>ОЧЕРЕДЬ ЖАЛОБ</b>\n\n"
        "Сначала идут самые старые необработанные жалобы.",
        b.as_markup(),
    )


async def render_admin_report(message, report_id, page=0):
    report = await db.admin_report(report_id)
    if not report or report["status"] != "open":
        await render_admin_reports(message, page)
        return

    target_type = report["target_type"]
    target_id = report["target_id"]

    if target_type == "story":
        target = await db.admin_story(target_id)
        if target:
            preview = (
                f"⚖️ <b>Дело №{target_id}</b>\n"
                f"Статус: <b>{h(target['status'])}</b>\n"
                f"Автор: {h(target['author_nickname'])} · "
                f"<code>{target['author_tg_id']}</code>\n\n"
                f"<b>{h(target['title'])}</b>\n"
                f"{h(target['body'][:1000])}"
            )
        else:
            preview = f"⚖️ Дело №{target_id} не найдено."
    else:
        target = await db.admin_comment(target_id)
        if target:
            preview = (
                f"💬 <b>Совет №{target_id}</b>\n"
                f"Статус: <b>{h(target['status'])}</b>\n"
                f"Автор: {h(target['author_nickname'])} · "
                f"<code>{target['author_tg_id']}</code>\n"
                f"К делу: {h(target['story_title'])}\n\n"
                f"{h(target['body'][:1200])}"
            )
        else:
            preview = f"💬 Совет №{target_id} не найден."

    text = (
        f"🚩 <b>ЖАЛОБА №{report_id}</b>\n\n"
        f"Причина: <b>{h(report['reason'])}</b>\n"
        f"От: {h(report['reporter_nickname'])} · "
        f"<code>{report['reporter_tg_id']}</code>\n\n"
        f"{preview}"
    )

    b = InlineKeyboardBuilder()
    if target:
        b.button(
            text="🙈 Скрыть и закрыть",
            callback_data=f"admin:reportact:{report_id}:hide:{page}",
        )
    b.button(
        text="✅ Отклонить жалобу",
        callback_data=f"admin:reportact:{report_id}:dismiss:{page}",
    )
    b.button(text="⬅️ К жалобам", callback_data=f"admin:reports:{page}")
    b.adjust(1)

    await safe_edit(message, text, b.as_markup())


async def render_admin_users(message, page=0):
    total = await db.admin_user_count()
    if total == 0:
        await safe_edit(message, "👥 Пользователей пока нет.", admin_menu())
        return

    pages = max(1, (total + ADMIN_PAGE_SIZE - 1) // ADMIN_PAGE_SIZE)
    page = clamp(page, 0, pages - 1)
    rows = await db.admin_users_page(page * ADMIN_PAGE_SIZE, ADMIN_PAGE_SIZE)

    text = "👥 <b>ПОЛЬЗОВАТЕЛИ</b>\n\n"
    b = InlineKeyboardBuilder()
    for u in rows:
        role = "🛡️" if u["staff_role"] else "👤"
        b.button(
            text=f"{role} {u['nickname'][:22]} · {u['tg_id']}",
            callback_data=f"admin:user:{u['tg_id']}:{page}",
        )
    if pages > 1:
        prev_p = (page - 1) % pages
        next_p = (page + 1) % pages
        nav_row(b, f"admin:users:{prev_p}", f"{page + 1}/{pages}", f"admin:users:{next_p}")
    b.button(text="⬅️ CEO Панель", callback_data="admin:home")
    b.adjust(*([1] * len(rows)), 3 if pages > 1 else 1, 1)
    await safe_edit(message, text, b.as_markup())


async def render_admin_user(message, tg_id, page=0):
    u = await db.admin_user(tg_id)
    if not u:
        await safe_edit(message, "Пользователь не найден.", admin_menu())
        return

    username = f"@{h(u['tg_username'])}" if u["tg_username"] else "не указан"
    role = h(u["staff_role"] or "обычный пользователь")
    text = (
        "👤 <b>КАРТОЧКА ПОЛЬЗОВАТЕЛЯ</b>\n\n"
        f"Telegram ID: <code>{u['tg_id']}</code>\n"
        f"Username: {username}\n"
        f"Ник: <b>{h(u['nickname'])}</b>\n"
        f"Роль: <b>{role}</b>\n"
        f"Звание: <b>{h(u['title'])}</b>\n"
        f"⭐ Репутация: <b>{u['reputation']}</b>\n"
        f"⚖️ Дел: <b>{u['stories_count']}</b>\n"
        f"💬 Советов: <b>{u['comments_count']}</b>"
    )

    b = InlineKeyboardBuilder()
    if int(u["tg_id"]) != owner_id:
        b.button(text="💻 Developer", callback_data=f"admin:role:{u['tg_id']}:developer:{page}")
        b.button(text="🛡️ Moderator", callback_data=f"admin:role:{u['tg_id']}:moderator:{page}")
        b.button(text="👤 Снять роль", callback_data=f"admin:role:{u['tg_id']}:clear:{page}")
    b.button(text="⬅️ К пользователям", callback_data=f"admin:users:{page}")
    b.adjust(2, 1, 1)
    await safe_edit(message, text, b.as_markup())


async def render_admin_cases(message, page=0):
    total = await db.admin_story_count()
    if total == 0:
        await safe_edit(message, "⚖️ Реальных дел пока нет.", admin_menu())
        return

    pages = max(1, (total + ADMIN_PAGE_SIZE - 1) // ADMIN_PAGE_SIZE)
    page = clamp(page, 0, pages - 1)
    rows = await db.admin_stories_page(page * ADMIN_PAGE_SIZE, ADMIN_PAGE_SIZE)

    b = InlineKeyboardBuilder()
    for s in rows:
        icon = "🟢" if s["status"] == "open" else "🙈"
        b.button(
            text=f"{icon} #{s['id']} · {str(s['title'])[:24]}",
            callback_data=f"admin:story:{s['id']}:{page}",
        )
    if pages > 1:
        prev_p = (page - 1) % pages
        next_p = (page + 1) % pages
        nav_row(b, f"admin:cases:{prev_p}", f"{page + 1}/{pages}", f"admin:cases:{next_p}")
    b.button(text="⬅️ CEO Панель", callback_data="admin:home")
    b.adjust(*([1] * len(rows)), 3 if pages > 1 else 1, 1)

    await safe_edit(
        message,
        "⚖️ <b>ДЕЛА</b>\n\n🟢 открыто · 🙈 скрыто",
        b.as_markup(),
    )


async def render_admin_story(message, sid, page=0):
    s = await db.admin_story(sid)
    if not s:
        await safe_edit(message, "Дело не найдено.", admin_menu())
        return

    status = "🟢 открыто" if s["status"] == "open" else "🙈 скрыто"
    body = h(s["body"][:2200])
    if len(s["body"]) > 2200:
        body += "…"

    text = (
        f"⚖️ <b>ДЕЛО №{s['id']}</b>\n\n"
        f"Статус: <b>{status}</b>\n"
        f"Категория: {h(s['category'])}\n"
        f"Автор: <b>{h(s['author_nickname'])}</b>\n"
        f"Telegram ID: <code>{s['author_tg_id']}</code>\n"
        f"👁 {s['views']} · 💬 {s['comments_count']}\n\n"
        f"<b>{h(s['title'])}</b>\n"
        f"{body}"
    )

    b = InlineKeyboardBuilder()
    if s["status"] == "open":
        b.button(text="🙈 Скрыть дело", callback_data=f"admin:status:{sid}:hidden:{page}")
    else:
        b.button(text="🟢 Вернуть в зал", callback_data=f"admin:status:{sid}:open:{page}")
    b.button(text="⬅️ К делам", callback_data=f"admin:cases:{page}")
    b.adjust(1)
    await safe_edit(message, text, b.as_markup())


@dp.message(CommandStart())
async def start(m: Message):
    await ensure_message_user(m)
    u = await db.get_user(m.from_user.id)
    await m.answer(
        home_text(u),
        parse_mode="HTML",
        reply_markup=main_keyboard(m.from_user.id),
    )


@dp.callback_query(F.data == "noop")
async def noop(c: CallbackQuery):
    await c.answer()


@dp.callback_query(F.data == "home")
async def home(c: CallbackQuery):
    await ensure_callback_user(c)
    u = await db.get_user(c.from_user.id)
    await safe_edit(c.message, home_text(u), home_inline(c.from_user.id))
    await c.answer()


@dp.callback_query(F.data == "new")
async def new(c: CallbackQuery, state: FSMContext):
    await ensure_callback_user(c)
    if not is_owner(c.from_user.id):
        remaining = await db.story_cooldown_remaining(c.from_user.id)
        if remaining:
            await c.answer(
                f"Подожди ещё {remaining} сек. перед новым делом.",
                show_alert=True,
            )
            return
    await state.set_state(Story.category)
    b = InlineKeyboardBuilder()
    for x in CATS:
        b.button(text=x, callback_data="cat:" + x)
    b.button(text="⬅️ Главное меню", callback_data="home")
    b.adjust(2, 2, 2, 1, 1)
    await safe_edit(
        c.message,
        "📝 <b>НОВОЕ ДЕЛО</b>\n\nВыбери категорию:",
        b.as_markup(),
    )
    await c.answer()


@dp.callback_query(F.data.startswith("cat:"))
async def cat(c: CallbackQuery, state: FSMContext):
    await ensure_callback_user(c)
    await state.update_data(category=c.data[4:])
    await state.set_state(Story.title)
    await c.message.answer("Напиши короткое название ситуации:")
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
        "Теперь расскажи ситуацию подробно.\n"
        "Не указывай телефоны, адреса, документы и другие персональные данные."
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

    if not is_owner(m.from_user.id):
        remaining = await db.story_cooldown_remaining(m.from_user.id)
        if remaining:
            await m.answer(
                f"⏳ Слишком быстро. Подожди ещё {remaining} сек. перед публикацией нового дела."
            )
            return

    d = await state.get_data()
    sid = await db.create_story(m.from_user.id, d["category"], d["title"], text)
    await state.clear()

    b = InlineKeyboardBuilder()
    b.button(text="📖 Открыть моё дело", callback_data=f"mycase:{sid}:0")
    b.button(text="🏛️ Зал суда", callback_data="feed:0")
    b.adjust(1)
    await m.answer(
        f"✅ <b>Дело №{sid} опубликовано анонимно.</b>",
        parse_mode="HTML",
        reply_markup=b.as_markup(),
    )


@dp.callback_query(F.data.startswith("feed:"))
async def feed_callback(c: CallbackQuery):
    await ensure_callback_user(c)
    parts = c.data.split(":")
    try:
        index = int(parts[1])
    except (ValueError, IndexError):
        index = 0
    cat_key = parts[2] if len(parts) > 2 else "all"
    sort = parts[3] if len(parts) > 3 else "new"
    await render_feed(c.message, index, cat_key, sort)
    await c.answer()


@dp.callback_query(F.data.startswith("feedcat:"))
async def feed_category_callback(c: CallbackQuery):
    await ensure_callback_user(c)
    parts = c.data.split(":")
    cat_key = parts[1] if len(parts) > 1 else "all"
    sort = parts[2] if len(parts) > 2 else "new"
    await render_feed_categories(c.message, cat_key, sort)
    await c.answer()


@dp.callback_query(F.data.startswith("feedsort:"))
async def feed_sort_callback(c: CallbackQuery):
    await ensure_callback_user(c)
    parts = c.data.split(":")
    cat_key = parts[1] if len(parts) > 1 else "all"
    sort = parts[2] if len(parts) > 2 else "new"
    await render_feed_sorts(c.message, cat_key, sort)
    await c.answer()


@dp.callback_query(F.data.startswith("case:"))
async def case_callback(c: CallbackQuery):
    await ensure_callback_user(c)
    parts = c.data.split(":")
    try:
        sid = int(parts[1])
        index = int(parts[2])
    except (ValueError, IndexError):
        await c.answer("Некорректное дело", show_alert=True)
        return
    cat_key = parts[3] if len(parts) > 3 else "all"
    sort = parts[4] if len(parts) > 4 else "new"
    await render_case(
        c.message,
        sid,
        index,
        "feed",
        count_view=True,
        cat_key=cat_key,
        sort=sort,
    )
    await c.answer()


@dp.callback_query(F.data.startswith("mycase:"))
async def mycase_callback(c: CallbackQuery):
    await ensure_callback_user(c)
    try:
        _, sid, index = c.data.split(":")
        sid, index = int(sid), int(index)
    except (ValueError, IndexError):
        await c.answer("Некорректное дело", show_alert=True)
        return
    await render_case(c.message, sid, index, "my", count_view=False)
    await c.answer()


@dp.callback_query(F.data.startswith("comments:"))
async def comments_callback(c: CallbackQuery):
    await ensure_callback_user(c)
    parts = c.data.split(":")
    try:
        sid = int(parts[1])
        index = int(parts[2])
        feed_index = int(parts[3])
    except (ValueError, IndexError):
        await c.answer("Некорректная страница", show_alert=True)
        return
    cat_key = parts[4] if len(parts) > 4 else "all"
    sort = parts[5] if len(parts) > 5 else "new"
    await render_comments(
        c.message,
        sid,
        index,
        feed_index,
        cat_key,
        sort,
    )
    await c.answer()


@dp.callback_query(F.data.startswith("react:"))
async def react_callback(c: CallbackQuery):
    await ensure_callback_user(c)
    parts = c.data.split(":")
    try:
        value = int(parts[1])
        cid = int(parts[2])
        sid = int(parts[3])
        index = int(parts[4])
        feed_index = int(parts[5])
    except (ValueError, IndexError):
        await c.answer("Некорректная реакция", show_alert=True)
        return

    cat_key = parts[6] if len(parts) > 6 else "all"
    sort = parts[7] if len(parts) > 7 else "new"

    result = await db.react(c.from_user.id, cid, value)
    if result["status"] == "self":
        await c.answer("Свой совет оценивать нельзя.", show_alert=True)
        return
    if result["status"] == "not_found":
        await c.answer("Совет не найден.", show_alert=True)
        return
    if result["status"] == "unchanged":
        await c.answer("Эта оценка уже стоит.")
        return

    await render_comments(
        c.message,
        sid,
        index,
        feed_index,
        cat_key,
        sort,
    )

    delta = result.get("reputation_delta", 0)
    if delta > 0:
        message = "👍 Автору +1 репутации"
        if (
            result.get("author_notifications")
            and result.get("author_tg_id")
            and int(result["author_tg_id"]) != int(c.from_user.id)
        ):
            rb = InlineKeyboardBuilder()
            rb.button(
                text="💬 Открыть советы",
                callback_data=f"comments:{sid}:0:0:all:new",
            )
            await safe_notify(
                result["author_tg_id"],
                "👍 <b>Твой совет отметили полезным</b>\n\n"
                "За эту оценку тебе начислено <b>+1 репутации</b>.",
                rb.as_markup(),
            )
    elif delta < 0:
        message = "Лайк снят: −1 репутации"
    else:
        message = "Оценка сохранена"
    await c.answer(message)


@dp.callback_query(F.data.startswith("advice:"))
async def advice_start(c: CallbackQuery, state: FSMContext):
    await ensure_callback_user(c)
    parts = c.data.split(":")
    try:
        sid = int(parts[1])
        feed_index = int(parts[2])
    except (ValueError, IndexError):
        await c.answer("Некорректное дело", show_alert=True)
        return

    cat_key = parts[3] if len(parts) > 3 else "all"
    sort = parts[4] if len(parts) > 4 else "new"

    if not is_owner(c.from_user.id):
        remaining = await db.comment_cooldown_remaining(c.from_user.id)
        if remaining:
            await c.answer(
                f"Подожди ещё {remaining} сек. перед следующим советом.",
                show_alert=True,
            )
            return

    story = await db.story(sid)
    if not story or story["is_demo"]:
        await c.answer(
            "К демонстрационным делам советы не добавляются.",
            show_alert=True,
        )
        return
    if int(story["author_tg_id"]) == int(c.from_user.id):
        await c.answer(
            "Нельзя давать советы собственному делу.",
            show_alert=True,
        )
        return

    await state.update_data(
        sid=sid,
        feed_index=feed_index,
        cat_key=cat_key,
        sort=sort,
    )
    await state.set_state(Comment.body)
    await c.message.answer(
        "💬 Напиши совет или мнение.\n\n"
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

    if not is_owner(m.from_user.id):
        remaining = await db.comment_cooldown_remaining(m.from_user.id)
        if remaining:
            await m.answer(
                f"⏳ Слишком быстро. Подожди ещё {remaining} сек. перед следующим советом."
            )
            return

    d = await state.get_data()
    result = await db.comment(m.from_user.id, d["sid"], text)
    await state.clear()
    u = await db.get_user(m.from_user.id)

    cat_key = d.get("cat_key", "all")
    sort = d.get("sort", "new")
    feed_index = d.get("feed_index", 0)

    open_case = InlineKeyboardBuilder()
    open_case.button(
        text="📖 Открыть дело",
        callback_data=f"case:{d['sid']}:0:all:new",
    )

    owner_tg_id = result.get("story_owner_tg_id")
    if (
        result.get("story_owner_notifications")
        and owner_tg_id
        and int(owner_tg_id) != int(m.from_user.id)
    ):
        await safe_notify(
            owner_tg_id,
            "💬 <b>Новый совет к твоему делу</b>\n\n"
            f"⚖️ {h(result.get('story_title'))}\n"
            f"🧠 {h(result.get('commenter_nickname'))} оставил новый совет.",
            open_case.as_markup(),
        )

    followers = await db.favorite_subscribers(
        d["sid"],
        exclude_tg_ids=[m.from_user.id, owner_tg_id],
    )
    if followers:
        asyncio.create_task(
            notify_many(
                followers,
                "⭐ <b>Новое в сохранённом деле</b>\n\n"
                f"⚖️ {h(result.get('story_title'))}\n"
                "Появился новый совет.",
                open_case.as_markup(),
            )
        )

    b = InlineKeyboardBuilder()
    b.button(
        text="⬅️ К делу",
        callback_data=f"case:{d['sid']}:{feed_index}:{cat_key}:{sort}",
    )
    reward = int(result.get("reputation_reward", 0))
    reward_line = (
        f"<b>+{reward} репутации.</b>"
        if reward
        else "Повторный совет к этому делу репутацию не начисляет."
    )
    await m.answer(
        "✅ Совет опубликован. " + reward_line + "\n\n"
        "Если другие участники поставят 👍, репутация автора совета тоже вырастет.\n\n"
        f"🎖️ {h(u['title'])} · ⭐ {u['reputation']}",
        parse_mode="HTML",
        reply_markup=b.as_markup(),
    )


@dp.callback_query(F.data.startswith("ai:"))
async def ai_callback(c: CallbackQuery):
    await ensure_callback_user(c)
    parts = c.data.split(":")
    try:
        sid = int(parts[1])
        feed_index = int(parts[2])
    except (ValueError, IndexError):
        await c.answer("Некорректное дело", show_alert=True)
        return

    cat_key = parts[3] if len(parts) > 3 else "all"
    sort = parts[4] if len(parts) > 4 else "new"

    story = await db.story(sid)
    if not story:
        await c.answer("Дело не найдено", show_alert=True)
        return

    result = await review(story["title"], story["body"])
    b = InlineKeyboardBuilder()
    b.button(
        text="⬅️ К делу",
        callback_data=f"case:{sid}:{feed_index}:{cat_key}:{sort}",
    )
    await safe_edit(
        c.message,
        "🧠 <b>РАЗБОР СОВЕТНИКА</b>\n\n" + h(result),
        b.as_markup(),
    )
    await c.answer()


@dp.callback_query(F.data == "profile")
async def profile_callback(c: CallbackQuery):
    await ensure_callback_user(c)
    await render_profile(c.message, c.from_user.id)
    await c.answer()


@dp.callback_query(F.data == "edit")
async def edit_profile(c: CallbackQuery, state: FSMContext):
    await ensure_callback_user(c)
    await state.set_state(Profile.nickname)
    await c.message.answer("✏️ Напиши новый ник (до 32 символов):")
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
    await m.answer("Теперь описание профиля (до 160 символов):")


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
    b = InlineKeyboardBuilder()
    b.button(text="👤 Открыть профиль", callback_data="profile")
    await m.answer("✅ Профиль обновлён.", reply_markup=b.as_markup())


@dp.callback_query(F.data.startswith("my:"))
async def my_cases_callback(c: CallbackQuery):
    await ensure_callback_user(c)
    try:
        index = int(c.data.split(":")[1])
    except (ValueError, IndexError):
        index = 0
    await render_my_cases(c.message, c.from_user.id, index)
    await c.answer()


@dp.callback_query(F.data.startswith("favorites:"))
async def favorites_callback(c: CallbackQuery):
    await ensure_callback_user(c)
    try:
        index = int(c.data.split(":")[1])
    except (ValueError, IndexError):
        index = 0
    await render_favorites(c.message, c.from_user.id, index)
    await c.answer()


@dp.callback_query(F.data.startswith("favcase:"))
async def favorite_case_callback(c: CallbackQuery):
    await ensure_callback_user(c)
    try:
        _, sid, index = c.data.split(":")
        sid, index = int(sid), int(index)
    except (ValueError, IndexError):
        await c.answer("Некорректное дело", show_alert=True)
        return
    await render_case(
        c.message,
        sid,
        index,
        back_to="favorites",
        count_view=True,
    )
    await c.answer()


@dp.callback_query(F.data.startswith("fav:"))
async def favorite_toggle_callback(c: CallbackQuery):
    await ensure_callback_user(c)
    parts = c.data.split(":")
    try:
        sid = int(parts[1])
        index = int(parts[2])
    except (ValueError, IndexError):
        await c.answer("Некорректное дело", show_alert=True)
        return

    back_to = parts[3] if len(parts) > 3 else "feed"
    cat_key = parts[4] if len(parts) > 4 else "all"
    sort = parts[5] if len(parts) > 5 else "new"

    enabled = await db.toggle_favorite(c.from_user.id, sid)
    await render_case(
        c.message,
        sid,
        index,
        back_to=back_to,
        count_view=False,
        cat_key=cat_key,
        sort=sort,
    )
    await c.answer(
        "⭐ Дело сохранено. Буду сообщать о новых советах."
        if enabled
        else "Дело удалено из избранного."
    )


@dp.callback_query(F.data == "notifications:toggle")
async def notifications_toggle_callback(c: CallbackQuery):
    await ensure_callback_user(c)
    enabled = await db.toggle_notifications(c.from_user.id)
    await render_profile(c.message, c.from_user.id)
    await c.answer(
        "🔔 Уведомления включены." if enabled else "🔕 Уведомления выключены."
    )


@dp.callback_query(F.data.startswith("report:"))
async def report_menu_callback(c: CallbackQuery):
    await ensure_callback_user(c)
    parts = c.data.split(":")
    if len(parts) < 3:
        await c.answer("Некорректная жалоба", show_alert=True)
        return

    target_type = parts[1]
    try:
        target_id = int(parts[2])
    except ValueError:
        await c.answer("Некорректная жалоба", show_alert=True)
        return

    sid = None
    if target_type == "comment":
        try:
            sid = int(parts[3])
        except (ValueError, IndexError):
            await c.answer("Некорректная жалоба", show_alert=True)
            return

    short_type = "s" if target_type == "story" else "c"
    b = InlineKeyboardBuilder()
    for reason_key, label in REPORT_REASONS.items():
        suffix = f":{sid}" if sid is not None else ""
        b.button(
            text=label,
            callback_data=f"reportdo:{short_type}:{target_id}:{reason_key}{suffix}",
        )

    if target_type == "story":
        b.button(
            text="⬅️ К делу",
            callback_data=f"case:{target_id}:0:all:new",
        )
    else:
        b.button(
            text="⬅️ К советам",
            callback_data=f"comments:{sid}:0:0:all:new",
        )
    b.adjust(1)

    await safe_edit(
        c.message,
        "🚩 <b>ЖАЛОБА</b>\n\n"
        "Выбери причину. Жалоба попадёт в закрытую очередь модерации.",
        b.as_markup(),
    )
    await c.answer()


@dp.callback_query(F.data.startswith("reportdo:"))
async def report_submit_callback(c: CallbackQuery):
    await ensure_callback_user(c)
    parts = c.data.split(":")
    if len(parts) < 4:
        await c.answer("Некорректная жалоба", show_alert=True)
        return

    target_type = "story" if parts[1] == "s" else "comment"
    try:
        target_id = int(parts[2])
    except ValueError:
        await c.answer("Некорректная жалоба", show_alert=True)
        return

    reason_key = parts[3]
    if reason_key not in REPORT_REASONS:
        await c.answer("Неизвестная причина", show_alert=True)
        return

    sid = target_id if target_type == "story" else None
    if target_type == "comment":
        try:
            sid = int(parts[4])
        except (ValueError, IndexError):
            await c.answer("Некорректная жалоба", show_alert=True)
            return

    result = await db.report_target(
        c.from_user.id,
        target_type,
        target_id,
        REPORT_REASONS[reason_key],
    )

    status = result.get("status")
    if status == "self":
        await c.answer("На собственный контент жалобу отправлять нельзя.", show_alert=True)
        return
    if status == "duplicate":
        await c.answer("Ты уже отправлял жалобу на этот материал.", show_alert=True)
        return
    if status != "created":
        await c.answer("Не удалось отправить жалобу.", show_alert=True)
        return

    report_id = result["report_id"]
    if owner_id and int(owner_id) != int(c.from_user.id):
        rb = InlineKeyboardBuilder()
        rb.button(
            text="🚩 Открыть жалобу",
            callback_data=f"admin:report:{report_id}:0",
        )
        await safe_notify(
            owner_id,
            "🚩 <b>Новая жалоба в Anon Verdict</b>\n\n"
            f"Причина: {h(REPORT_REASONS[reason_key])}",
            rb.as_markup(),
        )

    back = InlineKeyboardBuilder()
    back.button(
        text="⬅️ Вернуться",
        callback_data=(
            f"case:{sid}:0:all:new"
            if target_type == "story"
            else f"comments:{sid}:0:0:all:new"
        ),
    )
    await safe_edit(
        c.message,
        "✅ <b>Жалоба отправлена.</b>\n\n"
        "Модерация увидит её в закрытой панели.",
        back.as_markup(),
    )
    await c.answer("Жалоба отправлена")


@dp.callback_query(F.data.startswith("rating:"))
async def rating_callback(c: CallbackQuery):
    await ensure_callback_user(c)
    try:
        page = int(c.data.split(":")[1])
    except (ValueError, IndexError):
        page = 0
    await render_rating(c.message, page)
    await c.answer()


@dp.callback_query(F.data == "ranks")
async def ranks_callback(c: CallbackQuery):
    await ensure_callback_user(c)
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Профиль", callback_data="profile")
    b.button(text="🏠 Главное меню", callback_data="home")
    b.adjust(1)
    await safe_edit(
        c.message,
        "🎖️ <b>СУДЕБНЫЕ ЗВАНИЯ</b>\n\n"
        "🌱 Новичок — 0+\n"
        "⚔️ Присяжный — 10+\n"
        "⚖️ Судья — 30+\n"
        "🏛️ Старший судья — 75+\n"
        "👑 Верховный судья — 150+\n\n"
        "⭐ Опубликованный совет даёт +2 репутации.\n"
        "👍 Каждый уникальный лайк от другого пользователя даёт автору совета ещё +1.\n"
        "👎 Дизлайк сам по себе репутацию не отнимает.\n"
        "🛡️ Роли команды Anon Verdict существуют отдельно от судебных званий.",
        b.as_markup(),
    )
    await c.answer()


@dp.callback_query(F.data == "help")
async def help_callback(c: CallbackQuery):
    await ensure_callback_user(c)
    b = InlineKeyboardBuilder()
    b.button(text="🏠 Главное меню", callback_data="home")
    await safe_edit(
        c.message,
        "ℹ️ <b>КАК ЭТО РАБОТАЕТ</b>\n\n"
        "📝 <b>Подать дело</b> — анонимно рассказать ситуацию.\n"
        "🏛️ <b>Зал суда</b> — листать дела по одному, фильтровать по категориям и сортировать.\n"
        "🔥 Можно открыть популярные дела или найти те, где ещё нет советов.\n"
        "💬 <b>Советы</b> — тоже листаются внутри одного сообщения.\n"
        "⚖️ <b>Мои дела</b> — отдельная лента твоих публикаций.\n"
        "⭐ <b>Избранное</b> — сохраняй дела и получай уведомления о новых советах.\n"
        "🚩 На неподходящее дело или совет можно отправить жалобу.\n"
        "🏆 <b>Рейтинг</b> учитывает репутацию и полезные оценки советов.\n"
        "⭐ Первый совет к делу даёт +2 репутации, каждый уникальный 👍 — ещё +1.\n"
        "⏳ Антиспам ограничивает слишком частые публикации.\n\n"
        "Быстрая клавиатура снизу остаётся: она нужна для мгновенного перехода в разделы.",
        b.as_markup(),
    )
    await c.answer()


@dp.message(F.text == "🏛️ Зал суда")
async def menu_feed(m: Message):
    await ensure_message_user(m)
    msg = await m.answer("🏛️ Открываю Зал суда…", parse_mode="HTML")
    await render_feed(msg, 0)


@dp.message(F.text == "👤 Мой профиль")
async def menu_profile(m: Message):
    await ensure_message_user(m)
    msg = await m.answer("👤 Открываю профиль…")
    await render_profile(msg, m.from_user.id)


@dp.message(F.text == "🏆 Рейтинг")
async def menu_rating(m: Message):
    await ensure_message_user(m)
    msg = await m.answer("🏆 Открываю рейтинг…")
    await render_rating(msg, 0)


@dp.message(F.text == "⚖️ Мои дела")
async def menu_my_cases(m: Message):
    await ensure_message_user(m)
    msg = await m.answer("⚖️ Открываю твои дела…")
    await render_my_cases(msg, m.from_user.id, 0)


@dp.message(F.text == "⭐ Избранное")
async def menu_favorites(m: Message):
    await ensure_message_user(m)
    msg = await m.answer("⭐ Открываю избранное…")
    await render_favorites(msg, m.from_user.id, 0)


@dp.message(F.text == "🎖️ Звания")
async def menu_ranks(m: Message):
    await ensure_message_user(m)
    b = InlineKeyboardBuilder()
    b.button(text="👤 Профиль", callback_data="profile")
    b.button(text="🏠 Главное меню", callback_data="home")
    b.adjust(1)
    await m.answer(
        "🎖️ <b>СУДЕБНЫЕ ЗВАНИЯ</b>\n\n"
        "🌱 Новичок — 0+\n"
        "⚔️ Присяжный — 10+\n"
        "⚖️ Судья — 30+\n"
        "🏛️ Старший судья — 75+\n"
        "👑 Верховный судья — 150+\n\n"
        "⭐ Первый совет к каждому чужому делу даёт +2 репутации.\n"
        "👍 Полезные оценки других пользователей дают ещё +1.",
        parse_mode="HTML",
        reply_markup=b.as_markup(),
    )


@dp.message(F.text == "ℹ️ Как это работает")
async def menu_help(m: Message):
    await ensure_message_user(m)
    b = InlineKeyboardBuilder()
    b.button(text="🏠 Главное меню", callback_data="home")
    await m.answer(
        "ℹ️ <b>КАК ЭТО РАБОТАЕТ</b>\n\n"
        "📝 Подай дело → расскажи ситуацию анонимно.\n"
        "🏛️ Зал суда → листай дела, выбирай категорию и сортировку.\n"
        "🔥 Популярные / 🆘 без советов → быстрые режимы ленты.\n"
        "💬 Советы → листай внутри карточки дела.\n"
        "⚖️ Мои дела → следи за своими публикациями.\n"
        "⭐ Избранное → сохраняй интересные дела и следи за обновлениями.\n"
        "🚩 Жалоба → отправляй спорный контент в закрытую очередь модерации.\n"
        "🏆 Рейтинг → открывай страницы по 10 человек.\n\n"
        "Быстрые кнопки снизу остаются — это твоя постоянная панель навигации.",
        parse_mode="HTML",
        reply_markup=b.as_markup(),
    )


@dp.message(F.text == "📝 Подать дело")
async def menu_new(m: Message, state: FSMContext):
    await ensure_message_user(m)
    if not is_owner(m.from_user.id):
        remaining = await db.story_cooldown_remaining(m.from_user.id)
        if remaining:
            await m.answer(
                f"⏳ Подожди ещё {remaining} сек. перед новым делом."
            )
            return
    await state.set_state(Story.category)
    b = InlineKeyboardBuilder()
    for x in CATS:
        b.button(text=x, callback_data="cat:" + x)
    b.adjust(2)
    await m.answer(
        "📝 <b>НОВОЕ ДЕЛО</b>\n\nВыбери категорию:",
        parse_mode="HTML",
        reply_markup=b.as_markup(),
    )


@dp.message(Command("profile"))
async def profile_command(m: Message):
    await ensure_message_user(m)
    u = await db.get_user(m.from_user.id)
    await m.answer(
        f"👤 <b>{h(u['nickname'])}</b>\n"
        f"🎖️ {h(u['title'])}\n"
        f"⭐ {u['reputation']}",
        parse_mode="HTML",
        reply_markup=main_keyboard(m.from_user.id),
    )


@dp.message(F.text == "🛡️ CEO Панель")
@dp.message(Command("admin"))
async def admin_entry(m: Message, state: FSMContext):
    await ensure_message_user(m)
    if not is_owner(m.from_user.id):
        await m.answer("Раздел доступен только владельцу проекта.")
        return
    await state.clear()
    msg = await m.answer("🛡️ Открываю CEO-панель…")
    await render_admin_home(msg)


@dp.callback_query(F.data == "admin:home")
async def admin_home_callback(c: CallbackQuery, state: FSMContext):
    if not is_owner(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await render_admin_home(c.message)
    await c.answer()


@dp.callback_query(F.data == "admin:stats")
async def admin_stats_callback(c: CallbackQuery):
    if not is_owner(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    s = await db.admin_stats()
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ CEO Панель", callback_data="admin:home")
    await safe_edit(
        c.message,
        "📊 <b>СТАТИСТИКА ANON VERDICT</b>\n\n"
        f"👥 Пользователи: <b>{s['users']}</b>\n"
        f"🛡️ Команда: <b>{s['staff']}</b>\n"
        f"⚖️ Реальные дела: <b>{s['stories']}</b>\n"
        f"🟢 Открытые: <b>{s['open_stories']}</b>\n"
        f"🙈 Скрытые: <b>{s['hidden_stories']}</b>\n"
        f"💬 Советы: <b>{s['comments']}</b>\n"
        f"👍👎 Реакции: <b>{s['reactions']}</b>\n"
        f"⭐ Сохранений: <b>{s['favorites']}</b>\n"
        f"🚩 Открытых жалоб: <b>{s['reports']}</b>",
        b.as_markup(),
    )
    await c.answer()


@dp.callback_query(F.data.startswith("admin:reports:"))
async def admin_reports_callback(c: CallbackQuery):
    if not is_owner(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    try:
        page = int(c.data.split(":")[2])
    except (ValueError, IndexError):
        page = 0
    await render_admin_reports(c.message, page)
    await c.answer()


@dp.callback_query(F.data.startswith("admin:report:"))
async def admin_report_callback(c: CallbackQuery):
    if not is_owner(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    try:
        _, _, report_id, page = c.data.split(":")
        report_id, page = int(report_id), int(page)
    except (ValueError, IndexError):
        await c.answer("Некорректная жалоба", show_alert=True)
        return
    await render_admin_report(c.message, report_id, page)
    await c.answer()


@dp.callback_query(F.data.startswith("admin:reportact:"))
async def admin_report_action_callback(c: CallbackQuery):
    if not is_owner(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return

    try:
        _, _, report_id, action, page = c.data.split(":")
        report_id, page = int(report_id), int(page)
    except (ValueError, IndexError):
        await c.answer("Некорректная команда", show_alert=True)
        return

    report = await db.admin_report(report_id)
    if not report or report["status"] != "open":
        await c.answer("Жалоба уже обработана.", show_alert=True)
        await render_admin_reports(c.message, page)
        return

    if action == "dismiss":
        await db.resolve_report(report_id, "dismissed")
        await render_admin_reports(c.message, page)
        await c.answer("Жалоба отклонена.")
        return

    if action != "hide":
        await c.answer("Неизвестное действие", show_alert=True)
        return

    if report["target_type"] == "story":
        await db.set_story_status(report["target_id"], "hidden")
    else:
        await db.set_comment_status(report["target_id"], "hidden")

    await db.resolve_report(report_id, "resolved")
    await render_admin_reports(c.message, page)
    await c.answer("Материал скрыт, жалоба закрыта.")


@dp.callback_query(F.data.startswith("admin:users:"))
async def admin_users_callback(c: CallbackQuery):
    if not is_owner(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    try:
        page = int(c.data.split(":")[2])
    except (ValueError, IndexError):
        page = 0
    await render_admin_users(c.message, page)
    await c.answer()


@dp.callback_query(F.data == "admin:find")
async def admin_find(c: CallbackQuery, state: FSMContext):
    if not is_owner(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(Admin.user_lookup)
    await c.message.answer(
        "🔎 Отправь Telegram ID пользователя числом.\n"
        "Например: <code>123456789</code>",
        parse_mode="HTML",
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
    u = await db.admin_user(int(raw))
    if not u:
        await m.answer("Пользователь с таким Telegram ID не найден.")
        return
    msg = await m.answer("👤 Открываю карточку…")
    await render_admin_user(msg, int(raw), 0)


@dp.callback_query(F.data.startswith("admin:user:"))
async def admin_user_callback(c: CallbackQuery):
    if not is_owner(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    try:
        _, _, tg_id, page = c.data.split(":")
        tg_id, page = int(tg_id), int(page)
    except (ValueError, IndexError):
        await c.answer("Некорректный ID", show_alert=True)
        return
    await render_admin_user(c.message, tg_id, page)
    await c.answer()


@dp.callback_query(F.data.startswith("admin:role:"))
async def admin_role_callback(c: CallbackQuery):
    if not is_owner(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    try:
        _, _, tg_id, role_key, page = c.data.split(":")
        tg_id, page = int(tg_id), int(page)
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

    await render_admin_user(c.message, tg_id, page)
    await c.answer("Роль обновлена")


@dp.callback_query(F.data.startswith("admin:cases:"))
async def admin_cases_callback(c: CallbackQuery):
    if not is_owner(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    try:
        page = int(c.data.split(":")[2])
    except (ValueError, IndexError):
        page = 0
    await render_admin_cases(c.message, page)
    await c.answer()


@dp.callback_query(F.data.startswith("admin:story:"))
async def admin_story_callback(c: CallbackQuery):
    if not is_owner(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    try:
        _, _, sid, page = c.data.split(":")
        sid, page = int(sid), int(page)
    except (ValueError, IndexError):
        await c.answer("Некорректное дело", show_alert=True)
        return
    await render_admin_story(c.message, sid, page)
    await c.answer()


@dp.callback_query(F.data.startswith("admin:status:"))
async def admin_status_callback(c: CallbackQuery):
    if not is_owner(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    try:
        _, _, sid, status, page = c.data.split(":")
        sid, page = int(sid), int(page)
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

    await render_admin_story(c.message, sid, page)
    await c.answer("Статус обновлён")


async def main():
    await db.init()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
