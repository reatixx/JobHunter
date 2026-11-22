"""LinkedIn — публичная гостевая выдача вакансий (без логина, ваш аккаунт не используется).

Официального API для поиска вакансий нет, поэтому это парсинг страниц: делаем мало запросов
и с паузами. Если LinkedIn начнёт отвечать 429/999 — источник просто вернёт ошибку.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from ..models import Job
from .base import Source, SourceError, html_to_text

SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"


class LinkedInSource(Source):
    name = "linkedin"
    title = "LinkedIn"

    def _check(self, r):
        if r.status_code in (429, 999):
            raise SourceError(f"LinkedIn временно ограничил запросы (HTTP {r.status_code}), попробуйте позже")
        r.raise_for_status()

    def search(self, keywords, location, limit):
        days = int(self.settings.get("linkedin_days", 7) or 0)
        # «remote: European Union» — только удалённые вакансии (фильтр LinkedIn f_WT=2) в этом регионе
        remote = bool(location) and location.lower().startswith("remote:")
        if remote:
            location = location.split(":", 1)[1].strip()
        jobs: list[Job] = []
        start = 0
        while len(jobs) < limit:
            params = {"keywords": keywords, "location": location or "Spain", "start": start}
            if days:
                params["f_TPR"] = f"r{days * 86400}"
            if remote:
                params["f_WT"] = "2"
            r = self.session.get(SEARCH, params=params, timeout=30)
            self._check(r)
            cards = BeautifulSoup(r.text, "html.parser").select("div.base-card[data-entity-urn]")
            if not cards:
                break
            for c in cards:
                job = self._parse_card(c)
                if job:
                    jobs.append(job)
            start += len(cards)
            self.polite_pause()
        jobs = jobs[:limit]

        for j in jobs:
            try:
                r = self.session.get(DETAIL.format(id=j.external_id), timeout=30)
                if r.status_code in (429, 999):  # одна повторная попытка после паузы
                    self.polite_pause(8, 12)
                    r = self.session.get(DETAIL.format(id=j.external_id), timeout=30)
                self._check(r)
                soup = BeautifulSoup(r.text, "html.parser")
                desc = soup.select_one("div.description__text, div.show-more-less-html__markup")
                criteria = [
                    f"{h.get_text(strip=True)}: {v.get_text(strip=True)}"
                    for h, v in zip(soup.select("h3.description__job-criteria-subheader"),
                                    soup.select("span.description__job-criteria-text"))
                ]
                j.description = "\n".join(criteria) + "\n\n" + html_to_text(str(desc) if desc else "")
            except SourceError:
                break  # упёрлись в лимит — оставшиеся без описания
            except Exception:
                pass
            self.polite_pause(1.0, 2.5)
        return jobs

    def _parse_card(self, c) -> Job | None:
        m = re.search(r"jobPosting:(\d+)", c.get("data-entity-urn", ""))
        if not m:
            return None

        def txt(sel):
            el = c.select_one(sel)
            return el.get_text(" ", strip=True) if el else ""

        link = c.select_one("a.base-card__full-link")
        posted = c.select_one("time")
        return Job(
            source=self.name,
            external_id=m.group(1),
            title=txt("h3.base-search-card__title"),
            company=txt("h4.base-search-card__subtitle"),
            location=txt("span.job-search-card__location"),
            url=(link["href"].split("?")[0] if link else f"https://www.linkedin.com/jobs/view/{m.group(1)}"),
            salary=txt("span.job-search-card__salary-info"),
            posted=posted.get("datetime", "") if posted else "",
        )
