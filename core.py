"""Чистые функции без Telegram и БД: даты, склонения, таблицы, календарь."""
import calendar
import re
from datetime import date, datetime, timedelta

# В названии у тебя "фамилия и имя" — значит имя идёт вторым словом.
# Если вводишь "Имя Фамилия", поставь False.
SURNAME_FIRST = True

CATEGORIES = {
    "family": ("👨‍👩‍👧", "Семья"),
    "classmates": ("👨‍🎓", "Одногруппники"),
    "friends": ("❤️", "Близкие друзья"),
    "university": ("🏥", "Университет"),
    "work": ("💼", "Работа"),
    "other": ("📌", "Другое"),
}
CAT_ALIASES = {
    "семья": "family", "семьи": "family",
    "одногруппники": "classmates", "группа": "classmates",
    "друзья": "friends", "друг": "friends", "близкие": "friends",
    "университет": "university", "вуз": "university",
    "работа": "work", "коллеги": "work",
    "другое": "other",
}

MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль",
          "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]

DATE_RE = re.compile(r"^\s*\d{1,2}[./-]\d{1,2}(?:[./-]\d{4})?\s*$")
_DATE = r"(\d{1,2}[./-]\d{1,2}(?:[./-]\d{4})?)"
_NAME_DATE = re.compile(rf"^\s*(.+?)[\s—–:,-]*{_DATE}\s*$")
_DATE_NAME = re.compile(rf"^\s*{_DATE}[\s—–:,-]*(.+?)\s*$")
TIME_RE = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*$")


def find_category(text: str):
    t = text.strip().casefold()
    if len(t) < 3:
        return None
    for alias, key in CAT_ALIASES.items():
        if alias.startswith(t) or t.startswith(alias[:4]):
            return key
    return None


def cat_label(key: str) -> str:
    emoji, title = CATEGORIES.get(key, CATEGORIES["other"])
    return f"{emoji} {title}"


# ---------- даты ----------
def parse_date(text: str):
    """Вернуть date. Если год не указан, используется 2000 как служебный год."""
    t = re.sub(r"[-/]", ".", text.strip())
    for fmt in ("%d.%m.%Y", "%d.%m"):
        try:
            d = datetime.strptime(t, fmt).date() if fmt.endswith("%Y") else datetime.strptime(t + ".2000", "%d.%m.%Y").date()
            if fmt.endswith("%Y") and (d.year < 1900 or d > date.today()):
                return None
            return d
        except ValueError:
            continue
    return None


def has_birth_year(text: str) -> bool:
    return bool(re.fullmatch(r"\s*\d{1,2}[./-]\d{1,2}[./-]\d{4}\s*", text))


def birth_str(p) -> str:
    """Дата для показа: без служебного года 2000, если год неизвестен."""
    return p["birth"].strftime("%d.%m.%Y" if p.get("has_year", True) else "%d.%m")


def parse_bulk(text: str):
    """Строки вида 'Иванов Иван 18.01.2007' или '18.01.2007 Иванов Иван'."""
    result = []
    for line in text.splitlines():
        if not line.strip():
            continue
        m = _NAME_DATE.match(line)
        if m:
            name, raw = m.group(1), m.group(2)
        else:
            m = _DATE_NAME.match(line)
            if not m:
                return []
            raw, name = m.group(1), m.group(2)
        d = parse_date(raw)
        if not d:
            return []
        result.append((name.strip()[:60], d, has_birth_year(raw)))
    return result


def parse_time(text: str):
    m = TIME_RE.match(text)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return f"{h:02d}:{mi:02d}"


def days_until(born: date, today: date) -> int:
    for year in (today.year, today.year + 1):
        try:
            nxt = born.replace(year=year)
        except ValueError:  # 29 февраля в невисокосный год
            nxt = date(year, 3, 1)
        if nxt >= today:
            return (nxt - today).days
    return 0


def next_age(born: date, today: date) -> int:
    return (today + timedelta(days=days_until(born, today))).year - born.year


def plural(n: int, forms) -> str:
    n = abs(n) % 100
    if 11 <= n <= 19:
        return forms[2]
    n %= 10
    if n == 1:
        return forms[0]
    if 2 <= n <= 4:
        return forms[1]
    return forms[2]


def years_text(n: int) -> str:
    return f"{n} {plural(n, ('год', 'года', 'лет'))}"


def days_text(n: int) -> str:
    if n == 0:
        return "сегодня 🎉"
    if n == 1:
        return "завтра"
    return f"через {n} {plural(n, ('день', 'дня', 'дней'))}"


def first_name(full: str) -> str:
    parts = full.split()
    if len(parts) > 1 and SURNAME_FIRST:
        return parts[1]
    return parts[0] if parts else full


# ---------- оформление ----------
def _cut(s: str, w: int) -> str:
    return s if len(s) <= w else s[: w - 1] + "…"


def table(people, today: date, start: int = 1) -> str:
    """Моноширинная таблица; people уже отсортированы. Вернуть нужно внутри <pre>."""
    lines = [f"{'№':<3}{'Имя':<12}{'Дата':<11}{'Будет':<6}До ДР",
             "─" * 38]
    for i, p in enumerate(people, start):
        n = days_until(p["birth"], today)
        left = "сегодня" if n == 0 else "завтра" if n == 1 else f"{n} дн."
        age = str(next_age(p["birth"], today)) if p.get("has_year", True) else "—"
        lines.append(
            f"{i:<3}{_cut(p['name'], 11):<12}{birth_str(p):<11}{age:<6}{left}"
        )
    return "\n".join(lines)


def month_grid(year: int, month: int, marks) -> str:
    rows = ["Пн Вт Ср Чт Пт Сб Вс"]
    for week in calendar.monthcalendar(year, month):
        cells = []
        for d in week:
            if d == 0:
                cells.append("   ")
            else:
                cells.append(f"{d:2d}" + ("*" if d in marks else " "))
        rows.append("".join(cells).rstrip())
    return "\n".join(rows)


def shift_month(year: int, month: int, delta: int):
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1
