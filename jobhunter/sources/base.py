from __future__ import annotations

import random
import re
import time
import unicodedata

import requests
from bs4 import BeautifulSoup

from ..models import Job

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


class SourceError(Exception):
    """Ошибка источника, которую нужно показать пользователю (нет ключа, блокировка и т.п.)."""


class Source:
    name = "base"
    title = "Base"

    def __init__(self, settings: dict):
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "es-ES,es;q=0.9"})

    def is_configured(self) -> tuple[bool, str]:
        return True, ""

    def search(self, keywords: str, location: str, limit: int) -> list[Job]:
        raise NotImplementedError

    @staticmethod
    def polite_pause(a: float = 1.5, b: float = 3.5) -> None:
        time.sleep(random.uniform(a, b))


def html_to_text(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for li in soup.find_all("li"):
        li.insert_before("\n• ")
    text = soup.get_text(" ")
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")
