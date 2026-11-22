"""Job Today — public search pages (jobtoday.com/es/trabajos-<query>/<city>).

Jobs come from the JSON (__NEXT_DATA__) embedded in the page, which already has the full description.
There is no API, so this module may break if the site changes — the other sources keep working.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from ..models import Job
from .base import Source, SourceError, html_to_text, slugify
from ..i18n import tr

BASE = "https://jobtoday.com"


class JobTodaySource(Source):
    name = "jobtoday"
    title = "Job Today"

    def search(self, keywords, location, limit):
        city = slugify(location or "madrid") or "madrid"
        url = f"{BASE}/es/trabajos-{slugify(keywords)}/{city}"
        jobs: list[Job] = []
        page = 1
        while len(jobs) < limit and page <= 5:
            r = self.session.get(url, params={"page": page} if page > 1 else None, timeout=30)
            if r.status_code == 404:
                raise SourceError(tr("Job Today doesn't know the city «{0}» (try Madrid, Barcelona, Valencia…)", location))
            r.raise_for_status()
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
            if not m:
                raise SourceError(tr("Job Today changed its page structure — the parser needs updating"))
            props = json.loads(m.group(1)).get("props", {}).get("pageProps", {})
            items = [it for s in props.get("feed", {}).get("sections", []) if s.get("type") == "items"
                     for it in s.get("items", []) if it.get("type") == "job"]
            if not items:
                break
            for it in items:
                jobs.append(self._from_payload(it.get("payload", {})))
            if not props.get("pagination", {}).get("hasNext"):
                break
            page += 1
            self.polite_pause()
        return jobs[:limit]

    def _from_payload(self, p: dict) -> Job:
        addr = (p.get("addressInfo") or {}).get("display", {})
        created = p.get("createDate")
        etype = {"FULL_TIME": "Jornada completa", "PART_TIME": "Jornada parcial"}.get(p.get("employmentType", ""), "")
        head = [x for x in [etype, "Incorporación inmediata" if p.get("immediateStart") else "",
                            "Sin experiencia" if p.get("experienceNotRequired") else ""] if x]
        return Job(
            source=self.name,
            external_id=p.get("key", ""),
            title=p.get("role", ""),
            company=p.get("companyName") or (p.get("company") or {}).get("name", ""),
            location=addr.get("shortName") or p.get("address", ""),
            # jobs from partner sites (isExternalJob) have an empty canonicalUrl; the address is in externalUrl
            url=(BASE + p["canonicalUrl"]) if p.get("canonicalUrl") else (p.get("externalUrl") or BASE),
            description=("\n".join(head) + "\n\n" if head else "") + html_to_text(p.get("description", "")),
            salary=p.get("salary", "") if isinstance(p.get("salary"), str) else "",
            posted=datetime.fromtimestamp(created / 1000).date().isoformat() if created else "",
        )
