"""SQLite-хранилище вакансий, анализов, писем и резюме."""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime

from .config import DB_PATH
from .models import Job

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    source TEXT, external_id TEXT,
    title TEXT, company TEXT, location TEXT, url TEXT,
    description TEXT, salary TEXT, posted TEXT,
    dedup_key TEXT,
    fetched_at TEXT,
    status TEXT DEFAULT 'new',
    score INTEGER,
    analysis TEXT,
    letter TEXT,
    tailoring TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_dedup ON jobs(dedup_key);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS resume (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    text TEXT, profile TEXT, updated_at TEXT
);
"""

# Колонки, добавленные после первой версии: дописываются в старую базу при подключении
EXTRA_COLUMNS = {
    "filtered": "TEXT",      # почему автопилот отсеял вакансию (без ИИ)
    "cv": "TEXT",            # JSON: резюме под эту вакансию {lang, resume, changes_made, questions_for_user}
    "app_dir": "TEXT",       # папка с готовыми PDF и письмом
    "prepared_at": "TEXT",
    "applied_at": "TEXT",
    "notified_at": "TEXT",   # когда отклик отправлен в Telegram
}

STATUSES = {
    "new": "🆕 Новая",
    "ready": "📦 Готово к отправке",
    "interesting": "⭐ Интересная",
    "applied": "📨 Отправлено",
    "interview": "🗣 Собеседование",
    "rejected": "❌ Отказ",
    "hidden": "🙈 Скрыта",
}


@contextmanager
def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        con.executescript(SCHEMA)
        have = {r["name"] for r in con.execute("PRAGMA table_info(jobs)")}
        for col, typ in EXTRA_COLUMNS.items():
            if col not in have:
                con.execute(f"ALTER TABLE jobs ADD COLUMN {col} {typ}")
        yield con
        con.commit()
    finally:
        con.close()


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def dedup_key(job: Job) -> str:
    return f"{_norm(job.title)}|{_norm(job.company)}"


def save_jobs(jobs: list[Job]) -> int:
    """Сохраняет новые вакансии, пропуская дубли. Возвращает число новых."""
    return len(save_jobs_ids(jobs))


def save_jobs_ids(jobs: list[Job]) -> list[str]:
    """Сохраняет новые вакансии, пропуская дубли (тот же id или то же название+компания). Возвращает id новых."""
    added: list[str] = []
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as con:
        for j in jobs:
            key = dedup_key(j)
            exists = con.execute(
                "SELECT 1 FROM jobs WHERE id=? OR (dedup_key=? AND dedup_key != '|')", (j.uid, key)
            ).fetchone()
            if exists:
                # чиним старые записи Job Today без ссылки, если теперь ссылка нашлась
                if j.url and j.url.rstrip("/") != "https://jobtoday.com":
                    con.execute("UPDATE jobs SET url=? WHERE (id=? OR dedup_key=?) AND url IN ('', 'https://jobtoday.com')",
                                (j.url, j.uid, key))
                continue
            con.execute(
                "INSERT INTO jobs (id, source, external_id, title, company, location, url, description,"
                " salary, posted, dedup_key, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (j.uid, j.source, j.external_id, j.title, j.company, j.location, j.url,
                 j.description, j.salary, j.posted, key, now),
            )
            added.append(j.uid)
    return added


def list_jobs(min_score: int | None = None, sources: list[str] | None = None,
              statuses: list[str] | None = None, analyzed: bool | None = None) -> list[dict]:
    q, args = "SELECT rowid AS rid, * FROM jobs WHERE 1=1", []
    if min_score is not None:
        q += " AND (score IS NULL OR score >= ?)"
        args.append(min_score)
    if sources:
        q += f" AND source IN ({','.join('?' * len(sources))})"
        args += sources
    if statuses:
        q += f" AND status IN ({','.join('?' * len(statuses))})"
        args += statuses
    if analyzed is True:
        q += " AND analysis IS NOT NULL"
    elif analyzed is False:
        q += " AND analysis IS NULL"
    q += " ORDER BY score IS NULL, score DESC, fetched_at DESC"
    with connect() as con:
        rows = [dict(r) for r in con.execute(q, args)]
    for r in rows:
        for f in JSON_FIELDS:
            r[f] = json.loads(r[f]) if r[f] else None
    return rows


JSON_FIELDS = ("analysis", "tailoring", "cv")


def get_job(job_id: str | int) -> dict | None:
    """По id вакансии или по короткому номеру rowid (он нужен кнопкам Telegram: лимит 64 байта)."""
    col = "rowid" if isinstance(job_id, int) else "id"
    with connect() as con:
        row = con.execute(f"SELECT rowid AS rid, * FROM jobs WHERE {col}=?", (job_id,)).fetchone()
    if not row:
        return None
    r = dict(row)
    for f in JSON_FIELDS:
        r[f] = json.loads(r[f]) if r[f] else None
    return r


def update_job(job_id: str, **fields) -> None:
    for f in JSON_FIELDS:
        if f in fields and fields[f] is not None and not isinstance(fields[f], str):
            fields[f] = json.dumps(fields[f], ensure_ascii=False)
    sets = ", ".join(f"{k}=?" for k in fields)
    with connect() as con:
        con.execute(f"UPDATE jobs SET {sets} WHERE id=?", [*fields.values(), job_id])


def delete_job(job_id: str) -> None:
    with connect() as con:
        con.execute("DELETE FROM jobs WHERE id=?", (job_id,))


def stats() -> dict:
    with connect() as con:
        total = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        analyzed = con.execute("SELECT COUNT(*) FROM jobs WHERE analysis IS NOT NULL").fetchone()[0]
        by_source = dict(con.execute("SELECT source, COUNT(*) FROM jobs GROUP BY source").fetchall())
    return {"total": total, "analyzed": analyzed, "by_source": by_source}


def save_resume(text: str, profile: dict | None) -> None:
    with connect() as con:
        con.execute(
            "INSERT INTO resume (id, text, profile, updated_at) VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET text=excluded.text, profile=excluded.profile, updated_at=excluded.updated_at",
            (text, json.dumps(profile, ensure_ascii=False) if profile else None,
             datetime.now().isoformat(timespec="seconds")),
        )


def load_resume() -> tuple[str, dict | None]:
    with connect() as con:
        row = con.execute("SELECT text, profile FROM resume WHERE id=1").fetchone()
    if not row:
        return "", None
    return row["text"] or "", json.loads(row["profile"]) if row["profile"] else None


def kv_get(key: str):
    with connect() as con:
        row = con.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else None


def kv_set(key: str, value) -> None:
    with connect() as con:
        con.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(value, ensure_ascii=False)))
