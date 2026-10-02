"""Асинхронное хранение Birthday Manager в PostgreSQL."""
import csv
import io
import json
import os
import asyncpg

pool: asyncpg.Pool | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    chat_id BIGINT PRIMARY KEY,
    notify BOOLEAN NOT NULL DEFAULT TRUE,
    remind_time TEXT NOT NULL DEFAULT '09:00',
    remind_days INTEGER[] NOT NULL DEFAULT '{7,3,1,0}',
    last_sent DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS people (
    id SERIAL PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    birth DATE NOT NULL,
    category TEXT NOT NULL DEFAULT 'other',
    has_year BOOLEAN NOT NULL DEFAULT TRUE,
    note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS people_chat_idx ON people (chat_id);
CREATE INDEX IF NOT EXISTS people_birth_idx ON people (chat_id, birth);
"""

async def init(url: str):
    global pool
    pool = await asyncpg.create_pool(url, min_size=1, max_size=5)
    async with pool.acquire() as con:
        await con.execute(SCHEMA)
        # Безопасная миграция существующей БД happy/birthday-manager.
        await con.execute("ALTER TABLE people ADD COLUMN IF NOT EXISTS has_year BOOLEAN NOT NULL DEFAULT TRUE")
        await con.execute("ALTER TABLE people ADD COLUMN IF NOT EXISTS note TEXT NOT NULL DEFAULT ''")
        await con.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now()")

async def close():
    if pool:
        await pool.close()

async def get_user(chat_id: int) -> dict:
    await pool.execute("INSERT INTO users (chat_id) VALUES ($1) ON CONFLICT DO NOTHING", chat_id)
    row = await pool.fetchrow("SELECT * FROM users WHERE chat_id=$1", chat_id)
    return dict(row)

async def update_user(chat_id: int, **fields):
    allowed = {"notify", "remind_time", "remind_days"}
    for col, val in fields.items():
        if col not in allowed:
            raise ValueError(col)
        await pool.execute(f"UPDATE users SET {col}=$2 WHERE chat_id=$1", chat_id, val)

async def due_users(today, hhmm: str):
    rows = await pool.fetch(
        "SELECT * FROM users WHERE notify AND remind_time <= $2 "
        "AND (last_sent IS NULL OR last_sent < $1)", today, hhmm)
    return [dict(r) for r in rows]

async def mark_sent(chat_id: int, today):
    await pool.execute("UPDATE users SET last_sent=$2 WHERE chat_id=$1", chat_id, today)

async def add_person(chat_id: int, name: str, birth, category="other", has_year=True, note="") -> int:
    # у каждого, у кого есть записи, должна быть строка настроек — иначе не придут напоминания
    await pool.execute("INSERT INTO users (chat_id) VALUES ($1) ON CONFLICT DO NOTHING", chat_id)
    return await pool.fetchval(
        "INSERT INTO people (chat_id,name,birth,category,has_year,note) VALUES ($1,$2,$3,$4,$5,$6) RETURNING id",
        chat_id, name, birth, category, has_year, note[:300])

async def person_exists(chat_id: int, name: str, birth) -> bool:
    return bool(await pool.fetchval(
        "SELECT 1 FROM people WHERE chat_id=$1 AND lower(name)=lower($2) AND birth=$3 LIMIT 1",
        chat_id, name.strip(), birth))

async def list_people(chat_id: int, category: str | None = None):
    if category:
        rows = await pool.fetch("SELECT * FROM people WHERE chat_id=$1 AND category=$2", chat_id, category)
    else:
        rows = await pool.fetch("SELECT * FROM people WHERE chat_id=$1", chat_id)
    return [dict(r) for r in rows]

async def get_person(chat_id: int, pid: int):
    row = await pool.fetchrow("SELECT * FROM people WHERE chat_id=$1 AND id=$2", chat_id, pid)
    return dict(row) if row else None

async def update_person(chat_id: int, pid: int, **fields):
    allowed = {"name", "birth", "category", "has_year", "note"}
    for col, val in fields.items():
        if col not in allowed:
            raise ValueError(col)
        await pool.execute(f"UPDATE people SET {col}=$3 WHERE chat_id=$1 AND id=$2", chat_id, pid, val)

async def delete_person(chat_id: int, pid: int):
    await pool.execute("DELETE FROM people WHERE chat_id=$1 AND id=$2", chat_id, pid)

async def import_legacy(path: str) -> int:
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    count = 0
    for chat, friends in data.items():
        chat_id = int(chat)
        if await pool.fetchval("SELECT 1 FROM people WHERE chat_id=$1 LIMIT 1", chat_id):
            continue
        for fr in friends:
            if not fr.get("name") or not fr.get("date"):
                continue
            from datetime import datetime
            birth = datetime.strptime(fr["date"], "%Y-%m-%d").date()
            await add_person(chat_id, fr["name"], birth, "other", True)
            count += 1
    return count

async def export_csv(chat_id: int) -> bytes:
    rows = await pool.fetch("SELECT name,birth,category,has_year,note FROM people WHERE chat_id=$1 ORDER BY birth, name", chat_id)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Имя", "Дата рождения", "Категория", "Год известен", "Заметка"])
    for r in rows:
        writer.writerow([r["name"], r["birth"].strftime("%d.%m.%Y" if r["has_year"] else "%d.%m"), r["category"], "Да" if r["has_year"] else "Нет", r["note"]])
    return out.getvalue().encode("utf-8-sig")
