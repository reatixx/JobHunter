"""InfoJobs — official API (app registration: https://developer.infojobs.net)."""
from __future__ import annotations

from ..config import get_secret
from ..models import Job
from .base import Source, SourceError, html_to_text
from ..i18n import tr

API = "https://api.infojobs.net/api"


class InfoJobsSource(Source):
    name = "infojobs"
    title = "InfoJobs"

    def _auth(self):
        cid, secret = get_secret("INFOJOBS_CLIENT_ID"), get_secret("INFOJOBS_CLIENT_SECRET")
        if not cid or not secret:
            raise SourceError(tr("missing INFOJOBS_CLIENT_ID / INFOJOBS_CLIENT_SECRET"))
        return cid, secret

    def is_configured(self):
        try:
            self._auth()
            return True, ""
        except SourceError as e:
            return False, tr("{0} (Settings → keys)", e)

    def _get(self, path, params=None):
        r = self.session.get(f"{API}{path}", params=params, auth=self._auth(), timeout=30)
        if r.status_code in (401, 403):
            raise SourceError(tr("InfoJobs rejected the keys (HTTP {0})", r.status_code))
        r.raise_for_status()
        return r.json()

    def search(self, keywords, location, limit):
        province = self.settings.get("infojobs_province", "").strip()
        jobs: list[Job] = []
        page = 1
        while len(jobs) < limit:
            params = {"q": keywords, "maxResults": min(50, limit), "page": page, "order": "updated-desc"}
            if province:
                params["province"] = province
            data = self._get("/9/offer", params)
            offers = data.get("offers") or data.get("items") or []
            if not offers:
                break
            for o in offers:
                jobs.append(self._from_offer(o))
                if len(jobs) >= limit:
                    break
            if page >= int(data.get("totalPages", 1) or 1):
                break
            page += 1

        # The full description is only on the job page
        for j in jobs:
            try:
                d = self._get(f"/7/offer/{j.external_id}")
                parts = [d.get("description", ""), d.get("minRequirements", ""), d.get("desiredRequirements", "")]
                labels = ["", "Requisitos mínimos:\n", "Requisitos deseados:\n"]
                j.description = "\n\n".join(l + html_to_text(p) for l, p in zip(labels, parts) if p) or j.description
            except Exception:
                pass
        return jobs

    def _from_offer(self, o: dict) -> Job:
        def val(x):
            return x.get("value", "") if isinstance(x, dict) else (x or "")

        loc = ", ".join(v for v in [o.get("city", ""), val(o.get("province"))] if v)
        extra = [f"{k}: {val(o.get(f))}" for k, f in
                 [("Contrato", "contractType"), ("Jornada", "workDay"), ("Teletrabajo", "teleworking"),
                  ("Experiencia mínima", "experienceMin"), ("Estudios", "study")] if val(o.get(f))]
        return Job(
            source=self.name,
            external_id=str(o.get("id")),
            title=o.get("title", ""),
            company=val(o.get("author", {}).get("name") if isinstance(o.get("author"), dict) else o.get("author")),
            location=loc,
            url=o.get("link", ""),
            description="\n".join(extra + [o.get("requirementMin", "")]).strip(),
            salary=o.get("salaryDescription", "") or "",
            posted=(o.get("published") or o.get("updated") or "")[:10],
        )
