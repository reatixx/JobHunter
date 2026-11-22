"""Project settings: keys from .env, everything else in data/settings.json (editable from the UI)."""
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

# override=True: project keys win over Windows variables with the same name (they may hold another bot's token)
load_dotenv(ENV_PATH, override=True)

# Keys that can be set from the UI (stored only in the local .env)
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
    "infojobs_province": "",          # e.g. "madrid", "barcelona"; empty = all of Spain
    "sources": ["linkedin", "jooble", "infojobs", "jobtoday"],
    "max_per_source": 25,
    "linkedin_days": 7,               # jobs from the last N days
    "exclude_words": "",              # comma-separated; jobs with these words in the title are skipped
    "ollama_url": "http://localhost:11434",
    "ollama_model": "qwen2.5:14b",
    "claude_model": "claude-opus-5-5",
    # backend used by each agent: "ollama" or "claude"
    "backend_job_analyst": "ollama",
    "backend_resume_analyst": "claude",
    "backend_cover_letter": "claude",
    "backend_resume_tailor": "claude",
    "backend_ats_auditor": "claude",
    "backend_resume_writer": "claude",
    "resume_language": "auto",       # auto = same as the source resume; or es / en / ru
    "letter_language": "es",
    "applicant_notes": "",            # extra info for letters: work permit, availability, etc.
    # --- autopilot (jobhunter/pipeline.py) ---
    "auto_queries": "",               # one query per line: "keywords | city"; empty = keywords/location
    "auto_min_score": 65,             # minimum % match to prepare a resume and letter
    "auto_max_prepare": 5,            # max applications prepared per run (these are Claude calls)
    "auto_max_analyze": 60,           # max jobs scored by Ollama per run
    "auto_remote_only": False,        # keep remote jobs only
    "auto_skip_english_required": True,  # filter out jobs that require high English
    "auto_skip_english_ads": True,    # filter out job ads written entirely in English
    "auto_block_words": "",           # comma-separated; words in the job text that filter it out
    "auto_times": "09:00,15:00",      # when to run on schedule
    # international remote: separate queries (in English — these boards are English-speaking), no city
    "remote_sources": ["remotive", "remoteok", "himalayas", "jobicy", "arbeitnow"],
    "remote_queries": "russian\ncustomer support\nit support\nhelpdesk\ntechnical support\nservice desk\nfrontend junior",
    # a region-restricted job is kept only if the region contains one of these words
    "remote_regions": "worldwide, anywhere, global, europe, emea, eu, spain, españa, cet, utc, sin restricción",
    # Russian-language sources (Habr Career, Telegram channels) and their queries
    "ru_sources": ["habr", "telegram"],
    "ru_queries": "техническая поддержка\nспециалист поддержки\nsupport\nhelpdesk\nтестировщик\nfrontend\nверстальщик",
    "tg_channels": "remote_jobs_ru, remoteit, jobforjunior, remocate, forfrontend, geekjobs",
    "skip_ru_by": True,               # filter out companies from Russia and Belarus
    # no relocation: keep only remote jobs or jobs in these places
    "no_relocation": True,
    "home_area": ("bilbao, bizkaia, vizcaya, barakaldo, getxo, basauri, leioa, erandio, sestao, portugalete, "
                  "santurtzi, galdakao, derio, zamudio, sondika, arrigorriaga, durango, bilbo"),
    "telegram_chat_id": 0,           # chat the bot is bound to (the first one that sent /start)
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
