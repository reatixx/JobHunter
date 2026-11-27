"""SQLite storage for jobs, analyses, letters and resumes."""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime

from .config import DB_PATH
from .models import Job
from .i18n import tr

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
-- what happened to each sent application: viewed, rejected, interview, reply…
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    at TEXT NOT NULL,
    note TEXT DEFAULT '',
    UNIQUE (job_id, kind)
);
"""

# Columns added after the first version: added to an existing database on connect
EXTRA_COLUMNS = {
    "filtered": "TEXT",      # why the autopilot filtered the job out (without AI)
    "cv": "TEXT",            # JSON: resume for this job {lang, resume, changes_made, questions_for_user}
    "app_dir": "TEXT",       # folder with the prepared PDFs and letter
    "prepared_at": "TEXT",
    "applied_at": "TEXT",
    "notified_at": "TEXT",   # when the application was sent to Telegram
    "queued_at": "TEXT",     # the user asked Claude to send this application (Telegram button)
    "channel": "TEXT",       # where the application was sent: linkedin, infojobs, company site…
}

# Application events, in funnel order. A later event moves the job status forward (never back).
EVENT_KINDS = ("applied", "viewed", "reply", "interview", "offer", "rejected")
EVENT_STATUS = {"applied": "applied", "viewed": "viewed", "reply": "viewed", "interview": "interview",
                "offer": "interview", "rejected": "rejected"}

STATUSES = {
    "new": tr("🆕 New"),
    "ready": tr("📦 Ready to send"),
    "interesting": tr("⭐ Interesting"),
    "applied": tr("📨 Sent"),
    "viewed": tr("👀 Viewed"),
    "interview": tr("🗣 Interview"),
    "rejected": tr("❌ Rejected"),
    "hidden": tr("🙈 Hidden"),
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
    """Saves new jobs, skipping duplicates. Returns the number of new ones."""
    return len(save_jobs_ids(jobs))


def save_jobs_ids(jobs: list[Job]) -> list[str]:
    """Saves new jobs, skipping duplicates (same id or same title+company). Returns the new ids."""
    added: list[str] = []
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as con:
        for j in jobs:
            key = dedup_key(j)
            exists = con.execute(
                "SELECT 1 FROM jobs WHERE id=? OR (dedup_key=? AND dedup_key != '|')", (j.uid, key)
            ).fetchone()
            if exists:
                # repair old Job Today rows without a link once a link is found
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
    """By job id or by the short rowid (needed for Telegram buttons: 64-byte limit)."""
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


# --- application tracking -------------------------------------------------------------
_RANK = {"applied": 0, "viewed": 1, "interview": 2, "rejected": 3}


def add_event(job_id: str, kind: str, at: str | None = None, note: str = "") -> bool:
    """Records what happened to an application. Returns False if this event was already recorded."""
    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown event kind: {kind}")
    at = at or datetime.now().isoformat(timespec="seconds")
    with connect() as con:
        cur = con.execute("INSERT OR IGNORE INTO events (job_id, kind, at, note) VALUES (?,?,?,?)",
                          (job_id, kind, at, note))
        if not cur.rowcount:
            return False
        row = con.execute("SELECT status, applied_at FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row:
            new = EVENT_STATUS[kind]
            if _RANK.get(new, 0) >= _RANK.get(row["status"], -1):
                con.execute("UPDATE jobs SET status=? WHERE id=?", (new, job_id))
            if kind == "applied" and not row["applied_at"]:
                con.execute("UPDATE jobs SET applied_at=? WHERE id=?", (at, job_id))
    return True


def add_application(company: str, title: str, channel: str, applied_at: str,
                    url: str = "", location: str = "") -> str:
    """An application sent outside the autopilot (by hand on a job site). Returns the job id."""
    job = Job(source="manual", external_id=_norm(f"{company} {title}").replace(" ", "-")[:80],
              title=title, company=company, location=location, url=url)
    with connect() as con:
        con.execute(
            "INSERT OR IGNORE INTO jobs (id, source, external_id, title, company, location, url, dedup_key,"
            " fetched_at, status, applied_at, channel) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (job.uid, job.source, job.external_id, title, company, location, url, dedup_key(job),
             applied_at, "applied", applied_at, channel))
    add_event(job.uid, "applied", applied_at)
    return job.uid


def find_applications(text: str) -> list[dict]:
    """Sent applications whose company or title contains the text (for marking events by name)."""
    needle = _norm(text)
    with connect() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT id, company, title, status, applied_at FROM jobs WHERE applied_at IS NOT NULL")]
    return [r for r in rows if needle in _norm(f"{r['company']} {r['title']}")]


def applications() -> list[dict]:
    """All sent applications with their events: {..job, events: {kind: at}}."""
    with connect() as con:
        jobs = [dict(r) for r in con.execute(
            "SELECT id, source, channel, title, company, location, status, applied_at FROM jobs"
            " WHERE applied_at IS NOT NULL ORDER BY applied_at")]
        ev: dict[str, dict] = {}
        for r in con.execute("SELECT job_id, kind, at FROM events"):
            ev.setdefault(r["job_id"], {})[r["kind"]] = r["at"]
    for j in jobs:
        j["events"] = ev.get(j["id"], {})
    return jobs
