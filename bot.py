import csv
import html
import io
import logging
import os
import re
import zipfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

try:  # локальный запуск: читаем .env, если он есть
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
from io import BytesIO
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Kb
from telegram import ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

try:  # кнопка «Скопировать» (нужен python-telegram-bot >= 22)
    from telegram import CopyTextButton
except ImportError:  # pragma: no cover
    CopyTextButton = None

import db
from core import (
    CATEGORIES, DATE_RE, MONTHS, cat_label, days_text, days_until, find_category,
    first_name, month_grid, next_age, parse_bulk, parse_date, parse_time, shift_month,
    table, years_text, has_birth_year, birth_str,
)
from greetings import STYLES, make_greeting

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("birthday-bot")

TOKEN = os.environ["BOT_TOKEN"]            # токен от @BotFather
DATABASE_URL = os.environ["DATABASE_URL"]  # postgresql://user:pass@host/db
TZ = ZoneInfo(os.getenv("TZ_NAME", "Asia/Almaty"))
LEGACY_JSON = os.getenv("LEGACY_JSON", "birthdays.json")

PAGE = 8
REMIND_OPTIONS = [30, 14, 7, 3, 1, 0]
TIME_OPTIONS = ["08:00", "09:00", "12:00", "18:00", "21:00"]

BTN_ADD, BTN_LIST = "➕ Добавить", "📋 Мои дни рождения"
BTN_UP, BTN_SEARCH = "📅 Ближайшие", "🔍 Поиск"
BTN_CAL, BTN_STATS = "🗓 Календарь", "📊 Статистика"
BTN_CATS, BTN_SET = "👥 Категории", "⚙️ Настройки"
BTN_TODAY, BTN_EXPORT = "🎉 Сегодня", "📤 Экспорт"

MENU = ReplyKeyboardMarkup(
    [[BTN_ADD, BTN_LIST], [BTN_UP, BTN_SEARCH], [BTN_CAL, BTN_STATS], [BTN_CATS, BTN_SET],
     [BTN_TODAY, BTN_EXPORT]],
    resize_keyboard=True,
    is_persistent=True,
)

WELCOME = (
    "👋 <b>Добро пожаловать в Birthday Manager!</b>\n\n"
    "Я помогу не забыть про дни рождения близких: напомню заранее, "
    "посчитаю, сколько человеку исполнится, и подготовлю готовое поздравление 🎁\n\n"
    "<b>Что я умею</b>\n"
    "➕ добавлять людей (по одному или списком)\n"
    "📋 показывать все дни рождения таблицей\n"
    "📅 подсказывать, кто ближайший\n"
    "🔍 искать, ✏️ редактировать, 🗑 удалять\n"
    "🗓 календарь, 📊 статистика, 👥 категории\n"
    "⚙️ напоминания — за сколько дней и в какое время\n"
    "📤 экспорт списка в CSV\n"
    "📝 заметки для каждого человека\n"
    "✍️ просто напиши имя — проверю, есть ли человек в базе\n"
    "📥 пришли файл Google Календаря или Контактов (/import) — перенесу дни рождения\n\n"
    "Чтобы начать, нажми <b>➕ Добавить</b> или просто отправь дату "
    "в формате <code>ДД.ММ.ГГГГ</code> или <code>ДД.ММ</code>, например <code>18.01.2007</code>."
)


# ---------- утилиты ----------
def esc(s: str) -> str:
    return html.escape(s)


def today_() -> "date":
    return datetime.now(TZ).date()


async def reply(update: Update, text: str, kb=None):
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=kb
    )


async def show(update: Update, text: str, kb=None):
    """Из inline-кнопки редактируем сообщение, иначе шлём новое."""
    q = update.callback_query
    if q:
        try:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
    await reply(update, text, kb)


def by_upcoming(people, today):
    return sorted(people, key=lambda p: (days_until(p["birth"], today), p["name"].casefold()))


def person_btn_label(p) -> str:
    return f"{CATEGORIES.get(p['category'], CATEGORIES['other'])[0]} {p['name']} · {birth_str(p)}"


def picker_kb(people, action: str, page: int = 0):
    people = sorted(people, key=lambda p: p["name"].casefold())
    chunk = people[page * PAGE:(page + 1) * PAGE]
    rows = [[Btn(person_btn_label(p), callback_data=f"pk:{action}:{p['id']}")] for p in chunk]
    nav = []
    if page > 0:
        nav.append(Btn("⬅️", callback_data=f"pg:{action}:{page - 1}"))
    if (page + 1) * PAGE < len(people):
        nav.append(Btn("➡️", callback_data=f"pg:{action}:{page + 1}"))
    if nav:
        rows.append(nav)
    return Kb(rows)


def category_kb(prefix: str):
    keys = list(CATEGORIES)
    rows = [[Btn(cat_label(k), callback_data=f"{prefix}:{k}")] for k in keys]
    return Kb(rows)


def card_text(p, today) -> str:
    n = days_until(p["birth"], today)
    has_year = p.get("has_year", True)
    age = next_age(p["birth"], today)
    title = first_name(p["name"]).upper()
    pad = max(0, (22 - len(title) - 3) // 2)
    box = (
        "╔" + "═" * 22 + "╗\n"
        + " " * pad + "🎂 " + esc(title) + "\n"
        + "╚" + "═" * 22 + "╝"
    )
    return (
        f"<pre>{box}</pre>\n"
        f"👤 {esc(p['name'])}\n"
        f"📅 {birth_str(p)}" + ("\n" if has_year else " (год неизвестен)\n") +
        (f"🎈 Исполняется: {years_text(age)}\n" if has_year else "🎈 Возраст не рассчитывается\n") +
        f"{cat_label(p['category'])}\n"
        + (f"📝 {esc(p['note'])}\n" if p.get("note") else "") +
        f"\n⏳ {days_text(n).capitalize()}"
    )


def card_kb(pid: int):
    return Kb([
        [Btn("🎉 Создать поздравление", callback_data=f"gr:{pid}:friendly:0")],
        [Btn("👥 Категория", callback_data=f"ed:cat:{pid}")],
        [Btn("✏️ Изменить", callback_data=f"pk:edit:{pid}"),
         Btn("🗑 Удалить", callback_data=f"pk:del:{pid}")],
    ])


# ---------- /start, меню ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await db.get_user(update.effective_chat.id)
    await reply(update, WELCOME, MENU)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await reply(update, "Отменено.", MENU)


# ---------- добавление ----------
async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["mode"] = "add_date"
    await reply(
        update,
        "Отправь дату рождения: <code>ДД.ММ.ГГГГ</code> или <code>ДД.ММ</code> — потом спрошу фамилию и имя.\n\n"
        "Или сразу несколько людей, каждого с новой строки:\n"
        "<code>Иванов Иван 18.01.2007\nСадыкова Алия 03.02.2007</code>\n\n"
        "/cancel — отмена",
    )


async def add_got_date(update, context, text: str):
    d = parse_date(text)
    if not d:
        await reply(update, "Такой даты не существует. Попробуй ещё раз: <code>ДД.ММ.ГГГГ</code> или <code>ДД.ММ</code>")
        return
    context.user_data.update(mode="add_name", date=d.isoformat(), has_year=has_birth_year(text))
    await reply(update, f"Дата {d.strftime('%d.%m.%Y' if has_birth_year(text) else '%d.%m')}" + ("" if has_birth_year(text) else " (год не указан)") + " записана ✅\nТеперь напиши фамилию и имя.")


async def add_got_name(update, context, text: str):
    name = text.strip()[:60]
    if not name or DATE_RE.match(name):
        await reply(update, "Напиши фамилию и имя текстом.")
        return
    context.user_data.update(mode="add_cat", name=name)
    await reply(update, f"<b>{esc(name)}</b> — выбери категорию:", category_kb("nc"))


_BD = r"(\d{1,2})[./\-](\d{1,2})(?:[./\-](\d{4}|\d{2}))?"
_BULK_END = re.compile(rf"^\s*(.+?)[\s—–:,\-]*{_BD}\s*[.,;]?\s*$")
_BULK_START = re.compile(rf"^\s*{_BD}\s*[.,;]?[\s—–:,\-]*(.+?)\s*$")
MAX_BULK = 300


def parse_bulk2(text: str):
    """Каждая строка разбирается отдельно: 'Имя 17.09.2007', '26.06.07', '28.07.' (без года), номера в начале.
    Возвращает (список (имя, месяц, день, год|None), список непонятых строк)."""
    now_year = datetime.now(TZ).year
    items, bad, seen = [], [], set()
    for line in text.splitlines():
        if not line.strip():
            continue
        m = _BULK_END.match(line)
        if m:
            name, d, mo, y = m[1], m[2], m[3], m[4]
        else:
            m = _BULK_START.match(line)
            if not m:
                bad.append(line.strip())
                continue
            d, mo, y, name = m[1], m[2], m[3], m[4]
        name = re.sub(r"^\s*(?:\d+[.)]|[-•*])\s+", "", name).strip(" -–—:,.")[:60]
        year = None
        if y:
            year = int(y)
            if len(y) == 2:
                year += 2000 if year <= now_year % 100 else 1900
        dm = _mk(year, int(mo), int(d))
        if not name or not dm or (y and dm[2] is None):  # плохая дата или год вне 1900..сейчас
            bad.append(line.strip())
            continue
        key = (name.casefold(), dm[0], dm[1])
        if key not in seen:
            seen.add(key)
            items.append((name, *dm))
    return items[:MAX_BULK], bad


async def add_bulk(update, context, text: str):
    items, bad = parse_bulk2(text)
    if not items:
        await reply(update, "Не понял 🤔 Отправь дату <code>ДД.ММ.ГГГГ</code> или <code>ДД.ММ</code> "
                            "или строки вида <code>Иванов Иван 18.01.2007</code>."
                            + (f"\n\nНе разобрал строк: {len(bad)}" if bad else ""))
        return
    context.user_data.clear()
    context.user_data["bulk"] = items
    lines = [f"📥 <b>Нашёл людей: {len(items)}</b>\n"]
    lines += [f"• {esc(n)} — {d:02d}.{m:02d}" + (f".{y}" if y else "") for n, m, d, y in items[:20]]
    if len(items) > 20:
        lines.append(f"… и ещё {len(items) - 20}")
    if bad:
        lines.append(f"\n⚠️ Не разобрал строк: {len(bad)}")
        lines += [f"<code>{esc(b_[:60])}</code>" for b_ in bad[:5]]
    lines.append("\nВ какую категорию добавить?")
    rows = chunk_buttons([Btn(cat_label(k), callback_data=f"bk:{k}") for k in CATEGORIES], 2)
    rows.append([Btn("✖️ Отмена", callback_data="bk:c")])
    await reply(update, "\n".join(lines), Kb(rows))


def chunk_buttons(btns, n):
    return [btns[i:i + n] for i in range(0, len(btns), n)]


async def save_people(chat: int, items, category: str, with_years: bool = True):
    added = dup = 0
    for n, m, d, y in items:
        hy = bool(y) and with_years
        birth = datetime(y if hy else 2000, m, d).date()
        if await db.person_exists(chat, n, birth):
            dup += 1
            continue
        await db.add_person(chat, n, birth, category, hy)
        added += 1
    return added, dup


# ---------- списки ----------
async def send_table(update, people, title: str):
    if not people:
        await reply(update, "Список пуст. Нажми ➕ Добавить.")
        return
    today = today_()
    people = by_upcoming(people, today)
    await reply(update, f"🎂 <b>{title}</b> · {len(people)}")
    for i in range(0, len(people), 20):
        chunk = people[i:i + 20]
        await reply(update, f"<pre>{esc(table(chunk, today, start=i + 1))}</pre>")


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat.id
    if context.args:
        key = find_category(" ".join(context.args))
        if not key:
            await reply(update, "Не знаю такой категории. Например: /list друзья")
            return
        await send_table(update, await db.list_people(chat, key), cat_label(key))
        return
    await send_table(update, await db.list_people(chat), "ДНИ РОЖДЕНИЯ")


async def categories_view(chat: int):
    people = await db.list_people(chat)
    counts = {k: 0 for k in CATEGORIES}
    for p in people:
        counts[p["category"]] = counts.get(p["category"], 0) + 1
    rows = [[Btn(f"{cat_label(k)} · {counts.get(k, 0)}", callback_data=f"cg:{k}")] for k in CATEGORIES]
    return "👥 <b>Категории</b>\nВыбери категорию, чтобы посмотреть людей или добавить новых:", Kb(rows)


async def categories_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, kb = await categories_view(update.effective_chat.id)
    await reply(update, text, kb)


async def category_screen(update: Update, key: str, note: str = ""):
    members = sorted(await db.list_people(update.effective_chat.id, key), key=lambda p: p["name"].casefold())
    lines = [f"<b>{cat_label(key)}</b> · {len(members)}"]
    if note:
        lines.append(note)
    lines.append("")
    if members:
        lines += [f"• {esc(p['name'])} — {birth_str(p)}" for p in members[:30]]
        if len(members) > 30:
            lines.append(f"… и ещё {len(members) - 30}")
    else:
        lines.append("Пока никого. Нажми «➕ Добавить людей».")
    kb = Kb([[Btn("➕ Добавить людей", callback_data=f"cp:{key}:0")],
             [Btn("📋 Таблица", callback_data=f"ct:{key}"), Btn("⬅️ Категории", callback_data="cgm")]])
    await show(update, "\n".join(lines), kb)


PICK_PAGE = 8


async def category_picker(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str, page: int):
    """Список людей с галочками: отметь нескольких и нажми «Готово»."""
    ud = context.user_data
    if ud.get("sel_cat") != key:
        ud["sel_cat"], ud["sel"] = key, set()
    sel = ud.setdefault("sel", set())
    cands = sorted((p for p in await db.list_people(update.effective_chat.id) if p["category"] != key),
                   key=lambda p: p["name"].casefold())
    if not cands:
        await show(update, f"В «{cat_label(key)}» уже все, кого ты добавил. Сначала добавь людей через ➕ Добавить.",
                   Kb([[Btn("⬅️ Назад", callback_data=f"cg:{key}")]]))
        return
    pages = (len(cands) + PICK_PAGE - 1) // PICK_PAGE
    page = max(0, min(page, pages - 1))
    rows = [[Btn(("✅ " if p["id"] in sel else "⬜ ") + f"{p['name'][:30]} {CATEGORIES.get(p['category'], CATEGORIES['other'])[0]}",
                 callback_data=f"cx:{key}:{p['id']}:{page}")]
            for p in cands[page * PICK_PAGE:(page + 1) * PICK_PAGE]]
    nav = []
    if page > 0:
        nav.append(Btn("⬅️", callback_data=f"cp:{key}:{page - 1}"))
    if pages > 1:
        nav.append(Btn(f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(Btn("➡️", callback_data=f"cp:{key}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([Btn(f"💾 Готово ({len(sel)})", callback_data=f"cs:{key}"),
                 Btn("⬅️ Назад", callback_data=f"cg:{key}")])
    await show(update, f"Отметь, кого добавить в <b>{cat_label(key)}</b> (значок справа — текущая категория):", Kb(rows))


async def upcoming(update: Update, context: ContextTypes.DEFAULT_TYPE):
    people = await db.list_people(update.effective_chat.id)
    if not people:
        await reply(update, "Список пуст. Нажми ➕ Добавить.")
        return
    today = today_()
    top = by_upcoming(people, today)[:10]
    lines = ["🎂 <b>Ближайшие дни рождения:</b>\n"]
    for i, p in enumerate(top, 1):
        n = days_until(p["birth"], today)
        age = next_age(p["birth"], today) if p.get("has_year", True) else None
        lines.append(f"{i}. {esc(p['name'])} — {days_text(n)}" + (f" (исполнится {age})" if age is not None else ""))
    kb = Kb([[Btn(f"🎁 {p['name']}", callback_data=f"pk:card:{p['id']}")] for p in top[:5]])
    await reply(update, "\n".join(lines), kb)


# ---------- поиск / правка / удаление ----------
async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        await do_search(update, " ".join(context.args))
        return
    context.user_data.clear()
    context.user_data["mode"] = "search"
    await reply(update, "Кого ищем? Напиши имя или фамилию.")


async def do_search(update, query: str):
    q = query.strip().casefold()
    people = [p for p in await db.list_people(update.effective_chat.id)
              if q in p["name"].casefold()]
    if not people:
        await reply(update, f"По запросу «{esc(query)}» ничего не нашёл.")
        return
    today = today_()
    people = by_upcoming(people, today)[:10]
    lines = [f"🔍 <b>Найдено: {len(people)}</b>\n"]
    for p in people:
        lines.append(f"• {esc(p['name'])} — {birth_str(p)} ({days_text(days_until(p['birth'], today))})")
    kb = Kb([[Btn(person_btn_label(p), callback_data=f"pk:card:{p['id']}")] for p in people])
    await reply(update, "\n".join(lines), kb)


async def picker_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, title: str):
    people = await db.list_people(update.effective_chat.id)
    if not people:
        await reply(update, "Список пуст. Нажми ➕ Добавить.")
        return
    await reply(update, title, picker_kb(people, action))


async def edit_cmd(update, context):
    await picker_cmd(update, context, "edit", "✏️ Кого редактируем?")


async def delete_cmd(update, context):
    await picker_cmd(update, context, "del", "🗑 Кого удалить?")


async def card_cmd(update, context):
    await picker_cmd(update, context, "card", "🎁 Выбери человека:")


# ---------- статистика / календарь ----------
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    people = await db.list_people(update.effective_chat.id)
    if not people:
        await reply(update, "Пока пусто — добавь первого человека ➕")
        return
    today = today_()
    this_month = sum(1 for p in people if p["birth"].month == today.month)
    passed = sum(1 for p in people if (p["birth"].month, p["birth"].day) < (today.month, today.day))
    nearest = min(days_until(p["birth"], today) for p in people)
    await reply(
        update,
        "📊 <b>Твоя статистика</b>\n\n"
        f"👥 Всего людей: {len(people)}\n"
        f"🎂 В этом месяце: {this_month}\n"
        f"📅 В этом году уже было: {passed}\n"
        f"⏳ Ближайший день рождения: {days_text(nearest)}",
    )


async def calendar_view(update, year: int, month: int):
    people = await db.list_people(update.effective_chat.id)
    in_month = sorted((p for p in people if p["birth"].month == month),
                      key=lambda p: (p["birth"].day, p["name"].casefold()))
    marks = {p["birth"].day for p in in_month}
    text = f"📅 <b>{MONTHS[month - 1]} {year}</b>\n<pre>{month_grid(year, month, marks)}</pre>\n* — день рождения"
    if in_month:
        text += "\n\n" + "\n".join(f"🎂 {p['birth'].day:02d} — {esc(p['name'])}" for p in in_month)
    py, pm = shift_month(year, month, -1)
    ny, nm = shift_month(year, month, 1)
    kb = Kb([[Btn("⬅️ Предыдущий", callback_data=f"cal:{py}:{pm}"),
              Btn(MONTHS[month - 1], callback_data=f"cal:{year}:{month}"),
              Btn("Следующий ➡️", callback_data=f"cal:{ny}:{nm}")]])
    await show(update, text, kb)


async def calendar_cmd(update, context):
    t = today_()
    await calendar_view(update, t.year, t.month)


# ---------- настройки ----------
def settings_view(u):
    days = u["remind_days"]
    lines = ["⚙️ <b>Настройки</b>\n",
             f"🔔 Уведомления: {'ВКЛ' if u['notify'] else 'ВЫКЛ'}",
             f"⏰ Время: {u['remind_time']}"]
    for d in REMIND_OPTIONS:
        title = "🎉 В день рождения" if d == 0 else f"📅 За {d} {'день' if d == 1 else 'дн.'}"
        lines.append(f"{title}: {'✅' if d in days else '❌'}")
    rows = [[Btn("🔔 Выключить" if u["notify"] else "🔕 Включить", callback_data="st:notify")]]
    row = []
    for d in REMIND_OPTIONS:
        label = ("🎉 " if d == 0 else f"{d} дн. ") + ("✅" if d in days else "❌")
        row.append(Btn(label, callback_data=f"st:d:{d}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([Btn(("• " if t == u["remind_time"] else "") + t, callback_data=f"st:t:{t}")
                 for t in TIME_OPTIONS])
    rows.append([Btn("✍️ Своё время", callback_data="st:custom")])
    return "\n".join(lines), Kb(rows)


async def settings_cmd(update, context):
    u = await db.get_user(update.effective_chat.id)
    text, kb = settings_view(u)
    await show(update, text, kb)


# ---------- поздравления ----------
async def greeting_view(update, p, style: str, idx: int):
    today = today_()
    age = next_age(p["birth"], today) if p.get("has_year", True) else None
    text, idx = make_greeting(p["name"], age, style, idx)
    n = days_until(p["birth"], today)
    head = (f"🎉 Сегодня день рождения у {esc(first_name(p['name']))}!" if n == 0
            else f"🎁 Поздравление для {esc(first_name(p['name']))} ({days_text(n)})")
    age_line = f"Исполняется: {years_text(age)}.\n\n" if age is not None else ""
    body = f"{head}\n\n{age_line}💌 <b>Готовое поздравление:</b>\n«{esc(text)}»"
    pid = p["id"]
    rows, row = [], []
    for key, (label, _) in STYLES.items():
        row.append(Btn(("• " if key == style else "") + label, callback_data=f"gr:{pid}:{key}:0"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if CopyTextButton and len(text) <= 256:
        copy_btn = Btn("📋 Скопировать", copy_text=CopyTextButton(text=text))
    else:
        copy_btn = Btn("📋 Скопировать", callback_data=f"gc:{pid}:{style}:{idx}")
    rows.append([copy_btn, Btn("🔄 Другое поздравление", callback_data=f"gr:{pid}:{style}:{idx + 1}")])
    await show(update, body, Kb(rows))



async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat.id
    today = today_()
    people = [p for p in await db.list_people(chat) if days_until(p["birth"], today) == 0]
    if not people:
        await reply(update, "🎉 Сегодня дней рождения нет.")
        return
    people.sort(key=lambda p: p["name"].casefold())
    lines = ["🎉 <b>Дни рождения сегодня:</b>", ""]
    for p in people:
        age = next_age(p["birth"], today) if p.get("has_year", True) else None
        lines.append(f"• {esc(p['name'])}" + (f" — {years_text(age)}" if age is not None else ""))
    kb = Kb([[Btn(f"🎁 {p['name']}", callback_data=f"pk:card:{p['id']}")] for p in people[:8]])
    await reply(update, "\n".join(lines), kb)

async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await db.list_people(update.effective_chat.id):
        data = b""
    else:
        data = await db.export_csv(update.effective_chat.id)
    if data == b"":
        await reply(update, "📤 Экспорт пуст: у тебя пока нет записей.")
        return
    await update.effective_message.reply_document(
        document=BytesIO(data), filename="birthdays.csv",
        caption="📤 Твой список дней рождения в CSV.")

# ---------- inline-кнопки ----------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":", 3)
    kind = parts[0]
    chat = update.effective_chat.id

    if kind == "add_more":
        await add_start(update, context)

    elif kind == "nc":  # категория для нового человека
        ud = context.user_data
        if ud.get("mode") != "add_cat":
            await q.edit_message_reply_markup(None)
            return
        name, birth = ud["name"], datetime.fromisoformat(ud["date"]).date()
        has_year = ud.get("has_year", True)
        if await db.person_exists(chat, name, birth):
            await show(update, "⚠️ Такой человек с такой датой уже есть в списке.")
            return
        pid = await db.add_person(chat, name, birth, parts[1], has_year)
        ud.clear()
        await show(update, f"Готово 🎂 <b>{esc(name)}</b> — {birth.strftime('%d.%m.%Y' if has_year else '%d.%m')}\n{cat_label(parts[1])}",
                   Kb([[Btn("🎁 Карточка", callback_data=f"pk:card:{pid}"),
                        Btn("➕ Добавить ещё", callback_data="add_more")]]))

    elif kind == "nm":  # имя не найдено: добавить или нет
        ud = context.user_data
        if parts[1] == "add" and ud.get("pending_name"):
            name = ud.pop("pending_name")
            ud.clear()
            ud.update(mode="add_date_named", name=name)
            await show(update, f"Отлично! Дата рождения для <b>{esc(name)}</b>: "
                               "<code>ДД.ММ.ГГГГ</code> или <code>ДД.ММ</code>\n\n/cancel — отмена")
        elif parts[1] == "add":
            await show(update, "Напиши имя ещё раз 🙂")
        else:
            ud.pop("pending_name", None)
            await show(update, "Хорошо, не добавляю.")

    elif kind == "bk":  # массовое добавление: выбрана категория
        ud = context.user_data
        items = ud.get("bulk")
        if parts[1] == "c" or not items or parts[1] not in CATEGORIES:
            ud.pop("bulk", None)
            await show(update, "Отменено." if parts[1] == "c" else "Список устарел. Пришли его ещё раз.")
            return
        added, dup = await save_people(chat, items, parts[1])
        ud.pop("bulk", None)
        await show(update, f"Готово 🎂 Добавлено: {added} · {cat_label(parts[1])}"
                   + (f"\nУже были в базе: {dup}" if dup else ""),
                   Kb([[Btn("➕ Добавить ещё", callback_data="add_more"), Btn("👥 Категории", callback_data="cgm")]]))

    elif kind == "cgm":
        text, kb = await categories_view(chat)
        await show(update, text, kb)

    elif kind == "cg" and parts[1] in CATEGORIES:
        await category_screen(update, parts[1])

    elif kind == "cp" and parts[1] in CATEGORIES:
        await category_picker(update, context, parts[1], int(parts[2]))

    elif kind == "cx" and parts[1] in CATEGORIES:  # cx:<cat>:<pid>:<page> — отметить/снять галочку
        ud = context.user_data
        if ud.get("sel_cat") != parts[1]:
            ud["sel_cat"], ud["sel"] = parts[1], set()
        ud["sel"] ^= {int(parts[2])}
        await category_picker(update, context, parts[1], int(parts[3]))

    elif kind == "cs" and parts[1] in CATEGORIES:  # сохранить выбранных
        ud = context.user_data
        ids = list(ud.get("sel", ())) if ud.get("sel_cat") == parts[1] else []
        for pid in ids:
            await db.update_person(chat, pid, category=parts[1])
        ud.pop("sel", None)
        ud.pop("sel_cat", None)
        await category_screen(update, parts[1], f"✅ Добавлено: {len(ids)}" if ids else "Никого не отметил.")

    elif kind == "noop":
        pass

    elif kind == "im":  # импорт из файла
        ud = context.user_data
        items = ud.get("import")
        if parts[1] == "c" or not items:
            ud.pop("import", None)
            await show(update, "Импорт отменён." if parts[1] == "c" else "Файл устарел. Пришли его ещё раз.")
            return
        with_years = parts[1] == "y"
        added = dup = 0
        for n, m, d, y in items:
            hy = bool(y) and with_years
            birth = datetime(y if hy else 2000, m, d).date()
            if await db.person_exists(chat, n, birth):
                dup += 1
                continue
            await db.add_person(chat, n, birth, "other", hy)
            added += 1
        ud.pop("import", None)
        await show(update, f"Готово 🎂 Добавлено: {added}" + (f"\nУже были в базе: {dup}" if dup else "")
                   + "\n\nВсе в категории «Другое». Распредели через 👥 Категории → ➕ Добавить людей.",
                   Kb([[Btn("👥 Категории", callback_data="cgm")]]))

    elif kind == "pg":  # листание списка выбора
        people = await db.list_people(chat)
        await q.edit_message_reply_markup(picker_kb(people, parts[1], int(parts[2])))

    elif kind == "pk":  # выбран человек
        action, pid = parts[1], int(parts[2])
        p = await db.get_person(chat, pid)
        if not p:
            await show(update, "Этого человека уже нет в списке.")
            return
        if action == "card":
            await show(update, card_text(p, today_()), card_kb(pid))
        elif action == "edit":
            await show(update, f"✏️ <b>{esc(p['name'])}</b> — {birth_str(p)}\n{cat_label(p['category'])}\n\nЧто изменить?",
                       Kb([[Btn("Имя", callback_data=f"ed:name:{pid}"),
                            Btn("Дату", callback_data=f"ed:date:{pid}"),
                            Btn("Категорию", callback_data=f"ed:cat:{pid}")],
                           [Btn("📝 Заметку", callback_data=f"ed:note:{pid}")]]))
        elif action == "del":
            await show(update, f"Удалить <b>{esc(p['name'])}</b> ({birth_str(p)})?",
                       Kb([[Btn("🗑 Да, удалить", callback_data=f"dl:{pid}:y"),
                            Btn("Отмена", callback_data=f"dl:{pid}:n")]]))

    elif kind == "ed":
        field, pid = parts[1], int(parts[2])
        if field == "cat":
            await show(update, "Выбери новую категорию:", category_kb(f"ec:{pid}"))
        else:
            context.user_data.clear()
            context.user_data.update(mode=f"edit_{field}", id=pid)
            prompts = {
                "name": "Напиши новое имя:",
                "date": "Напиши новую дату: <code>ДД.ММ.ГГГГ</code> или <code>ДД.ММ</code>",
                "note": "Напиши заметку (например, что подарить). Чтобы удалить заметку, отправь <code>-</code>",
            }
            await show(update, prompts.get(field, "Напиши новое значение:"))

    elif kind == "ec":  # ec:<pid>:<cat> — split вернёт [ec, pid, cat]
        pid, key = int(parts[1]), parts[2]
        await db.update_person(chat, pid, category=key)
        p = await db.get_person(chat, pid)
        await show(update, f"Сохранено ✅\n\n{card_text(p, today_())}", card_kb(pid))

    elif kind == "dl":
        pid = int(parts[1])
        if parts[2] == "y":
            await db.delete_person(chat, pid)
            await show(update, "Удалено 🗑")
        else:
            await show(update, "Оставил как есть 👌")

    elif kind == "ct":
        await q.edit_message_reply_markup(None)
        await send_table(update, await db.list_people(chat, parts[1]), cat_label(parts[1]))

    elif kind == "cal":
        await calendar_view(update, int(parts[1]), int(parts[2]))

    elif kind == "gr":  # gr:<pid>:<style>:<idx>
        p = await db.get_person(chat, int(parts[1]))
        if p:
            await greeting_view(update, p, parts[2], int(parts[3]))

    elif kind == "gc":  # запасной вариант «Скопировать» без CopyTextButton
        p = await db.get_person(chat, int(parts[1]))
        if p:
            text, _ = make_greeting(p["name"], next_age(p["birth"], today_()) if p.get("has_year", True) else None, parts[2], int(parts[3]))
            await reply(update, f"<code>{esc(text)}</code>")

    elif kind == "st":
        u = await db.get_user(chat)
        if parts[1] == "notify":
            await db.update_user(chat, notify=not u["notify"])
        elif parts[1] == "d":
            d = int(parts[2])
            days = set(u["remind_days"])
            days.symmetric_difference_update({d})
            await db.update_user(chat, remind_days=sorted(days, reverse=True))
        elif parts[1] == "t":
            await db.update_user(chat, remind_time=q.data.split(":", 2)[2])
        elif parts[1] == "custom":
            context.user_data.clear()
            context.user_data["mode"] = "set_time"
            await reply(update, "Напиши время в формате <code>ЧЧ:ММ</code>, например <code>07:30</code>.")
            return
        text, kb = settings_view(await db.get_user(chat))
        await show(update, text, kb)


# ---------- импорт из календаря и контактов ----------
BDAY_WORDS = re.compile(r"(день\s+рождения|дни\s+рождения|birthday|b-?day|туған\s+күн|\bдр\b|🎂)", re.I)
MAX_IMPORT = 1000


def _mk(y, m, d):
    """(месяц, день, год|None) или None, если дата невозможна. Годы вне 1900..сейчас — неизвестны."""
    yy = y if y and 1900 <= y <= datetime.now(TZ).year else None
    try:
        datetime(yy or 2000, m, d)
    except ValueError:
        return None
    return (m, d, yy)


def _unfold(text: str) -> str:
    return re.sub(r"\r?\n[ \t]", "", text)


def _event_name(summary: str) -> str:
    s = BDAY_WORDS.sub(" ", summary.replace("\\,", ","))
    s = re.sub(r"[🎉🎈🎁]", " ", s)
    s = re.sub(r"['’]s\b", "", s)
    return re.sub(r"\s+", " ", s).strip(" -–—:,.|()[]'’")[:60]


def parse_ics(text: str):
    """Google/Apple/Outlook календарь (.ics). Берём только события со словами «день рождения / birthday / ДР»."""
    items, skipped = [], 0
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", _unfold(text), re.S):
        props = {}
        for line in block.splitlines():
            key, sep, val = line.partition(":")
            if sep:
                props.setdefault(key.split(";")[0].strip().upper(), val.strip())
        summary = props.get("SUMMARY", "")
        m = re.search(r"(\d{4})(\d{2})(\d{2})", props.get("DTSTART", ""))
        if not (summary and m and BDAY_WORDS.search(summary)):
            if summary and "FREQ=YEARLY" in props.get("RRULE", ""):
                skipped += 1
            continue
        name, dm = _event_name(summary), _mk(int(m[1]), int(m[2]), int(m[3]))
        if name and dm:
            items.append((name, *dm))
        else:
            skipped += 1
    return items, skipped


def parse_vcf(text: str):
    """Контакты (.vcf): имя из N/FN, дата из BDAY."""
    from core import SURNAME_FIRST
    items, skipped = [], 0
    for card in re.findall(r"BEGIN:VCARD(.*?)END:VCARD", _unfold(text), re.S | re.I):
        fn = n_name = bday = None
        for line in card.splitlines():
            key, sep, val = line.partition(":")
            k = key.split(";")[0].strip().upper()
            if k == "FN":
                fn = val.strip()
            elif k == "N":
                parts = (val.split(";") + ["", ""])[:2]
                fam, giv = parts[0].strip(), parts[1].strip()
                n_name = " ".join(x for x in ((fam, giv) if SURNAME_FIRST else (giv, fam)) if x)
            elif k == "BDAY":
                bday = val.strip()
        if not bday:
            continue
        name = (n_name or fn or "").replace("\\,", ",").strip()[:60]
        m = re.match(r"^(?:--|(\d{4})-?)(\d{2})-?(\d{2})", bday)
        dm = _mk(int(m[1]) if m and m[1] else None, int(m[2]), int(m[3])) if m else None
        if name and dm:
            items.append((name, *dm))
        else:
            skipped += 1
    return items, skipped


def _any_date(v: str):
    v = v.strip()
    m = re.match(r"^(?:--|(\d{4})-)(\d{1,2})-(\d{1,2})", v)
    if m:
        return _mk(int(m[1]) if m[1] else None, int(m[2]), int(m[3]))
    m = re.match(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?$", v)
    if m:
        return _mk(int(m[3]) if m[3] else None, int(m[2]), int(m[1]))
    return None  # форматы вроде 05/17/1990 неоднозначны — не угадываем


def parse_csv(text: str):
    """CSV контактов (Google CSV, Outlook и т.п.) с колонкой Birthday / Дата рождения."""
    from core import SURNAME_FIRST
    sample = text[:2000]
    rows = list(csv.DictReader(io.StringIO(text), delimiter=";" if sample.count(";") > sample.count(",") else ","))
    if not rows:
        return [], 0
    cols = {(c or "").strip().casefold(): c for c in rows[0]}

    def col(*names):
        return next((cols[n] for n in names if n in cols), None)

    bcol = col("birthday", "birth date", "birthdate", "date of birth", "bday", "дата рождения", "день рождения", "др")
    if not bcol:
        return [], 0
    first, last = col("first name", "given name", "имя"), col("last name", "family name", "фамилия")
    whole = col("name", "full name", "фио", "имя и фамилия")
    items, skipped = [], 0
    for r in rows:
        raw = (r.get(bcol) or "").strip()
        if not raw:
            continue
        f, l = (r.get(first) or "").strip() if first else "", (r.get(last) or "").strip() if last else ""
        name = " ".join(x for x in ((l, f) if SURNAME_FIRST else (f, l)) if x) or (r.get(whole) or "").strip()
        dm = _any_date(raw)
        if name and dm:
            items.append((name[:60], *dm))
        else:
            skipped += 1
    return items, skipped


def decode_text(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def parse_file(filename: str, raw: bytes):
    fn = filename.lower()
    if fn.endswith(".zip"):  # экспорт Google Календаря — zip с .ics
        items, skipped = [], 0
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            infos = [i for i in zf.infolist() if i.filename.lower().endswith(".ics")]
            if sum(i.file_size for i in infos) > 30 * 1024 * 1024:
                raise ValueError("архив слишком большой")
            for i in infos:
                it, sk = parse_ics(decode_text(zf.read(i)))
                items += it
                skipped += sk
        return items, skipped
    text = decode_text(raw)
    if fn.endswith(".ics"):
        return parse_ics(text)
    if fn.endswith(".vcf"):
        return parse_vcf(text)
    return parse_csv(text)


IMPORT_HELP = (
    "📥 <b>Импорт дней рождения</b>\n\n"
    "Пришли мне файл прямо в чат: <code>.ics</code>, <code>.vcf</code>, <code>.csv</code> или <code>.zip</code> с календарём.\n\n"
    "<b>Google Контакты</b> (надёжнее всего): contacts.google.com → Экспорт → «vCard» или «Google CSV».\n"
    "<b>Google Календарь</b>: Настройки → Импорт и экспорт → Экспорт (получится zip). "
    "Дни рождения из «Контактов» в такой экспорт могут не попасть — тогда используй экспорт контактов.\n\n"
    "Названия пунктов в Google могут отличаться. Перед сохранением я покажу, что нашёл, и ничего не добавлю без твоего «да»."
)


async def import_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply(update, IMPORT_HELP)


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    name = doc.file_name or ""
    if not name.lower().endswith((".ics", ".vcf", ".csv", ".zip")):
        await reply(update, IMPORT_HELP)
        return
    if doc.file_size and doc.file_size > 10 * 1024 * 1024:
        await reply(update, "Файл больше 10 МБ — слишком большой для импорта.")
        return
    try:
        raw = bytes(await (await doc.get_file()).download_as_bytearray())
        items, skipped = parse_file(name, raw)
    except Exception:
        log.exception("import failed")
        await reply(update, "Не смог прочитать файл 😕 Проверь, что это экспорт из Google (.ics, .vcf или .csv).")
        return
    seen, uniq = set(), []
    for it in items:
        k = (it[0].casefold(), it[1], it[2])
        if k not in seen:
            seen.add(k)
            uniq.append(it)
    items = uniq[:MAX_IMPORT]
    if not items:
        await reply(update, "Дни рождения в файле не нашёл 🤔\n\n" + IMPORT_HELP)
        return
    context.user_data["import"] = items
    lines = [f"📥 <b>Нашёл дней рождения: {len(items)}</b>\n"]
    lines += [f"• {esc(n)} — {d:02d}.{m:02d}" + (f".{y}" if y else "") for n, m, d, y in items[:15]]
    if len(items) > 15:
        lines.append(f"… и ещё {len(items) - 15}")
    if skipped:
        lines.append(f"\nПропущено (не разобрал): {skipped}")
    if any(y for _, _, _, y in items):
        lines.append("\nГода в файле иногда стоят не рождения, а создания события. Если не уверен, выбери «Без годов».")
        buttons = [[Btn("✅ С годами", callback_data="im:y"), Btn("📅 Без годов", callback_data="im:n")]]
    else:
        buttons = [[Btn("✅ Импортировать", callback_data="im:n")]]
    buttons.append([Btn("✖️ Отмена", callback_data="im:c")])
    await reply(update, "\n".join(lines), Kb(buttons))


# ---------- проверка имени ----------
def looks_like_name(text: str) -> bool:
    return 0 < len(text) <= 60 and "\n" not in text and not any(ch.isdigit() for ch in text)


async def name_lookup(update, context, text: str):
    """Написали просто имя: если человек есть в базе — показываем, если нет — предлагаем добавить."""
    chat = update.effective_chat.id
    today = today_()
    q = text.strip().casefold()
    found = [p for p in await db.list_people(chat) if q in p["name"].casefold()]
    if len(found) == 1:
        p = found[0]
        await reply(update, f"✅ <b>{esc(p['name'])}</b> уже есть в базе.\n\n{card_text(p, today)}", card_kb(p["id"]))
    elif found:
        found = by_upcoming(found, today)[:10]
        lines = [f"✅ В базе нашёл ({len(found)}):\n"]
        for p in found:
            lines.append(f"• {esc(p['name'])} — {birth_str(p)} ({days_text(days_until(p['birth'], today))})")
        kb = Kb([[Btn(person_btn_label(p), callback_data=f"pk:card:{p['id']}")] for p in found])
        await reply(update, "\n".join(lines), kb)
    else:
        name = text.strip()[:60]
        context.user_data["pending_name"] = name
        await reply(update, f"«{esc(name)}» в базе нет. Добавить?",
                    Kb([[Btn("➕ Добавить", callback_data="nm:add"), Btn("✖️ Нет", callback_data="nm:no")]]))


# ---------- текст ----------
MENU_ACTIONS = {
    BTN_TODAY: today_cmd, BTN_EXPORT: export_cmd,
    BTN_ADD: add_start, BTN_LIST: list_cmd, BTN_UP: upcoming, BTN_SEARCH: search_cmd,
    BTN_CAL: calendar_cmd, BTN_STATS: stats, BTN_CATS: categories_menu, BTN_SET: settings_cmd,
}


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    ud = context.user_data
    chat = update.effective_chat.id

    if text in MENU_ACTIONS:
        context.args = []
        await MENU_ACTIONS[text](update, context)
        return

    mode = ud.get("mode")
    if mode == "add_date":
        if DATE_RE.match(text):
            await add_got_date(update, context, text)
        else:
            await add_bulk(update, context, text)
    elif mode == "add_name":
        await add_got_name(update, context, text)
    elif mode == "add_date_named":
        d = parse_date(text)
        if not d:
            await reply(update, "Такой даты не существует. Попробуй ещё раз: <code>ДД.ММ.ГГГГ</code> или <code>ДД.ММ</code>")
            return
        ud.update(mode="add_cat", date=d.isoformat(), has_year=has_birth_year(text))
        await reply(update, f"<b>{esc(ud['name'])}</b> — выбери категорию:", category_kb("nc"))
    elif mode == "add_cat":
        await reply(update, "Выбери категорию кнопкой выше 👆 или /cancel")
    elif mode == "search":
        ud.clear()
        await do_search(update, text)
    elif mode == "edit_name":
        await db.update_person(chat, ud["id"], name=text[:60])
        p = await db.get_person(chat, ud["id"])
        ud.clear()
        await reply(update, f"Сохранено ✅\n\n{card_text(p, today_())}", card_kb(p["id"]))
    elif mode == "edit_note":
        note = "" if text.strip() == "-" else text.strip()[:300]
        await db.update_person(chat, ud["id"], note=note)
        p = await db.get_person(chat, ud["id"])
        ud.clear()
        await reply(update, f"Сохранено ✅\n\n{card_text(p, today_())}", card_kb(p["id"]))
    elif mode == "edit_date":
        d = parse_date(text)
        if not d:
            await reply(update, "Такой даты не существует. Попробуй ещё раз: <code>ДД.ММ.ГГГГ</code> или <code>ДД.ММ</code>")
            return
        await db.update_person(chat, ud["id"], birth=d, has_year=has_birth_year(text))
        p = await db.get_person(chat, ud["id"])
        ud.clear()
        await reply(update, f"Сохранено ✅\n\n{card_text(p, today_())}", card_kb(p["id"]))
    elif mode == "set_time":
        t = parse_time(text)
        if not t:
            await reply(update, "Не похоже на время. Пример: <code>09:00</code>")
            return
        await db.update_user(chat, remind_time=t)
        ud.clear()
        s, kb = settings_view(await db.get_user(chat))
        await reply(update, s, kb)
    elif DATE_RE.match(text):  # как раньше: просто прислали дату
        d = parse_date(text)
        if not d:
            await reply(update, "Такой даты не существует. Попробуй ещё раз: <code>ДД.ММ.ГГГГ</code> или <code>ДД.ММ</code>")
            return
        ud.update(mode="add_name", date=d.isoformat(), has_year=has_birth_year(text))
        await reply(update, f"Дата {d.strftime('%d.%m.%Y' if has_birth_year(text) else '%d.%m')}" + ("" if has_birth_year(text) else " (год не указан)") + " записана ✅\nТеперь напиши фамилию и имя.")
    elif parse_bulk2(text)[0]:  # прислали одну или несколько строк «Имя дата»
        await add_bulk(update, context, text)
    elif looks_like_name(text):
        await name_lookup(update, context, text)
    else:
        await reply(update, "Выбери действие в меню 👇", MENU)


# ---------- напоминания ----------
async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(TZ)
    today, hhmm = now.date(), now.strftime("%H:%M")
    for u in await db.due_users(today, hhmm):
        chat = u["chat_id"]
        try:
            soon, birthday = [], []
            for p in await db.list_people(chat):
                n = days_until(p["birth"], today)
                if n == 0 and 0 in u["remind_days"]:
                    birthday.append(p)
                elif n and n in u["remind_days"]:
                    soon.append((n, p))
            if soon:
                soon.sort(key=lambda x: x[0])
                lines = ["⏳ <b>Скоро дни рождения:</b>\n"]
                lines += [f"• {esc(p['name'])} — {days_text(n)}" + (f" (исполнится {next_age(p['birth'], today)})" if p.get("has_year", True) else "")
                          for n, p in soon]
                await context.bot.send_message(chat, "\n".join(lines), parse_mode=ParseMode.HTML)
            for p in birthday:
                age = next_age(p["birth"], today) if p.get("has_year", True) else None
                message = f"🎉 <b>Сегодня день рождения: {esc(p['name'])}!</b>\n" + (f"Исполняется: {years_text(age)}." if age is not None else "Год рождения неизвестен.") + " Не забудь поздравить!"
                sent = await context.bot.send_message(
                    chat,
                    message,
                    parse_mode=ParseMode.HTML,
                    reply_markup=Kb([[Btn("🎉 Создать поздравление", callback_data=f"gr:{p['id']}:friendly:0")]]),
                )
                await pin_birthday(context.bot, chat, sent.message_id)
        except Exception:
            log.exception("reminder failed for %s", chat)
        await db.mark_sent(chat, today)


# ---------- закрепление сообщения на 24 часа ----------
PINS_SQL = ("CREATE TABLE IF NOT EXISTS bot_pins ("
            "chat_id BIGINT NOT NULL, message_id BIGINT NOT NULL, until TIMESTAMPTZ NOT NULL)")


async def pin_birthday(bot, chat: int, message_id: int):
    """Закрепляем поздравление вверху чата; откроем через 24 часа в unpin_expired."""
    try:
        await bot.pin_chat_message(chat, message_id, disable_notification=True)
    except Exception as e:
        log.warning("не удалось закрепить сообщение в %s: %s", chat, e)
        return
    await db.pool.execute("INSERT INTO bot_pins (chat_id, message_id, until) VALUES ($1, $2, now() + interval '24 hours')",
                          chat, message_id)


async def unpin_expired(context: ContextTypes.DEFAULT_TYPE):
    for r in await db.pool.fetch("SELECT chat_id, message_id FROM bot_pins WHERE until <= now()"):
        try:
            await context.bot.unpin_chat_message(r["chat_id"], r["message_id"])
        except Exception as e:  # уже откреплено или удалено пользователем
            log.info("unpin %s/%s: %s", r["chat_id"], r["message_id"], e)
        await db.pool.execute("DELETE FROM bot_pins WHERE chat_id=$1 AND message_id=$2", r["chat_id"], r["message_id"])


# ---------- запуск ----------
async def post_init(app: Application):
    await db.init(DATABASE_URL)
    await db.pool.execute(PINS_SQL)
    imported = await db.import_legacy(LEGACY_JSON)
    if imported:
        log.info("Импортировано из %s: %d", LEGACY_JSON, imported)


async def post_shutdown(app: Application):
    await db.close()


async def on_error(update, context):
    log.exception("Ошибка", exc_info=context.error)


def start_health_server():
    """Render Web Service требует открытый порт. Локально (без PORT) не запускается."""
    port = os.getenv("PORT")
    if not port:
        return

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        do_HEAD = do_GET

        def log_message(self, *args):
            pass

    threading.Thread(target=HTTPServer(("0.0.0.0", int(port)), Handler).serve_forever,
                     daemon=True).start()
    log.info("Health-сервер на порту %s", port)


def main():
    start_health_server()
    app = Application.builder().token(TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()

    def cmd(name, fn):
        async def wrapped(update, context):
            context.user_data.clear()
            await fn(update, context)
        app.add_handler(CommandHandler(name, wrapped))

    cmd("start", start)
    cmd("menu", start)
    cmd("help", start)
    cmd("cancel", cancel)
    cmd("add", add_start)
    cmd("edit", edit_cmd)
    cmd("delete", delete_cmd)
    cmd("search", search_cmd)
    cmd("upcoming", upcoming)
    cmd("list", list_cmd)
    cmd("stats", stats)
    cmd("calendar", calendar_cmd)
    cmd("settings", settings_cmd)
    cmd("card", card_cmd)
    cmd("today", today_cmd)
    cmd("export", export_cmd)
    cmd("import", import_help)

    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    app.job_queue.run_repeating(check_reminders, interval=60, first=10)
    app.job_queue.run_repeating(unpin_expired, interval=300, first=30)
    app.run_polling()


if __name__ == "__main__":
    main()
