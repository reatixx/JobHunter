"""Application tracking report: what was sent, who answered, which channels and job types work.

CLI (run with the project's .venv python):
  python -m jobhunter.report                       — print the report
  python -m jobhunter.report --days 30             — report for another period
  python -m jobhunter.report event <name> <kind> [--at YYYY-MM-DD] [--note …]
        kind: applied | viewed | reply | interview | offer | rejected
        name: part of the company or job title (must match exactly one sent application)
  python -m jobhunter.report add <company> <title> --channel <where> --at YYYY-MM-DD [--url …]
"""
from __future__ import annotations

import argparse
import html
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from . import db
from .i18n import tr

SILENCE_DAYS = 14   # no news for this long after sending = silence
FOLLOW_UP_DAYS = 10  # waiting this long with no news: a polite reminder may help

# job type by words in the title (first match wins)
TRACKS = [
    ("support", r"soporte|support|helpdesk|help desk|service desk|cau\b|noc\b|n1\b|n2\b|l1\b|l2\b|inform[aá]tic|workplace|sistemas|itsm|account manager"),
    ("ai", r"\bia\b|\bai\b|agentic|agentica|machine learning"),
    ("dev", r"desarroll|programad|developer|frontend|front-end|web|java|python"),
]
TRACK_NAMES = {"support": "Support / helpdesk", "ai": "AI", "dev": "Development", "other": "Other"}


def track_of(title: str) -> str:
    t = (title or "").lower()
    for name, rx in TRACKS:
        if re.search(rx, t):
            return name
    return "other"


def channel_of(job: dict) -> str:
    return job.get("channel") or job.get("source") or "?"


def _date(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(s[:19]) if s else None
    except ValueError:
        return None


def outcome(job: dict) -> str:
    """Furthest point reached: interview > rejected > viewed (incl. reply) > waiting / silence."""
    ev = job["events"]
    if "interview" in ev or "offer" in ev:
        return "interview"
    if "rejected" in ev:
        return "rejected"
    if "viewed" in ev or "reply" in ev:
        return "viewed"
    sent = _date(job["applied_at"])
    return "silence" if sent and (datetime.now() - sent).days >= SILENCE_DAYS else "waiting"


def company(job: dict) -> str:
    """Short company name: «DIBAL | Soluciones de Pesaje…» → «DIBAL»."""
    return html.escape((job.get("company") or "?").split("|")[0].strip()[:40])


def _rate(group: list[dict]) -> str:
    n = len(group)
    answered = sum(outcome(j) in ("viewed", "rejected", "interview") for j in group)
    return tr("{0} sent, {1} reacted ({2}%)", n, answered, round(100 * answered / n) if n else 0)


def build(days: int = 7) -> str:
    """Report as Telegram HTML."""
    e = html.escape
    apps = db.applications()
    if not apps:
        return tr("No sent applications yet.")
    now = datetime.now()
    since = now - timedelta(days=days)
    recent = [j for j in apps if (_date(j["applied_at"]) or now) >= since]
    events_recent = [(j, k, at) for j in apps for k, at in j["events"].items()
                     if k != "applied" and (_date(at) or since) >= since]
    out = Counter(outcome(j) for j in apps)

    lines = [tr("📊 <b>Applications: last {0} days</b>", days), ""]
    lines.append(tr("Sent: <b>{0}</b>", len(recent))
                 + (" — " + ", ".join(company(j) for j in recent) if recent else ""))
    if events_recent:
        icons = {"viewed": "👀", "reply": "✉️", "interview": "🗣", "offer": "🎉", "rejected": "❌"}
        lines.append(tr("News:"))
        for j, k, at in sorted(events_recent, key=lambda x: _date(x[2]) or now):
            lines.append(f"  {icons.get(k, '•')} {company(j)} — {e(j['title'][:60])}")
    else:
        lines.append(tr("No news from companies this period."))

    lines += ["", tr("<b>All time</b>: {0} applications", len(apps)),
              tr("  🗣 interview: {0}\n  👀 viewed / replied: {1}\n  ❌ rejected: {2}\n  ⏳ waiting (< {5} days): {3}\n  🔇 silence ({5}+ days): {4}",
                 out["interview"], out["viewed"], out["rejected"], out["waiting"], out["silence"], SILENCE_DAYS)]

    by_channel, by_track = defaultdict(list), defaultdict(list)
    for j in apps:
        by_channel[channel_of(j)].append(j)
        by_track[track_of(j["title"])].append(j)
    lines += ["", tr("<b>Where companies react</b>")]
    for ch, group in sorted(by_channel.items(), key=lambda x: -len(x[1])):
        lines.append(f"  {e(ch)}: {_rate(group)}")
    lines += ["", tr("<b>By job type</b>")]
    for t, group in sorted(by_track.items(), key=lambda x: -len(x[1])):
        lines.append(f"  {tr(TRACK_NAMES[t])}: {_rate(group)}")

    follow = [j for j in apps if outcome(j) in ("waiting", "silence")
              and FOLLOW_UP_DAYS <= (now - (_date(j["applied_at"]) or now)).days < 30]
    if follow:
        lines += ["", tr("<b>No answer for {0}+ days</b> (a short reminder may help):", FOLLOW_UP_DAYS)]
        lines += [f"  • {company(j)} — {e(j['title'][:60])}" for j in follow]
    if len(apps) < 30:
        lines += ["", tr("<i>Still few applications: the percentages will mean something after ~30.</i>")]
    return "\n".join(lines)


def _plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="python -m jobhunter.report")
    p.add_argument("--days", type=int, default=7)
    sub = p.add_subparsers(dest="cmd")
    pe = sub.add_parser("event")
    pe.add_argument("name")
    pe.add_argument("kind", choices=db.EVENT_KINDS)
    pe.add_argument("--at")
    pe.add_argument("--note", default="")
    pa = sub.add_parser("add")
    pa.add_argument("company")
    pa.add_argument("title")
    pa.add_argument("--channel", required=True)
    pa.add_argument("--at", required=True)
    pa.add_argument("--url", default="")
    pa.add_argument("--location", default="")
    a = p.parse_args(argv)

    if a.cmd == "event":
        found = db.find_applications(a.name)
        if len(found) != 1:
            print(f"'{a.name}': {len(found)} matches", *[f"  {r['id']} | {r['company']} | {r['title']}" for r in found], sep="\n")
            return 1
        ok = db.add_event(found[0]["id"], a.kind, a.at, a.note)
        print(("added" if ok else "already recorded"), a.kind, "→", found[0]["company"], "|", found[0]["title"])
    elif a.cmd == "add":
        print("added", db.add_application(a.company, a.title, a.channel, a.at, a.url, a.location))
    else:
        print(_plain(build(a.days)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
