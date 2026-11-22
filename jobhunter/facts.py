"""Fact base (source of truth) about the candidate, and the agents' follow-up questions.

Resumes, letters and job scoring are built from the fact base, not from the old resume,
so the agents don't spread things that never happened. Items marked "TO CLARIFY"
are not used; the agents ask about them instead (in Telegram and in the "Fact base" tab).
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime

from . import db
from .config import DATA_DIR
from .i18n import tr

FACTS_KEY = "facts"
QA_KEY = "facts_qa"
MASTER_DIR = DATA_DIR / "master"
MAX_OPEN_QUESTIONS = 12

TRACKS = {
    "support": {
        "name": tr("Support"),
        "roles": ["Técnico de soporte IT", "Técnico de soporte N1/N2", "Helpdesk", "Soporte de aplicaciones",
                  "Atención al cliente técnico con ruso", "Técnico de sistemas junior"],
        "focus": ("Track «support»: position the candidate as a technical support specialist (soporte IT "
                  "N1/N2, helpdesk, soporte de aplicaciones). Put first: handling tickets and incidents, "
                  "access management, troubleshooting, liaising between users and developers, languages. Web "
                  "skills (HTML/CSS, etc.) are an extra technical plus, not the main thing."),
    },
    "frontend": {
        "name": tr("Frontend"),
        "roles": ["Desarrollador Frontend Junior", "Maquetador web HTML/CSS", "Desarrollador web junior"],
        "focus": ("Track «frontend»: position the candidate as a junior developer / HTML-CSS developer. Put "
                  "first: HTML, CSS, JavaScript, templates, courses and study projects. Support experience "
                  "shows understanding of users and production incidents."),
    },
}

_FRONTEND = re.compile(r"front|react|angular|vue|javascript|\bjs\b|typescript|maquetad|desarrollador|developer|"
                       r"programador|web|full ?stack|software engineer", re.I)


def job_track(job: dict) -> str:
    return "frontend" if _FRONTEND.search(job.get("title") or "") else "support"


# --- fact base -------------------------------------------------------------------
def get_facts() -> str:
    return (db.kv_get(FACTS_KEY) or {}).get("text", "")


def set_facts(text: str) -> None:
    db.kv_set(FACTS_KEY, {"text": text, "updated": datetime.now().isoformat(timespec="seconds")})


def source_text(fallback_resume: str = "") -> str:
    """Text the agents use for resumes and letters: fact base + answers to questions."""
    facts = get_facts().strip()
    if not facts:
        return fallback_resume
    answered = [q for q in get_qa() if q["status"] == "answered"]
    if answered:
        facts += "\n\n## Candidate's answers to follow-up questions (these are verified facts)\n" + "\n".join(
            f"- Q: {q['q']}\n  A: {q['a']}" for q in answered)
    return facts


# --- questions ---------------------------------------------------------------------------
def get_qa() -> list[dict]:
    return db.kv_get(QA_KEY) or []


def _save_qa(qa: list[dict]) -> None:
    db.kv_set(QA_KEY, qa)


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"\w{4,}", s.lower())}


def _similar(a: str, b: str) -> bool:
    wa, wb = _words(a), _words(b)
    return bool(wa and wb) and len(wa & wb) / min(len(wa), len(wb)) >= 0.6


def add_questions(questions: list[str], context: str = "") -> list[dict]:
    """Adds new questions, skipping ones similar to those already asked (including answered). Returns the added ones."""
    qa = get_qa()
    open_n = sum(q["status"] == "open" for q in qa)
    added = []
    for text in questions:
        text = text.strip()
        if not text or open_n >= MAX_OPEN_QUESTIONS or any(_similar(text, q["q"]) for q in qa):
            continue
        item = {"id": uuid.uuid4().hex[:8], "q": text, "a": "", "status": "open", "context": context,
                "asked": datetime.now().isoformat(timespec="seconds"), "tg_msg": None}
        qa.append(item)
        added.append(item)
        open_n += 1
    _save_qa(qa)
    return added


def open_questions() -> list[dict]:
    return [q for q in get_qa() if q["status"] == "open"]


def update_question(qid: str, **fields) -> dict | None:
    qa = get_qa()
    for q in qa:
        if q["id"] == qid:
            q.update(fields)
            _save_qa(qa)
            return q
    return None


def answer(qid: str, text: str) -> dict | None:
    return update_question(qid, a=text.strip(), status="answered",
                           answered=datetime.now().isoformat(timespec="seconds"))


def skip(qid: str) -> dict | None:
    return update_question(qid, status="skipped")


def by_tg_message(message_id: int) -> dict | None:
    return next((q for q in get_qa() if q.get("tg_msg") == message_id), None)


# --- master resumes per track --------------------------------------------------------------
def track_ats(settings: dict, track: str, lang: str) -> dict:
    """Track ATS profile: from this track's jobs in the database, otherwise from market knowledge. Cached."""
    from .agents import AtsAuditor
    key = f"ats_{track}_{lang}"
    if cached := db.kv_get(key):
        return cached
    pool = [j for j in db.list_jobs(statuses=["new", "ready", "interesting", "applied", "interview"])
            if job_track(j) == track and (j["score"] or 0) >= 45]
    ats = AtsAuditor(settings).run(pool, {"target_roles": TRACKS[track]["roles"]}, lang)
    db.kv_set(key, ats)
    return ats


def build_master(settings: dict, track: str) -> dict:
    """Fully rewrites the resume from the fact base for a track. Returns ResumeRewrite + PDF path."""
    from .agents import ResumeWriter, resume_language
    from .ats import keyword_coverage
    from .render import to_pdf
    original, _ = db.load_resume()
    lang = resume_language(settings, original)
    src = source_text(original)
    ats = track_ats(settings, track, lang)
    rw = ResumeWriter(settings).run(src, ats, keyword_coverage(src, ats["keywords"]), [], lang, None, track=track)
    MASTER_DIR.mkdir(parents=True, exist_ok=True)
    pdf = MASTER_DIR / f"CV_{'_'.join((rw['resume'].get('full_name') or 'CV').split())}_{track}.pdf"
    pdf.write_bytes(to_pdf(rw["resume"], lang))
    rw["pdf"], rw["lang"] = str(pdf), lang
    db.kv_set(f"master_{track}", rw)
    add_questions(rw["questions_for_user"], tr("master resume: {0}", TRACKS[track]['name']))
    return rw
