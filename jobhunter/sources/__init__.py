from __future__ import annotations

from typing import Callable

from ..models import Job
from .base import Source, SourceError
from .infojobs import InfoJobsSource
from .jobtoday import JobTodaySource
from .jooble import JoobleSource
from .linkedin import LinkedInSource
from .manual import job_from_text, job_from_url
from .remote import REMOTE_SOURCES
from .russian import RU_SOURCES

SOURCES: dict[str, type[Source]] = {
    "linkedin": LinkedInSource,
    "jooble": JoobleSource,
    "infojobs": InfoJobsSource,
    "jobtoday": JobTodaySource,
    **REMOTE_SOURCES,
    **RU_SOURCES,
}


def reset_caches() -> None:
    """Ленты RemoteOK/Arbeitnow/Telegram и карточки Habr кэшируются на прогон — сбрасываем перед новым."""
    for cls in SOURCES.values():
        for attr in ("_feed", "_posts"):
            if attr in cls.__dict__:
                setattr(cls, attr, None)
        if "_details" in cls.__dict__:
            cls._details = {}


def collect(settings: dict, keywords: str, location: str, names: list[str],
            on_progress: Callable[[str, str], None] = lambda name, msg: None) -> tuple[list[Job], dict[str, str]]:
    """Опрашивает выбранные источники. Ошибка одного источника не мешает остальным."""
    excluded = [w.strip().lower() for w in settings.get("exclude_words", "").split(",") if w.strip()]
    limit = int(settings.get("max_per_source", 25))
    all_jobs: list[Job] = []
    report: dict[str, str] = {}
    for name in names:
        src = SOURCES[name](settings)
        ok, why = src.is_configured()
        if not ok:
            report[name] = f"пропущен: {why}"
            on_progress(name, report[name])
            continue
        on_progress(name, "ищу…")
        try:
            jobs = src.search(keywords, location, limit)
            jobs = [j for j in jobs if not any(w in j.title.lower() for w in excluded)]
            all_jobs += jobs
            report[name] = f"найдено {len(jobs)}"
        except SourceError as e:
            report[name] = f"ошибка: {e}"
        except Exception as e:  # сеть, неожиданный формат ответа
            report[name] = f"ошибка: {type(e).__name__}: {e}"
        on_progress(name, report[name])
    return all_jobs, report


__all__ = ["SOURCES", "REMOTE_SOURCES", "RU_SOURCES", "collect", "reset_caches", "job_from_text", "job_from_url", "SourceError"]
