import asyncio, os
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
TOKEN=os.getenv("BOT_TOKEN")
if not TOKEN: raise RuntimeError("BOT_TOKEN is not set")
bot=Bot(TOKEN); dp=Dispatcher(); db=DB(os.getenv("DB_PATH") or os.path.join(os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "/app/data"), "anon_verdict.sqlite3"))

CATS=["❤️ Отношения","💼 Работа","💰 Деньги","👨‍👩‍👦 Семья","🧑‍🤝‍🧑 Дружба","🎓 Учёба","🚀 Другое"]

class Story(StatesGroup):
    category=State(); title=State(); body=State()
class Profile(StatesGroup):
    nickname=State(); bio=State()
class Comment(StatesGroup):
    body=State()

def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📝 Подать дело"), KeyboardButton(text="🏛️ Зал суда")],
            [KeyboardButton(text="👤 Мой профиль"), KeyboardButton(text="🏆 Рейтинг")],
            [KeyboardButton(text="⚖️ Мои дела"), KeyboardButton(text="🎖️ Звания")],
            [KeyboardButton(text="ℹ️ Как это работает")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выбери действие…"
    )

def menu():
    b=InlineKeyboardBuilder()
    b.button(text="📝 Подать дело",callback_data="new")
    b.button(text="🏛️ Зал суда",callback_data="feed")
    b.button(text="👤 Профиль",callback_data="profile")
    b.button(text="🏆 Рейтинг",callback_data="rating")
    b.button(text="🎖️ Звания",callback_data="ranks")
    b.button(text="ℹ️ Как это работает",callback_data="help")
    b.adjust(2,2,2)
    return b.as_markup()

def home_button():
    b=InlineKeyboardBuilder()
    b.button(text="🏠 Главное меню",callback_data="home")
    return b.as_markup()

@dp.message(CommandStart())
async def start(m:Message):
    await db.ensure_user(m.from_user.id,m.from_user.username)
    await m.answer(
        "⚖️ <b>ANON VERDICT</b>\n\n"
        "Добро пожаловать в анонимный зал суда.\n"
        "Публикуй жизненные ситуации, выслушивай мнения и помогай другим разбирать их дела.\n\n"
        "🎖️ За полезные советы растёт репутация и судебное звание.\n"
        "🔒 Автор истории скрыт от других пользователей.",
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )

@dp.callback_query(F.data=="new")
async def new(c:CallbackQuery,state:FSMContext):
    await state.set_state(Story.category)
    b=InlineKeyboardBuilder()
    for x in CATS: b.button(text=x,callback_data="cat:"+x)
    b.adjust(2); await c.message.answer("⚖️ <b>Новое дело</b>\n\nВыбери категорию ситуации:",parse_mode="HTML",reply_markup=b.as_markup()); await c.answer()

@dp.callback_query(F.data.startswith("cat:"))
async def cat(c:CallbackQuery,state:FSMContext):
    await state.update_data(category=c.data[4:]); await state.set_state(Story.title)
    await c.message.answer("Коротко назови ситуацию:"); await c.answer()

@dp.message(Story.title)
async def title(m:Message,state:FSMContext):
    text,reasons=moderate(m.text or "")
    if reasons: await m.answer("Удали из текста персональные данные."); return
    await state.update_data(title=text); await state.set_state(Story.body)
    await m.answer("Теперь расскажи подробно. Не указывай телефоны, адреса и документы.")

@dp.message(Story.body)
async def body(m:Message,state:FSMContext):
    text,reasons=moderate(m.text or "")
    if reasons: await m.answer("Удали из текста персональные данные."); return
    d=await state.get_data(); sid=await db.create_story(m.from_user.id,d["category"],d["title"],text)
    await state.clear(); await m.answer(f"📜 История №{sid} опубликована анонимно.",reply_markup=main_keyboard())

@dp.callback_query(F.data=="home")
async def home(c:CallbackQuery):
    await c.message.answer("⚖️ <b>Главное меню</b>\n\nВыбирай действие на клавиатуре ниже:",parse_mode="HTML",reply_markup=main_keyboard())
    await c.answer()

@dp.callback_query(F.data=="help")
async def help_menu(c:CallbackQuery):
    await c.message.answer(
        "⚖️ <b>КАК РАБОТАЕТ ANON VERDICT</b>\n\n"
        "1️⃣ <b>Подай дело</b> — анонимно расскажи свою ситуацию.\n"
        "2️⃣ <b>Зал суда</b> — читай истории других людей.\n"
        "3️⃣ <b>Выскажись</b> — дай совет или своё мнение.\n"
        "4️⃣ <b>Решение за автором</b> — он сам выбирает, что делать.\n\n"
        "🏛️ Здесь есть профили, репутация, рейтинг и судебные звания.",
        parse_mode="HTML", reply_markup=home_button())
    await c.answer()

@dp.callback_query(F.data=="ranks")
async def ranks(c:CallbackQuery):
    await c.message.answer(
        "🎖️ <b>СУДЕБНЫЕ ЗВАНИЯ</b>\n\n"
        "🌱 <b>Новичок</b> — знакомится с залом суда.\n"
        "⚔️ <b>Присяжный</b> — активно участвует в обсуждениях.\n"
        "⚖️ <b>Судья</b> — даёт полезные и содержательные советы.\n"
        "🏛️ <b>Старший судья</b> — высокий уровень репутации.\n"
        "👑 <b>Верховный судья</b> — высшее звание проекта.\n\n"
        "⭐ Репутация будет расти за полезную активность.",
        parse_mode="HTML", reply_markup=home_button())
    await c.answer()

@dp.callback_query(F.data=="feed")
async def feed(c:CallbackQuery):
    rows=await db.latest()
    if not rows: await c.message.answer("Историй пока нет. Стань первым.",reply_markup=menu())
    for s in rows:
        b=InlineKeyboardBuilder(); b.button(text="💬 Открыть",callback_data=f"s:{s['id']}")
        await c.message.answer(
            f"⚖️ <b>Дело №{s['id']}</b>\n"
            f"🏷️ {s['category']}\n\n"
            f"<b>{s['title']}</b>\n{s['body'][:700]}",
            parse_mode="HTML", reply_markup=b.as_markup())
    await c.answer()

@dp.callback_query(F.data.startswith("s:"))
async def show(c:CallbackQuery):
    sid=int(c.data[2:]); s=await db.story(sid,True)
    if not s: await c.answer("Не найдено",show_alert=True); return
    b=InlineKeyboardBuilder()
    b.button(text="💬 Дать совет",callback_data=f"c:{sid}")
    b.button(text="🧠 Разбор",callback_data=f"a:{sid}")
    b.adjust(2)
    await c.message.answer(
        f"⚖️ <b>Дело №{sid}</b>\n"
        f"🏷️ {s['category']}\n\n"
        f"<b>{s['title']}</b>\n{s['body']}\n\n"
        f"👁 Просмотров: {s['views']}",
        parse_mode="HTML", reply_markup=b.as_markup())
    for x in await db.comments(sid):
        rb=InlineKeyboardBuilder(); rb.button(text="👍",callback_data=f"r:1:{x['id']}:{sid}"); rb.button(text="👎",callback_data=f"r:-1:{x['id']}:{sid}")
        await c.message.answer(f"🧠 <b>{x['nickname']}</b> · {x['title']}\n{x['body']}\n\n👍 {x['likes']} 👎 {x['dislikes']}",parse_mode="HTML",reply_markup=rb.as_markup())
    await c.answer()

@dp.callback_query(F.data.startswith("c:"))
async def cstart(c:CallbackQuery,state:FSMContext):
    await state.update_data(sid=int(c.data[2:])); await state.set_state(Comment.body)
    await c.message.answer("Напиши совет или мнение:"); await c.answer()

@dp.message(Comment.body)
async def comment(m:Message,state:FSMContext):
    text,reasons=moderate(m.text or "")
    if reasons: await m.answer("Удали персональные данные."); return
    d=await state.get_data(); await db.comment(m.from_user.id,d["sid"],text)
    await state.clear(); await m.answer("Совет опубликован. +2 репутации.",reply_markup=main_keyboard())

@dp.callback_query(F.data.startswith("r:"))
async def react(c:CallbackQuery):
    _,v,cid,_=c.data.split(":"); await db.react(c.from_user.id,int(cid),int(v)); await c.answer("Оценка сохранена")

@dp.callback_query(F.data.startswith("a:"))
async def ai(c:CallbackQuery):
    s=await db.story(int(c.data[2:]))
    await c.message.answer("🧠 Советник разбирает ситуацию...")
    await c.message.answer(await review(s["title"],s["body"])); await c.answer()

@dp.callback_query(F.data=="profile")
async def profile(c:CallbackQuery):
    u=await db.get_user(c.from_user.id); b=InlineKeyboardBuilder(); b.button(text="✏️ Изменить",callback_data="edit")
    await c.message.answer(f"👤 <b>{u['nickname']}</b>\n🎖 {u['title']}\n⭐ {u['reputation']}\n📈 Уровень {u['level']}\n\n{u['bio'] or 'Описание не добавлено.'}",parse_mode="HTML",reply_markup=b.as_markup()); await c.answer()

@dp.callback_query(F.data=="edit")
async def edit(c:CallbackQuery,state:FSMContext):
    await state.set_state(Profile.nickname); await c.message.answer("Новый ник (до 32 символов):"); await c.answer()

@dp.message(Profile.nickname)
async def nick(m:Message,state:FSMContext):
    await state.update_data(nick=(m.text or "Пользователь")[:32]); await state.set_state(Profile.bio); await m.answer("Описание профиля (до 160 символов):")

@dp.message(Profile.bio)
async def bio(m:Message,state:FSMContext):
    d=await state.get_data(); await db.update_profile(m.from_user.id,d["nick"],(m.text or "")[:160]); await state.clear()
    await m.answer("Профиль обновлён.",reply_markup=menu())

@dp.callback_query(F.data=="rating")
async def rating(c:CallbackQuery):
    rows=await db.leaderboard()
    text="🏆 <b>Рейтинг</b>\n\n"+"\n".join(f"{i}. {x['nickname']} — ⭐ {x['reputation']} · {x['title']}" for i,x in enumerate(rows,1))
    await c.message.answer(text,parse_mode="HTML",reply_markup=home_button()); await c.answer()

@dp.message(F.text == "📝 Подать дело")
async def menu_new(m:Message,state:FSMContext):
    await state.set_state(Story.category)
    b=InlineKeyboardBuilder()
    for x in CATS: b.button(text=x,callback_data="cat:"+x)
    b.adjust(2)
    await m.answer("⚖️ <b>Новое дело</b>\n\nВыбери категорию:",parse_mode="HTML",reply_markup=b.as_markup())

@dp.message(F.text == "🏛️ Зал суда")
async def menu_feed(m:Message):
    rows=await db.latest()
    if not rows:
        await m.answer("🏛️ <b>Зал суда пока пуст</b>\n\nСтань первым, кто подаст дело.",parse_mode="HTML",reply_markup=main_keyboard())
        return
    await m.answer("🏛️ <b>ЗАЛ СУДА</b>\n\nВыбери дело, которое хочешь разобрать.",parse_mode="HTML")
    for s in rows:
        b=InlineKeyboardBuilder()
        b.button(text="💬 Открыть дело",callback_data=f"s:{s['id']}")
        await m.answer(
            f"⚖️ <b>Дело №{s['id']}</b>\n"
            f"🏷️ {s['category']}\n\n"
            f"<b>{s['title']}</b>\n{s['body'][:700]}",
            parse_mode="HTML",reply_markup=b.as_markup())

@dp.message(F.text == "👤 Мой профиль")
async def menu_profile(m:Message):
    u=await db.get_user(m.from_user.id)
    b=InlineKeyboardBuilder()
    b.button(text="✏️ Изменить профиль",callback_data="edit")
    b.button(text="🎖️ Мои звания",callback_data="ranks")
    b.adjust(1)
    await m.answer(
        f"👤 <b>{u['nickname']}</b>\n"
        f"🎖️ {u['title']}\n"
        f"⭐ Репутация: {u['reputation']}\n"
        f"📈 Уровень: {u['level']}\n\n"
        f"📝 {u['bio'] or 'Описание пока не добавлено.'}",
        parse_mode="HTML",reply_markup=b.as_markup())

@dp.message(F.text == "🏆 Рейтинг")
async def menu_rating(m:Message):
    rows=await db.leaderboard()
    text="🏆 <b>РЕЙТИНГ ЗАЛА</b>\n\n"
    text += "\n".join(f"<b>{i}.</b> {x['nickname']} — ⭐ {x['reputation']} · {x['title']}" for i,x in enumerate(rows,1))
    await m.answer(text,parse_mode="HTML",reply_markup=main_keyboard())

@dp.message(F.text == "⚖️ Мои дела")
async def my_cases(m:Message):
    await m.answer(
        "⚖️ <b>МОИ ДЕЛА</b>\n\n"
        "Здесь будут собираться твои опубликованные истории, "
        "обсуждения и продолжения дел.",
        parse_mode="HTML",reply_markup=main_keyboard())

@dp.message(F.text == "🎖️ Звания")
async def menu_ranks(m:Message):
    await m.answer(
        "🎖️ <b>СУДЕБНЫЕ ЗВАНИЯ</b>\n\n"
        "🌱 Новичок\n"
        "⚔️ Присяжный\n"
        "⚖️ Судья\n"
        "🏛️ Старший судья\n"
        "👑 Верховный судья\n\n"
        "⭐ Чем полезнее твоя активность, тем выше репутация.",
        parse_mode="HTML",reply_markup=main_keyboard())

@dp.message(F.text == "ℹ️ Как это работает")
async def menu_help(m:Message):
    await m.answer(
        "ℹ️ <b>КАК ЭТО РАБОТАЕТ</b>\n\n"
        "📝 Подай дело → расскажи ситуацию анонимно.\n"
        "🏛️ Зал суда → читай истории.\n"
        "💬 Дай совет → помоги автору.\n"
        "⭐ Репутация → развивай свой профиль.\n"
        "🎖️ Звания → поднимайся от Новичка до Верховного судьи.",
        parse_mode="HTML",reply_markup=main_keyboard())

@dp.message(Command("profile"))
async def p(m:Message):
    await db.ensure_user(m.from_user.id,m.from_user.username); u=await db.get_user(m.from_user.id)
    await m.answer(f"👤 {u['nickname']}\n⭐ {u['reputation']}\n📈 Уровень {u['level']}\n🎖 {u['title']}")

async def main():
    await db.init(); await dp.start_polling(bot)

if __name__=="__main__": asyncio.run(main())
