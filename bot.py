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

PRIMARY_NAV_TEXTS = {
    "📝 Подать дело",
    "🏛️ Зал суда",
    "👤 Мой профиль",
    "🏆 Рейтинг",
    "⚖️ Мои дела",
    "⭐ Избранное",
    "🛡️ CEO Панель",
}


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


class Discussion(StatesGroup):
    body = State()


class StoryUpdate(StatesGroup):
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
        [KeyboardButton(text="⚖️ Мои дела"), KeyboardButton(text="👤 Мой профиль")],
        [KeyboardButton(text="⭐ Избранное"), KeyboardButton(text="🏆 Рейтинг")],
    ]
    if is_owner(user_id):
        rows.append([KeyboardButton(text="🛡️ CEO Панель")])

    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Что открыть?",
    )


def home_inline(user_id=None):
    b = InlineKeyboardBuilder()
    b.button(text="🏛️ Зал суда", callback_data="feed:0")
    b.button(text="📝 Подать дело", callback_data="new")
    b.button(text="⚖️ Мои дела", callback_data="my:0")
    b.button(text="👤 Профиль", callback_data="profile")
    b.button(text="⭐ Избранное", callback_data="favorites:0")
    b.button(text="🏆 Рейтинг", callback_data="rating:0")
    if is_owner(user_id):
        b.button(text="🛡️ CEO Панель", callback_data="admin:home")
        b.adjust(2, 2, 2, 1)
    else:
        b.adjust(2, 2, 2)
    return b.as_markup()


def home_text(user=None):
    role_text = ""
    if user and user["staff_role"] and user["staff_role"] != "SYSTEM":
        role_text = f"\n🛡️ Роль: <b>{h(user['staff_role'])}</b>"
    return (
        "⚖️ <b>ANON VERDICT</b>\n\n"
        "Анонимно расскажи ситуацию, обсуди её и получи советы со стороны.\n\n"
        "🔒 Автор дела скрыт от других пользователей.\n"
        "⭐ Полезные советы повышают репутацию."
        f"{role_text}"
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


async def present(message, text, reply_markup=None, edit=True):
    if edit:
        await safe_edit(message, text, reply_markup)
    else:
        await message.answer(
            text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )


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


async def upsert_advice_notification(owner_tg_id, sid, story_title, commenter_nickname, total):
    inbox = await db.advice_inbox(owner_tg_id, sid)
    seen = int(inbox["last_seen_count"] or 0) if inbox else 0
    unread = max(1, int(total) - seen)

    b = InlineKeyboardBuilder()
    b.button(
        text=f"💬 Читать ответы · {total}",
        callback_data=f"comments:{sid}:{max(0, int(total)-1)}:0:all:new:my",
    )
    markup = b.as_markup()
    text = (
        "💬 <b>НОВЫЕ ОТВЕТЫ К ТВОЕМУ ДЕЛУ</b>\n\n"
        f"⚖️ {h(story_title)}\n"
        f"🆕 Непрочитанных: <b>{unread}</b> · Всего: <b>{total}</b>\n"
        f"Последний ответ: 🧠 {h(commenter_nickname)}"
    )

    message_id = int(inbox["message_id"]) if inbox and inbox["message_id"] else None
    if message_id:
        try:
            await bot.edit_message_text(
                chat_id=owner_tg_id,
                message_id=message_id,
                text=text,
                parse_mode="HTML",
                reply_markup=markup,
            )
            return True
        except TelegramBadRequest:
            pass

    try:
        sent = await bot.send_message(
            owner_tg_id,
            text,
            parse_mode="HTML",
            reply_markup=markup,
        )
        await db.set_advice_inbox(owner_tg_id, sid, sent.message_id, seen)
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
    if (m.text or "") in PRIMARY_NAV_TEXTS:
        await db.clear_pending_input(m.from_user.id)


async def ensure_callback_user(c: CallbackQuery):
    await db.ensure_user(c.from_user.id, c.from_user.username)
    await db.clear_pending_input(c.from_user.id)


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


def feed_case_status(story):
    if story["is_demo"]:
        return "🧪 Пример"
    if story["comments_count"] == 0:
        return "🆘 Нужны советы"
    if story["discussion_count"] > 0:
        return "🗣 Идёт обсуждение"
    if story["comments_count"] >= 5:
        return "🔥 Много мнений"
    return "🟢 Открыто"


def story_status_label(status):
    return {
        "open": "🟢 Открыто",
        "closed": "✅ Завершено",
        "hidden": "🙈 Скрыто модерацией",
        "deleted": "🗑 Удалено",
    }.get(status, h(status))


def feed_card_text(story, index, total, cat_key="all", sort="new"):
    cat_key, sort, _ = feed_options(cat_key, sort)

    excerpt = str(story["body"]).strip()
    if len(excerpt) > 280:
        excerpt = excerpt[:280].rstrip() + "…"

    filter_label = FEED_CATEGORIES[cat_key][0]
    sort_label = FEED_SORTS[sort]
    status = feed_case_status(story)

    return (
        "🏛️ <b>ЗАЛ СУДА</b>\n"
        f"<i>{h(filter_label)} · {h(sort_label)}</i>\n\n"
        f"⚖️ <b>ДЕЛО №{story['id']}</b>\n"
        f"{h(story['category'])} · {h(status)}\n\n"
        f"<b>{h(story['title'])}</b>\n\n"
        f"{h(excerpt)}\n\n"
        "────────────\n"
        f"👁 {story['views']}   💬 {story['comments_count']}   "
        f"🗣 {story['discussion_count']}   ⭐ {story['favorites_count']}\n"
        f"<i>{'Нужно мнение суда' if story['comments_count'] == 0 and not story['is_demo'] else 'Открой дело, чтобы посмотреть детали'}</i>"
    )


def feed_keyboard(index, total, sid, cat_key="all", sort="new"):
    cat_key, sort, _ = feed_options(cat_key, sort)
    b = InlineKeyboardBuilder()

    b.button(
        text="📖 Читать дело",
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


async def render_feed(message, index=0, cat_key="all", sort="new", edit=True):
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
        await present(
            message,
            "🏛️ <b>ЗАЛ СУДА</b>\n\n"
            "По выбранным фильтрам дел пока нет.",
            b.as_markup(),
            edit=edit,
        )
        return

    index = clamp(index, 0, total - 1)
    story = await db.feed_item(index, category=category, sort=sort)
    if not story:
        index = 0
        story = await db.feed_item(0, category=category, sort=sort)

    await present(
        message,
        feed_card_text(story, index, total, cat_key, sort),
        feed_keyboard(index, total, story["id"], cat_key, sort),
        edit=edit,
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
        text="⬅️ В Зал суда",
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
        text="⬅️ В Зал суда",
        callback_data=f"feed:0:{cat_key}:{current_sort}",
    )
    b.adjust(1)
    await safe_edit(
        message,
        "↕️ <b>СОРТИРОВКА</b>\n\n"
        "🆕 Новые — свежие дела первыми.\n"
        "🔥 Популярные — больше советов и просмотров.\n"
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

    status_line = ""
    if not story["is_demo"]:
        status_line = f"\n{story_status_label(story['status'])}"

    body = h(story["body"][:3000])
    if len(story["body"]) > 3000:
        body += "…"

    return (
        f"⚖️ <b>Дело №{story['id']}</b>\n"
        f"🏷️ {h(story['category'])}{status_line}\n\n"
        f"<b>{h(story['title'])}</b>\n\n"
        f"{body}\n\n"
        f"👁 {story['views']} · 💬 {story['comments_count']} · "
        f"🗣 {story['discussion_count']} · ⭐ {story['favorites_count']}"
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
    status = story["status"]

    if status == "open" and not story["is_demo"] and not own_story:
        b.button(
            text="💬 Дать совет",
            callback_data=f"advice:{story['id']}:{feed_index}:{cat_key}:{sort}:{back_to}",
        )

    if not story["is_demo"]:
        b.button(
            text="🗣 Обсудить" if status == "open" else "🗣 Обсуждение",
            callback_data=f"discuss:{story['id']}:latest:{feed_index}:{cat_key}:{sort}:{back_to}",
        )

    if story["comments_count"]:
        advice_index = max(0, int(story["comments_count"]) - 1) if own_story else 0
        advice_label = "Ответы" if own_story else "Советы"
        b.button(
            text=f"💬 {advice_label} {story['comments_count']}",
            callback_data=f"comments:{story['id']}:{advice_index}:{feed_index}:{cat_key}:{sort}:{back_to}",
        )

    if not story["is_demo"]:
        b.button(
            text="📝 Обновления",
            callback_data=f"updates:{story['id']}:{feed_index}:{back_to}:{cat_key}:{sort}",
        )

    if own_story and not story["is_demo"] and status in {"open", "closed"}:
        b.button(
            text="⚙️ Управление",
            callback_data=f"life:menu:{story['id']}:{feed_index}:{back_to}:{cat_key}:{sort}",
        )
    elif not own_story:
        b.button(
            text="✅ Сохранено" if favorite else "⭐ Сохранить",
            callback_data=f"fav:{story['id']}:{feed_index}:{back_to}:{cat_key}:{sort}",
        )

    b.button(
        text="🧠 Разбор",
        callback_data=f"ai:{story['id']}:{feed_index}:{cat_key}:{sort}:{back_to}",
    )

    if not story["is_demo"] and not own_story:
        b.button(
            text="•••",
            callback_data=f"more:s:{story['id']}:{feed_index}:{back_to}:{cat_key}:{sort}",
        )

    if back_to == "my":
        b.button(text="⬅️ Мои дела", callback_data=f"my:{feed_index}")
    elif back_to == "favorites":
        b.button(text="⬅️ Избранное", callback_data=f"favorites:{feed_index}")
    else:
        b.button(
            text="⬅️ В Зал суда",
            callback_data=f"feed:{feed_index}:{cat_key}:{sort}",
        )

    b.adjust(2, 2, 2, 1)
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
    viewer_id = message.chat.id
    story = await db.story(
        sid,
        view=count_view,
        viewer_tg_id=viewer_id if count_view else None,
    )
    if not story:
        await safe_edit(
            message,
            "Дело не найдено.",
            home_inline(viewer_id),
        )
        return

    own_story = int(story["author_tg_id"]) == int(viewer_id)

    if story["status"] == "deleted":
        b = InlineKeyboardBuilder()
        if own_story:
            b.button(text="⚖️ Мои дела", callback_data="my:0")
        else:
            b.button(text="🏛️ В Зал суда", callback_data="feed:0")
        await safe_edit(
            message,
            "🗑 <b>ДЕЛО УДАЛЕНО</b>\n\nЭто дело больше недоступно.",
            b.as_markup(),
        )
        return

    if story["status"] == "hidden":
        if own_story:
            b = InlineKeyboardBuilder()
            b.button(text="⬅️ Мои дела", callback_data=f"my:{feed_index}")
            await safe_edit(
                message,
                "🙈 <b>ДЕЛО СКРЫТО МОДЕРАЦИЕЙ</b>\n\n"
                "Оно не показывается другим пользователям и недоступно для управления.",
                b.as_markup(),
            )
        else:
            await safe_edit(
                message,
                "Это дело сейчас недоступно.",
                home_inline(viewer_id),
            )
        return

    favorite = await db.favorite_state(viewer_id, sid)

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


async def render_story_updates(message, sid, feed_index=0, back_to="feed", cat_key="all", sort="new"):
    story = await db.story(sid)
    if not story or story["status"] in {"hidden", "deleted"}:
        await safe_edit(message, "📝 Обновления дела недоступны.", home_inline(message.chat.id))
        return
    rows = await db.story_updates(sid, 5)
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ К делу", callback_data=f"caseback:{sid}:{feed_index}:{back_to}:{cat_key}:{sort}")
    if not rows:
        await safe_edit(message, "📝 <b>ОБНОВЛЕНИЯ АВТОРА</b>\n\nПока обновлений нет.", b.as_markup())
        return
    blocks = ["📝 <b>ОБНОВЛЕНИЯ АВТОРА</b>"]
    for n, row in enumerate(reversed(rows), 1):
        blocks.append(f"<b>Обновление {n}</b>\n{h(row['body'])}")
    await safe_edit(message, "\n\n".join(blocks), b.as_markup())


async def render_case_management(
    message,
    user_id,
    sid,
    feed_index=0,
    back_to="my",
    cat_key="all",
    sort="new",
):
    story = await db.story(sid)
    if (
        not story
        or story["is_demo"]
        or int(story["author_tg_id"]) != int(user_id)
    ):
        await safe_edit(message, "Управление этим делом недоступно.", home_inline(user_id))
        return

    status = story["status"]
    b = InlineKeyboardBuilder()

    if status in {"open", "closed"}:
        b.button(
            text="📝 Обновить ситуацию",
            callback_data=f"life:update:{sid}:{feed_index}:{back_to}:{cat_key}:{sort}",
        )

    if status == "open":
        b.button(
            text="✅ Завершить дело",
            callback_data=f"life:confirm:c:{sid}:{feed_index}:{back_to}:{cat_key}:{sort}",
        )
        b.button(
            text="🗑 Удалить дело",
            callback_data=f"life:confirm:d:{sid}:{feed_index}:{back_to}:{cat_key}:{sort}",
        )
        info = (
            "🟢 Сейчас дело открыто.\n"
            "Пользователи могут оставлять новые советы и писать в обсуждении."
        )
    elif status == "closed":
        b.button(
            text="🔓 Возобновить дело",
            callback_data=f"life:do:o:{sid}:{feed_index}:{back_to}:{cat_key}:{sort}",
        )
        b.button(
            text="🗑 Удалить дело",
            callback_data=f"life:confirm:d:{sid}:{feed_index}:{back_to}:{cat_key}:{sort}",
        )
        info = (
            "✅ Дело завершено.\n"
            "Старые советы и обсуждение доступны для чтения, "
            "но новые сообщения и советы закрыты."
        )
    elif status == "hidden":
        b.button(text="⬅️ Мои дела", callback_data=f"my:{feed_index}")
        await safe_edit(
            message,
            "🙈 <b>ДЕЛО СКРЫТО МОДЕРАЦИЕЙ</b>\n\n"
            "Изменить его статус самостоятельно нельзя.",
            b.as_markup(),
        )
        return
    else:
        b.button(text="⚖️ Мои дела", callback_data="my:0")
        await safe_edit(
            message,
            "🗑 <b>ДЕЛО УДАЛЕНО</b>\n\nОно больше недоступно.",
            b.as_markup(),
        )
        return

    b.button(
        text="⬅️ К делу",
        callback_data=f"caseback:{sid}:{feed_index}:{back_to}:{cat_key}:{sort}",
    )
    b.adjust(1)

    await safe_edit(
        message,
        f"⚙️ <b>УПРАВЛЕНИЕ ДЕЛОМ №{sid}</b>\n\n"
        f"{info}\n\n"
        "Завершение можно отменить позже. Удаление — окончательное.",
        b.as_markup(),
    )


async def render_lifecycle_confirmation(
    message,
    user_id,
    action,
    sid,
    feed_index=0,
    back_to="my",
    cat_key="all",
    sort="new",
):
    story = await db.story(sid)
    if (
        not story
        or story["is_demo"]
        or int(story["author_tg_id"]) != int(user_id)
    ):
        await safe_edit(message, "Управление этим делом недоступно.", home_inline(user_id))
        return

    b = InlineKeyboardBuilder()
    if action == "c":
        if story["status"] != "open":
            await render_case_management(
                message, user_id, sid, feed_index, back_to, cat_key, sort
            )
            return
        b.button(
            text="✅ Да, завершить",
            callback_data=f"life:do:c:{sid}:{feed_index}:{back_to}:{cat_key}:{sort}",
        )
        title = "✅ <b>ЗАВЕРШИТЬ ДЕЛО?</b>"
        body = (
            "Оно исчезнет из активного Зала суда.\n"
            "Новые советы и сообщения обсуждения будут закрыты.\n"
            "Старые ответы останутся доступными для чтения.\n\n"
            "Позже дело можно будет возобновить."
        )
    elif action == "d":
        if story["status"] not in {"open", "closed"}:
            await render_case_management(
                message, user_id, sid, feed_index, back_to, cat_key, sort
            )
            return
        b.button(
            text="🗑 Да, удалить",
            callback_data=f"life:do:d:{sid}:{feed_index}:{back_to}:{cat_key}:{sort}",
        )
        title = "🗑 <b>УДАЛИТЬ ДЕЛО?</b>"
        body = (
            "Дело исчезнет из твоего списка и станет недоступно другим.\n"
            "Опубликованные ранее советы, реакции и репутация пользователей сохранятся.\n\n"
            "<b>Отменить удаление нельзя.</b>"
        )
    else:
        await render_case_management(
            message, user_id, sid, feed_index, back_to, cat_key, sort
        )
        return

    b.button(
        text="⬅️ Не менять",
        callback_data=f"life:menu:{sid}:{feed_index}:{back_to}:{cat_key}:{sort}",
    )
    b.adjust(1)
    await safe_edit(message, f"{title}\n\n{body}", b.as_markup())


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
    back_to="feed",
    discussion_open=True,
):
    cat_key, sort, _ = feed_options(cat_key, sort)
    b = InlineKeyboardBuilder()
    b.button(
        text=f"👍 Полезно {comment['likes']}",
        callback_data=f"react:1:{comment['id']}:{sid}:{index}:{feed_index}:{cat_key}:{sort}:{back_to}",
    )
    b.button(
        text=f"👎 {comment['dislikes']}",
        callback_data=f"react:-1:{comment['id']}:{sid}:{index}:{feed_index}:{cat_key}:{sort}:{back_to}",
    )

    prev_i = (index - 1) % total
    next_i = (index + 1) % total
    nav_row(
        b,
        f"comments:{sid}:{prev_i}:{feed_index}:{cat_key}:{sort}:{back_to}",
        f"{index + 1}/{total}",
        f"comments:{sid}:{next_i}:{feed_index}:{cat_key}:{sort}:{back_to}",
    )

    b.button(
        text="🗣 Обсудить" if discussion_open else "🗣 Обсуждение",
        callback_data=f"discuss:{sid}:latest:{feed_index}:{cat_key}:{sort}:{back_to}",
    )
    b.button(
        text="•••",
        callback_data=f"more:c:{comment['id']}:{sid}:{index}:{feed_index}:{cat_key}:{sort}:{back_to}",
    )
    b.button(
        text="⬅️ К делу",
        callback_data=f"caseback:{sid}:{feed_index}:{back_to}:{cat_key}:{sort}",
    )
    b.adjust(2, 3, 2, 1)
    return b.as_markup()


async def render_comments(
    message,
    sid,
    index,
    feed_index,
    cat_key="all",
    sort="new",
    back_to="feed",
):
    story = await db.story(sid)
    if not story or story["status"] in {"hidden", "deleted"}:
        await safe_edit(
            message,
            "💬 <b>СОВЕТЫ</b>\n\nЭто дело сейчас недоступно.",
            home_inline(message.chat.id),
        )
        return

    total = await db.comment_count(sid)
    if int(story["author_tg_id"]) == int(message.chat.id):
        await db.mark_advice_seen(message.chat.id, sid, total)
        inbox = await db.advice_inbox(message.chat.id, sid)
        if inbox and inbox["message_id"]:
            try:
                b = InlineKeyboardBuilder()
                b.button(
                    text=f"💬 Читать ответы · {total}",
                    callback_data=f"comments:{sid}:{max(0,total-1)}:0:all:new:my",
                )
                await bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=int(inbox["message_id"]),
                    text=(
                        "💬 <b>ОТВЕТЫ К ТВОЕМУ ДЕЛУ</b>\n\n"
                        f"⚖️ {h(story['title'])}\n"
                        f"✅ Всё прочитано · Всего: <b>{total}</b>"
                    ),
                    parse_mode="HTML",
                    reply_markup=b.as_markup(),
                )
            except TelegramBadRequest:
                pass
    if total == 0:
        favorite = await db.favorite_state(message.chat.id, sid)
        own_story = int(story["author_tg_id"]) == int(message.chat.id)
        keyboard = case_keyboard(
            story,
            feed_index,
            back_to,
            cat_key,
            sort,
            favorite=favorite,
            own_story=own_story,
        )
        empty_text = (
            "Пока советов нет. Можно стать первым."
            if story["status"] == "open"
            else "Дело завершено. Новые советы больше не принимаются."
        )
        await safe_edit(
            message,
            "💬 <b>СОВЕТЫ</b>\n\n" + empty_text,
            keyboard,
        )
        return

    index = clamp(index, 0, total - 1)
    comment = await db.comment_item(sid, index)
    text = comment_text(comment, index, total)
    if story["status"] == "closed":
        text += "\n\n✅ <i>Дело завершено — новые советы закрыты.</i>"
    await safe_edit(
        message,
        text,
        comment_keyboard(
            comment,
            sid,
            index,
            total,
            feed_index,
            cat_key,
            sort,
            back_to,
            discussion_open=story["status"] == "open",
        ),
    )


DISCUSSION_PAGE_SIZE = 3
DISCUSSION_MARKS = ("①", "②", "③")


def discussion_window_text(rows, offset, total):
    blocks = ["🗣 <b>ОБСУЖДЕНИЕ ДЕЛА</b>"]
    for pos, item in enumerate(rows):
        mark = DISCUSSION_MARKS[pos]
        is_author = item["author_id"] == item["story_author_id"]
        author_label = "👑 Автор" if is_author else h(item["nickname"])
        staff = ""
        if item["staff_role"] and item["staff_role"] != "SYSTEM":
            staff = f" · 🛡️ {h(item['staff_role'])}"

        reply_block = ""
        if item["reply_to_id"] and item["reply_body"]:
            reply_author = (
                "👑 Автор"
                if item["reply_author_id"] == item["story_author_id"]
                else h(item["reply_nickname"] or "участнику")
            )
            quote = str(item["reply_body"]).strip().replace("\n", " ")
            if len(quote) > 110:
                quote = quote[:110].rstrip() + "…"
            reply_block = f"\n↩️ <i>{reply_author}: {h(quote)}</i>"

        body = str(item["body"]).strip()
        if len(body) > 650:
            body = body[:650].rstrip() + "…"

        blocks.append(
            f"\n{mark} <b>{author_label}</b>{staff}"
            f"{reply_block}\n{h(body)}\n"
            f"👍 {item['likes']}"
        )

    shown_from = offset + 1
    shown_to = offset + len(rows)
    blocks.append(f"\n📄 Сообщения {shown_from}–{shown_to} из {total}")
    return "\n".join(blocks)


def discussion_window_keyboard(
    rows,
    sid,
    offset,
    total,
    feed_index,
    cat_key="all",
    sort="new",
    back_to="feed",
    allow_write=True,
):
    cat_key, sort, _ = feed_options(cat_key, sort)
    b = InlineKeyboardBuilder()

    for pos, item in enumerate(rows):
        mark = DISCUSSION_MARKS[pos]
        if allow_write:
            b.button(
                text=f"↩️ {mark}",
                callback_data=f"dreply:{item['id']}:{sid}:{offset}:{feed_index}:{cat_key}:{sort}:{back_to}",
            )
        b.button(
            text=f"👍 {mark} {item['likes']}",
            callback_data=f"dlike:{item['id']}:{sid}:{offset}:{feed_index}:{cat_key}:{sort}:{back_to}",
        )
        b.button(
            text=f"••• {mark}",
            callback_data=f"more:d:{item['id']}:{sid}:{offset}:{feed_index}:{cat_key}:{sort}:{back_to}",
        )

    older_offset = max(0, offset - DISCUSSION_PAGE_SIZE)
    newer_offset = offset + DISCUSSION_PAGE_SIZE
    if offset > 0:
        b.button(
            text="⬆️ Раньше",
            callback_data=f"discuss:{sid}:{older_offset}:{feed_index}:{cat_key}:{sort}:{back_to}",
        )
    if newer_offset < total:
        b.button(
            text="⬇️ Новее",
            callback_data=f"discuss:{sid}:{newer_offset}:{feed_index}:{cat_key}:{sort}:{back_to}",
        )

    if allow_write:
        b.button(
            text="✍️ Написать",
            callback_data=f"dwrite:{sid}:{offset}:{feed_index}:{cat_key}:{sort}:{back_to}",
        )
    b.button(
        text="⬅️ К делу",
        callback_data=f"caseback:{sid}:{feed_index}:{back_to}:{cat_key}:{sort}",
    )

    rows_layout = [3 if allow_write else 2] * len(rows)
    if offset > 0 or newer_offset < total:
        rows_layout.append(2 if offset > 0 and newer_offset < total else 1)
    if allow_write:
        rows_layout.append(1)
    rows_layout.append(1)
    b.adjust(*rows_layout)
    return b.as_markup()


async def render_discussion(
    message,
    sid,
    offset=0,
    feed_index=0,
    cat_key="all",
    sort="new",
    back_to="feed",
):
    story = await db.story(sid)
    if not story or story["is_demo"] or story["status"] in {"hidden", "deleted"}:
        await safe_edit(
            message,
            "🗣 <b>ОБСУЖДЕНИЕ</b>\n\nДля этого дела обсуждение недоступно.",
            home_inline(message.chat.id),
        )
        return

    allow_write = story["status"] == "open"
    total = await db.discussion_count(sid)
    if total == 0:
        b = InlineKeyboardBuilder()
        if allow_write:
            b.button(
                text="✍️ Начать обсуждение",
                callback_data=f"dwrite:{sid}:0:{feed_index}:{cat_key}:{sort}:{back_to}",
            )
        b.button(
            text="⬅️ К делу",
            callback_data=f"caseback:{sid}:{feed_index}:{back_to}:{cat_key}:{sort}",
        )
        b.adjust(1)
        empty_text = (
            "Пока здесь тихо. Задай вопрос автору или начни обсуждение ситуации."
            if allow_write
            else "Дело завершено. Новые сообщения закрыты, а обсуждение пока пустое."
        )
        await safe_edit(
            message,
            "🗣 <b>ОБСУЖДЕНИЕ ДЕЛА</b>\n\n" + empty_text,
            b.as_markup(),
        )
        return

    max_offset = max(0, total - DISCUSSION_PAGE_SIZE)
    offset = clamp(offset, 0, max_offset)
    rows = await db.discussion_page(sid, offset, DISCUSSION_PAGE_SIZE)
    if not rows:
        offset = max_offset
        rows = await db.discussion_page(sid, offset, DISCUSSION_PAGE_SIZE)

    text = discussion_window_text(rows, offset, total)
    if not allow_write:
        text += "\n\n✅ <i>Дело завершено — обсуждение доступно только для чтения.</i>"

    await safe_edit(
        message,
        text,
        discussion_window_keyboard(
            rows,
            sid,
            offset,
            total,
            feed_index,
            cat_key,
            sort,
            back_to,
            allow_write=allow_write,
        ),
    )


async def render_profile(message, user_id, edit=True):
    u = await db.get_user(user_id)
    stats = await db.profile_stats(user_id)

    role = ""
    if u["staff_role"] and u["staff_role"] != "SYSTEM":
        role = f"🛡️ <b>{h(u['staff_role'])}</b>\n"

    notifications_on = bool(u["notifications_enabled"])
    notif_text = "🔔 Включены" if notifications_on else "🔕 Выключены"

    next_thresholds = {1: 10, 2: 30, 3: 75, 4: 150}
    next_threshold = next_thresholds.get(int(u["level"]))
    if next_threshold:
        progress_line = f"📈 До следующего звания: <b>{u['reputation']} / {next_threshold}</b>"
    else:
        progress_line = "👑 <b>Максимальное судебное звание</b>"

    b = InlineKeyboardBuilder()
    b.button(text="✏️ Профиль", callback_data="edit")
    b.button(
        text="🔕 Выключить" if notifications_on else "🔔 Включить",
        callback_data="notifications:toggle",
    )
    b.button(text="🎖️ Звания", callback_data="ranks")
    b.button(text="ℹ️ Справка", callback_data="help")
    b.adjust(2, 2)

    await present(
        message,
        f"👤 <b>{h(u['nickname'])}</b>\n"
        f"{role}"
        f"🎖️ {h(u['title'])} · ⭐ <b>{u['reputation']}</b>\n"
        f"{progress_line}\n\n"
        f"⚖️ Дел: <b>{stats['stories_count'] if stats else 0}</b> · "
        f"💬 Советов: <b>{stats['advice_count'] if stats else 0}</b> · "
        f"👍 Полезных: <b>{stats['helpful_likes'] if stats else 0}</b>\n"
        f"{notif_text}\n\n"
        f"📝 {h(u['bio'] or 'Описание пока не добавлено.')}",
        b.as_markup(),
        edit=edit,
    )


async def render_my_cases(message, user_id, index=0, edit=True):
    total = await db.user_story_count(user_id)
    if total == 0:
        b = InlineKeyboardBuilder()
        b.button(text="📝 Подать первое дело", callback_data="new")
        await present(
            message,
            "⚖️ <b>МОИ ДЕЛА</b>\n\nТы ещё ничего не публиковал.",
            b.as_markup(),
            edit=edit,
        )
        return

    index = clamp(index, 0, total - 1)
    story = await db.user_story_item(user_id, index)
    text = (
        f"⚖️ <b>МОИ ДЕЛА</b>\n\n"
        f"<b>Дело №{story['id']}</b> · {h(story['category'])}\n"
        f"{story_status_label(story['status'])}\n\n"
        f"<b>{h(story['title'])}</b>\n\n"
        f"👁 {story['views']} · 💬 {story['comments_count']} · "
        f"🗣 {story['discussion_count']} · ⭐ {story['favorites_count']}\n"
        f"📄 {index + 1} из {total}"
    )
    b = InlineKeyboardBuilder()
    b.button(text="📖 Открыть дело", callback_data=f"mycase:{story['id']}:{index}")
    prev_i = (index - 1) % total
    next_i = (index + 1) % total
    nav_row(b, f"my:{prev_i}", f"{index + 1}/{total}", f"my:{next_i}")
    b.adjust(1, 3)
    await present(message, text, b.as_markup(), edit=edit)


async def render_favorites(message, user_id, index=0, edit=True):
    total = await db.favorite_count(user_id)
    if total == 0:
        b = InlineKeyboardBuilder()
        b.button(text="🏛️ Найти дела", callback_data="feed:0")
        await present(
            message,
            "⭐ <b>ИЗБРАННОЕ</b>\n\n"
            "Сохраняй интересные дела — сюда попадут их карточки и важные обновления.",
            b.as_markup(),
            edit=edit,
        )
        return

    index = clamp(index, 0, total - 1)
    story = await db.favorite_item(user_id, index)
    if not story:
        await present(
            message,
            "⭐ <b>ИЗБРАННОЕ</b>\n\nСписок изменился. Открой его ещё раз.",
            home_inline(user_id),
            edit=edit,
        )
        return

    text = (
        "⭐ <b>ИЗБРАННОЕ</b>\n\n"
        f"⚖️ <b>Дело №{story['id']}</b> · {h(story['category'])}\n"
        f"{story_status_label(story['status'])}\n\n"
        f"<b>{h(story['title'])}</b>\n\n"
        f"👁 {story['views']} · 💬 {story['comments_count']} · "
        f"🗣 {story['discussion_count']} · ⭐ {story['favorites_count']}\n"
        f"📄 {index + 1} из {total}"
    )

    b = InlineKeyboardBuilder()
    b.button(text="📖 Открыть дело", callback_data=f"favcase:{story['id']}:{index}")
    prev_i = (index - 1) % total
    next_i = (index + 1) % total
    nav_row(
        b,
        f"favorites:{prev_i}",
        f"{index + 1}/{total}",
        f"favorites:{next_i}",
    )
    b.adjust(1, 3)
    await present(message, text, b.as_markup(), edit=edit)


async def render_rating(message, page=0, edit=True):
    total = await db.leaderboard_count()
    if total == 0:
        await present(
            message,
            "🏆 <b>РЕЙТИНГ</b>\n\nПока здесь пусто.",
            None,
            edit=edit,
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
            f"   💬 {x['advice_count']} · 👍 {x['helpful_likes']}"
        )

    b = InlineKeyboardBuilder()
    if pages > 1:
        prev_p = (page - 1) % pages
        next_p = (page + 1) % pages
        nav_row(b, f"rating:{prev_p}", f"{page + 1}/{pages}", f"rating:{next_p}")
        b.adjust(3)

    await present(
        message,
        "🏆 <b>РЕЙТИНГ ЗАЛА</b>\n\n" + "\n".join(lines),
        b.as_markup(),
        edit=edit,
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


async def render_admin_home(message, edit=True):
    reports = await db.admin_report_count()
    await present(
        message,
        "🛡️ <b>CEO ANON VERDICT</b>\n\n"
        "Статистика, пользователи, роли и модерация.\n\n"
        f"🚩 Открытых жалоб: <b>{reports}</b>",
        admin_menu(),
        edit=edit,
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
        icon = {
            "story": "⚖️",
            "comment": "💬",
            "discussion": "🗣",
        }.get(report["target_type"], "🚩")
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
    elif target_type == "comment":
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
    else:
        target = await db.admin_discussion_message(target_id)
        if target:
            preview = (
                f"🗣 <b>Сообщение обсуждения №{target_id}</b>\n"
                f"Статус: <b>{h(target['status'])}</b>\n"
                f"Автор: {h(target['author_nickname'])} · "
                f"<code>{target['author_tg_id']}</code>\n"
                f"К делу: {h(target['story_title'])}\n\n"
                f"{h(target['body'][:1200])}"
            )
        else:
            preview = f"🗣 Сообщение №{target_id} не найдено."

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
        icon = {
            "open": "🟢",
            "closed": "✅",
            "deleted": "🗑",
            "hidden": "🙈",
        }.get(s["status"], "⚪")
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
        "⚖️ <b>ДЕЛА</b>\n\n🟢 открыто · ✅ завершено · 🗑 удалено · 🙈 скрыто",
        b.as_markup(),
    )


async def render_admin_story(message, sid, page=0):
    s = await db.admin_story(sid)
    if not s:
        await safe_edit(message, "Дело не найдено.", admin_menu())
        return

    status = {
        "open": "🟢 открыто",
        "closed": "✅ завершено",
        "deleted": "🗑 удалено",
        "hidden": "🙈 скрыто",
    }.get(s["status"], h(s["status"]))
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
    if s["status"] in {"open", "closed"}:
        b.button(text="🙈 Скрыть дело", callback_data=f"admin:status:{sid}:hidden:{page}")
    elif s["status"] == "hidden":
        b.button(
            text="↩️ Вернуть предыдущий статус",
            callback_data=f"admin:status:{sid}:open:{page}",
        )
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
    if (m.text or "") in PRIMARY_NAV_TEXTS:
        await state.clear()
        await route_primary_navigation(m)
        return
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
    if (m.text or "") in PRIMARY_NAV_TEXTS:
        await state.clear()
        await route_primary_navigation(m)
        return
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
async def feed_callback(c: CallbackQuery, state: FSMContext):
    await state.clear()
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
async def feed_category_callback(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await ensure_callback_user(c)
    parts = c.data.split(":")
    cat_key = parts[1] if len(parts) > 1 else "all"
    sort = parts[2] if len(parts) > 2 else "new"
    await render_feed_categories(c.message, cat_key, sort)
    await c.answer()


@dp.callback_query(F.data.startswith("feedsort:"))
async def feed_sort_callback(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await ensure_callback_user(c)
    parts = c.data.split(":")
    cat_key = parts[1] if len(parts) > 1 else "all"
    sort = parts[2] if len(parts) > 2 else "new"
    await render_feed_sorts(c.message, cat_key, sort)
    await c.answer()


@dp.callback_query(F.data.startswith("random:"))
async def random_case_callback(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await ensure_callback_user(c)
    parts = c.data.split(":")
    try:
        current_index = int(parts[1])
    except (ValueError, IndexError):
        current_index = 0

    cat_key = parts[2] if len(parts) > 2 else "all"
    sort = parts[3] if len(parts) > 3 else "new"
    cat_key, sort, category = feed_options(cat_key, sort)

    total = await db.feed_count(category=category, sort=sort)
    if total == 0:
        await c.answer("По этим фильтрам дел пока нет.", show_alert=True)
        return

    if total == 1:
        random_index = 0
    else:
        current_index = clamp(current_index, 0, total - 1)
        random_index = secrets.randbelow(total - 1)
        if random_index >= current_index:
            random_index += 1

    await render_feed(c.message, random_index, cat_key, sort)
    await c.answer("🎲 Случайное дело")


@dp.callback_query(F.data.startswith("case:"))
async def case_callback(c: CallbackQuery, state: FSMContext):
    await state.clear()
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


@dp.callback_query(F.data.startswith("caseback:"))
async def case_back_callback(c: CallbackQuery, state: FSMContext):
    await state.clear()
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

    await render_case(
        c.message,
        sid,
        index,
        back_to=back_to,
        count_view=False,
        cat_key=cat_key,
        sort=sort,
    )
    await c.answer()


@dp.callback_query(F.data.startswith("mycase:"))
async def mycase_callback(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await ensure_callback_user(c)
    try:
        _, sid, index = c.data.split(":")
        sid, index = int(sid), int(index)
    except (ValueError, IndexError):
        await c.answer("Некорректное дело", show_alert=True)
        return
    await render_case(c.message, sid, index, "my", count_view=False)
    await c.answer()


@dp.callback_query(F.data.startswith("updates:"))
async def updates_callback(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await ensure_callback_user(c)
    parts = c.data.split(":")
    try:
        sid = int(parts[1]); feed_index = int(parts[2])
    except (ValueError, IndexError):
        await c.answer("Некорректное дело", show_alert=True); return
    back_to = parts[3] if len(parts)>3 else "feed"
    cat_key = parts[4] if len(parts)>4 else "all"
    sort = parts[5] if len(parts)>5 else "new"
    await render_story_updates(c.message, sid, feed_index, back_to, cat_key, sort)
    await c.answer()


@dp.callback_query(F.data.startswith("life:update:"))
async def lifecycle_update_callback(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await ensure_callback_user(c)
    parts=c.data.split(":")
    try:
        sid=int(parts[2]); feed_index=int(parts[3])
    except (ValueError,IndexError):
        await c.answer("Некорректное дело",show_alert=True); return
    back_to=parts[4] if len(parts)>4 else "my"
    cat_key=parts[5] if len(parts)>5 else "all"
    sort=parts[6] if len(parts)>6 else "new"
    story=await db.story(sid)
    if not story or story["is_demo"] or int(story["author_tg_id"])!=int(c.from_user.id) or story["status"] not in {"open","closed"}:
        await c.answer("Обновление недоступно.",show_alert=True); return
    payload = {
        "sid": sid,
        "feed_index": feed_index,
        "back_to": back_to,
        "cat_key": cat_key,
        "sort": sort,
    }
    await db.set_pending_input(c.from_user.id, "story_update", payload)
    await state.set_state(StoryUpdate.body)
    await state.update_data(**payload)
    cancel=InlineKeyboardBuilder()
    cancel.button(text="❌ Отменить",callback_data=f"life:menu:{sid}:{feed_index}:{back_to}:{cat_key}:{sort}")
    await c.message.answer(
        "📝 <b>ОБНОВИТЬ СИТУАЦИЮ</b>\n\nРасскажи, что изменилось после публикации дела. Старый текст останется на месте — обновление добавится в историю.",
        parse_mode="HTML",reply_markup=cancel.as_markup()
    )
    await c.answer()


@dp.message(StoryUpdate.body)
async def story_update_message(m: Message, state: FSMContext):
    await ensure_message_user(m)
    if (m.text or "") in PRIMARY_NAV_TEXTS:
        await state.clear(); await route_primary_navigation(m); return
    body,reasons=moderate(m.text or ""); body=body.strip()
    if not body:
        await m.answer("Напиши обновление текстом."); return
    if reasons:
        await m.answer("Удали персональные данные или угрозы."); return
    d = await state.get_data()
    if not d.get("sid"):
        pending = await db.pending_input(m.from_user.id)
        if pending and pending.get("action") == "story_update":
            d = pending.get("payload") or {}
    sid = d.get("sid")
    if not sid:
        await state.clear()
        await db.clear_pending_input(m.from_user.id)
        await m.answer("Не удалось определить, какое дело обновлять. Открой «⚙️ Управление → 📝 Обновить ситуацию» ещё раз.")
        return

    result = await db.add_story_update(m.from_user.id, sid, body)
    await state.clear()
    await db.clear_pending_input(m.from_user.id)
    if result.get("status")!="created":
        await m.answer("Не удалось добавить обновление. Дело недоступно."); return

    story=await db.story(sid)
    followers=await db.favorite_subscribers(sid,exclude_tg_ids=[m.from_user.id])
    if followers:
        b=InlineKeyboardBuilder()
        b.button(text="📝 Читать обновление",callback_data=f"updates:{d['sid']}:0:feed:all:new")
        asyncio.create_task(notify_many(
            followers,
            "📝 <b>Автор обновил сохранённое дело</b>\n\n"
            f"⚖️ {h(story['title'])}\n"
            f"{h(body[:500])}{'…' if len(body)>500 else ''}",
            b.as_markup(),
        ))
    b=InlineKeyboardBuilder()
    b.button(text="📖 К делу",callback_data=f"caseback:{d['sid']}:{d.get('feed_index',0)}:{d.get('back_to','my')}:{d.get('cat_key','all')}:{d.get('sort','new')}")
    await m.answer("✅ <b>Обновление добавлено.</b>\n\nСтарый текст дела сохранён.",parse_mode="HTML",reply_markup=b.as_markup())


@dp.callback_query(F.data.startswith("life:menu:"))
async def lifecycle_menu_callback(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await ensure_callback_user(c)
    parts = c.data.split(":")
    try:
        sid = int(parts[2])
        feed_index = int(parts[3])
    except (ValueError, IndexError):
        await c.answer("Некорректное дело", show_alert=True)
        return

    back_to = parts[4] if len(parts) > 4 else "my"
    cat_key = parts[5] if len(parts) > 5 else "all"
    sort = parts[6] if len(parts) > 6 else "new"

    await render_case_management(
        c.message,
        c.from_user.id,
        sid,
        feed_index,
        back_to,
        cat_key,
        sort,
    )
    await c.answer()


@dp.callback_query(F.data.startswith("life:confirm:"))
async def lifecycle_confirm_callback(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await ensure_callback_user(c)
    parts = c.data.split(":")
    try:
        action = parts[2]
        sid = int(parts[3])
        feed_index = int(parts[4])
    except (ValueError, IndexError):
        await c.answer("Некорректное действие", show_alert=True)
        return

    back_to = parts[5] if len(parts) > 5 else "my"
    cat_key = parts[6] if len(parts) > 6 else "all"
    sort = parts[7] if len(parts) > 7 else "new"

    await render_lifecycle_confirmation(
        c.message,
        c.from_user.id,
        action,
        sid,
        feed_index,
        back_to,
        cat_key,
        sort,
    )
    await c.answer()


@dp.callback_query(F.data.startswith("life:do:"))
async def lifecycle_action_callback(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await ensure_callback_user(c)
    parts = c.data.split(":")
    try:
        action = parts[2]
        sid = int(parts[3])
        feed_index = int(parts[4])
    except (ValueError, IndexError):
        await c.answer("Некорректное действие", show_alert=True)
        return

    back_to = parts[5] if len(parts) > 5 else "my"
    cat_key = parts[6] if len(parts) > 6 else "all"
    sort = parts[7] if len(parts) > 7 else "new"

    targets = {"c": "closed", "o": "open", "d": "deleted"}
    target = targets.get(action)
    if not target:
        await c.answer("Некорректное действие", show_alert=True)
        return

    result = await db.change_own_story_status(c.from_user.id, sid, target)
    status = result.get("status")

    if status == "moderated":
        await c.answer("Дело скрыто модерацией — изменить его статус нельзя.", show_alert=True)
        return
    if status in {"not_found", "deleted"}:
        await c.answer("Дело недоступно.", show_alert=True)
        return
    if status not in {"updated", "unchanged"}:
        await c.answer("Не удалось изменить статус дела.", show_alert=True)
        return

    if target == "deleted":
        b = InlineKeyboardBuilder()
        b.button(text="⚖️ Мои дела", callback_data="my:0")
        await safe_edit(
            c.message,
            f"🗑 <b>ДЕЛО №{sid} УДАЛЕНО</b>\n\n"
            "Оно исчезло из твоего списка и больше недоступно другим пользователям.",
            b.as_markup(),
        )
        await c.answer("Дело удалено")
        return

    await render_case(
        c.message,
        sid,
        feed_index,
        back_to=back_to,
        count_view=False,
        cat_key=cat_key,
        sort=sort,
    )
    await c.answer("✅ Дело завершено" if target == "closed" else "🔓 Дело снова открыто")


@dp.callback_query(F.data.startswith("cancelinput:"))
async def cancel_input_callback(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await db.clear_pending_input(c.from_user.id)
    parts = c.data.split(":")
    target = parts[1] if len(parts) > 1 else "feed"

    if target == "case" and len(parts) >= 7:
        try:
            sid = int(parts[2])
            feed_index = int(parts[3])
        except ValueError:
            await c.answer("Ввод отменён")
            return
        back_to = parts[4]
        cat_key = parts[5]
        sort = parts[6]
        await render_case(
            c.message,
            sid,
            feed_index,
            back_to=back_to,
            count_view=False,
            cat_key=cat_key,
            sort=sort,
        )
    elif target == "discuss" and len(parts) >= 8:
        try:
            sid = int(parts[2])
            offset = int(parts[3])
            feed_index = int(parts[4])
        except ValueError:
            await c.answer("Ввод отменён")
            return
        cat_key = parts[5]
        sort = parts[6]
        back_to = parts[7]
        await render_discussion(
            c.message,
            sid,
            offset,
            feed_index,
            cat_key,
            sort,
            back_to,
        )
    else:
        await render_feed(c.message, 0)

    await c.answer("Ввод отменён")


@dp.callback_query(F.data.startswith("discuss:"))
async def discussion_callback(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await ensure_callback_user(c)
    parts = c.data.split(":")
    try:
        sid = int(parts[1])
        raw_offset = parts[2]
        feed_index = int(parts[3])
    except (ValueError, IndexError):
        await c.answer("Некорректное обсуждение", show_alert=True)
        return

    cat_key = parts[4] if len(parts) > 4 else "all"
    sort = parts[5] if len(parts) > 5 else "new"
    back_to = parts[6] if len(parts) > 6 else "feed"

    if raw_offset == "latest":
        total = await db.discussion_count(sid)
        offset = max(0, total - DISCUSSION_PAGE_SIZE)
    else:
        try:
            offset = int(raw_offset)
        except ValueError:
            offset = 0

    await render_discussion(
        c.message,
        sid,
        offset,
        feed_index,
        cat_key,
        sort,
        back_to,
    )
    await c.answer()


@dp.callback_query(F.data.startswith("dwrite:"))
async def discussion_write_callback(c: CallbackQuery, state: FSMContext):
    await ensure_callback_user(c)
    parts = c.data.split(":")
    try:
        sid = int(parts[1])
        offset = int(parts[2])
        feed_index = int(parts[3])
    except (ValueError, IndexError):
        await c.answer("Некорректное обсуждение", show_alert=True)
        return

    cat_key = parts[4] if len(parts) > 4 else "all"
    sort = parts[5] if len(parts) > 5 else "new"
    back_to = parts[6] if len(parts) > 6 else "feed"

    if not is_owner(c.from_user.id):
        remaining = await db.discussion_cooldown_remaining(c.from_user.id)
        if remaining:
            await c.answer(
                f"Подожди ещё {remaining} сек. перед следующим сообщением.",
                show_alert=True,
            )
            return

    story = await db.story(sid)
    if not story or story["is_demo"] or story["status"] != "open":
        await c.answer("Обсуждение этого дела недоступно.", show_alert=True)
        return

    payload = {
        "discussion_sid": sid,
        "discussion_offset": offset,
        "discussion_feed_index": feed_index,
        "discussion_cat_key": cat_key,
        "discussion_sort": sort,
        "discussion_back_to": back_to,
        "discussion_reply_to": None,
    }
    await db.set_pending_input(c.from_user.id, "discussion", payload)
    await state.update_data(**payload)
    await state.set_state(Discussion.body)
    cancel = InlineKeyboardBuilder()
    cancel.button(
        text="❌ Отменить",
        callback_data=f"cancelinput:discuss:{sid}:{offset}:{feed_index}:{cat_key}:{sort}:{back_to}",
    )
    await c.message.answer(
        "🗣 <b>Новое сообщение</b>\n\n"
        "Напиши вопрос, уточнение или мнение.",
        parse_mode="HTML",
        reply_markup=cancel.as_markup(),
    )
    await c.answer()


@dp.callback_query(F.data.startswith("dreply:"))
async def discussion_reply_callback(c: CallbackQuery, state: FSMContext):
    await ensure_callback_user(c)
    parts = c.data.split(":")
    try:
        message_id = int(parts[1])
        sid = int(parts[2])
        offset = int(parts[3])
        feed_index = int(parts[4])
    except (ValueError, IndexError):
        await c.answer("Некорректный ответ", show_alert=True)
        return

    cat_key = parts[5] if len(parts) > 5 else "all"
    sort = parts[6] if len(parts) > 6 else "new"
    back_to = parts[7] if len(parts) > 7 else "feed"

    if not is_owner(c.from_user.id):
        remaining = await db.discussion_cooldown_remaining(c.from_user.id)
        if remaining:
            await c.answer(
                f"Подожди ещё {remaining} сек. перед следующим сообщением.",
                show_alert=True,
            )
            return

    story = await db.story(sid)
    if not story or story["status"] != "open":
        await c.answer("Дело завершено. Новые ответы в обсуждении закрыты.", show_alert=True)
        return

    parent = await db.discussion_message(message_id)
    if not parent or int(parent["story_id"]) != sid or parent["status"] != "open":
        await c.answer("Сообщение уже недоступно.", show_alert=True)
        return

    quote = str(parent["body"]).strip().replace("\n", " ")
    if len(quote) > 180:
        quote = quote[:180].rstrip() + "…"

    payload = {
        "discussion_sid": sid,
        "discussion_offset": offset,
        "discussion_feed_index": feed_index,
        "discussion_cat_key": cat_key,
        "discussion_sort": sort,
        "discussion_back_to": back_to,
        "discussion_reply_to": message_id,
    }
    await db.set_pending_input(c.from_user.id, "discussion", payload)
    await state.update_data(**payload)
    await state.set_state(Discussion.body)
    cancel = InlineKeyboardBuilder()
    cancel.button(
        text="❌ Отменить",
        callback_data=f"cancelinput:discuss:{sid}:{offset}:{feed_index}:{cat_key}:{sort}:{back_to}",
    )
    await c.message.answer(
        "↩️ <b>Ответ на сообщение</b>\n"
        f"<i>{h(quote)}</i>\n\n"
        "Напиши ответ:",
        parse_mode="HTML",
        reply_markup=cancel.as_markup(),
    )
    await c.answer()


@dp.callback_query(F.data.startswith("dlike:"))
async def discussion_like_callback(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await ensure_callback_user(c)
    parts = c.data.split(":")
    try:
        message_id = int(parts[1])
        sid = int(parts[2])
        offset = int(parts[3])
        feed_index = int(parts[4])
    except (ValueError, IndexError):
        await c.answer("Некорректная реакция", show_alert=True)
        return

    cat_key = parts[5] if len(parts) > 5 else "all"
    sort = parts[6] if len(parts) > 6 else "new"
    back_to = parts[7] if len(parts) > 7 else "feed"

    story = await db.story(sid)
    if not story or story["status"] in {"hidden", "deleted"}:
        await c.answer("Это дело больше недоступно.", show_alert=True)
        return

    result = await db.toggle_discussion_like(c.from_user.id, message_id)
    if result["status"] == "self":
        await c.answer("Своё сообщение оценивать нельзя.", show_alert=True)
        return
    if result["status"] != "updated":
        await c.answer("Сообщение не найдено.", show_alert=True)
        return

    await render_discussion(
        c.message,
        sid,
        offset,
        feed_index,
        cat_key,
        sort,
        back_to,
    )
    await c.answer("👍 Отметка добавлена" if result["liked"] else "👍 Отметка снята")


@dp.message(Discussion.body)
async def discussion_message_submit(m: Message, state: FSMContext):
    await ensure_message_user(m)
    if (m.text or "") in PRIMARY_NAV_TEXTS:
        await state.clear()
        await route_primary_navigation(m)
        return
    text, reasons = moderate(m.text or "")
    text = text.strip()

    if not text:
        await m.answer("Напиши сообщение текстом.")
        return
    if reasons:
        await m.answer("Удали персональные данные или угрозы.")
        return

    if not is_owner(m.from_user.id):
        remaining = await db.discussion_cooldown_remaining(m.from_user.id)
        if remaining:
            await m.answer(f"⏳ Подожди ещё {remaining} сек.")
            return

    data = await state.get_data()
    sid = data.get("discussion_sid")
    if not sid:
        await state.clear()
        await m.answer("Обсуждение уже недоступно.")
        return

    reply_to = data.get("discussion_reply_to")
    result = await db.add_discussion_message(
        m.from_user.id,
        sid,
        text,
        reply_to_id=reply_to,
    )

    if result.get("status") != "created":
        await state.clear()
        await m.answer("Не удалось опубликовать сообщение. Возможно, дело уже закрыто.")
        return

    await state.clear()

    feed_index = data.get("discussion_feed_index", 0)
    cat_key = data.get("discussion_cat_key", "all")
    sort = data.get("discussion_sort", "new")
    back_to = data.get("discussion_back_to", "feed")

    reply_tg_id = result.get("reply_tg_id")
    if (
        reply_to
        and result.get("reply_notifications")
        and reply_tg_id
        and int(reply_tg_id) != int(m.from_user.id)
    ):
        rb = InlineKeyboardBuilder()
        rb.button(
            text="🗣 Открыть обсуждение",
            callback_data=f"discuss:{sid}:latest:0:all:new:feed",
        )
        await safe_notify(
            reply_tg_id,
            "↩️ <b>Тебе ответили в обсуждении</b>\n\n"
            f"⚖️ {h(result.get('story_title'))}\n"
            f"{'👑 Автор' if result.get('is_story_author') else h(result.get('author_nickname'))}: "
            f"{h(text[:240])}",
            rb.as_markup(),
        )

    if result.get("is_story_author"):
        followers = await db.favorite_subscribers(
            sid,
            exclude_tg_ids=[m.from_user.id, reply_tg_id],
        )
        if followers:
            rb = InlineKeyboardBuilder()
            rb.button(
                text="🗣 Открыть обсуждение",
                callback_data=f"discuss:{sid}:latest:0:all:new:feed",
            )
            asyncio.create_task(
                notify_many(
                    followers,
                    "👑 <b>Автор написал в обсуждении сохранённого дела</b>\n\n"
                    f"⚖️ {h(result.get('story_title'))}\n"
                    f"{h(text[:240])}",
                    rb.as_markup(),
                )
            )

    b = InlineKeyboardBuilder()
    b.button(
        text="🗣 К обсуждению",
        callback_data=f"discuss:{sid}:latest:{feed_index}:{cat_key}:{sort}:{back_to}",
    )
    b.button(
        text="⬅️ К делу",
        callback_data=f"caseback:{sid}:{feed_index}:{back_to}:{cat_key}:{sort}",
    )
    b.adjust(1)

    await m.answer(
        "✅ Сообщение опубликовано.\n"
        "<i>Обсуждение не начисляет репутацию.</i>",
        parse_mode="HTML",
        reply_markup=b.as_markup(),
    )


@dp.callback_query(F.data.startswith("comments:"))
async def comments_callback(c: CallbackQuery, state: FSMContext):
    await state.clear()
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
    back_to = parts[6] if len(parts) > 6 else "feed"

    await render_comments(
        c.message,
        sid,
        index,
        feed_index,
        cat_key,
        sort,
        back_to,
    )
    await c.answer()


@dp.callback_query(F.data.startswith("react:"))
async def react_callback(c: CallbackQuery, state: FSMContext):
    await state.clear()
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
    back_to = parts[8] if len(parts) > 8 else "feed"

    story = await db.story(sid)
    if not story or story["status"] in {"hidden", "deleted"}:
        await c.answer("Это дело больше недоступно.", show_alert=True)
        return

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
        back_to,
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
                callback_data=f"comments:{sid}:0:0:all:new:feed",
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
    back_to = parts[5] if len(parts) > 5 else "feed"

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
        await c.answer("К демонстрационным делам советы не добавляются.", show_alert=True)
        return
    if story["status"] == "closed":
        await c.answer("Дело завершено. Новые советы больше не принимаются.", show_alert=True)
        return
    if story["status"] != "open":
        await c.answer("Это дело сейчас недоступно.", show_alert=True)
        return
    if int(story["author_tg_id"]) == int(c.from_user.id):
        await c.answer("Нельзя давать совет собственному делу.", show_alert=True)
        return

    payload = {
        "sid": sid,
        "feed_index": feed_index,
        "cat_key": cat_key,
        "sort": sort,
        "back_to": back_to,
    }
    await db.set_pending_input(c.from_user.id, "comment", payload)
    await state.update_data(**payload)
    await state.set_state(Comment.body)
    cancel = InlineKeyboardBuilder()
    cancel.button(
        text="❌ Отменить",
        callback_data=f"cancelinput:case:{sid}:{feed_index}:{back_to}:{cat_key}:{sort}",
    )
    await c.message.answer(
        "💬 <b>Твой совет</b>\n\n"
        "Напиши конкретно, что бы ты сделал на месте автора и почему.",
        parse_mode="HTML",
        reply_markup=cancel.as_markup(),
    )
    await c.answer()


@dp.message(Comment.body)
async def comment(m: Message, state: FSMContext):
    await ensure_message_user(m)
    if (m.text or "") in PRIMARY_NAV_TEXTS:
        await state.clear()
        await route_primary_navigation(m)
        return
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
                f"⏳ Подожди ещё {remaining} сек. перед следующим советом."
            )
            return

    d = await state.get_data()
    result = await db.comment(m.from_user.id, sid, text)
    await state.clear()

    if result.get("status") == "story_closed":
        b = InlineKeyboardBuilder()
        b.button(text="📖 К делу", callback_data=f"caseback:{d['sid']}:0:feed:all:new")
        await m.answer(
            "✅ Дело уже завершено автором. Этот совет не был опубликован.",
            reply_markup=b.as_markup(),
        )
        return
    if result.get("status") != "created":
        await m.answer("Дело стало недоступно. Совет не был опубликован.")
        return

    u = await db.get_user(m.from_user.id)

    cat_key = d.get("cat_key", "all")
    sort = d.get("sort", "new")
    feed_index = d.get("feed_index", 0)
    back_to = d.get("back_to", "feed")

    open_case = InlineKeyboardBuilder()
    open_case.button(
        text="📖 Открыть дело",
        callback_data=f"case:{d['sid']}:0:all:new",
    )

    advice_total = await db.comment_count(sid)
    latest_advice_index = max(0, advice_total - 1)
    open_answers = InlineKeyboardBuilder()
    open_answers.button(
        text=f"💬 Читать ответы · {advice_total}",
        callback_data=f"comments:{d['sid']}:{latest_advice_index}:0:all:new:my",
    )

    owner_tg_id = result.get("story_owner_tg_id")
    if (
        result.get("story_owner_notifications")
        and owner_tg_id
        and int(owner_tg_id) != int(m.from_user.id)
    ):
        await upsert_advice_notification(
            owner_tg_id,
            sid,
            result.get("story_title"),
            result.get("commenter_nickname"),
            advice_total,
        )

    followers = await db.favorite_subscribers(
        sid,
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
        text="💬 Открыть советы",
        callback_data=f"comments:{d['sid']}:0:{feed_index}:{cat_key}:{sort}:{back_to}",
    )
    b.button(
        text="⬅️ К делу",
        callback_data=f"caseback:{d['sid']}:{feed_index}:{back_to}:{cat_key}:{sort}",
    )
    b.adjust(1)

    reward = int(result.get("reputation_reward", 0))
    reward_line = (
        f"<b>+{reward} репутации.</b>"
        if reward
        else "За повторный совет к этому делу репутация не начисляется."
    )
    await m.answer(
        "✅ <b>Совет опубликован.</b> " + reward_line + "\n\n"
        f"🎖️ {h(u['title'])} · ⭐ {u['reputation']}",
        parse_mode="HTML",
        reply_markup=b.as_markup(),
    )


@dp.callback_query(F.data.startswith("ai:"))
async def ai_callback(c: CallbackQuery, state: FSMContext):
    await state.clear()
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
    back_to = parts[5] if len(parts) > 5 else "feed"

    story = await db.story(sid)
    if not story or story["status"] in {"hidden", "deleted"}:
        await c.answer("Дело недоступно", show_alert=True)
        return

    result = await review(story["title"], story["body"])
    b = InlineKeyboardBuilder()
    b.button(
        text="⬅️ К делу",
        callback_data=f"caseback:{sid}:{feed_index}:{back_to}:{cat_key}:{sort}",
    )
    await safe_edit(
        c.message,
        "🧠 <b>РАЗБОР СОВЕТНИКА</b>\n\n" + h(result),
        b.as_markup(),
    )
    await c.answer()


@dp.callback_query(F.data == "profile")
async def profile_callback(c: CallbackQuery, state: FSMContext):
    await state.clear()
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
    if (m.text or "") in PRIMARY_NAV_TEXTS:
        await state.clear()
        await route_primary_navigation(m)
        return
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
    if (m.text or "") in PRIMARY_NAV_TEXTS:
        await state.clear()
        await route_primary_navigation(m)
        return
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
async def my_cases_callback(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await ensure_callback_user(c)
    try:
        index = int(c.data.split(":")[1])
    except (ValueError, IndexError):
        index = 0
    await render_my_cases(c.message, c.from_user.id, index)
    await c.answer()


@dp.callback_query(F.data.startswith("favorites:"))
async def favorites_callback(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await ensure_callback_user(c)
    try:
        index = int(c.data.split(":")[1])
    except (ValueError, IndexError):
        index = 0
    await render_favorites(c.message, c.from_user.id, index)
    await c.answer()


@dp.callback_query(F.data.startswith("favcase:"))
async def favorite_case_callback(c: CallbackQuery, state: FSMContext):
    await state.clear()
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
async def favorite_toggle_callback(c: CallbackQuery, state: FSMContext):
    await state.clear()
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
        "⭐ Дело сохранено. Буду сообщать о важных обновлениях."
        if enabled
        else "Дело удалено из избранного."
    )


@dp.callback_query(F.data.startswith("more:"))
async def more_menu_callback(c: CallbackQuery, state: FSMContext):
    await ensure_callback_user(c)
    await state.clear()
    parts = c.data.split(":")
    if len(parts) < 3:
        await c.answer("Некорректное меню", show_alert=True)
        return

    target_type = {"s": "story", "c": "comment", "d": "discussion"}.get(parts[1], parts[1])
    b = InlineKeyboardBuilder()

    if target_type == "story":
        try:
            sid = int(parts[2])
            feed_index = int(parts[3])
        except (ValueError, IndexError):
            await c.answer("Некорректное дело", show_alert=True)
            return
        back_to = parts[4] if len(parts) > 4 else "feed"
        cat_key = parts[5] if len(parts) > 5 else "all"
        sort = parts[6] if len(parts) > 6 else "new"

        b.button(text="🚩 Пожаловаться", callback_data=f"report:story:{sid}")
        b.button(
            text="⬅️ К делу",
            callback_data=f"caseback:{sid}:{feed_index}:{back_to}:{cat_key}:{sort}",
        )
        title = "••• <b>ДЕЙСТВИЯ С ДЕЛОМ</b>"

    elif target_type == "comment":
        try:
            cid = int(parts[2])
            sid = int(parts[3])
            index = int(parts[4])
            feed_index = int(parts[5])
        except (ValueError, IndexError):
            await c.answer("Некорректный совет", show_alert=True)
            return
        cat_key = parts[6] if len(parts) > 6 else "all"
        sort = parts[7] if len(parts) > 7 else "new"
        back_to = parts[8] if len(parts) > 8 else "feed"

        target = await db.admin_comment(cid)
        if target and int(target["author_tg_id"]) != int(c.from_user.id):
            b.button(text="🚩 Пожаловаться", callback_data=f"report:comment:{cid}:{sid}")
        b.button(
            text="⬅️ К совету",
            callback_data=f"comments:{sid}:{index}:{feed_index}:{cat_key}:{sort}:{back_to}",
        )
        title = "••• <b>ДЕЙСТВИЯ С СОВЕТОМ</b>"

    elif target_type == "discussion":
        try:
            message_id = int(parts[2])
            sid = int(parts[3])
            offset = int(parts[4])
            feed_index = int(parts[5])
        except (ValueError, IndexError):
            await c.answer("Некорректное сообщение", show_alert=True)
            return
        cat_key = parts[6] if len(parts) > 6 else "all"
        sort = parts[7] if len(parts) > 7 else "new"
        back_to = parts[8] if len(parts) > 8 else "feed"

        target = await db.discussion_message(message_id)
        if target and int(target["author_tg_id"]) != int(c.from_user.id):
            b.button(
                text="🚩 Пожаловаться",
                callback_data=f"report:discussion:{message_id}:{sid}",
            )
        b.button(
            text="⬅️ К обсуждению",
            callback_data=f"discuss:{sid}:{offset}:{feed_index}:{cat_key}:{sort}:{back_to}",
        )
        title = "••• <b>ДЕЙСТВИЯ С СООБЩЕНИЕМ</b>"
    else:
        await c.answer("Некорректное меню", show_alert=True)
        return

    b.adjust(1)
    await safe_edit(
        c.message,
        title + "\n\nВторостепенные действия находятся здесь, чтобы не перегружать основной экран.",
        b.as_markup(),
    )
    await c.answer()


@dp.callback_query(F.data == "notifications:toggle")
async def notifications_toggle_callback(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await ensure_callback_user(c)
    enabled = await db.toggle_notifications(c.from_user.id)
    await render_profile(c.message, c.from_user.id)
    await c.answer(
        "🔔 Уведомления включены." if enabled else "🔕 Уведомления выключены."
    )


@dp.callback_query(F.data.startswith("report:"))
async def report_menu_callback(c: CallbackQuery, state: FSMContext):
    await state.clear()
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
    if target_type in {"comment", "discussion"}:
        try:
            sid = int(parts[3])
        except (ValueError, IndexError):
            await c.answer("Некорректная жалоба", show_alert=True)
            return

    check_sid = target_id if target_type == "story" else sid
    story = await db.story(check_sid) if check_sid is not None else None
    if not story or story["status"] in {"hidden", "deleted"}:
        await c.answer("Этот материал больше недоступен.", show_alert=True)
        return

    short_type = {
        "story": "s",
        "comment": "c",
        "discussion": "d",
    }.get(target_type)
    if not short_type:
        await c.answer("Некорректная жалоба", show_alert=True)
        return
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
    elif target_type == "comment":
        b.button(
            text="⬅️ К советам",
            callback_data=f"comments:{sid}:0:0:all:new",
        )
    else:
        b.button(
            text="⬅️ К обсуждению",
            callback_data=f"discuss:{sid}:latest:0:all:new:feed",
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
async def report_submit_callback(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await ensure_callback_user(c)
    parts = c.data.split(":")
    if len(parts) < 4:
        await c.answer("Некорректная жалоба", show_alert=True)
        return

    target_type = {
        "s": "story",
        "c": "comment",
        "d": "discussion",
    }.get(parts[1])
    if not target_type:
        await c.answer("Некорректная жалоба", show_alert=True)
        return
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
    if target_type in {"comment", "discussion"}:
        try:
            sid = int(parts[4])
        except (ValueError, IndexError):
            await c.answer("Некорректная жалоба", show_alert=True)
            return

    story = await db.story(sid) if sid is not None else None
    if not story or story["status"] in {"hidden", "deleted"}:
        await c.answer("Этот материал больше недоступен.", show_alert=True)
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
            else (
                f"comments:{sid}:0:0:all:new:feed"
                if target_type == "comment"
                else f"discuss:{sid}:latest:0:all:new:feed"
            )
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
async def rating_callback(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await ensure_callback_user(c)
    try:
        page = int(c.data.split(":")[1])
    except (ValueError, IndexError):
        page = 0
    await render_rating(c.message, page)
    await c.answer()


@dp.callback_query(F.data == "ranks")
async def ranks_callback(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await ensure_callback_user(c)
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ В профиль", callback_data="profile")
    b.adjust(1)
    await safe_edit(
        c.message,
        "🎖️ <b>СУДЕБНЫЕ ЗВАНИЯ</b>\n\n"
        "🌱 Новичок — 0+\n"
        "⚔️ Присяжный — 10+\n"
        "⚖️ Судья — 30+\n"
        "🏛️ Старший судья — 75+\n"
        "👑 Верховный судья — 150+\n\n"
        "⭐ Первый совет к каждому чужому делу даёт +2 репутации.\n"
        "👍 Каждый уникальный лайк от другого пользователя даёт автору совета ещё +1.\n"
        "👎 Дизлайк сам по себе репутацию не отнимает.\n"
        "🛡️ Роли команды Anon Verdict существуют отдельно от судебных званий.",
        b.as_markup(),
    )
    await c.answer()


@dp.callback_query(F.data == "help")
async def help_callback(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await ensure_callback_user(c)
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ В профиль", callback_data="profile")
    await safe_edit(
        c.message,
        "ℹ️ <b>КАК ЭТО РАБОТАЕТ</b>\n\n"
        "📝 <b>Подать дело</b> — анонимно рассказать ситуацию.\n"
        "🏛️ <b>Зал суда</b> — компактная лента: короткое превью, фильтры и сортировка.\n"
        "🎲 Случайное дело — быстрый переход к другой истории.\n"
        "🔥 Можно открыть популярные дела или найти те, где ещё нет советов.\n"
        "💬 <b>Советы</b> — отдельные рекомендации, которые влияют на репутацию.\n"
        "🗣 <b>Обсуждение</b> — вопросы, уточнения и ответы внутри каждого дела без фарма репутации.\n"
        "⚖️ <b>Мои дела</b> — твои публикации и управление ими.\n"
        "✅ Завершённое дело остаётся читать, но новые советы и обсуждение закрываются.\n"
        "⭐ <b>Избранное</b> — сохраняй дела и получай важные обновления.\n"
        "••• Жалобы спрятаны во второстепенное меню, чтобы не перегружать карточки.\n"
        "🏆 <b>Рейтинг</b> учитывает репутацию и полезные оценки советов.\n"
        "⭐ Первый совет к делу даёт +2 репутации, каждый уникальный 👍 — ещё +1.\n"
        "⏳ Антиспам ограничивает слишком частые публикации.\n\n"
        "Быстрая клавиатура снизу остаётся: она нужна для мгновенного перехода в разделы.",
        b.as_markup(),
    )
    await c.answer()


async def route_primary_navigation(m: Message):
    text = m.text or ""
    if text == "🏛️ Зал суда":
        await render_feed(m, 0, edit=False)
    elif text == "👤 Мой профиль":
        await render_profile(m, m.from_user.id, edit=False)
    elif text == "🏆 Рейтинг":
        await render_rating(m, 0, edit=False)
    elif text == "⚖️ Мои дела":
        await render_my_cases(m, m.from_user.id, 0, edit=False)
    elif text == "⭐ Избранное":
        await render_favorites(m, m.from_user.id, 0, edit=False)
    elif text == "🛡️ CEO Панель":
        if is_owner(m.from_user.id):
            await render_admin_home(m, edit=False)
    elif text == "📝 Подать дело":
        b = InlineKeyboardBuilder()
        for x in CATS:
            b.button(text=x, callback_data="cat:" + x)
        b.adjust(2)
        await m.answer(
            "📝 <b>НОВОЕ ДЕЛО</b>\n\nВыбери категорию:",
            parse_mode="HTML",
            reply_markup=b.as_markup(),
        )


@dp.message(F.text == "🏛️ Зал суда")
async def menu_feed(m: Message):
    await ensure_message_user(m)
    await render_feed(m, 0, edit=False)


@dp.message(F.text == "👤 Мой профиль")
async def menu_profile(m: Message):
    await ensure_message_user(m)
    await render_profile(m, m.from_user.id, edit=False)


@dp.message(F.text == "🏆 Рейтинг")
async def menu_rating(m: Message):
    await ensure_message_user(m)
    await render_rating(m, 0, edit=False)


@dp.message(F.text == "⚖️ Мои дела")
async def menu_my_cases(m: Message):
    await ensure_message_user(m)
    await render_my_cases(m, m.from_user.id, 0, edit=False)


@dp.message(F.text == "⭐ Избранное")
async def menu_favorites(m: Message):
    await ensure_message_user(m)
    await render_favorites(m, m.from_user.id, 0, edit=False)


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
        "🏛️ Зал суда → короткие карточки, фильтры, сортировка и 🎲 случайное дело.\n"
        "🔥 Популярные / 🆘 без советов → быстрые режимы ленты.\n"
        "💬 Советы → отдельные рекомендации автору дела.\n"
        "🗣 Обсуждение → вопросы, ответы и разговор вокруг конкретного дела.\n"
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
    await render_profile(m, m.from_user.id, edit=False)


@dp.message(F.text == "🛡️ CEO Панель")
@dp.message(Command("admin"))
async def admin_entry(m: Message, state: FSMContext):
    await ensure_message_user(m)
    if not is_owner(m.from_user.id):
        await m.answer("Раздел доступен только владельцу проекта.")
        return
    await state.clear()
    await render_admin_home(m, edit=False)


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
        f"✅ Завершённые: <b>{s['closed_stories']}</b>\n"
        f"🗑 Удалённые: <b>{s['deleted_stories']}</b>\n"
        f"🙈 Скрытые: <b>{s['hidden_stories']}</b>\n"
        f"💬 Советы: <b>{s['comments']}</b>\n"
        f"🗣 Сообщения обсуждений: <b>{s['discussion_messages']}</b>\n"
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
    elif report["target_type"] == "comment":
        await db.set_comment_status(report["target_id"], "hidden")
    else:
        await db.set_discussion_status(report["target_id"], "hidden")

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
