"""Russian-language sources: Habr Career (the site's JSON API) and public Telegram job channels.

Telegram is read through the web feed t.me/s/<channel> — no login, no user account.
Companies from Russia and Belarus are not filtered here — the autopilot does it (pipeline.filter_reason),
so the rule works the same for every source.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from ..models import Job
from .base import Source, html_to_text
from ..i18n import tr

_REMOTE_RU = re.compile(r"удал[её]нн|удал[её]нк|remote|из любой (?:точки|страны)|full[- ]?remote|дистанционн", re.I)


def _stem_match(query: str, text: str) -> bool:
    """All query words appear in the text — matched by the first 6 letters to catch Russian word endings."""
    words = [w[:6] for w in re.findall(r"\w+", query.lower()) if len(w) >= 3]
    low = text.lower()
    return bool(words) and all(w in low for w in words)


class HabrCareerSource(Source):
    name = "habr"
    title = tr("Habr Career (remote)")
    API = "https://career.habr.com/api/frontend/vacancies"
    _details: dict[str, str] = {}

    def search(self, keywords, location, limit):
        out, page = [], 1
        while len(out) < limit and page <= 3:
            r = self.session.get(self.API, params={"q": keywords, "remote": "true", "type": "all",
                                                   "per_page": 25, "page": page}, timeout=30)
            r.raise_for_status()
            items = r.json().get("list") or []
            if not items:
                break
            for it in items:
                if it.get("archived"):
                    continue
                cities = ", ".join(l.get("title", "") for l in it.get("locations") or [])
                href = "https://career.habr.com" + it.get("href", "")
                out.append(Job(
                    source=self.name, external_id=str(it["id"]), title=it.get("title", "").strip(),
                    company=(it.get("company") or {}).get("title", ""),
                    location="Remote" + (f" · {cities}" if cities else ""),
                    url=href, description=self._detail(href, it),
                    salary=(it.get("salary") or {}).get("formatted") or "",
                    posted=((it.get("publishedDate") or {}).get("date") or "")[:10],
                ))
            page += 1
        return out[:limit]

    def _detail(self, href: str, it: dict) -> str:
        if href in self._details:
            return self._details[href]
        skills = ", ".join(s.get("title", "") for s in it.get("skills") or [])
        text = ""
        try:
            r = self.session.get(href, timeout=30)
            if r.ok:
                soup = BeautifulSoup(r.text, "html.parser")
                node = soup.select_one(".vacancy-description__text") or soup.select_one(".style-ugc")
                text = html_to_text(str(node)) if node else ""
            self.polite_pause(0.8, 1.8)
        except Exception:
            pass
        text = (text + (f"\n\nSkills: {skills}" if skills else "")).strip()
        self._details[href] = text
        return text


_VACANCY = re.compile(r"ваканси|ищем|требуется|обязанности|требования|зарплат|з/п|вилка|salary|"
                      r"формат работы|#vacancy|#job|responsibilit|requirements|we are looking|откликнуться", re.I)


class TelegramChannelsSource(Source):
    name = "telegram"
    title = tr("Telegram job channels")
    _posts: list[dict] | None = None

    def is_configured(self):
        if not self._channels():
            return False, tr("no channels configured (Autopilot → settings)")
        return True, ""

    def _channels(self) -> list[str]:
        return [c.strip().lstrip("@") for c in self.settings.get("tg_channels", "").split(",") if c.strip()]

    def _load(self) -> list[dict]:
        posts = []
        for ch in self._channels():
            before = None
            for _ in range(2):  # two feed pages ≈ the last 40 posts
                url = f"https://t.me/s/{ch}" + (f"?before={before}" if before else "")
                try:
                    r = self.session.get(url, timeout=30)
                    r.raise_for_status()
                except Exception:
                    break
                soup = BeautifulSoup(r.text, "html.parser")
                msgs = soup.select("div.tgme_widget_message[data-post]")
                if not msgs:
                    break
                for m in msgs:
                    body = m.select_one(".tgme_widget_message_text")
                    if not body:
                        continue
                    t = m.select_one("time[datetime]")
                    posts.append({"post": m["data-post"], "text": html_to_text(str(body)),
                                  "date": (t["datetime"] if t else "")[:10]})
                before = min(int(m["data-post"].split("/")[-1]) for m in msgs)
                self.polite_pause(0.8, 1.6)
        return posts

    def search(self, keywords, location, limit):
        if TelegramChannelsSource._posts is None:
            TelegramChannelsSource._posts = self._load()
        out = []
        for p in TelegramChannelsSource._posts:
            text = p["text"]
            if len(text) < 150 or not _VACANCY.search(text) or not _stem_match(keywords, text):
                continue
            lines = [re.sub(r"^[\W_]+", "", l).strip() for l in text.splitlines()]
            title = next((l for l in lines if len(l) > 3), "Job")[:120]
            m = re.search(r"(?:компания|company|работодатель)\s*[:—–-]\s*([^\n]{2,60})", text, re.I)
            company = m.group(1).strip() if m else ""
            # Remocate format: "Position at Company : https://…"
            if rm := re.match(r"(.+?)\s+в\s+(.+?)\s*:\s*https?://", title):
                title, company = rm.group(1).strip(), company or rm.group(2).strip()
            channel = p["post"].split("/")[0]
            out.append(Job(
                source=self.name, external_id=p["post"], title=title,
                company=company,
                location=("Remote · " if _REMOTE_RU.search(text) else "") + f"Telegram @{channel}",
                url=f"https://t.me/{p['post']}", description=text, posted=p["date"],
            ))
            if len(out) >= limit:
                break
        return out


RU_SOURCES = {cls.name: cls for cls in (HabrCareerSource, TelegramChannelsSource)}
