"""Interface language. Strings in the code are English; translations live in locales/<lang>.json.

The language comes from the ui_lang setting (en / ru) and is read once at import time:
restart the app and the bot after changing it.
"""
from __future__ import annotations

import json
from pathlib import Path

LOCALES = Path(__file__).resolve().parent.parent / "locales"
LANG_NAMES = {"en": "English", "ru": "Russian"}


def _load_lang() -> str:
    # read settings.json directly: config itself imports i18n
    path = LOCALES.parent / "data" / "settings.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("ui_lang", "en")
    except (OSError, ValueError):
        return "en"


LANG = _load_lang()
_TABLE: dict[str, str] = {}
if LANG != "en" and (LOCALES / f"{LANG}.json").exists():
    _TABLE = json.loads((LOCALES / f"{LANG}.json").read_text(encoding="utf-8"))


def tr(text: str, *args) -> str:
    """Translate a UI string; args are substituted into {0}, {1}…"""
    s = _TABLE.get(text, text)
    return s.format(*args) if args else s


def ui_language_name() -> str:
    """Name of the interface language for prompts: agents write explanations in it."""
    return LANG_NAMES.get(LANG, "English")
