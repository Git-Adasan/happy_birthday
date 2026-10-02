import html
import logging
import os

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
    "📝 заметки для каждого человека\n\n"
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


async def add_bulk(update, context, text: str):
    items = parse_bulk(text)
    if not items:
        await reply(update, "Не понял 🤔 Отправь дату <code>ДД.ММ.ГГГГ</code> или <code>ДД.ММ</code> "
                            "или строки вида <code>Иванов Иван 18.01.2007</code>.")
        return
    chat = update.effective_chat.id
    for name, d, hy in items:
        await db.add_person(chat, name, d, "other", hy)
    context.user_data.clear()
    lines = "\n".join(
        f"• {esc(n)} — {d.strftime('%d.%m.%Y' if hy else '%d.%m')}" for n, d, hy in items
    )
    await reply(update, f"Добавлено ({len(items)}) 🎂\n{lines}",
                Kb([[Btn("➕ Добавить ещё", callback_data="add_more")]]))


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


async def categories_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    people = await db.list_people(update.effective_chat.id)
    counts = {k: 0 for k in CATEGORIES}
    for p in people:
        counts[p["category"]] = counts.get(p["category"], 0) + 1
    rows = [[Btn(f"{cat_label(k)} · {counts[k]}", callback_data=f"ct:{k}")] for k in CATEGORIES]
    await reply(update, "👥 <b>Категории</b>\nВыбери, кого показать:", Kb(rows))


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
                message = f"🎉 <b>Сегодня день рождения у {esc(p['name'])}!</b>\n" + (f"Исполняется: {years_text(age)}." if age is not None else "Год рождения неизвестен.") + " Не забудь поздравить!"
                await context.bot.send_message(
                    chat,
                    message,
                    parse_mode=ParseMode.HTML,
                    reply_markup=Kb([[Btn("🎉 Создать поздравление", callback_data=f"gr:{p['id']}:friendly:0")]]),
                )
        except Exception:
            log.exception("reminder failed for %s", chat)
        await db.mark_sent(chat, today)


# ---------- запуск ----------
async def post_init(app: Application):
    await db.init(DATABASE_URL)
    imported = await db.import_legacy(LEGACY_JSON)
    if imported:
        log.info("Импортировано из %s: %d", LEGACY_JSON, imported)


async def post_shutdown(app: Application):
    await db.close()


async def on_error(update, context):
    log.exception("Ошибка", exc_info=context.error)


def main():
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

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    app.job_queue.run_repeating(check_reminders, interval=60, first=10)
    app.run_polling()


if __name__ == "__main__":
    main()
