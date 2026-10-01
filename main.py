import json
import os
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# pip install "python-telegram-bot[job-queue]"
TOKEN = os.getenv("BOT_TOKEN")  # токен от @BotFather
TZ = ZoneInfo("Asia/Almaty")    # свой часовой пояс
REMIND_AT = time(9, 0, tzinfo=TZ)  # во сколько присылать напоминание
DB = "birthdays.json"

ASK_NAME = 1
DATE_PATTERN = r"^\s*\d{1,2}\.\d{1,2}\.\d{4}\s*$"


REMIND_DAYS = [7, 3, 1, 0]  # за сколько дней напоминать (0 = в сам день)


def days_until(born: date, today: date) -> int:
    for year in (today.year, today.year + 1):
        try:
            nxt = born.replace(year=year)
        except ValueError:  # 29 февраля
            nxt = date(year, 3, 1)
        if nxt >= today:
            return (nxt - today).days


def days_text(n: int) -> str:
    if n == 0:
        return "сегодня 🎉"
    if n == 1:
        return "завтра"
    return f"через {n} дн."


def load():
    if not os.path.exists(DB):
        return {}
    with open(DB, encoding="utf-8") as f:
        return json.load(f)


def save(data):
    with open(DB, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Отправь дату рождения друга в формате ДД.ММ.ГГГГ, "
        "например 18.01.2007, а я спрошу его фамилию и имя.\n\n"
        "/list — все дни рождения\n/cancel — отменить ввод"
    )


async def got_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        d = datetime.strptime(text, "%d.%m.%Y")
    except ValueError:
        await update.message.reply_text("Такой даты не существует. Попробуй ещё раз: ДД.ММ.ГГГГ")
        return ConversationHandler.END

    data = load()
    chat = str(update.effective_chat.id)
    data.setdefault(chat, []).append({"name": None, "date": d.strftime("%Y-%m-%d")})
    save(data)
    context.user_data["idx"] = len(data[chat]) - 1

    await update.message.reply_text(
        f"Дата {d.strftime('%d.%m.%Y')} записана ✅\nТеперь напиши фамилию и имя."
    )
    return ASK_NAME


async def got_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    data = load()
    chat = str(update.effective_chat.id)
    friend = data[chat][context.user_data["idx"]]
    friend["name"] = name
    save(data)

    d = datetime.strptime(friend["date"], "%Y-%m-%d")
    await update.message.reply_text(f"Готово 🎂 {name} — {d.strftime('%d.%m.%Y')}")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # убираем запись без имени
    data = load()
    chat = str(update.effective_chat.id)
    idx = context.user_data.get("idx")
    if idx is not None and idx < len(data.get(chat, [])) and data[chat][idx]["name"] is None:
        data[chat].pop(idx)
        save(data)
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END


async def list_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    friends = [f for f in load().get(str(update.effective_chat.id), []) if f["name"]]
    if not friends:
        await update.message.reply_text("Список пуст.")
        return
    today = datetime.now(TZ).date()
    rows = []
    for f in friends:
        born = datetime.strptime(f["date"], "%Y-%m-%d").date()
        rows.append((days_until(born, today), born, f["name"]))
    rows.sort(key=lambda r: r[0])  # ближайшие сверху
    lines = [
        f"{name} — {born.strftime('%d.%m.%Y')} ({days_text(n)})"
        for n, born, name in rows
    ]
    await update.message.reply_text("\n".join(lines))


async def daily_check(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(TZ).date()
    for chat, friends in load().items():
        for f in friends:
            if not f["name"]:
                continue
            born = datetime.strptime(f["date"], "%Y-%m-%d").date()
            n = days_until(born, today)
            if n not in REMIND_DAYS:
                continue
            age = (today + timedelta(days=n)).year - born.year
            if n == 0:
                text = f"🎉 Сегодня день рождения у {f['name']} ({age} лет). Не забудь поздравить!"
            else:
                text = f"⏳ {days_text(n).capitalize()} день рождения у {f['name']} ({age} лет). Осталось дней: {n}"
            await context.bot.send_message(chat_id=int(chat), text=text)


def main():
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(DATE_PATTERN), got_date)],
        states={ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_name)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_all))
    app.add_handler(conv)
    app.job_queue.run_daily(daily_check, REMIND_AT)

    app.run_polling()


if __name__ == "__main__":
    main()
