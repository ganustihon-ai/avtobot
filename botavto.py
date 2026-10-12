"""
Автопостер для Telegram-каналов.
Мульти-юзер · Мульти-каналы · Premium · Поддержка · Админка.
"""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton, FSInputFile,
    LabeledPrice, PreCheckoutQuery,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler


# ═══════════════════════════════════════════════
#   ⚙️  НАСТРОЙКИ — ЗАМЕНИ ЭТИ ДВЕ СТРОКИ
# ═══════════════════════════════════════════════

BOT_TOKEN = "8976134919:AAGUqMC58UsSLuXvG17rrE1C8ybThM0y6GA"
ADMIN_IDS = [883137958]

# ═══════════════════════════════════════════════

FREE_LIMIT = 10
FREE_CHANNEL_LIMIT = 1
PRO_CHANNEL_LIMIT = 20

PLANS = {
    "1m": (30,  100,  "1 месяц"),
    "3m": (90,  150, "3 месяца"),
    "6m": (180, 300, "6 месяцев"),
    "1y": (365, 500, "1 год"),
}

DB_PATH   = "autopost.db"
MEDIA_DIR = Path("media")
MEDIA_DIR.mkdir(exist_ok=True)

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")


# ═══════════════════════════════════════════════
#   БАЗА ДАННЫХ
# ═══════════════════════════════════════════════

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER UNIQUE,
    username TEXT,
    first_name TEXT,
    posts_used INTEGER DEFAULT 0,
    is_pro INTEGER DEFAULT 0,
    pro_until TEXT,
    is_banned INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    last_seen TEXT
);
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    channel_id TEXT,
    title TEXT,
    username TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, channel_id)
);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    channel_row_id INTEGER,
    channel_id TEXT,
    channel_title TEXT,
    text TEXT,
    media_type TEXT,
    media_path TEXT,
    status TEXT DEFAULT 'scheduled',
    scheduled_at TEXT,
    published_at TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    amount INTEGER,
    charge_id TEXT,
    days INTEGER,
    plan TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def db_exec(q, p=()):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(q, p)
        await db.commit()
        return cur.lastrowid


async def db_all(q, p=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(q, p)
        return await cur.fetchall()


async def db_one(q, p=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(q, p)
        return await cur.fetchone()


async def get_user(tg_id, username=None, first_name=None):
    u = await db_one("SELECT * FROM users WHERE tg_id=?", (tg_id,))
    if u:
        await db_exec("UPDATE users SET last_seen=? WHERE id=?",
                      (datetime.now().isoformat(), u["id"]))
        return u
    uid = await db_exec(
        "INSERT INTO users (tg_id, username, first_name, last_seen) VALUES (?,?,?,?)",
        (tg_id, username, first_name, datetime.now().isoformat()),
    )
    return await db_one("SELECT * FROM users WHERE id=?", (uid,))


def is_admin(tg_id):
    return tg_id in ADMIN_IDS


def pro_active(user):
    if not user["is_pro"] or not user["pro_until"]:
        return False
    try:
        return datetime.fromisoformat(user["pro_until"]) > datetime.now()
    except Exception:
        return False


def pro_days_left(user):
    if not user["pro_until"]:
        return 0
    try:
        delta = datetime.fromisoformat(user["pro_until"]) - datetime.now()
        return max(0, delta.days)
    except Exception:
        return 0


async def user_channels(user_id):
    return await db_all("SELECT * FROM channels WHERE user_id=? ORDER BY id ASC", (user_id,))


# ═══════════════════════════════════════════════
#   FSM
# ═══════════════════════════════════════════════

class SetupFSM(StatesGroup):
    channel = State()


class PostFSM(StatesGroup):
    choose_channel = State()
    type = State()
    text = State()
    media = State()
    preview = State()
    time = State()


class SupportFSM(StatesGroup):
    message = State()


class AdminFSM(StatesGroup):
    find_user = State()
    broadcast = State()
    grant_pro = State()
    ban_user = State()
    reply_user = State()


# ═══════════════════════════════════════════════
#   ОТЛОЖЕННЫЕ ЗАДАЧИ
# ═══════════════════════════════════════════════

async def publish(post):
    text = post["text"] or ""
    media_path = post["media_path"]
    mtype = post["media_type"]
    channel = post["channel_id"]

    if mtype == "video" and media_path:
        await bot.send_video(channel, FSInputFile(media_path), caption=text)
    elif mtype == "photo" and media_path:
        await bot.send_photo(channel, FSInputFile(media_path), caption=text)
    elif mtype == "document" and media_path:
        await bot.send_document(channel, FSInputFile(media_path), caption=text)
    else:
        await bot.send_message(channel, text)


async def check_scheduled():
    now = datetime.now().isoformat()
    posts = await db_all(
        "SELECT * FROM posts WHERE status='scheduled' AND scheduled_at<=?", (now,)
    )
    for p in posts:
        try:
            await publish(p)
            await db_exec(
                "UPDATE posts SET status='published', published_at=? WHERE id=?",
                (datetime.now().isoformat(), p["id"])
            )
            user = await db_one("SELECT * FROM users WHERE id=?", (p["user_id"],))
            if user:
                try:
                    await bot.send_message(
                        user["tg_id"],
                        f"✅ Пост #{p['id']} опубликован в «{p['channel_title']}»"
                    )
                except Exception:
                    pass
        except Exception as e:
            print(f"Ошибка поста {p['id']}: {e}")


async def check_pro_expiry():
    users = await db_all("SELECT * FROM users WHERE is_pro=1")
    now = datetime.now()
    for u in users:
        if not u["pro_until"]:
            continue
        try:
            until = datetime.fromisoformat(u["pro_until"])
        except Exception:
            continue

        delta = (until - now).days

        if until <= now:
            await db_exec("UPDATE users SET is_pro=0 WHERE id=?", (u["id"],))
            try:
                kb = InlineKeyboardBuilder()
                kb.row(InlineKeyboardButton(text="⭐ Продлить Premium", callback_data="premium"))
                await bot.send_message(
                    u["tg_id"],
                    f"⌛ <b>Premium закончился</b>\n\n"
                    f"Теперь Free: {FREE_LIMIT} постов/мес, {FREE_CHANNEL_LIMIT} канал.",
                    reply_markup=kb.as_markup(),
                )
            except Exception:
                pass
            continue

        if delta == 3:
            try:
                kb = InlineKeyboardBuilder()
                kb.row(InlineKeyboardButton(text="⭐ Продлить", callback_data="premium"))
                await bot.send_message(
                    u["tg_id"],
                    f"🔔 <b>Premium закончится через 3 дня</b>\n\n"
                    f"Дата: {until.strftime('%d.%m.%Y')}",
                    reply_markup=kb.as_markup(),
                )
            except Exception:
                pass


# ═══════════════════════════════════════════════
#   МЕНЮ
# ═══════════════════════════════════════════════

async def main_menu(user):
    chans = await user_channels(user["id"])
    kb = InlineKeyboardBuilder()

    if chans:
        kb.row(InlineKeyboardButton(text="✍️ Создать пост", callback_data="new"))
        kb.row(
            InlineKeyboardButton(text=f"📡 Мои каналы ({len(chans)})", callback_data="my_channels"),
            InlineKeyboardButton(text="📋 Мои посты", callback_data="list"),
        )
        kb.row(
            InlineKeyboardButton(text="📊 Статистика", callback_data="stats"),
            InlineKeyboardButton(text="⭐ Premium", callback_data="premium"),
        )
        kb.row(
            InlineKeyboardButton(text="❓ Помощь", callback_data="help"),
            InlineKeyboardButton(text="🆘 Поддержка", callback_data="support"),
        )
        kb.row(InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings"))
    else:
        kb.row(InlineKeyboardButton(text="➕ Подключить канал", callback_data="setup"))
        kb.row(
            InlineKeyboardButton(text="⭐ Premium", callback_data="premium"),
            InlineKeyboardButton(text="📖 Как это работает?", callback_data="how_it_works"),
        )
        kb.row(
            InlineKeyboardButton(text="❓ Помощь", callback_data="help"),
            InlineKeyboardButton(text="🆘 Поддержка", callback_data="support"),
        )
    return kb


@dp.message(CommandStart())
async def cmd_start(message: Message):
    user = await get_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
    )

    if user["is_banned"]:
        await message.answer("🚫 Ты заблокирован.")
        return

    chans = await user_channels(user["id"])
    kb = await main_menu(user)
    name = user["first_name"] or "друг"

    if chans:
        active = pro_active(user)
        if active:
            plan_line = f"⭐ Premium · осталось {pro_days_left(user)} дн."
        else:
            plan_line = f"🆓 Free · {user['posts_used']}/{FREE_LIMIT} постов"
        text = (
            f"👋 Привет, <b>{name}</b>!\n\n"
            f"🤖 <b>Автопостер</b> — публикую твои посты в Telegram-каналы по расписанию.\n\n"
            f"📡 Каналов подключено: <b>{len(chans)}</b>\n"
            f"Тариф: {plan_line}\n\n"
            f"<b>Что можно сделать:</b>\n"
            f"• ✍️ Создать пост (текст, фото, видео, документ)\n"
            f"• ⏰ Запланировать на любое время\n"
            f"• 📡 Управлять каналами\n"
            f"• 📊 Смотреть статистику\n\n"
            f"Выбирай кнопку ниже 👇"
        )
    else:
        text = (
            f"👋 Привет, <b>{name}</b>!\n\n"
            f"🤖 Я — <b>Автопостер</b>. Публикую посты в твои Telegram-каналы "
            f"по расписанию. Пока ты занят или спишь — посты выходят сами.\n\n"
            f"<b>━━━ Что я умею ━━━</b>\n\n"
            f"📝 <b>Создавать посты</b>\n"
            f"Текст, фото, видео, документы — любой контент.\n\n"
            f"⏰ <b>Планировать</b>\n"
            f"Публикую в указанное время. Хоть через месяц.\n\n"
            f"📡 <b>Работать с каналами</b>\n"
            f"Можно подключить несколько и выбрать, куда постить.\n\n"
            f"📊 <b>Считать статистику</b>\n"
            f"Сколько постов, где опубликовано, когда.\n\n"
            f"🆘 <b>Поддержка 24/7</b>\n"
            f"Пиши — отвечу.\n\n"
            f"<b>━━━ Тарифы ━━━</b>\n\n"
            f"🆓 <b>Free</b> (бесплатно)\n"
            f"• До {FREE_LIMIT} постов в месяц\n"
            f"• {FREE_CHANNEL_LIMIT} канал\n"
            f"• Планирование на 30 дней вперёд\n\n"
            f"⭐ <b>Premium</b> (от 500 звёзд)\n"
            f"• <b>Безлимит</b> постов\n"
            f"• До {PRO_CHANNEL_LIMIT} каналов\n"
            f"• Планирование на год вперёд\n"
            f"• Приоритетная публикация\n\n"
            f"<b>━━━ Как начать ━━━</b>\n\n"
            f"1️⃣ Добавь меня админом в свой канал\n"
            f"2️⃣ Нажми «➕ Подключить канал»\n"
            f"3️⃣ Создай первый пост\n\n"
            f"👇 <b>Начни прямо сейчас!</b>"
        )

    if is_admin(message.from_user.id):
        kb.row(InlineKeyboardButton(text="👑 Админ-панель", callback_data="admin"))

    await message.answer(text, reply_markup=kb.as_markup())


@dp.callback_query(F.data == "home")
async def cb_home(cb: CallbackQuery):
    try:
        await cb.message.delete()
    except Exception:
        pass
    await cmd_start(cb.message)
    await cb.answer()


# ═══════════════════════════════════════════════
#   ПОДКЛЮЧЕНИЕ КАНАЛА
# ═══════════════════════════════════════════════

@dp.callback_query(F.data == "setup")
async def cb_setup(cb: CallbackQuery, state: FSMContext):
    user = await get_user(cb.from_user.id)
    chans = await user_channels(user["id"])
    active = pro_active(user)
    limit = PRO_CHANNEL_LIMIT if active else FREE_CHANNEL_LIMIT

    if len(chans) >= limit:
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="⭐ Купить Premium", callback_data="premium"))
        kb.row(InlineKeyboardButton(text="🏠 В меню", callback_data="home"))
        await cb.message.answer(
            f"🔒 <b>Достигнут лимит каналов</b>\n\n"
            f"На твоём тарифе можно подключить <b>{limit}</b> "
            f"{'канал' if limit == 1 else 'каналов'}.\n\n"
            f"Оформи Premium, чтобы добавить до {PRO_CHANNEL_LIMIT}.",
            reply_markup=kb.as_markup(),
        )
        await cb.answer()
        return

    me = await bot.me()
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="❌ Отмена", callback_data="home"))
    await cb.message.answer(
        "📌 <b>Как подключить канал:</b>\n\n"
        "1. Открой свой канал\n"
        "2. Управление → Администраторы → Добавить\n"
        f"3. Найди бота <b>@{me.username}</b>\n"
        "   Дай права: <b>Публикация сообщений</b>\n\n"
        "4. Отправь сюда ссылку на канал:\n"
        "• <code>@my_channel</code>\n"
        "• <code>https://t.me/my_channel</code>\n"
        "• <code>t.me/my_channel</code>",
        reply_markup=kb.as_markup(),
    )
    await state.set_state(SetupFSM.channel)
    await cb.answer()


@dp.message(SetupFSM.channel)
async def setup_channel(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    channel = None

    if raw.startswith("@"):
        channel = raw
    elif "t.me/" in raw:
        part = raw.split("t.me/")[-1].strip("/").split("?")[0]
        if part.startswith("+"):
            await message.answer("❌ Приватные ссылки не подходят. Нужен публичный @username.")
            return
        channel = "@" + part
    else:
        await message.answer("❌ Не понимаю ссылку. Отправь @my_channel или t.me/my_channel.")
        return

    try:
        chat = await bot.get_chat(channel)
    except Exception as e:
        await message.answer(f"❌ Не могу открыть канал.\n\n<code>{e}</code>")
        return

    try:
        me = await bot.me()
        member = await bot.get_chat_member(chat.id, me.id)
        if member.status not in ("administrator", "creator"):
            await message.answer(
                "❌ Бот не админ этого канала.\n\n"
                "Добавь бота в админы с правом «Публикация» и попробуй снова."
            )
            return
    except Exception as e:
        await message.answer(f"❌ Ошибка: <code>{e}</code>")
        return

    user = await get_user(message.from_user.id, message.from_user.username, message.from_user.first_name)

    existing = await db_one(
        "SELECT * FROM channels WHERE user_id=? AND channel_id=?",
        (user["id"], str(chat.id))
    )
    if existing:
        await message.answer(f"ℹ️ Канал <b>{chat.title}</b> уже подключён.")
        await state.clear()
        return

    await db_exec(
        "INSERT INTO channels (user_id, channel_id, title, username) VALUES (?,?,?,?)",
        (user["id"], str(chat.id), chat.title or "канал", chat.username),
    )

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="✍️ Создать пост", callback_data="new"))
    kb.row(InlineKeyboardButton(text="📡 Мои каналы", callback_data="my_channels"))
    kb.row(InlineKeyboardButton(text="🏠 В меню", callback_data="home"))

    await message.answer(
        f"✅ Канал <b>{chat.title}</b> подключён!",
        reply_markup=kb.as_markup(),
    )
    await state.clear()


# ═══════════════════════════════════════════════
#   МОИ КАНАЛЫ
# ═══════════════════════════════════════════════

@dp.callback_query(F.data == "my_channels")
async def cb_my_channels(cb: CallbackQuery):
    user = await get_user(cb.from_user.id)
    chans = await user_channels(user["id"])

    if not chans:
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="➕ Подключить канал", callback_data="setup"))
        kb.row(InlineKeyboardButton(text="🏠 В меню", callback_data="home"))
        await cb.message.answer("У тебя пока нет каналов.", reply_markup=kb.as_markup())
        await cb.answer()
        return

    kb = InlineKeyboardBuilder()
    for ch in chans:
        kb.row(InlineKeyboardButton(
            text=f"📡 {ch['title']}",
            callback_data=f"ch_open_{ch['id']}"
        ))
    kb.row(InlineKeyboardButton(text="➕ Добавить канал", callback_data="setup"))
    kb.row(InlineKeyboardButton(text="🏠 В меню", callback_data="home"))

    active = pro_active(user)
    limit = PRO_CHANNEL_LIMIT if active else FREE_CHANNEL_LIMIT

    await cb.message.answer(
        f"📡 <b>Мои каналы</b> ({len(chans)}/{limit})\n\n"
        f"Нажми на канал, чтобы посмотреть детали:",
        reply_markup=kb.as_markup(),
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("ch_open_"))
async def cb_channel_open(cb: CallbackQuery):
    ch_id = int(cb.data.replace("ch_open_", ""))
    user = await get_user(cb.from_user.id)

    ch = await db_one("SELECT * FROM channels WHERE id=? AND user_id=?", (ch_id, user["id"]))
    if not ch:
        await cb.answer("Канал не найден", show_alert=True)
        return

    pub = (await db_one(
        "SELECT COUNT(*) c FROM posts WHERE user_id=? AND channel_row_id=? AND status='published'",
        (user["id"], ch_id)
    ))["c"]
    sched = (await db_one(
        "SELECT COUNT(*) c FROM posts WHERE user_id=? AND channel_row_id=? AND status='scheduled'",
        (user["id"], ch_id)
    ))["c"]

    status = "✅ активно"
    try:
        me = await bot.me()
        member = await bot.get_chat_member(int(ch["channel_id"]), me.id)
        if member.status not in ("administrator", "creator"):
            status = "⚠️ бот не админ"
    except Exception:
        status = "⚠️ недоступен"

    uname_line = f"@{ch['username']}" if ch["username"] else "—"

    kb = InlineKeyboardBuilder()
    if ch["username"]:
        kb.row(InlineKeyboardButton(
            text="🔗 Открыть канал",
            url=f"https://t.me/{ch['username']}"
        ))
    kb.row(InlineKeyboardButton(
        text="✍️ Создать пост в этот канал",
        callback_data=f"new_in_{ch_id}"
    ))
    kb.row(InlineKeyboardButton(
        text="📋 Посты канала",
        callback_data=f"ch_posts_{ch_id}"
    ))
    kb.row(InlineKeyboardButton(
        text="🗑 Отключить канал",
        callback_data=f"ch_del_{ch_id}"
    ))
    kb.row(InlineKeyboardButton(text="🏠 К списку", callback_data="my_channels"))

    await cb.message.answer(
        f"📡 <b>{ch['title']}</b>\n\n"
        f"Юзернейм: {uname_line}\n"
        f"ID: <code>{ch['channel_id']}</code>\n"
        f"Статус: {status}\n\n"
        f"📝 Опубликовано: <b>{pub}</b>\n"
        f"⏰ В очереди: <b>{sched}</b>",
        reply_markup=kb.as_markup(),
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("ch_del_"))
async def cb_channel_delete(cb: CallbackQuery):
    ch_id = int(cb.data.replace("ch_del_", ""))
    user = await get_user(cb.from_user.id)

    ch = await db_one("SELECT * FROM channels WHERE id=? AND user_id=?", (ch_id, user["id"]))
    if not ch:
        await cb.answer("Не найдено", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"ch_delok_{ch_id}"))
    kb.row(InlineKeyboardButton(text="↩️ Отмена", callback_data=f"ch_open_{ch_id}"))

    await cb.message.answer(
        f"⚠️ <b>Удалить канал «{ch['title']}»?</b>\n\n"
        f"Посты этого канала в очереди тоже будут удалены.",
        reply_markup=kb.as_markup(),
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("ch_delok_"))
async def cb_channel_delete_ok(cb: CallbackQuery):
    ch_id = int(cb.data.replace("ch_delok_", ""))
    user = await get_user(cb.from_user.id)

    ch = await db_one("SELECT * FROM channels WHERE id=? AND user_id=?", (ch_id, user["id"]))
    if not ch:
        await cb.answer("Не найдено", show_alert=True)
        return

    await db_exec(
        "DELETE FROM posts WHERE user_id=? AND channel_row_id=? AND status='scheduled'",
        (user["id"], ch_id)
    )
    await db_exec("DELETE FROM channels WHERE id=?", (ch_id,))

    await cb.message.answer(f"🗑 Канал «{ch['title']}» отключён.")
    await cb.answer()
    await cb_my_channels(cb)


@dp.callback_query(F.data.startswith("ch_posts_"))
async def cb_channel_posts(cb: CallbackQuery):
    ch_id = int(cb.data.replace("ch_posts_", ""))
    user = await get_user(cb.from_user.id)

    ch = await db_one("SELECT * FROM channels WHERE id=? AND user_id=?", (ch_id, user["id"]))
    if not ch:
        await cb.answer("Не найдено", show_alert=True)
        return

    posts = await db_all(
        "SELECT * FROM posts WHERE user_id=? AND channel_row_id=? ORDER BY id DESC LIMIT 15",
        (user["id"], ch_id)
    )

    if not posts:
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="✍️ Создать пост", callback_data=f"new_in_{ch_id}"))
        kb.row(InlineKeyboardButton(text="↩️ Назад", callback_data=f"ch_open_{ch_id}"))
        await cb.message.answer(
            f"📋 Постов в «{ch['title']}» пока нет.",
            reply_markup=kb.as_markup()
        )
        await cb.answer()
        return

    text = f"📋 <b>Посты «{ch['title']}»</b>\n\n"
    for p in posts:
        icon = {"scheduled": "⏰", "published": "✅"}.get(p["status"], "❓")
        when = (p["scheduled_at"] or "")[:16].replace("T", " ")
        t = {"photo": "🖼", "video": "🎬", "document": "📎"}.get(p["media_type"], "📝")
        preview = (p["text"] or "[без текста]")[:35]
        text += f"{icon} #{p['id']} {t} · {when}\n   {preview}\n\n"

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="✍️ Создать пост", callback_data=f"new_in_{ch_id}"))
    kb.row(InlineKeyboardButton(text="🗑 Удалить", callback_data=f"ch_posts_del_{ch_id}"))
    kb.row(InlineKeyboardButton(text="↩️ Назад", callback_data=f"ch_open_{ch_id}"))

    await cb.message.answer(text, reply_markup=kb.as_markup())
    await cb.answer()


@dp.callback_query(F.data.startswith("ch_posts_del_"))
async def cb_channel_posts_del(cb: CallbackQuery):
    ch_id = int(cb.data.replace("ch_posts_del_", ""))
    await cb.message.answer(
        f"Отправь: <code>/delch {ch_id} 5</code>\n\n"
        f"где 5 — номер поста (из списка выше)."
    )
    await cb.answer()


# ═══════════════════════════════════════════════
#   СОЗДАНИЕ ПОСТА
# ═══════════════════════════════════════════════

@dp.callback_query(F.data == "new")
async def cb_new(cb: CallbackQuery, state: FSMContext):
    user = await get_user(cb.from_user.id)

    if user["is_banned"]:
        await cb.answer("Ты заблокирован", show_alert=True)
        return

    chans = await user_channels(user["id"])
    if not chans:
        await cb.answer("Сначала подключи канал", show_alert=True)
        return

    if not pro_active(user) and user["posts_used"] >= FREE_LIMIT:
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="⭐ Купить Premium", callback_data="premium"))
        await cb.message.answer(
            f"🔒 <b>Лимит Free исчерпан</b>\n\n"
            f"Использовано {FREE_LIMIT}/{FREE_LIMIT} постов.\n"
            f"Оформи Premium — безлимит постов.",
            reply_markup=kb.as_markup(),
        )
        await cb.answer()
        return

    if len(chans) == 1:
        await state.update_data(channel_row_id=chans[0]["id"])
        await show_type_choose(cb.message, state)
    else:
        kb = InlineKeyboardBuilder()
        for ch in chans:
            kb.row(InlineKeyboardButton(
                text=f"📡 {ch['title']}",
                callback_data=f"pick_ch_{ch['id']}"
            ))
        kb.row(InlineKeyboardButton(text="❌ Отмена", callback_data="home"))
        await cb.message.answer(
            "📡 <b>В какой канал публикуем?</b>",
            reply_markup=kb.as_markup(),
        )
        await state.set_state(PostFSM.choose_channel)
    await cb.answer()


@dp.callback_query(F.data.startswith("new_in_"))
async def cb_new_in_channel(cb: CallbackQuery, state: FSMContext):
    ch_id = int(cb.data.replace("new_in_", ""))
    user = await get_user(cb.from_user.id)
    ch = await db_one("SELECT * FROM channels WHERE id=? AND user_id=?", (ch_id, user["id"]))
    if not ch:
        await cb.answer("Не найдено", show_alert=True)
        return

    await state.update_data(channel_row_id=ch_id)
    await show_type_choose(cb.message, state)
    await cb.answer()


@dp.callback_query(F.data.startswith("pick_ch_"))
async def cb_pick_channel(cb: CallbackQuery, state: FSMContext):
    ch_id = int(cb.data.replace("pick_ch_", ""))
    await state.update_data(channel_row_id=ch_id)
    await show_type_choose(cb.message, state)
    await cb.answer()


async def show_type_choose(message, state):
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="📝 Только текст", callback_data="type_text"))
    kb.row(InlineKeyboardButton(text="🖼 Фото + подпись", callback_data="type_photo"))
    kb.row(InlineKeyboardButton(text="🎬 Видео + подпись", callback_data="type_video"))
    kb.row(InlineKeyboardButton(text="📎 Документ + подпись", callback_data="type_doc"))
    kb.row(InlineKeyboardButton(text="❌ Отмена", callback_data="home"))

    await message.answer(
        "✍️ <b>Создание поста</b>\n\nВыбери тип контента:",
        reply_markup=kb.as_markup(),
    )
    await state.set_state(PostFSM.type)


@dp.callback_query(F.data.startswith("type_"), PostFSM.type)
async def cb_type(cb: CallbackQuery, state: FSMContext):
    t = cb.data.split("_", 1)[1]
    await state.update_data(post_type=t)

    if t == "text":
        await cb.message.answer(
            "📝 Отправь текст поста.\n\n"
            "Можно HTML: <b>жирный</b>, <i>курсив</i>, <a href='...'>ссылка</a>."
        )
        await state.set_state(PostFSM.text)
    else:
        labels = {"photo": "🖼 фото", "video": "🎬 видео", "doc": "📎 документ"}
        await cb.message.answer(f"Отправь {labels[t]}.")
        await state.set_state(PostFSM.media)
    await cb.answer()


@dp.message(PostFSM.text)
async def fsm_text(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Отправь текст сообщением.")
        return
    await state.update_data(text=message.text, media_type=None, media_path=None)
    await show_preview(message, state)


@dp.message(PostFSM.media)
async def fsm_media(message: Message, state: FSMContext):
    data = await state.get_data()
    t = data.get("post_type")

    mtype = mpath = None
    if t == "photo" and message.photo:
        mtype = "photo"
        f = await bot.get_file(message.photo[-1].file_id)
        mpath = str(MEDIA_DIR / f"{message.photo[-1].file_id}.jpg")
        await bot.download_file(f.file_path, mpath)
    elif t == "video" and message.video:
        mtype = "video"
        f = await bot.get_file(message.video.file_id)
        mpath = str(MEDIA_DIR / f"{message.video.file_id}.mp4")
        await bot.download_file(f.file_path, mpath)
    elif t == "doc" and message.document:
        mtype = "document"
        f = await bot.get_file(message.document.file_id)
        mpath = str(MEDIA_DIR / f"{message.document.file_id}_{message.document.file_name}")
        await bot.download_file(f.file_path, mpath)
    else:
        await message.answer("❌ Не то. Отправь нужный тип или /start.")
        return

    caption = message.caption or ""
    await state.update_data(text=caption, media_type=mtype, media_path=mpath)
    await show_preview(message, state)


async def show_preview(message: Message, state: FSMContext):
    data = await state.get_data()
    ch = await db_one("SELECT * FROM channels WHERE id=?", (data.get("channel_row_id"),))
    ch_title = ch["title"] if ch else "—"

    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="🚀 Опубликовать сейчас", callback_data="pub_now"),
        InlineKeyboardButton(text="⏰ По расписанию", callback_data="pub_sched"),
    )
    kb.row(InlineKeyboardButton(text="🔙 Выбрать другой тип", callback_data="new"))
    kb.row(InlineKeyboardButton(text="❌ Отмена", callback_data="home"))

    header = (
        f"👁 <b>Предпросмотр</b>\n"
        f"📡 Канал: <b>{ch_title}</b>\n"
        f"——————\n"
    )
    text_preview = data.get("text") or "<i>без текста</i>"
    footer = "\n——————"

    mtype = data.get("media_type")

    if mtype == "photo":
        await message.answer_photo(FSInputFile(data["media_path"]),
                                   caption=header + text_preview[:800] + footer,
                                   reply_markup=kb.as_markup())
    elif mtype == "video":
        await message.answer_video(FSInputFile(data["media_path"]),
                                   caption=header + text_preview[:800] + footer,
                                   reply_markup=kb.as_markup())
    elif mtype == "document":
        await message.answer_document(FSInputFile(data["media_path"]),
                                      caption=header + text_preview[:800] + footer,
                                      reply_markup=kb.as_markup())
    else:
        await message.answer(header + text_preview + footer, reply_markup=kb.as_markup())

    await state.set_state(PostFSM.preview)


@dp.callback_query(F.data == "pub_now", PostFSM.preview)
async def cb_pub_now(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user = await get_user(cb.from_user.id)
    ch = await db_one("SELECT * FROM channels WHERE id=?", (data.get("channel_row_id"),))

    post_id = await db_exec(
        "INSERT INTO posts (user_id, channel_row_id, channel_id, channel_title, text, media_type, media_path, status, scheduled_at) "
        "VALUES (?,?,?,?,?,?,?, 'scheduled', ?)",
        (user["id"], ch["id"], ch["channel_id"], ch["title"],
         data.get("text"), data.get("media_type"), data.get("media_path"),
         datetime.now().isoformat()),
    )
    await db_exec("UPDATE users SET posts_used=posts_used+1 WHERE id=?", (user["id"],))
    await state.clear()

    await cb.message.answer(f"🚀 Публикую пост #{post_id} в «{ch['title']}»...")
    await check_scheduled()
    await cb.answer()


@dp.callback_query(F.data == "pub_sched", PostFSM.preview)
async def cb_pub_sched(cb: CallbackQuery, state: FSMContext):
    now = datetime.now()
    kb = InlineKeyboardBuilder()
    for label, dt in [
        ("Через 1 час", now + timedelta(hours=1)),
        ("Сегодня в 20:00", now.replace(hour=20, minute=0, second=0, microsecond=0)),
        ("Завтра в 09:00", (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)),
        ("Завтра в 18:00", (now + timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)),
    ]:
        kb.row(InlineKeyboardButton(text=label, callback_data=f"sched_{dt.isoformat()}"))
    kb.row(InlineKeyboardButton(text="✏️ Своё время", callback_data="sched_custom"))
    kb.row(InlineKeyboardButton(text="❌ Отмена", callback_data="home"))

    await cb.message.answer("⏰ <b>Когда публиковать?</b>", reply_markup=kb.as_markup())
    await cb.answer()


@dp.callback_query(F.data.startswith("sched_"))
async def cb_sched(cb: CallbackQuery, state: FSMContext):
    if cb.data == "sched_custom":
        await cb.message.answer(
            "Отправь дату и время:\n<code>ДД.ММ.ГГГГ ЧЧ:ММ</code>\n\n"
            "Пример: <code>15.10.2026 09:00</code>"
        )
        await state.set_state(PostFSM.time)
        await cb.answer()
        return

    iso = cb.data.replace("sched_", "")
    await save_scheduled(cb.message, state, iso)
    await cb.answer()


@dp.message(PostFSM.time)
async def fsm_time(message: Message, state: FSMContext):
    try:
        dt = datetime.strptime(message.text.strip(), "%d.%m.%Y %H:%M")
    except ValueError:
        await message.answer("❌ Неверный формат. Пример: <code>15.10.2026 09:00</code>")
        return

    user = await get_user(message.from_user.id)
    max_days = 365 if pro_active(user) else 30
    if dt > datetime.now() + timedelta(days=max_days):
        await message.answer(f"❌ Максимум на {max_days} дней вперёд.")
        return

    await save_scheduled(message, state, dt.isoformat())


async def save_scheduled(message: Message, state: FSMContext, iso: str):
    data = await state.get_data()
    user = await get_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    ch = await db_one("SELECT * FROM channels WHERE id=?", (data.get("channel_row_id"),))

    post_id = await db_exec(
        "INSERT INTO posts (user_id, channel_row_id, channel_id, channel_title, text, media_type, media_path, scheduled_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (user["id"], ch["id"], ch["channel_id"], ch["title"],
         data.get("text"), data.get("media_type"), data.get("media_path"), iso),
    )
    await db_exec("UPDATE users SET posts_used=posts_used+1 WHERE id=?", (user["id"],))
    await state.clear()

    when = iso[:16].replace("T", " ")
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="📋 Мои посты", callback_data="list"))
    kb.row(InlineKeyboardButton(text="🏠 В меню", callback_data="home"))

    await message.answer(
        f"✅ Пост #{post_id} запланирован\n"
        f"📅 {when}\n"
        f"📡 {ch['title']}",
        reply_markup=kb.as_markup(),
    )


# ═══════════════════════════════════════════════
#   СПИСОК ВСЕХ ПОСТОВ
# ═══════════════════════════════════════════════

@dp.callback_query(F.data == "list")
async def cb_list(cb: CallbackQuery):
    user = await get_user(cb.from_user.id)
    posts = await db_all(
        "SELECT * FROM posts WHERE user_id=? ORDER BY id DESC LIMIT 15",
        (user["id"],)
    )
    if not posts:
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="✍️ Создать пост", callback_data="new"))
        kb.row(InlineKeyboardButton(text="🏠 В меню", callback_data="home"))
        await cb.message.answer("Постов пока нет.", reply_markup=kb.as_markup())
        await cb.answer()
        return

    text = "📋 <b>Мои посты</b> (последние 15)\n\n"
    for p in posts:
        icon = {"scheduled": "⏰", "published": "✅"}.get(p["status"], "❓")
        when = (p["scheduled_at"] or "")[:16].replace("T", " ")
        t = {"photo": "🖼", "video": "🎬", "document": "📎"}.get(p["media_type"], "📝")
        preview = (p["text"] or "[без текста]")[:30]
        ch = (p["channel_title"] or "")[:20]
        text += f"{icon} #{p['id']} {t} · {when}\n   📡 {ch}\n   {preview}\n\n"

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🗑 Удалить", callback_data="del_help"))
    kb.row(InlineKeyboardButton(text="🏠 В меню", callback_data="home"))

    await cb.message.answer(text, reply_markup=kb.as_markup())
    await cb.answer()


@dp.callback_query(F.data == "del_help")
async def cb_del_help(cb: CallbackQuery):
    await cb.message.answer("Отправь: <code>/del 5</code>\n\nгде 5 — номер поста.")
    await cb.answer()


# ═══════════════════════════════════════════════
#   СТАТИСТИКА
# ═══════════════════════════════════════════════

@dp.callback_query(F.data == "stats")
async def cb_stats(cb: CallbackQuery):
    user = await get_user(cb.from_user.id)
    chans = await user_channels(user["id"])
    total = (await db_one("SELECT COUNT(*) c FROM posts WHERE user_id=?", (user["id"],)))["c"]
    pub = (await db_one("SELECT COUNT(*) c FROM posts WHERE user_id=? AND status='published'", (user["id"],)))["c"]
    sched = (await db_one("SELECT COUNT(*) c FROM posts WHERE user_id=? AND status='scheduled'", (user["id"],)))["c"]

    active = pro_active(user)
    if active:
        limit_text = "∞"
        ch_limit = PRO_CHANNEL_LIMIT
        tariff = f"⭐ Premium (осталось {pro_days_left(user)} дн.)"
    else:
        limit_text = str(FREE_LIMIT)
        ch_limit = FREE_CHANNEL_LIMIT
        tariff = "🆓 Free"

    kb = InlineKeyboardBuilder()
    if not active:
        kb.row(InlineKeyboardButton(text="⭐ Улучшить", callback_data="premium"))
    kb.row(InlineKeyboardButton(text="🏠 В меню", callback_data="home"))

    await cb.message.answer(
        f"📊 <b>Статистика</b>\n\n"
        f"📡 Каналов: <b>{len(chans)}/{ch_limit}</b>\n"
        f"📝 Всего постов: {total}\n"
        f"✅ Опубликовано: {pub}\n"
        f"⏰ В очереди: {sched}\n\n"
        f"Тариф: {tariff}\n"
        f"Постов: {user['posts_used']} / {limit_text}",
        reply_markup=kb.as_markup(),
    )
    await cb.answer()


# ═══════════════════════════════════════════════
#   НАСТРОЙКИ
# ═══════════════════════════════════════════════

@dp.callback_query(F.data == "settings")
async def cb_settings(cb: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="📡 Мои каналы", callback_data="my_channels"))
    kb.row(InlineKeyboardButton(text="⭐ Premium", callback_data="premium"))
    kb.row(InlineKeyboardButton(text="🏠 В меню", callback_data="home"))

    await cb.message.answer("⚙️ <b>Настройки</b>", reply_markup=kb.as_markup())
    await cb.answer()


# ═══════════════════════════════════════════════
#   PREMIUM
# ═══════════════════════════════════════════════

@dp.callback_query(F.data == "premium")
async def cb_premium(cb: CallbackQuery):
    user = await get_user(cb.from_user.id)
    active = pro_active(user)

    kb = InlineKeyboardBuilder()
    for key, (days, stars, label) in PLANS.items():
        kb.row(InlineKeyboardButton(
            text=f"⭐ {label} — {stars} звёзд",
            callback_data=f"buy_{key}"
        ))
    kb.row(InlineKeyboardButton(text="🏠 В меню", callback_data="home"))

    if active:
        status = (
            f"✅ <b>Premium активен</b>\n"
            f"📅 До: {datetime.fromisoformat(user['pro_until']).strftime('%d.%m.%Y')}\n"
            f"⏳ Осталось: {pro_days_left(user)} дн.\n\n"
        )
    else:
        status = ""

    await cb.message.answer(
        f"⭐ <b>Premium-подписка</b>\n\n"
        f"{status}"
        f"<b>Что даёт Premium:</b>\n"
        f"• 🚀 Безлимит постов (Free: {FREE_LIMIT}/мес)\n"
        f"• 📡 До {PRO_CHANNEL_LIMIT} каналов (Free: {FREE_CHANNEL_LIMIT})\n"
        f"• 📅 Планирование на год (Free: 30 дней)\n"
        f"• ⚡ Приоритетная публикация\n"
        f"• 💬 Поддержка\n\n"
        f"<b>Выбери срок:</b>",
        reply_markup=kb.as_markup(),
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("buy_"))
async def cb_buy(cb: CallbackQuery):
    key = cb.data.replace("buy_", "")
    if key not in PLANS:
        await cb.answer("Неверный план", show_alert=True)
        return

    days, stars, label = PLANS[key]

    await bot.send_invoice(
        chat_id=cb.from_user.id,
        title=f"Premium — {label}",
        description=f"Безлимит постов, до {PRO_CHANNEL_LIMIT} каналов, планирование на год.",
        payload=f"pro:{key}:{cb.from_user.id}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=f"Premium {label}", amount=stars)],
        start_parameter=f"premium_{key}",
    )
    await cb.answer()


@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    try:
        if q.invoice_payload.startswith("pro:"):
            await q.answer(ok=True)
        else:
            await q.answer(ok=False, error_message="Неизвестный товар")
    except Exception as e:
        print(f"Pre-checkout error: {e}")
        await q.answer(ok=False, error_message="Ошибка")


@dp.message(F.successful_payment)
async def on_successful_payment(message: Message):
    sp = message.successful_payment
    payload = sp.invoice_payload

    if not payload.startswith("pro:"):
        return

    parts = payload.split(":")
    plan_key = parts[1]
    if plan_key not in PLANS:
        return

    days, stars, label = PLANS[plan_key]
    user = await get_user(message.from_user.id, message.from_user.username, message.from_user.first_name)

    now = datetime.now()
    if pro_active(user):
        base = datetime.fromisoformat(user["pro_until"])
        if base < now:
            base = now
    else:
        base = now
    until = base + timedelta(days=days)

    await db_exec("UPDATE users SET is_pro=1, pro_until=? WHERE id=?",
                  (until.isoformat(), user["id"]))
    await db_exec(
        "INSERT INTO payments (user_id, amount, charge_id, days, plan) VALUES (?,?,?,?,?)",
        (user["id"], sp.total_amount, sp.telegram_payment_charge_id, days, plan_key),
    )

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="✍️ Создать пост", callback_data="new"))
    kb.row(InlineKeyboardButton(text="⭐ Моя подписка", callback_data="premium"))
    kb.row(InlineKeyboardButton(text="🏠 В меню", callback_data="home"))

    await message.answer(
        f"🎉 <b>Оплата прошла!</b>\n\n"
        f"⭐ Premium ({label}) активирован\n"
        f"📅 До: <b>{until.strftime('%d.%m.%Y')}</b>\n\n"
        f"Теперь доступно:\n"
        f"• Безлимит постов\n"
        f"• До {PRO_CHANNEL_LIMIT} каналов\n"
        f"• Планирование на год\n\n"
        f"Спасибо! 🚀",
        reply_markup=kb.as_markup(),
    )

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"💰 <b>Оплата Premium</b>\n\n"
                f"{user['first_name'] or '—'} (@{user['username'] or '—'})\n"
                f"ID: <code>{user['tg_id']}</code>\n"
                f"План: <b>{label}</b> · {sp.total_amount} ⭐\n"
                f"До: {until.strftime('%d.%m.%Y')}"
            )
        except Exception:
            pass


# ═══════════════════════════════════════════════
#   ПОМОЩЬ / КОМАНДЫ / ПОДДЕРЖКА
# ═══════════════════════════════════════════════

HELP_TEXT = """📖 <b>Помощь по боту</b>

<b>Что умеет бот:</b>
Публикует посты в твои Telegram-каналы по расписанию. Можно подключить несколько каналов, планировать посты заранее.

<b>━━━ Кнопки меню ━━━</b>

<b>✍️ Создать пост</b>
Выбираешь канал → тип контента → отправляешь → предпросмотр → публикуешь (сразу или по расписанию).

<b>📡 Мои каналы</b>
Список всех подключённых каналов. Нажми на любой — увидишь детали, посты, кнопки управления.

<b>📋 Мои посты</b>
Все твои посты (опубликованные и запланированные).

<b>📊 Статистика</b>
Сколько постов создал, сколько опубликовано, лимиты тарифа.

<b>⭐ Premium</b>
Тарифы подписки. Оформить → платёж через Telegram Stars.

<b>🆘 Поддержка</b>
Напиши вопрос — уйдёт администратору. Ответ придёт сюда же.

<b>⚙️ Настройки</b>
Управление каналами и подпиской.
<b>━━━━━━━━━━━━━━━━━━━</b>
📝 <b>Команды:</b>

/start — главное меню
/help — эта справка
/commands — список команд
/support — написать в поддержку
/cancel — отменить действие
/del 5 — удалить свой пост №5
/delch 3 5 — удалить пост №5 из канала №3"""


COMMANDS_TEXT = """📝 <b>Все команды</b>

<b>━━━ Основные ━━━</b>
/start — главное меню
/help — помощь
/commands — этот список
/support — поддержка
/cancel — отменить действие

<b>━━━ Посты ━━━</b>
/del 5 — удалить свой пост №5
/delch 3 5 — удалить пост №5 из канала №3

<b>━━━ Для админа ━━━</b>
/admin — админ-панель
/adel 15 — удалить любой пост
/reply 12345 текст — ответить юзеру

💡 Номера постов смотри в «📋 Мои посты»."""


@dp.callback_query(F.data == "help")
async def cb_help(cb: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="📝 Все команды", callback_data="commands"))
    kb.row(InlineKeyboardButton(text="🆘 Поддержка", callback_data="support"))
    kb.row(InlineKeyboardButton(text="🏠 В меню", callback_data="home"))
    await cb.message.answer(HELP_TEXT, reply_markup=kb.as_markup())
    await cb.answer()


@dp.callback_query(F.data == "commands")
async def cb_commands(cb: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="↩️ Назад", callback_data="help"))
    kb.row(InlineKeyboardButton(text="🏠 В меню", callback_data="home"))
    await cb.message.answer(COMMANDS_TEXT, reply_markup=kb.as_markup())
    await cb.answer()


@dp.callback_query(F.data == "how_it_works")
async def cb_how(cb: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="➕ Подключить канал", callback_data="setup"))
    kb.row(InlineKeyboardButton(text="🏠 В меню", callback_data="home"))
    await cb.message.answer(
        "📖 <b>Как это работает</b>\n\n"
        "<b>Шаг 1. Добавь бота в канал</b>\n"
        "Открой канал → Управление → Администраторы → Добавить → "
        "найди бота → дай право «Публикация сообщений».\n\n"
        "<b>Шаг 2. Подключи канал</b>\n"
        "Нажми «➕ Подключить канал» и отправь ссылку "
        "(@my_channel или t.me/my_channel).\n\n"
        "<b>Шаг 3. Создай пост</b>\n"
        "Нажми «✍️ Создать пост» → выбери канал → выбери тип "
        "(текст/фото/видео/документ) → отправь контент → предпросмотр → "
        "опубликуй.\n\n"
        "<b>Шаг 4. Планируй</b>\n"
        "При создании поста выбери «⏰ По расписанию» и укажи время. "
        "Бот опубликует сам.\n\n"
        "✅ Всё. Можешь заниматься другими делами, бот работает 24/7.",
        reply_markup=kb.as_markup(),
    )
    await cb.answer()


@dp.message(Command("help"))
async def cmd_help(message: Message):
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="📝 Все команды", callback_data="commands"))
    kb.row(InlineKeyboardButton(text="🆘 Поддержка", callback_data="support"))
    kb.row(InlineKeyboardButton(text="🏠 В меню", callback_data="home"))
    await message.answer(HELP_TEXT, reply_markup=kb.as_markup())


@dp.message(Command("commands"))
async def cmd_commands(message: Message):
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="↩️ Назад", callback_data="help"))
    kb.row(InlineKeyboardButton(text="🏠 В меню", callback_data="home"))
    await message.answer(COMMANDS_TEXT, reply_markup=kb.as_markup())


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    current = await state.get_state()
    if current is None:
        await message.answer("Нечего отменять. /start")
        return
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🏠 В меню", callback_data="home"))
    await message.answer("✅ Действие отменено.", reply_markup=kb.as_markup())


# ─────────── ПОДДЕРЖКА ───────────

@dp.callback_query(F.data == "support")
async def cb_support(cb: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="❌ Отмена", callback_data="home"))
    await cb.message.answer(
        "🆘 <b>Поддержка</b>\n\n"
        "Опиши проблему или задай вопрос одним сообщением.\n\n"
        "<b>Что стоит указать:</b>\n"
        "• Что делал\n"
        "• Что не получилось\n"
        "• Скриншот ошибки (если есть)\n\n"
        "Отвечу как можно скорее 👇",
        reply_markup=kb.as_markup(),
    )
    await state.set_state(SupportFSM.message)
    await cb.answer()


@dp.message(Command("support"))
async def cmd_support(message: Message, state: FSMContext):
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="❌ Отмена", callback_data="home"))
    await message.answer(
        "🆘 <b>Поддержка</b>\n\n"
        "Опиши проблему одним сообщением 👇",
        reply_markup=kb.as_markup(),
    )
    await state.set_state(SupportFSM.message)


@dp.message(SupportFSM.message)
async def support_message(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id, message.from_user.username, message.from_user.first_name)

    text_to_admin = (
        f"🆘 <b>Новое обращение в поддержку</b>\n\n"
        f"От: {user['first_name'] or '—'} (@{user['username'] or '—'})\n"
        f"ID: <code>{user['tg_id']}</code>\n"
        f"Внутренний: <code>#{user['id']}</code>\n\n"
        f"<b>Сообщение:</b>\n{message.text}\n\n"
        f"<b>Ответить:</b> <code>/reply {user['tg_id']} текст</code>"
    )

    sent = 0
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text_to_admin)
            sent += 1
        except Exception:
            pass

    await state.clear()

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🏠 В меню", callback_data="home"))

    if sent:
        await message.answer(
            "✅ <b>Сообщение отправлено</b>\n\nОтвет придёт в этот чат.",
            reply_markup=kb.as_markup(),
        )
    else:
        await message.answer("⚠️ Не удалось отправить. Попробуй позже.", reply_markup=kb.as_markup())


@dp.message(Command("reply"))
async def cmd_reply(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return

    try:
        parts = message.text.split(maxsplit=2)
        target = int(parts[1])
        text = parts[2]
    except Exception:
        await message.answer(
            "Формат: <code>/reply 123456789 текст ответа</code>"
        )
        return

    try:
        await bot.send_message(
            target,
            f"💬 <b>Ответ поддержки</b>\n\n{text}\n\n"
            f"<i>Можешь ответить снова — /support</i>",
        )
        await message.answer(f"✅ Отправлено юзеру <code>{target}</code>")
    except Exception as e:
        await message.answer(f"❌ Не доставлено: <code>{e}</code>")


# ─────────── УДАЛЕНИЕ ───────────

@dp.message(Command("del"))
async def cmd_del(message: Message):
    user = await get_user(message.from_user.id)
    try:
        pid = int(message.text.split()[1])
    except Exception:
        await message.answer("Формат: <code>/del 5</code>")
        return

    p = await db_one("SELECT * FROM posts WHERE id=? AND user_id=?", (pid, user["id"]))
    if not p:
        await message.answer("Пост не найден.")
        return

    await db_exec("DELETE FROM posts WHERE id=?", (pid,))
    await message.answer(f"🗑 Пост #{pid} удалён.")


@dp.message(Command("delch"))
async def cmd_delch(message: Message):
    user = await get_user(message.from_user.id)
    try:
        _, ch_id, pid = message.text.split()
        ch_id, pid = int(ch_id), int(pid)
    except Exception:
        await message.answer("Формат: <code>/delch 3 5</code>")
        return

    p = await db_one(
        "SELECT * FROM posts WHERE id=? AND user_id=? AND channel_row_id=?",
        (pid, user["id"], ch_id)
    )
    if not p:
        await message.answer("Пост не найден.")
        return

    await db_exec("DELETE FROM posts WHERE id=?", (pid,))
    await message.answer(f"🗑 Пост #{pid} удалён.")


# ═══════════════════════════════════════════════
#   АДМИН-ПАНЕЛЬ
# ═══════════════════════════════════════════════

def admin_kb():
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="📊 Дашборд", callback_data="adm_dash"),
        InlineKeyboardButton(text="💰 Доходы", callback_data="adm_income"),
    )
    kb.row(
        InlineKeyboardButton(text="👥 Юзеры", callback_data="adm_users"),
        InlineKeyboardButton(text="⭐ Premium", callback_data="adm_pros"),
    )
    kb.row(
        InlineKeyboardButton(text="📝 Посты", callback_data="adm_posts"),
        InlineKeyboardButton(text="📡 Каналы", callback_data="adm_channels"),
    )
    kb.row(
        InlineKeyboardButton(text="🎁 Выдать Premium", callback_data="adm_grant"),
        InlineKeyboardButton(text="🚫 Бан", callback_data="adm_ban"),
    )
    kb.row(InlineKeyboardButton(text="📢 Рассылка", callback_data="adm_broadcast"))
    kb.row(InlineKeyboardButton(text="🔄 Обновить", callback_data="adm_dash"))
    return kb.as_markup()


@dp.callback_query(F.data == "admin")
async def cb_admin(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    await cb.message.answer("👑 <b>Админ-панель</b>\n\nВыбери раздел:",
                            reply_markup=admin_kb())
    await cb.answer()


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return
    await message.answer("👑 <b>Админ-панель</b>", reply_markup=admin_kb())


@dp.callback_query(F.data == "adm_dash")
async def adm_dash(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    now = datetime.now()
    day_ago = (now - timedelta(days=1)).isoformat()

    users_total = (await db_one("SELECT COUNT(*) c FROM users"))["c"]
    users_pro = (await db_one("SELECT COUNT(*) c FROM users WHERE is_pro=1"))["c"]
    users_banned = (await db_one("SELECT COUNT(*) c FROM users WHERE is_banned=1"))["c"]
    users_today = (await db_one("SELECT COUNT(*) c FROM users WHERE created_at>=?", (day_ago,)))["c"]
    channels = (await db_one("SELECT COUNT(*) c FROM channels"))["c"]

    posts_total = (await db_one("SELECT COUNT(*) c FROM posts"))["c"]
    posts_pub = (await db_one("SELECT COUNT(*) c FROM posts WHERE status='published'"))["c"]
    posts_sched = (await db_one("SELECT COUNT(*) c FROM posts WHERE status='scheduled'"))["c"]
    income_total = (await db_one("SELECT COALESCE(SUM(amount),0) s FROM payments"))["s"]

    text = (
        f"📊 <b>Дашборд</b>\n<i>{now.strftime('%d.%m.%Y %H:%M')}</i>\n\n"
        f"👥 <b>Юзеры</b>\n"
        f"• Всего: {users_total}\n"
        f"• Новых за сутки: {users_today}\n"
        f"• Premium: {users_pro}\n"
        f"• Забанено: {users_banned}\n\n"
        f"📡 <b>Каналы:</b> {channels}\n\n"
        f"📝 <b>Посты</b>\n"
        f"• Всего: {posts_total}\n"
        f"• Опубликовано: {posts_pub}\n"
        f"• В очереди: {posts_sched}\n\n"
        f"💰 Заработано: <b>{income_total} ⭐</b>"
    )
    try:
        await cb.message.edit_text(text, reply_markup=admin_kb())
    except Exception:
        await cb.message.answer(text, reply_markup=admin_kb())
    await cb.answer()


@dp.callback_query(F.data == "adm_income")
async def adm_income(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    now = datetime.now()
    day_ago = (now - timedelta(days=1)).isoformat()
    week_ago = (now - timedelta(days=7)).isoformat()
    month_ago = (now - timedelta(days=30)).isoformat()

    total = (await db_one("SELECT COALESCE(SUM(amount),0) s FROM payments"))["s"]
    today = (await db_one("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE created_at>=?", (day_ago,)))["s"]
    week = (await db_one("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE created_at>=?", (week_ago,)))["s"]
    month = (await db_one("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE created_at>=?", (month_ago,)))["s"]
    count = (await db_one("SELECT COUNT(*) c FROM payments"))["c"]

    last = await db_all(
        "SELECT p.*, u.username, u.first_name FROM payments p "
        "LEFT JOIN users u ON u.id = p.user_id ORDER BY p.id DESC LIMIT 10"
    )

    text = (
        f"💰 <b>Доходы</b>\n\n"
        f"• За сутки: <b>{today} ⭐</b>\n"
        f"• За неделю: <b>{week} ⭐</b>\n"
        f"• За месяц: <b>{month} ⭐</b>\n"
        f"• Всего: <b>{total} ⭐</b>\n"
        f"• Платежей: {count}\n\n"
    )
    if last:
        text += "🕐 <b>Последние:</b>\n"
        for p in last:
            who = f"@{p['username']}" if p["username"] else (p["first_name"] or f"#{p['user_id']}")
            when = (p["created_at"] or "")[:16].replace("T", " ")
            text += f"• {when} · {who} · {p['amount']} ⭐\n"

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🏠 В меню", callback_data="adm_dash"))
    await cb.message.answer(text, reply_markup=kb.as_markup())
    await cb.answer()


@dp.callback_query(F.data == "adm_pros")
async def adm_pros(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    users = await db_all("SELECT * FROM users WHERE is_pro=1 ORDER BY pro_until ASC")
    if not users:
        await cb.message.answer("Premium-юзеров нет.")
        await cb.answer()
        return
    text = f"⭐ <b>Premium-юзеры</b> ({len(users)})\n\n"
    for u in users:
        until = (u["pro_until"] or "")[:10]
        uname = f"@{u['username']}" if u["username"] else (u["first_name"] or f"#{u['id']}")
        text += f"• {uname} — до {until} ({pro_days_left(u)} дн.)\n"
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🏠 В меню", callback_data="adm_dash"))
    await cb.message.answer(text, reply_markup=kb.as_markup())
    await cb.answer()


@dp.callback_query(F.data == "adm_users")
async def adm_users(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    users = await db_all("SELECT * FROM users ORDER BY last_seen DESC LIMIT 30")
    if not users:
        await cb.message.answer("Юзеров нет.")
        await cb.answer()
        return
    text = "👥 <b>Последние 30 юзеров</b>\n\n"
    for u in users:
        badge = []
        if pro_active(u): badge.append("⭐")
        elif u["is_pro"]: badge.append("⌛")
        if u["is_banned"]: badge.append("🚫")
        mark = " ".join(badge)
        uname = f"@{u['username']}" if u["username"] else (u["first_name"] or "—")
        text += f"#{u['id']} · {uname} {mark}\n"
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🔍 Найти юзера", callback_data="adm_find"))
    kb.row(InlineKeyboardButton(text="🏠 В меню", callback_data="adm_dash"))
    await cb.message.answer(text, reply_markup=kb.as_markup())
    await cb.answer()


@dp.callback_query(F.data == "adm_find")
async def adm_find(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    await cb.message.answer("Отправь @username, tg_id или #id.")
    await state.set_state(AdminFSM.find_user)
    await cb.answer()


@dp.message(AdminFSM.find_user)
async def adm_find_process(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    q = message.text.strip()
    if q.startswith("#"):
        user = await db_one("SELECT * FROM users WHERE id=?", (int(q[1:]),))
    elif q.startswith("@"):
        user = await db_one("SELECT * FROM users WHERE username=?", (q[1:],))
    else:
        try:
            user = await db_one("SELECT * FROM users WHERE tg_id=?", (int(q),))
        except ValueError:
            user = None
    if not user:
        await message.answer("❌ Не нашёл.")
        await state.clear()
        return

    chans = await user_channels(user["id"])
    posts_total = (await db_one("SELECT COUNT(*) c FROM posts WHERE user_id=?", (user["id"],)))["c"]
    posts_pub = (await db_one("SELECT COUNT(*) c FROM posts WHERE user_id=? AND status='published'", (user["id"],)))["c"]
    paid = (await db_one("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE user_id=?", (user["id"],)))["s"]

    if pro_active(user):
        pro_line = f"⭐ до {(user['pro_until'] or '')[:10]} ({pro_days_left(user)} дн.)"
    elif user["is_pro"]:
        pro_line = "⌛ истёк"
    else:
        pro_line = "🆓 Free"

    text = (
        f"👤 <b>Карточка юзера</b>\n\n"
        f"ID: <code>{user['id']}</code>\n"
        f"TG: <code>{user['tg_id']}</code>\n"
        f"@{user['username'] or '—'}\n"
        f"Имя: {user['first_name'] or '—'}\n\n"
        f"Подписка: {pro_line}\n"
        f"Оплачено: {paid} ⭐\n"
        f"Забанен: {'🚫 Да' if user['is_banned'] else '✅ Нет'}\n"
        f"Каналов: {len(chans)}\n"
        f"Постов: {posts_total} (опубл. {posts_pub})\n"
        f"Создан: {(user['created_at'] or '')[:16]}\n"
        f"Активность: {(user['last_seen'] or '')[:16]}"
    )

    if chans:
        text += "\n\n📡 <b>Каналы:</b>\n"
        for ch in chans:
            un = f"@{ch['username']}" if ch["username"] else "—"
            text += f"• {ch['title']} ({un})\n"

    kb = InlineKeyboardBuilder()
    if user["is_pro"]:
        kb.row(InlineKeyboardButton(text="❌ Забрать Premium", callback_data=f"adm_revoke_{user['id']}"))
    else:
        kb.row(InlineKeyboardButton(text="⭐ Выдать Premium", callback_data=f"adm_pro_{user['id']}"))
    if user["is_banned"]:
        kb.row(InlineKeyboardButton(text="✅ Разбанить", callback_data=f"adm_unban_{user['id']}"))
    else:
        kb.row(InlineKeyboardButton(text="🚫 Забанить", callback_data=f"adm_do_ban_{user['id']}"))
    kb.row(InlineKeyboardButton(text="✉️ Написать", callback_data=f"adm_write_{user['id']}"))
    kb.row(InlineKeyboardButton(text="🏠 В меню", callback_data="adm_dash"))

    await message.answer(text, reply_markup=kb.as_markup())
    await state.clear()


@dp.callback_query(F.data.startswith("adm_write_"))
async def adm_write(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    uid = int(cb.data.split("_")[2])
    await state.update_data(target_uid=uid)
    await cb.message.answer("Отправь текст сообщения для юзера.")
    await state.set_state(AdminFSM.reply_user)
    await cb.answer()


@dp.message(AdminFSM.reply_user)
async def adm_write_process(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    uid = data.get("target_uid")
    user = await db_one("SELECT * FROM users WHERE id=?", (uid,))
    if not user:
        await message.answer("Не нашёл.")
        await state.clear()
        return
    try:
        await bot.send_message(
            user["tg_id"],
            f"💬 <b>Сообщение от админа</b>\n\n{message.text}\n\n"
            f"<i>Ответить — /support</i>"
        )
        await message.answer("✅ Отправлено")
    except Exception as e:
        await message.answer(f"❌ Ошибка: <code>{e}</code>")
    await state.clear()


@dp.callback_query(F.data.startswith("adm_pro_"))
async def adm_pro(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    uid = int(cb.data.split("_")[2])
    user = await db_one("SELECT * FROM users WHERE id=?", (uid,))
    until = (datetime.now() + timedelta(days=30)).isoformat()
    await db_exec("UPDATE users SET is_pro=1, pro_until=? WHERE id=?", (until, uid))
    await cb.answer("Premium выдан", show_alert=True)
    try:
        await bot.send_message(user["tg_id"], "🎁 Тебе активирован Premium на 30 дней!")
    except Exception:
        pass


@dp.callback_query(F.data.startswith("adm_revoke_"))
async def adm_revoke(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    uid = int(cb.data.split("_")[2])
    await db_exec("UPDATE users SET is_pro=0, pro_until=NULL WHERE id=?", (uid,))
    await cb.answer("Premium забран", show_alert=True)


@dp.callback_query(F.data.startswith("adm_do_ban_"))
async def adm_ban_do(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    uid = int(cb.data.split("_")[3])
    user = await db_one("SELECT * FROM users WHERE id=?", (uid,))
    await db_exec("UPDATE users SET is_banned=1 WHERE id=?", (uid,))
    await cb.answer("Забанен", show_alert=True)
    try:
        await bot.send_message(user["tg_id"], "🚫 Ты заблокирован.")
    except Exception:
        pass


@dp.callback_query(F.data.startswith("adm_unban_"))
async def adm_unban(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    uid = int(cb.data.split("_")[2])
    user = await db_one("SELECT * FROM users WHERE id=?", (uid,))
    await db_exec("UPDATE users SET is_banned=0 WHERE id=?", (uid,))
    await cb.answer("Разбанен", show_alert=True)
    try:
        await bot.send_message(user["tg_id"], "✅ Ты разбанен.")
    except Exception:
        pass


@dp.callback_query(F.data == "adm_posts")
async def adm_posts(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    posts = await db_all(
        "SELECT p.*, u.username, u.first_name FROM posts p "
        "LEFT JOIN users u ON u.id = p.user_id ORDER BY p.id DESC LIMIT 20"
    )
    if not posts:
        await cb.message.answer("Постов нет.")
        await cb.answer()
        return
    text = "📝 <b>Последние 20 постов</b>\n\n"
    for p in posts:
        icon = {"scheduled": "⏰", "published": "✅"}.get(p["status"], "❓")
        who = f"@{p['username']}" if p["username"] else (p["first_name"] or f"#{p['user_id']}")
        when = (p["scheduled_at"] or "")[:16].replace("T", " ")
        ch = (p["channel_title"] or "")[:20]
        text += f"{icon} #{p['id']} · {who} · {when}\n   📡 {ch}\n\n"
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🗑 Удалить", callback_data="adm_del_post_help"))
    kb.row(InlineKeyboardButton(text="🏠 В меню", callback_data="adm_dash"))
    await cb.message.answer(text, reply_markup=kb.as_markup())
    await cb.answer()


@dp.callback_query(F.data == "adm_del_post_help")
async def adm_del_post_help(cb: CallbackQuery):
    await cb.message.answer("Команда: <code>/adel 15</code>")
    await cb.answer()


@dp.message(Command("adel"))
async def adm_del_post(message: Message):
    if not is_admin(message.from_user.id):
        return
    try:
        pid = int(message.text.split()[1])
    except Exception:
        await message.answer("Формат: <code>/adel 15</code>")
        return
    await db_exec("DELETE FROM posts WHERE id=?", (pid,))
    await message.answer(f"🗑 Пост #{pid} удалён.")


@dp.callback_query(F.data == "adm_channels")
async def adm_channels(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    chans = await db_all(
        "SELECT c.*, u.username, u.first_name FROM channels c "
        "LEFT JOIN users u ON u.id = c.user_id ORDER BY c.id DESC LIMIT 50"
    )
    if not chans:
        await cb.message.answer("Каналов нет.")
        await cb.answer()
        return
    text = f"📡 <b>Все каналы</b> ({len(chans)})\n\n"
    for ch in chans:
        owner = f"@{ch['username']}" if ch["username"] else (ch["first_name"] or f"#{ch['user_id']}")
        text += f"• <b>{ch['title']}</b>\n  @{ch['channel_username'] if 'channel_username' in ch.keys() else (ch['username'] or '—')}\n  владелец: {owner}\n\n"
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🏠 В меню", callback_data="adm_dash"))
    await cb.message.answer(text, reply_markup=kb.as_markup())
    await cb.answer()


@dp.callback_query(F.data == "adm_grant")
async def adm_grant(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    await cb.message.answer("Отправь ID юзера для выдачи Premium на 30 дней.")
    await state.set_state(AdminFSM.grant_pro)
    await cb.answer()


@dp.message(AdminFSM.grant_pro)
async def adm_grant_process(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    q = message.text.strip()
    if q.startswith("#"):
        user = await db_one("SELECT * FROM users WHERE id=?", (int(q[1:]),))
    else:
        try:
            user = await db_one("SELECT * FROM users WHERE tg_id=?", (int(q),))
        except ValueError:
            user = None
    if not user:
        await message.answer("❌ Не нашёл.")
        await state.clear()
        return
    until = (datetime.now() + timedelta(days=30)).isoformat()
    await db_exec("UPDATE users SET is_pro=1, pro_until=? WHERE id=?", (until, user["id"]))
    await message.answer(f"✅ Premium выдан #{user['id']}")
    try:
        await bot.send_message(user["tg_id"], "🎁 Тебе активирован Premium на 30 дней!")
    except Exception:
        pass
    await state.clear()


@dp.callback_query(F.data == "adm_ban")
async def adm_ban_menu(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    await cb.message.answer("Отправь ID юзера для бана/разбана.")
    await state.set_state(AdminFSM.ban_user)
    await cb.answer()


@dp.message(AdminFSM.ban_user)
async def adm_ban_process(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    q = message.text.strip()
    if q.startswith("#"):
        user = await db_one("SELECT * FROM users WHERE id=?", (int(q[1:]),))
    else:
        try:
            user = await db_one("SELECT * FROM users WHERE tg_id=?", (int(q),))
        except ValueError:
            user = None
    if not user:
        await message.answer("❌ Не нашёл.")
        await state.clear()
        return
    new_status = 0 if user["is_banned"] else 1
    await db_exec("UPDATE users SET is_banned=? WHERE id=?", (new_status, user["id"]))
    await message.answer(f"{'🚫 Забанен' if new_status else '✅ Разбанен'}: #{user['id']}")
    await state.clear()


@dp.callback_query(F.data == "adm_broadcast")
async def adm_broadcast(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    total = (await db_one("SELECT COUNT(*) c FROM users"))["c"]
    await cb.message.answer(f"📢 Рассылка\n\nПолучателей: {total}\n\nОтправь текст.")
    await state.set_state(AdminFSM.broadcast)
    await cb.answer()


@dp.message(AdminFSM.broadcast)
async def adm_broadcast_process(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    users = await db_all("SELECT tg_id FROM users WHERE is_banned=0")
    ok = fail = 0
    status = await message.answer(f"📢 Отправляю... 0/{len(users)}")
    for i, u in enumerate(users, 1):
        try:
            await bot.send_message(u["tg_id"], message.text)
            ok += 1
        except Exception:
            fail += 1
        if i % 10 == 0:
            try:
                await status.edit_text(f"📢 Отправляю... {i}/{len(users)}")
            except Exception:
                pass
        await asyncio.sleep(0.05)
    await status.edit_text(f"✅ Готово\n\nДоставлено: {ok}\nОшибок: {fail}")
    await state.clear()


# ═══════════════════════════════════════════════
#   ЗАПУСК
# ═══════════════════════════════════════════════

async def main():
    await init_db()
    scheduler.add_job(check_scheduled, "interval", seconds=30)
    scheduler.add_job(check_pro_expiry, "cron", hour=10, minute=0)
    scheduler.start()
    print("🤖 Автопостер запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())