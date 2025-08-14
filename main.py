import os, re, asyncio, logging, math
from datetime import datetime, date, time, timedelta, timezone
from dateutil import tz
import pytz
import asyncpg
import httpx

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

# ---------- ENV ----------
BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS","").split(",") if x.strip()}
DEFAULT_TIME = os.getenv("DEFAULT_TIME","08:00")
DEFAULT_TZ = os.getenv("DEFAULT_TZ","Atlantic/Madeira")
DESIRED_RETENTION = float(os.getenv("DESIRED_RETENTION","0.9"))  # 0..1
AUTO_ACTIVATE_NEW_DECKS = os.getenv("AUTO_ACTIVATE_NEW_DECKS","false").lower() == "true"

# ---------- GLOBALS ----------
logging.basicConfig(level=logging.INFO)
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
_pool: asyncpg.pool.Pool | None = None

QUIZLET_RE = re.compile(r"https?://(www\.)?quizlet\.com/[^\s]+", re.I)

# ---------- DB ----------
async def pool() -> asyncpg.pool.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    return _pool

# ---------- HELPERS ----------
def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def parse_hhmm(s: str) -> time:
    hh, mm = s.split(":")
    return time(hour=int(hh), minute=int(mm))

def today_in_tz(tzname: str) -> date:
    tzinfo = pytz.timezone(tzname)
    return datetime.now(tzinfo).date()

def now_in_tz(tzname: str) -> datetime:
    tzinfo = pytz.timezone(tzname)
    return datetime.now(tzinfo)

def markdown_escape(text: str) -> str:
    # Простая экранизация Markdown для названий
    return text.replace("_","\\_").replace("*","\\*").replace("[","\\[")

# ---------- FSRS (упрощённый, на уровне сетов) ----------
# Модель: на каждый (user, deck) храним difficulty D (0..1, ниже=проще) и stability S (в днях).
# На клики:
#   Worked:   S *= (1 + 0.7*(1-D)); D = max(0.05, D - 0.05)
#   A bit:    S *= 1.05;             D = min(0.95, D + 0.02)
#   Didn’t:   S *= 0.75;             D = min(0.98, D + 0.05)
# Новое due выбираем так, чтобы Retrievability R ~= target к дате due: R = exp(-t / S_eff).
# S_eff = S * (1 + 0.6*(1-D)). Интервал t = ceil(-S_eff * ln(target)).
# Это простой и стабильный приближённый вариант под идею FSRS.
def fsrs_update_and_next(D: float, S: float, action: str, target: float) -> tuple[float,float,int]:
    if action == "worked":
        S = S * (1 + 0.7 * (1 - D))
        D = max(0.05, D - 0.05)
    elif action == "abit":
        S = S * 1.05
        D = min(0.95, D + 0.02)
    else:  # "didnt"
        S = S * 0.75
        D = min(0.98, D + 0.05)

    S_eff = S * (1 + 0.6*(1 - D))
    interval = max(1, math.ceil(-S_eff * math.log(target)))
    return D, S, interval

# ---------- QUIZLET SCRAPE (только заголовок) ----------
async def fetch_quizlet_title(url: str) -> str | None:
    # Заголовок берём из <title> ... flashcards | Quizlet</title>
    # Если не получится достать, используем хвост URL как title
    try:
        async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent":"TelegramBot/1.0"}) as client:
            r = await client.get(url)
            r.raise_for_status()
            m = re.search(r"<title>(.*?)</title>", r.text, re.I|re.S)
            if m:
                title = re.sub(r"\s*\|\s*Quizlet\s*$","",m.group(1)).strip()
                return title[:120]
    except Exception:
        pass
    tail = url.rstrip("/").split("/")[-1]
    title = tail.replace("-", " ").title() if tail else "Quizlet Set"
    return title[:120]

async def guess_next_unit(conn: asyncpg.Connection) -> str:
    # ищем максимальный u-N и прибавляем 1
    row = await conn.fetchrow("select unit from decks order by id desc limit 1")
    if not row:
        return "u-1"
    m = re.search(r"u-(\d+)", row["unit"] or "", re.I)
    if m:
        return f"u-{int(m.group(1))+1}"
    # fallback
    return f"u-{row['unit']}_{int(datetime.now().timestamp())}"

# ---------- KEYBOARDS ----------
def feedback_kb(unit: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Worked", callback_data=f"fb:{unit}:worked"),
        InlineKeyboardButton(text="A bit",  callback_data=f"fb:{unit}:abit"),
        InlineKeyboardButton(text="Didn’t", callback_data=f"fb:{unit}:didnt"),
    ]])

# ---------- COMMANDS ----------
@dp.message(Command("start"))
async def cmd_start(m: Message):
    # Русский для тебя: бот онбордит ученика, но хранит locale=en
    p = await pool()
    async with p.acquire() as conn:
        await conn.execute("""
        insert into users(telegram_id, tz, send_time, locale)
        values($1, $2, $3, 'en')
        on conflict (telegram_id) do nothing
        """, m.from_user.id, DEFAULT_TZ, DEFAULT_TIME)
    await m.answer(
        "Hi! I’ll remind you to review your Quizlet decks daily.\n"
        "Commands: /daily 08:00, /decks, /today, /stats\n"
        "Admins can paste a Quizlet link to add a deck."
    )

@dp.message(Command("daily"))
async def cmd_daily(m: Message, command: CommandObject):
    # /daily 08:30
    try:
        t = command.args.strip()
        _ = parse_hhmm(t)
        p = await pool()
        async with p.acquire() as conn:
            await conn.execute("update users set send_time=$1 where telegram_id=$2", t, m.from_user.id)
        await m.answer(f"Daily reminder time set to {t}.")
    except Exception:
        await m.answer("Usage: /daily 08:00")

@dp.message(Command("decks"))
async def cmd_decks(m: Message):
    p = await pool()
    async with p.acquire() as conn:
        rows = await conn.fetch("""
        select d.unit, d.title, d.quizlet_url, d.archived
        from decks d
        order by d.unit
        """)
    if not rows:
        return await m.answer("No decks yet.")
    lines = [f"• {r['unit']} — {r['title']} {'(archived)' if r['archived'] else ''}" for r in rows]
    await m.answer("\n".join(lines))

@dp.message(Command("assignall"))
async def cmd_assignall(m: Message, command: CommandObject):
    # /assignall u-4 on|off (admin only)
    if not is_admin(m.from_user.id):
        return await m.answer("Admins only.")
    try:
        unit, state = [x.strip() for x in command.args.split()]
        on = state.lower() == "on"
    except Exception:
        return await m.answer("Usage: /assignall u-4 on|off")

    p = await pool()
    async with p.acquire() as conn:
        deck = await conn.fetchrow("select id from decks where unit=$1", unit)
        if not deck:
            return await m.answer("Deck not found.")
        users = await conn.fetch("select telegram_id from users")
        for u in users:
            # upsert user_decks
            await conn.execute("""
            insert into user_decks(user_id, deck_id, active, next_due)
            values($1, $2, $3, current_date)
            on conflict (user_id, deck_id) do update set active=$3
            """, u["telegram_id"], deck["id"], on)
    await m.answer(f"{unit}: {'activated' if on else 'deactivated'} for all.")

@dp.message(Command("assign"))
async def cmd_assign(m: Message, command: CommandObject):
    # /assign @username u-4 on|off (admin only)
    if not is_admin(m.from_user.id):
        return await m.answer("Admins only.")
    try:
        username, unit, state = [x.strip() for x in command.args.split()]
        on = state.lower() == "on"
    except Exception:
        return await m.answer("Usage: /assign @username u-4 on|off")

    # здесь для простоты ожидаем, что пользователь уже /start нажимал (есть в users)
    # можно расширить: хранить mapping username->telegram_id
    await m.answer("This sample expects the user to have /start'ed first. If not, ask them to start.")

@dp.message(Command("bumpdeck"))
async def cmd_bumpdeck(m: Message, command: CommandObject):
    # /bumpdeck u-4  (admin) — сдвинуть next_due=завтра всем активным
    if not is_admin(m.from_user.id):
        return await m.answer("Admins only.")
    unit = (command.args or "").strip()
    if not unit:
        return await m.answer("Usage: /bumpdeck u-4")
    p = await pool()
    async with p.acquire() as conn:
        deck = await conn.fetchrow("select id from decks where unit=$1", unit)
        if not deck:
            return await m.answer("Deck not found.")
        await conn.execute("""
        update user_decks set next_due=current_date + 1
        where deck_id=$1 and active=true
        """, deck["id"])
    await m.answer(f"{unit}: next_due set to tomorrow for all active users.")

@dp.message(Command("today"))
async def cmd_today(m: Message):
    # Покажем ближайший due‑сет для пользователя
    p = await pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow("""
        select d.unit, d.title, d.quizlet_url
        from user_decks ud
        join decks d on d.id=ud.deck_id
        where ud.user_id=$1 and ud.active=true and d.archived=false
          and (ud.next_due is null or ud.next_due <= current_date)
        order by ud.next_due nulls first, d.unit
        limit 1
        """, m.from_user.id)
    if not row:
        return await m.answer("Nothing due today. See you tomorrow!")
    text = (
        f"⏰ Time to review: *{markdown_escape(row['unit'])} — {markdown_escape(row['title'])}*\n"
        f"🔗 Open set: {row['quizlet_url']}"
    )
    await m.answer(text, reply_markup=feedback_kb(row["unit"]), parse_mode="MarkdownV2")

@dp.message(Command("stats"))
async def cmd_stats(m: Message):
    p = await pool()
    async with p.acquire() as conn:
        counts = await conn.fetch("""
        select action, count(*) c from events where user_id=$1
        and ts > now() - interval '30 days'
        group by action
        """, m.from_user.id)
        ud = await conn.fetch("""
        select d.unit, d.title, ud.next_due
        from user_decks ud join decks d on d.id=ud.deck_id
        where ud.user_id=$1 and ud.active=true
        order by ud.next_due nulls last, d.unit
        """, m.from_user.id)
    parts = ["Last 30 days:"]
    mapp = {r["action"]: r["c"] for r in counts}
    parts.append(f"Worked: {mapp.get('worked',0)} | A bit: {mapp.get('abit',0)} | Didn’t: {mapp.get('didnt',0)}")
    parts.append("\nYour decks:")
    for r in ud:
        when = "today" if (r["next_due"] and r["next_due"] <= date.today()) else (f"in {(r['next_due']-date.today()).days} d" if r["next_due"] else "not scheduled")
        parts.append(f"• {r['unit']} — {r['title']} — {when}")
    await m.answer("\n".join(parts))

# ---------- FEEDBACK HANDLERS ----------
@dp.callback_query(F.data.startswith("fb:"))
async def on_feedback(c: CallbackQuery):
    _, unit, action = c.data.split(":")
    p = await pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow("""
        select ud.difficulty, ud.stability, d.id as deck_id
        from user_decks ud
        join decks d on d.id=ud.deck_id
        where ud.user_id=$1 and d.unit=$2 and ud.active=true and d.archived=false
        """, c.from_user.id, unit)
        if not row:
            return await c.answer("Deck not found or inactive.", show_alert=True)

        D, S = float(row["difficulty"]), float(row["stability"])
        D2, S2, days = fsrs_update_and_next(D, S, action, DESIRED_RETENTION)
        next_due = date.today() + timedelta(days=days)

        await conn.execute("""
        update user_decks set difficulty=$1, stability=$2, next_due=$3
        where user_id=$4 and deck_id=$5
        """, D2, S2, next_due, c.from_user.id, row["deck_id"])
        await conn.execute("""
        insert into events(user_id, deck_id, action) values ($1,$2,$3)
        """, c.from_user.id, row["deck_id"], action)

    msg = {
        "worked": f"Great job! I’ll remind you again in {days} day(s).",
        "abit":   "Nice micro‑session. I’ll ping you tomorrow.",
        "didnt":  "No worries. I’ll ping you tomorrow.",
    }[action]
    await c.message.edit_text(c.message.text + f"\n\n✅ {msg}")
    await c.answer()

# ---------- QUIZLET URL DROP (автодобавление) ----------
@dp.message(F.text.regexp(QUIZLET_RE))
async def on_quizlet_link(m: Message):
    # Только админы могут добавлять новые сеты через голую ссылку
    if not is_admin(m.from_user.id):
        return await m.answer("Please ask your teacher to add decks.")
    url = QUIZLET_RE.search(m.text).group(0)
    title = await fetch_quizlet_title(url)

    p = await pool()
    async with p.acquire() as conn:
        unit = await guess_next_unit(conn)
        deck = await conn.fetchrow("select id from decks where quizlet_url=$1", url)
        if deck:
            return await m.answer("This Quizlet link already exists in the bot.")
        await conn.execute("""
        insert into decks(unit, title, quizlet_url) values($1,$2,$3)
        """, unit, title, url)
        # автоактивация для всех студентов (если включена)
        if AUTO_ACTIVATE_NEW_DECKS:
            users = await conn.fetch("select telegram_id from users")
            for u in users:
                await conn.execute("""
                insert into user_decks(user_id, deck_id, next_due)
                select $1, d.id, current_date from decks d where d.unit=$2
                on conflict (user_id, deck_id) do nothing
                """, u["telegram_id"], unit)

    await m.answer(f"Added: {unit} — {title}\nAuto‑activate: {'ON' if AUTO_ACTIVATE_NEW_DECKS else 'OFF'}")

# ---------- DAILY JOB ----------
async def send_daily_for_all():
    p = await pool()
    async with p.acquire() as conn:
        users = await conn.fetch("select telegram_id, tz, send_time from users")
        for u in users:
            try:
                # Проверяем локальное время пользователя — с точностью до минуты
                tzinfo = pytz.timezone(u["tz"])
                now_local = datetime.now(tzinfo)
                hhmm = now_local.strftime("%H:%M")
                if hhmm != u["send_time"]:
                    continue
                # Выбираем один due‑сет
                row = await conn.fetchrow("""
                select d.unit, d.title, d.quizlet_url
                from user_decks ud
                join decks d on d.id=ud.deck_id
                where ud.user_id=$1 and ud.active=true and d.archived=false
                  and (ud.next_due is null or ud.next_due <= current_date)
                order by ud.next_due nulls first, d.unit
                limit 1
                """, u["telegram_id"])
                if not row:
                    continue
                text = (
                    f"⏰ Time to review: *{markdown_escape(row['unit'])} — {markdown_escape(row['title'])}*\n"
                    f"🔗 Open set: {row['quizlet_url']}"
                )
                await bot.send_message(u["telegram_id"], text, reply_markup=feedback_kb(row["unit"]), parse_mode="MarkdownV2")
            except Exception as e:
                logging.exception(f"send_daily_for_all error for {u['telegram_id']}: {e}")

# Примитивный планировщик: проверяем каждую минуту.
async def scheduler_loop():
    while True:
        try:
            await send_daily_for_all()
        except Exception as e:
            logging.exception(f"scheduler_loop error: {e}")
        await asyncio.sleep(60)

# ---------- ENTRY ----------
async def main():
    p = await pool()  # инициализация пула
    # Запускаем поллинг и планировщик параллельно
    await asyncio.gather(
        dp.start_polling(bot),
        scheduler_loop()
    )

if __name__ == "__main__":
    asyncio.run(main())
