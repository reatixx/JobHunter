"""База фактов (эталон) о кандидате и уточняющие вопросы агентов.

Резюме, письма и оценка вакансий строятся по базе фактов, а не по старому резюме:
так агенты не разносят по откликам то, чего не было. Пункты с пометкой «УТОЧНИТЬ»
агенты не используют, а задают по ним вопросы (в Telegram и во вкладке «База фактов»).
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime

from . import db
from .config import DATA_DIR

FACTS_KEY = "facts"
QA_KEY = "facts_qa"
MASTER_DIR = DATA_DIR / "master"
MAX_OPEN_QUESTIONS = 12

TRACKS = {
    "support": {
        "name": "Поддержка",
        "roles": ["Técnico de soporte IT", "Técnico de soporte N1/N2", "Helpdesk", "Soporte de aplicaciones",
                  "Atención al cliente técnico con ruso", "Técnico de sistemas junior"],
        "focus": ("Трек «поддержка»: позиционируй кандидата как специалиста технической поддержки (soporte IT N1/N2, "
                  "helpdesk, soporte de aplicaciones). На первый план: работа с заявками и инцидентами, доступы, "
                  "диагностика, связь между пользователями и разработчиками, языки (русский родной + испанский). "
                  "Веб-навыки (HTML/CSS и т.п.) — как дополнительный технический плюс, а не главное."),
    },
    "frontend": {
        "name": "Фронтенд",
        "roles": ["Desarrollador Frontend Junior", "Maquetador web HTML/CSS", "Desarrollador web junior"],
        "focus": ("Трек «фронтенд»: позиционируй кандидата как junior-разработчика / верстальщика. На первый план: "
                  "HTML, CSS, JavaScript, шаблоны, курсы и учебные проекты. Опыт поддержки — как понимание "
                  "пользователей и продакшн-инцидентов."),
    },
}

_FRONTEND = re.compile(r"front|react|angular|vue|javascript|\bjs\b|typescript|maquetad|desarrollador|developer|"
                       r"programador|web|full ?stack|software engineer", re.I)


def job_track(job: dict) -> str:
    return "frontend" if _FRONTEND.search(job.get("title") or "") else "support"


# --- база фактов -------------------------------------------------------------------
def get_facts() -> str:
    return (db.kv_get(FACTS_KEY) or {}).get("text", "")


def set_facts(text: str) -> None:
    db.kv_set(FACTS_KEY, {"text": text, "updated": datetime.now().isoformat(timespec="seconds")})


def source_text(fallback_resume: str = "") -> str:
    """Текст, по которому агенты пишут резюме и письма: база фактов + ответы на вопросы."""
    facts = get_facts().strip()
    if not facts:
        return fallback_resume
    answered = [q for q in get_qa() if q["status"] == "answered"]
    if answered:
        facts += "\n\n## Ответы кандидата на уточняющие вопросы (это проверенные факты)\n" + "\n".join(
            f"- В: {q['q']}\n  О: {q['a']}" for q in answered)
    return facts


# --- вопросы ---------------------------------------------------------------------------
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
    """Добавляет новые вопросы, пропуская похожие на уже заданные (в т.ч. отвеченные). Возвращает добавленные."""
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


# --- мастер-резюме по трекам --------------------------------------------------------------
def track_ats(settings: dict, track: str, lang: str) -> dict:
    """ATS-профиль трека: по вакансиям этого трека в базе, иначе по знаниям рынка. Кэшируется."""
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
    """Полностью переписывает резюме из базы фактов под трек. Возвращает ResumeRewrite + путь к PDF."""
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
    add_questions(rw["questions_for_user"], f"мастер-резюме: {TRACKS[track]['name']}")
    return rw
