"""Resume checks "through the eyes of an ATS", without AI: file readability, contacts, sections, keyword coverage.

An ATS (Applicant Tracking System — InfoJobs, Workday, SuccessFactors, Taleo, Personio, etc.) roughly does this:
1) extracts text from the file — images, columns, tables, icons and headers/footers are often lost or scrambled;
2) splits the text into sections by standard headings (Experiencia, Formación, Idiomas…);
3) looks for the job's keywords and knockout criteria (language, work permit, experience) and ranks candidates;
4) HR looks at the top of the first page for ~10 seconds: title, key skills, location, languages.
"""
from __future__ import annotations

import io
import re
import unicodedata
from .i18n import tr

SECTION_PATTERNS = {
    tr("Work experience"): r"experiencia|experience|trayectoria|опыт",
    tr("Education"): r"formaci[oó]n|educaci[oó]n|education|estudios|образование",
    tr("Skills"): r"competencias|habilidades|skills|conocimientos|aptitudes|навыки",
    tr("Languages"): r"idiomas|languages|языки",
}

_ES = {"de", "la", "el", "en", "y", "los", "las", "del", "con", "para", "por", "una", "experiencia", "como"}
_EN = {"the", "and", "of", "in", "with", "for", "to", "a", "an", "experience", "as", "on", "at"}


def detect_language(text: str) -> str:
    """'ru' / 'es' / 'en' based on the resume text."""
    letters = [c for c in text if c.isalpha()]
    if letters and sum("а" <= c.lower() <= "я" or c in "ёЁ" for c in letters) / len(letters) > 0.3:
        return "ru"
    words = re.findall(r"[a-záéíóúñü]+", text.lower())
    es = sum(w in _ES for w in words) + 2 * len(re.findall(r"[ñáéíóú]", text.lower()))
    en = sum(w in _EN for w in words)
    return "en" if en > es else "es"


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", s)


def _contains(text_norm: str, variant: str) -> bool:
    v = norm(variant).strip()
    if not v:
        return False
    # word boundaries, but allow things like C++, .NET, Node.js
    return re.search(rf"(?<!\w){re.escape(v)}(?!\w)", text_norm) is not None


def keyword_coverage(text: str, keywords: list[dict]) -> dict:
    """Which ATS profile keywords are found in the text. Score is weighted by importance (1-3)."""
    t = norm(text)
    rows, got, total = [], 0, 0
    for k in keywords:
        variants = [k["term"], *k.get("synonyms", [])]
        hit = next((v for v in variants if _contains(t, v)), None)
        w = max(1, min(3, int(k.get("importance", 1))))
        total += w
        got += w if hit else 0
        rows.append({"term": k["term"], "found": bool(hit), "as": hit or "", "importance": w,
                     "category": k.get("category", "")})
    rows.sort(key=lambda r: (r["found"], -r["importance"]))
    return {"score": round(100 * got / total) if total else 0, "rows": rows}


def format_checks(text: str, filename: str = "", data: bytes | None = None) -> list[tuple[str, str]]:
    """List of (level, message): level is ok / warn / fail."""
    out: list[tuple[str, str]] = []
    words = len(text.split())

    if len(text.strip()) < 300:
        out.append(("fail", tr("Almost no text can be extracted from the file — to the robot the resume is "
                               "empty (a scan or an image). You need a PDF/DOCX with real text.")))
    else:
        out.append(("ok", tr("Text is extractable ({0} words)", words)))

    out.append(("ok", tr("Email found")) if re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
               else ("fail", tr("No email found — the ATS won't be able to create a candidate record")))
    out.append(("ok", tr("Phone found")) if re.search(r"(\+?\d[\d\s().-]{7,}\d)", text)
               else ("warn", tr("No phone found")))

    for name, pat in SECTION_PATTERNS.items():
        if not re.search(pat, text, re.I):
            out.append(("warn", tr("No standard «{0}» section heading — the ATS may not map the text to fields", name)))

    if not re.search(r"\b(19|20)\d{2}\b", text):
        out.append(("warn", tr("No dates (years) found — the ATS won't calculate experience")))
    if words < 200:
        out.append(("warn", tr("The resume is too short: little room for keywords")))
    elif words > 1000:
        out.append(("warn", tr("The resume is longer than ~2 pages — HR usually expects 1-2 pages")))

    icons = sum(1 for ch in text if 0xE000 <= ord(ch) <= 0xF8FF or unicodedata.category(ch) == "So")
    if icons > 3:
        out.append(("warn", tr("Found {0} icons/special characters — the ATS turns them into garbage; plain "
                               "text is better", icons)))

    if data and filename.lower().endswith(".docx"):
        import docx
        d = docx.Document(io.BytesIO(data))
        if d.tables:
            out.append(("warn", tr("The DOCX has {0} table(s) — many ATS read tables out of order", len(d.tables))))
        hdr = " ".join(p.text for s in d.sections for p in [*s.header.paragraphs, *s.footer.paragraphs]).strip()
        if hdr:
            out.append(("warn", tr("There is text in headers/footers — ATS often skip it (don't keep "
                                   "contacts there)")))
        if d.inline_shapes:
            out.append(("warn", tr("The document has images (photo/icons/skill charts) — the robot can't see "
                                   "them")))
    if data and filename.lower().endswith(".pdf"):
        from pypdf import PdfReader
        r = PdfReader(io.BytesIO(data))
        if len(r.pages) > 2:
            out.append(("warn", tr("{0} page(s) — better keep it to 1-2", len(r.pages))))
        try:
            imgs = sum(len(p.images) for p in r.pages)
        except Exception:
            imgs = 0
        if imgs:
            out.append(("warn", tr("Images in the PDF: {0}. A photo is fine, but the robot can't read skill "
                                   "charts and icons", imgs)))
        lines = [l for l in text.splitlines() if l.strip()]
        short = sum(1 for l in lines if len(l.split()) <= 2)
        if len(lines) > 10 and short / len(lines) > 0.45:
            out.append(("warn", tr("Many very short lines — looks like a multi-column layout; the ATS may "
                                   "scramble the text")))
    return out
