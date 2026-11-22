"""Ручное добавление вакансии: по ссылке (любой сайт) или вставленным текстом."""
from __future__ import annotations

import hashlib
import re

import requests
from bs4 import BeautifulSoup

from ..models import Job
from .base import USER_AGENT, html_to_text


def _hid(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:12]


def job_from_text(text: str, title: str = "", company: str = "", url: str = "") -> Job:
    first_line = next((l.strip() for l in text.splitlines() if l.strip()), "Вакансия")
    return Job(source="manual", external_id=_hid(url or text), title=title or first_line[:120],
               company=company, url=url, description=text.strip())


def job_from_url(url: str) -> Job:
    li = re.search(r"linkedin\.com/jobs/view/(?:[^/]*-)?(\d+)", url)
    fetch_url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{li.group(1)}" if li else url
    r = requests.get(fetch_url, headers={"User-Agent": USER_AGENT, "Accept-Language": "es-ES,es;q=0.9"}, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    title_el = soup.select_one("h1, h2.top-card-layout__title") or soup.title
    title = title_el.get_text(" ", strip=True) if title_el else url
    company_el = soup.select_one("a.topcard__org-name-link, [class*=company]")
    for t in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        t.decompose()
    main = soup.select_one("div.description__text, main, article") or soup.body or soup
    return Job(source="manual", external_id=_hid(url), title=title[:200],
               company=company_el.get_text(" ", strip=True)[:100] if company_el else "",
               url=url, description=html_to_text(str(main))[:20000])
