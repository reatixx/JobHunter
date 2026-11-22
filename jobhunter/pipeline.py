"""Autopilot: search → filters → scoring → tailored resume and letter → "Ready to send" queue.

Run:      .venv\\Scripts\\python.exe -m jobhunter.pipeline [--skip-search] [--max-prepare N]
Schedule: python -m jobhunter.pipeline --install-schedule   (times from the auto_times setting)
          python -m jobhunter.pipeline --remove-schedule

It never sends anything: it only prepares applications. The user confirms each one.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import db, facts
from .agents import AtsAuditor, CoverLetterWriter, JobAnalyst, ResumeWriter, resume_language
from .ats import detect_language, format_checks, keyword_coverage
from .config import DATA_DIR, ROOT, get_secret, load_settings
from .llm import LLMError, ollama_status
from .render import to_pdf
from .sources import REMOTE_SOURCES, RU_SOURCES, collect, reset_caches
from .sources.base import slugify

APPS_DIR = DATA_DIR / "applications"
LOG_PATH = DATA_DIR / "pipeline.log"
LOCK_PATH = DATA_DIR / "pipeline.lock"
TASK_NAME = "JobHunter Autopilot"

log = logging.getLogger("jobhunter.pipeline")

# --- filters without AI ---------------------------------------------------------
_EN_LEVEL = r"(?:c1|c2|(?<!b1-)(?<!b1/)(?<!b1 )b2\+?|alto|avanzado|fluido|fluid[oa]|nativo|bilingüe|bilingue|excelente|muy bueno|proficient|fluent|native|advanced)"
_ENGLISH_REQUIRED = re.compile(
    rf"ingl[eé]s\W+(?:\w+\W+){{0,3}}?{_EN_LEVEL}"
    rf"|{_EN_LEVEL}\W+(?:\w+\W+){{0,3}}?(?:de\W+)?ingl[eé]s"
    rf"|(?:fluent|native|proficient|excellent|advanced|c1|c2)\W+(?:\w+\W+){{0,2}}?english"
    rf"|english\W+(?:\w+\W+){{0,3}}?(?:fluent|native|proficient|c1|c2|required|mandatory|essential)"
    rf"|английск\w*\W+(?:\w+\W+){{0,3}}?(?:b2|c1|c2|upper|advanced|fluent|свободн\w*|продвинут\w*|высок\w*|разговорн\w*)"
    rf"|(?:b2|c1|c2|upper[- ]intermediate|свободн\w*|продвинут\w*|высок\w*|разговорн\w*)\W+(?:\w+\W+){{0,2}}?английск",
    re.I,
)
_RUSSIAN = re.compile(r"russian|\bruso\b|\brusa\b|русск", re.I)

# "Valorable inglés B2" is a nice-to-have, not a requirement
_OPTIONAL = re.compile(r"valorable|se valorar[aá]|valoraremos|deseable|nice to have|a plus|is a bonus|opcional|"
                       r"плюсом|желательн|приветству|преимуществ", re.I)


def english_required(text: str) -> str:
    for m in _ENGLISH_REQUIRED.finditer(text):
        before = text[max(0, m.start() - 50):m.start()]
        after = text[m.end():m.end() + 25]
        # "Nice to have" after a match is usually the heading of the next block — ignore it
        if _OPTIONAL.search(before.split(".")[-1].split("\n")[-1]) or \
                re.search(r"^\W*(?:\w+\W+){0,3}?(?:is a plus|is a bonus|valorable|deseable|se valorar|будет плюсом|желательн|приветству)",
                          after.split(".")[0], re.I):
            continue
        return m.group(0).strip()[:60]
    return ""


_REMOTE = re.compile(r"remot[oae]|teletrabajo|en remoto|home ?office|work from home|wfh|100\s?% remot|anywhere", re.I)


# --- companies from Russia and Belarus (by job text; "except Russia" etc. doesn't count) ---
_RU_BY_NEG = re.compile(
    r"(?:кроме|вне|не из|не в|не для|за пределами|без|exclud\w*|except|outside(?: of)?)\s+"
    r"(?:рф|россии|беларуси|белоруссии|рб|russia|belarus)"
    r"(?:\s*(?:и|/|,|and)\s*(?:рф|россии|беларуси|белоруссии|рб|russia|belarus))?", re.I)
_RU_BY = re.compile(
    r"₽|\bруб\b|руб\.|рубл|\brur\b|\bbyn\b|тк рф|трудов\w* кодекс|аккредитованн|самозанят|"
    r"\bооо\b|\bпао\b|\bзао\b|\bоао\b|\bрф\b|росси[яию]|российск|беларус|белорус|\bрб\b|"
    r"москв|санкт-петербург|\bспб\b|петербург|новосибирск|екатеринбург|казан[ьи]|нижн\w* новгород|челябинск|"
    r"\bсамар[аеы]\b|\bомск|ростов|\bуф[аеы]\b|красноярск|воронеж|\bперм[ьи]\b|волгоград|краснодар|саратов|тюмен|"
    r"ижевск|барнаул|иркутск|хабаровск|ярославл|владивосток|\bтомск|оренбург|кемерово|рязан|калининград|"
    r"\bтул[аеы]\b|\bсочи\b|иннополис|минск|гомел|\bбрест|гродн|витебск|могил[её]в|"
    r"\brussia\b|\bbelarus\b|moscow|\bminsk\b|petersburg|"
    # large Russian companies — by name
    r"\bмтс\b|\bmts\b|сбер|\bsber|яндекс|yandex|тинькофф|tinkoff|т-банк|\bvk\b|\bвк\b|озон|\bozon\b|wildberries|"
    r"вайлдберриз|авито|\bavito\b|касперск|kaspersky|ростелеком|газпром|\bвтб\b|альфа-банк|\bx5\b|магнит\b|"
    r"лига цифровой|\b1с\b|мегафон|билайн|росатом|ланит|\bкрок\b|positive technologies|\bсибур", re.I)


def ru_by_marker(job: dict) -> str:
    text = "\n".join(str(job.get(k) or "") for k in ("title", "company", "location", "salary", "description"))
    m = _RU_BY.search(_RU_BY_NEG.sub(" ", text))
    return m.group(0) if m else ""


# --- no relocation: remote or close to home ---
_REMOTE_ANY = re.compile(r"remot[oae]|teletrabajo|home ?office|work from home|wfh|anywhere|"
                         r"удал[её]нн|удал[её]нк|дистанционн|из любой (?:точки|страны)", re.I)
_REMOTE_FULL = re.compile(r"100\s?%\s?(?:remot|teletrabajo)|full[- ]?remote|fully remote|totalmente remot|"
                          r"teletrabajo (?:total|completo)|remote[- ]first|from anywhere|полностью удал", re.I)
_ONSITE = re.compile(r"h[ií]brid|hybrid|(?<!no )(?<!sin )presencial|on-?site|in-?office|гибрид|в офисе", re.I)


def location_reason(job: dict, settings: dict) -> str:
    if not settings.get("no_relocation"):
        return ""
    loc = (job.get("location") or "").lower()
    if loc.startswith(("remoto:", "удалённо")):
        return ""
    if any(h.strip() and h.strip() in loc for h in settings.get("home_area", "").lower().split(",")):
        return ""
    text = f"{job.get('title', '')}\n{job.get('description', '')}"
    if _REMOTE_FULL.search(text):
        return ""
    if _REMOTE_ANY.search(text):
        return "" if not _ONSITE.search(text) else f"гибрид/офис не рядом с домом ({job.get('location') or '?'})"
    return f"офис не рядом с домом ({job.get('location') or 'город не указан'})"


_STOP = {
    "es": {"de", "la", "el", "en", "y", "los", "las", "del", "con", "para", "por", "una", "experiencia", "como", "que"},
    "en": {"the", "and", "of", "in", "with", "for", "to", "you", "we", "our", "experience", "will", "are"},
    "de": {"und", "der", "die", "das", "mit", "für", "wir", "sie", "ist", "auf", "eine", "ein", "zu", "bei"},
    "fr": {"et", "le", "les", "des", "du", "pour", "avec", "vous", "nous", "une", "est", "dans", "sur"},
    "pt": {"e", "o", "os", "da", "do", "das", "dos", "para", "com", "uma", "em", "você", "não", "experiência"},
    "it": {"e", "il", "di", "che", "per", "con", "una", "sono", "della", "nel", "esperienza", "lavoro"},
    "nl": {"en", "het", "van", "een", "voor", "met", "wij", "je", "jij", "zijn", "ervaring", "naar"},
}
LANG_RU = {"es": "испанском", "en": "английском", "de": "немецком", "fr": "французском",
           "pt": "португальском", "it": "итальянском", "nl": "нидерландском", "ru": "русском"}


def ad_language(text: str) -> str:
    """Language of the job text by frequent words (es/en/de/fr/pt/it/nl/ru)."""
    if detect_language(text) == "ru":
        return "ru"
    words = re.findall(r"[a-zà-ÿ]+", text.lower()[:4000])
    scores = {lang: sum(w in stop for w in words) for lang, stop in _STOP.items()}
    return max(scores, key=scores.get) if any(scores.values()) else "es"


def region_ok(location: str, settings: dict) -> bool:
    """For remote jobs: whether it is open to candidates in Spain (by region restriction)."""
    if not location.startswith("Remoto:"):
        return True
    loc = location.lower()
    if loc.removeprefix("remoto:").strip() in ("", "remote", "anywhere"):
        return True
    return any(t.strip() and t.strip() in loc for t in settings.get("remote_regions", "").lower().split(","))


# Management and senior roles: for a junior profile, don't spend model calls on them (auto_skip_senior).
_SENIOR = re.compile(
    r"\b(head|lead|leader|l[íi]der|manager|director|directora|senior|sr\.?|jefe|jefa|responsable|principal|"
    r"staff|architect|arquitect[oa]|vp|cto|cio|chief|руководител\w*|ведущ\w*|старш\w*|тимлид|лид)\b", re.I)


# Unpaid "internship" — usually selling courses disguised as a job.
_UNPAID = re.compile(
    r"неоплачиваем\w*|без оплаты|не оплачивается|бесплатн\w* стажир\w*|"
    r"no remunerad[oa]|sin remuneraci[óo]n|pr[áa]cticas no remuneradas|unpaid|without pay|no salary", re.I)


# mandatory driving licence/car (auto_skip_license, if you have none); "valorable" is not filtered out
_LICENSE = re.compile(r"carn[eé]t? de conducir|permiso de conducir|carn[eé]t? B\b|veh[ií]culo propio|driving licen[cs]e|"
                      r"водительск\w* (?:прав|удостоверени)", re.I)
_LIC_OPTIONAL = re.compile(r"valor|deseable|plus|preferib|se tendr[aá] en cuenta|nice to have|ideal", re.I)


def license_required(text: str) -> str:
    for m in _LICENSE.finditer(text):
        sent = re.split(r"[.\n•;]", text[max(0, m.start() - 150):m.end() + 60])
        around = next((x for x in sent if m.group(0) in x), m.group(0))
        if not _LIC_OPTIONAL.search(around):
            return m.group(0)
    return ""



def filter_reason(job: dict, settings: dict) -> str:
    """Why the job should be filtered out right away, without a model call. Empty string = keep."""
    text = f"{job.get('title', '')}\n{job.get('description', '')}"
    if settings.get("auto_skip_senior", True) and (m := _SENIOR.search(job.get("title") or "")):
        return f"руководящая/старшая роль («{m.group(0)}»)"
    if m := _UNPAID.search(text):
        return f"неоплачиваемая («{m.group(0)}»)"
    if settings.get("auto_skip_license", True) and (m := license_required(text)):
        return f"нужны водительские права («{m}»)"
    if not region_ok(job.get("location") or "", settings):
        return f"недоступна из Испании ({job['location'].removeprefix('Remoto:').strip()[:60]})"
    if settings.get("skip_ru_by", True) and (m := ru_by_marker(job)):
        return f"похоже на компанию из России/Беларуси («{m}»)"
    if why := location_reason(job, settings):
        return why
    if settings.get("auto_skip_english_required") and (m := english_required(text)):
        return f"обязательный английский: «{m}»"
    desc = job.get("description") or ""
    lang = ad_language(desc) if len(desc) > 300 else "es"
    if lang not in ("es", "en", "ru"):
        return f"вакансия на {LANG_RU[lang]}"
    if settings.get("auto_skip_english_ads") and lang == "en" \
            and not _RUSSIAN.search(text):  # "Russian speaking support" ads are often written in English
        return "вакансия написана по-английски"
    if settings.get("auto_remote_only") and not _REMOTE.search(f"{text}\n{job.get('location', '')}"):
        return "не удалённая"
    low = text.lower()
    for w in (w.strip().lower() for w in settings.get("auto_block_words", "").split(",")):
        if w and w in low:
            return f"стоп-слово «{w}»"
    return ""


def parse_queries(settings: dict) -> list[tuple[str, str]]:
    out = []
    for line in settings.get("auto_queries", "").splitlines():
        if not line.strip():
            continue
        kw, _, loc = line.partition("|")
        out.append((kw.strip(), (loc.strip() or settings.get("location", ""))))
    if not out and settings.get("keywords", "").strip():
        out.append((settings["keywords"].strip(), settings.get("location", "")))
    return out


# --- steps ---------------------------------------------------------------------
def step_search(settings: dict) -> list[str]:
    new_ids: list[str] = []
    local = [s for s in settings["sources"] if s not in REMOTE_SOURCES and s not in RU_SOURCES]
    remote = [s for s in settings.get("remote_sources", []) if s in REMOTE_SOURCES]
    ru = [s for s in settings.get("ru_sources", []) if s in RU_SOURCES]

    def lines(key: str) -> list[str]:
        return [k.strip() for k in settings.get(key, "").splitlines() if k.strip()]

    plan = [(kw, loc, local) for kw, loc in parse_queries(settings)] + \
           [(kw, "", remote) for kw in lines("remote_queries")] + \
           [(kw, "", ru) for kw in lines("ru_queries")]
    reset_caches()
    for kw, loc, names in plan:
        if not names:
            continue
        jobs, report = collect(settings, kw, loc, names)
        ids = db.save_jobs_ids(jobs)
        new_ids += ids
        log.info("Поиск «%s» / %s: %s → новых %d", kw, loc or "весь мир (удалёнка)",
                 "; ".join(f"{k}: {v}" for k, v in report.items()), len(ids))
    return new_ids


def step_filter(settings: dict, ids: list[str]) -> int:
    hidden = 0
    for jid in ids:
        job = db.get_job(jid)
        if job and (why := filter_reason(job, settings)):
            db.update_job(jid, status="hidden", filtered=why)
            hidden += 1
            log.info("Отсеяна: %s — %s (%s)", job["title"], job["company"], why)
    return hidden


def ensure_ollama(settings: dict) -> bool:
    ok, msg = ollama_status(settings)
    if ok:
        return True
    if msg == "Ollama не отвечает" and shutil.which("ollama"):
        log.info("Ollama не запущена — запускаю")
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(["ollama", "serve"], creationflags=flags,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(30):
            time.sleep(2)
            ok, msg = ollama_status(settings)
            if ok:
                return True
    log.error("Оценка вакансий пропущена: %s", msg)
    return False


def step_analyze(settings: dict, resume_text: str, profile: dict | None) -> int:
    pending = db.list_jobs(analyzed=False, statuses=["new"])
    pending = sorted(pending, key=lambda j: j["fetched_at"] or "", reverse=True)[: int(settings["auto_max_analyze"])]
    if not pending:
        return 0
    if settings["backend_job_analyst"] == "ollama" and not ensure_ollama(settings):
        return 0
    analyst, done = JobAnalyst(settings), 0
    for job in pending:
        try:
            a = analyst.run(job, profile, facts.source_text(resume_text))
            db.update_job(job["id"], analysis=a, score=a["match_score"])
            done += 1
            log.info("Оценка %3d%%: %s — %s", a["match_score"], job["title"], job["company"])
        except LLMError as e:
            log.error("Оценка остановлена: %s", e)
            break
        except Exception as e:  # one bad job must not break the whole run
            log.warning("Оценка не удалась (%s): %s", job["title"], e)
    return done


def get_ats_profile(settings: dict, profile: dict | None, lang: str) -> dict:
    """General ATS profile from the database. Uses the one saved in the "Resume" tab, otherwise builds it once."""
    saved = db.kv_get("ats_profile")
    if saved and saved.get("lang") == lang and saved.get("target") == "__all__":
        return saved["profile"]
    pool = db.list_jobs(statuses=["new", "ready", "interesting", "applied", "interview"])
    good = [j for j in pool if (j["score"] or 0) >= 45] or pool
    log.info("Строю ATS-профиль по %d вакансиям", len(good[:20]))
    ats = AtsAuditor(settings).run(good, profile, lang)
    db.kv_set("ats_profile", {"target": "__all__", "lang": lang, "profile": ats})
    return ats


def prepare_application(job: dict, settings: dict, resume_text: str, profile: dict | None, ats: dict | None = None) -> Path:
    """Tailored resume (PDF) + cover letter → folder and status "ready".

    If there is a fact base, the resume is rebuilt from it for the job's track (support / frontend),
    and the agent's questions go to the clarification queue (Telegram, "Fact base" tab).
    """
    lang = resume_language(settings, resume_text)
    letter_settings = settings
    # English job → resume and letter in English; Spanish job → in Spanish
    ad = ad_language(job.get("description") or "") if len(job.get("description") or "") > 300 else ""
    if ad in ("es", "en", "ru"):
        if settings.get("resume_language", "auto") == "auto":
            lang = ad
        letter_settings = dict(settings, letter_language=ad)
    src = facts.source_text(resume_text)
    track = facts.job_track(job) if facts.get_facts() else None
    if track:
        ats = facts.track_ats(settings, track, lang)
        issues = []
    else:
        ats = ats if ats and lang == resume_language(settings, resume_text) else get_ats_profile(settings, profile, lang)
        issues = format_checks(resume_text)
    cov = keyword_coverage(src, ats["keywords"])
    rw = ResumeWriter(settings).run(src, ats, cov, issues, lang, job, track=track)
    letter = CoverLetterWriter(letter_settings).run(job, src, profile, job.get("analysis"))
    if track:
        facts.add_questions(rw["questions_for_user"], f"{job['title']} — {job.get('company') or ''}")

    doc = rw["resume"]
    name = slugify(f"{datetime.now():%Y-%m-%d} {job.get('company') or 'empresa'} {job['title']}")[:90]
    folder = APPS_DIR / name
    folder.mkdir(parents=True, exist_ok=True)
    fname = "CV_" + "_".join((doc.get("full_name") or "candidato").split())
    (folder / f"{fname}.pdf").write_bytes(to_pdf(doc, lang))
    (folder / "carta.txt").write_text(letter, encoding="utf-8")
    (folder / "vacante.txt").write_text(
        f"{job['title']} — {job.get('company') or ''}\n{job.get('url') or ''}\n\n{job.get('description') or ''}",
        encoding="utf-8")

    db.update_job(job["id"], status="ready", letter=letter, app_dir=str(folder),
                  prepared_at=datetime.now().isoformat(timespec="seconds"),
                  cv={"lang": lang, "track": track, "resume": doc, "changes_made": rw["changes_made"],
                      "questions_for_user": rw["questions_for_user"],
                      "keywords_not_used": rw["keywords_not_used"]})
    return folder


def step_prepare(settings: dict, resume_text: str, profile: dict | None, limit: int) -> int:
    if limit <= 0:
        return 0
    if "claude" in (settings["backend_resume_writer"], settings["backend_cover_letter"]) \
            and not get_secret("ANTHROPIC_API_KEY"):
        log.error("Подготовка пропущена: не задан ANTHROPIC_API_KEY")
        return 0
    cands = [j for j in db.list_jobs(statuses=["new"], analyzed=True)
             if (j["score"] or 0) >= int(settings["auto_min_score"]) and not j["prepared_at"]]
    cands = cands[:limit]  # list_jobs already sorts by score
    if not cands:
        return 0
    lang = resume_language(settings, resume_text)
    ats = None if facts.get_facts() else get_ats_profile(settings, profile, lang)
    done = 0
    for job in cands:
        try:
            folder = prepare_application(job, settings, resume_text, profile, ats)
            done += 1
            log.info("Готов отклик %d%%: %s — %s → %s", job["score"], job["title"], job["company"], folder)
        except LLMError as e:
            log.error("Подготовка остановлена: %s", e)
            break
        except Exception as e:
            log.warning("Подготовка не удалась (%s): %s", job["title"], e)
    return done


# --- run ---------------------------------------------------------------------
def run(skip_search: bool = False, max_prepare: int | None = None) -> dict:
    settings = load_settings()
    resume_text, profile = db.load_resume()
    started = datetime.now().isoformat(timespec="seconds")
    summary = {"started": started, "new": 0, "filtered": 0, "analyzed": 0, "prepared": 0, "error": ""}
    try:
        if not resume_text:
            raise RuntimeError("резюме не загружено — загрузи его во вкладке «Резюме»")
        if not skip_search:
            ids = step_search(settings)
            summary["new"] = len(ids)
            summary["filtered"] = step_filter(settings, ids)
        summary["analyzed"] = step_analyze(settings, resume_text, profile)
        limit = int(settings["auto_max_prepare"] if max_prepare is None else max_prepare)
        summary["prepared"] = step_prepare(settings, resume_text, profile, limit)
    except Exception as e:
        summary["error"] = f"{type(e).__name__}: {e}"
        log.exception("Автопилот упал")
    summary["finished"] = datetime.now().isoformat(timespec="seconds")
    summary["ready_total"] = len(db.list_jobs(statuses=["ready"]))
    db.kv_set("last_run", summary)
    log.info("Итог: %s", summary)
    try:
        from .telegram_bot import notify_after_run
        notify_after_run(summary)
    except Exception as e:
        log.warning("Telegram: %s", e)
    return summary


def _acquire_lock() -> bool:
    if LOCK_PATH.exists() and time.time() - LOCK_PATH.stat().st_mtime < 3 * 3600:
        return False
    LOCK_PATH.write_text(str(os.getpid()))
    return True


# --- schedule (Windows Task Scheduler, no admin rights) -------------------
def install_schedule(times: str) -> None:
    pyw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    triggers = ", ".join(f"(New-ScheduledTaskTrigger -Daily -At '{t.strip()}')" for t in times.split(",") if t.strip())
    ps = (
        f"$a = New-ScheduledTaskAction -Execute '{pyw}' -Argument '-m jobhunter.pipeline' -WorkingDirectory '{ROOT}';"
        f"$t = @({triggers});"
        "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries "
        "-ExecutionTimeLimit (New-TimeSpan -Hours 3);"
        f"Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $a -Trigger $t -Settings $s "
        "-Description 'JobHunter: поиск вакансий и подготовка откликов' -Force | Out-Null"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)


def remove_schedule() -> None:
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false"], check=True)


def schedule_info() -> str:
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"$t = Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue;"
                        "if ($t) { ($t | Get-ScheduledTaskInfo).NextRunTime.ToString('dd.MM HH:mm') }"],
                       capture_output=True, text=True)
    return r.stdout.strip()


def main() -> None:
    p = argparse.ArgumentParser(description="JobHunter: автопилот")
    p.add_argument("--skip-search", action="store_true", help="не искать, только оценить и подготовить")
    p.add_argument("--max-prepare", type=int, help="сколько откликов подготовить (по умолчанию из настроек)")
    p.add_argument("--install-schedule", action="store_true", help="добавить запуск по расписанию")
    p.add_argument("--remove-schedule", action="store_true", help="убрать расписание")
    args = p.parse_args()

    if args.install_schedule:
        install_schedule(load_settings()["auto_times"])
        print("Расписание установлено, следующий запуск:", schedule_info())
        return
    if args.remove_schedule:
        remove_schedule()
        print("Расписание удалено")
        return

    handlers: list[logging.Handler] = [logging.FileHandler(LOG_PATH, encoding="utf-8")]
    if sys.stdout and sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8")
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S", handlers=handlers)
    for noisy in ("fontTools", "httpx", "anthropic", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if not _acquire_lock():
        log.warning("Автопилот уже работает — этот запуск пропущен")
        return
    try:
        run(args.skip_search, args.max_prepare)
    finally:
        LOCK_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
