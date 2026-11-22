"""Jooble — official REST API (free key: https://jooble.org/api/about)."""
from __future__ import annotations

from ..config import get_secret
from ..models import Job
from .base import Source, SourceError, html_to_text


class JoobleSource(Source):
    name = "jooble"
    title = "Jooble"

    def is_configured(self):
        if not get_secret("JOOBLE_API_KEY"):
            return False, "нет JOOBLE_API_KEY (Настройки → ключи)"
        return True, ""

    def search(self, keywords, location, limit):
        key = get_secret("JOOBLE_API_KEY")
        if not key:
            raise SourceError("нет JOOBLE_API_KEY")
        url = f"https://es.jooble.org/api/{key}"
        jobs: list[Job] = []
        page = 1
        while len(jobs) < limit and page <= 5:
            r = self.session.post(url, json={"keywords": keywords, "location": location, "page": str(page)}, timeout=30)
            if r.status_code in (401, 403):
                raise SourceError(f"Jooble отклонил ключ (HTTP {r.status_code})")
            r.raise_for_status()
            items = r.json().get("jobs", [])
            if not items:
                break
            for it in items:
                jobs.append(Job(
                    source=self.name,
                    external_id=str(it.get("id") or it.get("link")),
                    title=it.get("title", "").strip(),
                    company=(it.get("company") or "").strip(),
                    location=it.get("location", ""),
                    url=it.get("link", ""),
                    # Jooble only returns a snippet; the full text is on the original site
                    description=html_to_text(it.get("snippet", "")),
                    salary=it.get("salary", "") or "",
                    posted=(it.get("updated") or "")[:10],
                ))
            page += 1
        return jobs[:limit]
