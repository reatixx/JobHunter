"""Международные площадки удалённой работы с открытыми API (без ключей).

Remotive, RemoteOK, Himalayas, Jobicy, Arbeitnow. Их поиск работает по ключевым словам, без города:
поле location у вакансии — это ограничение по стране/региону («USA», «Europe», «Worldwide»),
по нему автопилот отсеивает то, что недоступно из Испании.
RemoteOK и Arbeitnow отдают общую ленту — фильтруем по словам у себя.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from ..models import Job
from .base import Source, html_to_text


def _matches(query: str, *texts: str) -> bool:
    """Все слова запроса (от 3 букв) есть в тексте вакансии."""
    words = [w for w in re.findall(r"\w+", query.lower()) if len(w) >= 3]
    hay = " ".join(t or "" for t in texts).lower()
    return all(w in hay for w in words)


def _salary(lo, hi, cur: str = "", period: str = "") -> str:
    lo, hi = (int(float(x)) if x not in (None, "", 0, "0", "None") else 0 for x in (lo, hi))
    if not lo and not hi:
        return ""
    rng = f"{lo:,}–{hi:,}" if lo and hi else f"{lo or hi:,}"
    return f"{rng} {cur} {('/' + period) if period else ''}".strip()


def _remote_loc(restriction) -> str:
    if isinstance(restriction, list):
        restriction = ", ".join(restriction)
    return f"Remoto: {restriction.strip() or 'sin restricción'}"


class RemotiveSource(Source):
    name = "remotive"
    title = "Remotive (удалёнка, мир)"

    def search(self, keywords, location, limit):
        # поиск API фактически игнорирует запрос — берём ленту и фильтруем по словам сами
        r = self.session.get("https://remotive.com/api/remote-jobs", params={"search": keywords}, timeout=30)
        r.raise_for_status()
        items = [it for it in r.json().get("jobs", [])
                 if _matches(keywords, it.get("title", ""), " ".join(it.get("tags") or []),
                             it.get("description", "")[:3000])]
        return [Job(
            source=self.name, external_id=str(it["id"]), title=it.get("title", "").strip(),
            company=it.get("company_name", ""), location=_remote_loc(it.get("candidate_required_location", "")),
            url=it.get("url", ""), description=html_to_text(it.get("description", "")),
            salary=it.get("salary") or "", posted=(it.get("publication_date") or "")[:10],
        ) for it in items[:limit]]


class RemoteOKSource(Source):
    name = "remoteok"
    title = "RemoteOK (удалёнка, мир)"
    _feed: list | None = None  # одна загрузка ленты на прогон

    def search(self, keywords, location, limit):
        if RemoteOKSource._feed is None:
            r = self.session.get("https://remoteok.com/api", timeout=30)
            r.raise_for_status()
            RemoteOKSource._feed = [x for x in r.json() if isinstance(x, dict) and x.get("id")]
        out = []
        for it in RemoteOKSource._feed:
            tags = " ".join(it.get("tags") or [])
            if not _matches(keywords, it.get("position", ""), tags, it.get("description", "")[:3000]):
                continue
            out.append(Job(
                source=self.name, external_id=str(it["id"]), title=it.get("position", "").strip(),
                company=it.get("company", ""), location=_remote_loc(it.get("location", "")),
                url=it.get("url", ""), description=html_to_text(it.get("description", "")),
                salary=_salary(it.get("salary_min"), it.get("salary_max"), "USD", "año"),
                posted=(it.get("date") or "")[:10],
            ))
            if len(out) >= limit:
                break
        return out


class HimalayasSource(Source):
    name = "himalayas"
    title = "Himalayas (удалёнка, мир)"

    def search(self, keywords, location, limit):
        out, offset = [], 0
        while len(out) < limit and offset < 100:
            r = self.session.get("https://himalayas.app/jobs/api/search",
                                 params={"q": keywords, "limit": 20, "offset": offset}, timeout=30)
            r.raise_for_status()
            items = r.json().get("jobs", [])
            if not items:
                break
            for it in items:
                out.append(Job(
                    source=self.name, external_id=str(it.get("guid") or it.get("applicationLink")),
                    title=it.get("title", "").strip(), company=it.get("companyName", ""),
                    location=_remote_loc(it.get("locationRestrictions") or []),
                    url=it.get("applicationLink") or it.get("guid") or "",
                    description=html_to_text(it.get("description", "")),
                    salary=_salary(it.get("minSalary"), it.get("maxSalary"), it.get("currency") or "",
                                   it.get("salaryPeriod") or ""),
                    posted=datetime.fromtimestamp(int(it["pubDate"]), timezone.utc).strftime("%Y-%m-%d")
                    if str(it.get("pubDate", "")).isdigit() else "",
                ))
            offset += 20
            self.polite_pause(0.5, 1.5)
        return out[:limit]


class JobicySource(Source):
    name = "jobicy"
    title = "Jobicy (удалёнка, мир)"

    def search(self, keywords, location, limit):
        r = self.session.get("https://jobicy.com/api/v2/remote-jobs",
                             params={"count": min(50, max(limit, 10)), "tag": keywords}, timeout=30)
        r.raise_for_status()
        return [Job(
            source=self.name, external_id=str(it["id"]), title=html_to_text(it.get("jobTitle", "")),
            company=it.get("companyName", ""), location=_remote_loc(it.get("jobGeo", "")),
            url=it.get("url", ""), description=html_to_text(it.get("jobDescription", "")),
            salary=_salary(it.get("salaryMin"), it.get("salaryMax"), it.get("salaryCurrency") or "",
                           it.get("salaryPeriod") or ""),
            posted=(it.get("pubDate") or "")[:10],
        ) for it in (r.json().get("jobs") or [])[:limit]]


class ArbeitnowSource(Source):
    name = "arbeitnow"
    title = "Arbeitnow (Европа, только удалённые)"
    _feed: list | None = None

    def search(self, keywords, location, limit):
        if ArbeitnowSource._feed is None:
            feed = []
            for page in range(1, 4):
                r = self.session.get("https://www.arbeitnow.com/api/job-board-api", params={"page": page}, timeout=30)
                r.raise_for_status()
                data = r.json().get("data", [])
                feed += [x for x in data if x.get("remote")]
                if not data:
                    break
                self.polite_pause(0.5, 1.5)
            ArbeitnowSource._feed = feed
        out = []
        for it in ArbeitnowSource._feed:
            if not _matches(keywords, it.get("title", ""), " ".join(it.get("tags") or []),
                            it.get("description", "")[:3000]):
                continue
            out.append(Job(
                source=self.name, external_id=it["slug"], title=it.get("title", "").strip(),
                company=it.get("company_name", ""), location=_remote_loc(it.get("location", "")),
                url=it.get("url", ""), description=html_to_text(it.get("description", "")),
                posted=datetime.fromtimestamp(int(it["created_at"]), timezone.utc).strftime("%Y-%m-%d")
                if str(it.get("created_at", "")).isdigit() else "",
            ))
            if len(out) >= limit:
                break
        return out


REMOTE_SOURCES = {cls.name: cls for cls in
                  (RemotiveSource, RemoteOKSource, HimalayasSource, JobicySource, ArbeitnowSource)}
