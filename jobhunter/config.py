"""Настройки проекта: ключи из .env, остальное — в data/settings.json (редактируется из UI)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv, set_key

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
ENV_PATH = ROOT / ".env"
SETTINGS_PATH = DATA_DIR / "settings.json"
DB_PATH = DATA_DIR / "jobs.db"

# override=True: ключи проекта важнее одноимённых переменных Windows (там может быть токен другого бота)
load_dotenv(ENV_PATH, override=True)

# Ключи, которые можно задать через UI (хранятся только в локальном .env)
SECRET_KEYS = {
    "ANTHROPIC_API_KEY": "Claude API (письма, анализ резюме)",
    "JOOBLE_API_KEY": "Jooble API — https://jooble.org/api/about",
    "INFOJOBS_CLIENT_ID": "InfoJobs client_id — https://developer.infojobs.net",
    "INFOJOBS_CLIENT_SECRET": "InfoJobs client_secret",
    "TELEGRAM_BOT_TOKEN": "Telegram-бот — токен от @BotFather",
}

DEFAULTS: dict = {
    "keywords": "",
    "location": "Madrid",
    "infojobs_province": "",          # напр. "madrid", "barcelona"; пусто = вся Испания
    "sources": ["linkedin", "jooble", "infojobs", "jobtoday"],
    "max_per_source": 25,
    "linkedin_days": 7,               # вакансии за последние N дней
    "exclude_words": "",              # через запятую; вакансии с такими словами в названии пропускаются
    "ollama_url": "http://localhost:11434",
    "ollama_model": "qwen2.5:14b",
    "claude_model": "claude-opus-5-5",
    # какой бэкенд использует каждый агент: "ollama" или "claude"
    "backend_job_analyst": "ollama",
    "backend_resume_analyst": "claude",
    "backend_cover_letter": "claude",
    "backend_resume_tailor": "claude",
    "backend_ats_auditor": "claude",
    "backend_resume_writer": "claude",
    "resume_language": "auto",       # auto = как исходное резюме; или es / en / ru
    "letter_language": "es",
    "applicant_notes": "",            # доп. сведения для писем: NIE, доступность, переезд и т.п.
    # --- автопилот (jobhunter/pipeline.py) ---
    "auto_queries": "",               # по строке на запрос: "ключевые слова | город"; пусто = keywords/location
    "auto_min_score": 65,             # с какого % совпадения готовить резюме и письмо
    "auto_max_prepare": 5,            # максимум откликов, готовящихся за один запуск (это вызовы Claude)
    "auto_max_analyze": 60,           # максимум вакансий на оценку Ollama за запуск
    "auto_remote_only": False,        # оставлять только удалённые вакансии
    "auto_skip_english_required": True,  # отсеивать вакансии с обязательным высоким английским
    "auto_skip_english_ads": True,    # отсеивать вакансии, написанные целиком по-английски
    "auto_block_words": "",           # через запятую; слова в тексте вакансии, при которых она отсеивается
    "auto_times": "09:00,15:00",      # когда запускать по расписанию
    # международная удалёнка: свои запросы (на английском — площадки англоязычные), город не нужен
    "remote_sources": ["remotive", "remoteok", "himalayas", "jobicy", "arbeitnow"],
    "remote_queries": "russian\ncustomer support\nit support\nhelpdesk\ntechnical support\nservice desk\nfrontend junior",
    # вакансия с ограничением по региону остаётся, только если в нём есть одно из этих слов
    "remote_regions": "worldwide, anywhere, global, europe, emea, eu, spain, españa, cet, utc, sin restricción",
    # русскоязычные источники (Habr Career, Telegram-каналы) и запросы для них
    "ru_sources": ["habr", "telegram"],
    "ru_queries": "техническая поддержка\nспециалист поддержки\nsupport\nhelpdesk\nтестировщик\nfrontend\nверстальщик",
    "tg_channels": "remote_jobs_ru, remoteit, jobforjunior, remocate, forfrontend, geekjobs",
    "skip_ru_by": True,               # отсеивать компании из России и Беларуси
    # без переезда: оставлять только удалённые вакансии или вакансии в этих местах
    "no_relocation": True,
    "home_area": ("bilbao, bizkaia, vizcaya, barakaldo, getxo, basauri, leioa, erandio, sestao, portugalete, "
                  "santurtzi, galdakao, derio, zamudio, sondika, arrigorriaga, durango, bilbo"),
    "telegram_chat_id": 0,           # чат, к которому привязан бот (первый, кто написал /start)
}


def get_secret(name: str) -> str:
    return os.environ.get(name, "").strip()


def save_secret(name: str, value: str) -> None:
    ENV_PATH.touch(exist_ok=True)
    set_key(str(ENV_PATH), name, value)
    os.environ[name] = value


def load_settings() -> dict:
    data = dict(DEFAULTS)
    if SETTINGS_PATH.exists():
        try:
            data.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return data


def save_settings(data: dict) -> None:
    SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
