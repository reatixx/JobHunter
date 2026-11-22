"""Проверки резюме «глазами ATS» без ИИ: читаемость файла, контакты, разделы, покрытие ключевых слов.

ATS (Applicant Tracking System — InfoJobs, Workday, SuccessFactors, Taleo, Personio и т.п.) делает примерно так:
1) извлекает из файла текст — картинки, колонки, таблицы, иконки и колонтитулы часто теряются или перемешиваются;
2) раскладывает текст по разделам по стандартным заголовкам (Experiencia, Formación, Idiomas…);
3) ищет ключевые слова вакансии и стоп-критерии (язык, разрешение на работу, опыт) и ранжирует кандидатов;
4) HR смотрит верх первой страницы ~10 секунд: должность, ключевые навыки, локация, языки.
"""
from __future__ import annotations

import io
import re
import unicodedata

SECTION_PATTERNS = {
    "Опыт работы": r"experiencia|experience|trayectoria|опыт",
    "Образование": r"formaci[oó]n|educaci[oó]n|education|estudios|образование",
    "Навыки": r"competencias|habilidades|skills|conocimientos|aptitudes|навыки",
    "Языки": r"idiomas|languages|языки",
}

_ES = {"de", "la", "el", "en", "y", "los", "las", "del", "con", "para", "por", "una", "experiencia", "como"}
_EN = {"the", "and", "of", "in", "with", "for", "to", "a", "an", "experience", "as", "on", "at"}


def detect_language(text: str) -> str:
    """'ru' / 'es' / 'en' по тексту резюме."""
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
    # границы слова, но допускаем символы вроде C++, .NET, Node.js
    return re.search(rf"(?<!\w){re.escape(v)}(?!\w)", text_norm) is not None


def keyword_coverage(text: str, keywords: list[dict]) -> dict:
    """Какие ключевые слова ATS-профиля найдены в тексте. Счёт взвешен по важности (1-3)."""
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
    """Список (уровень, сообщение): уровень — ok / warn / fail."""
    out: list[tuple[str, str]] = []
    words = len(text.split())

    if len(text.strip()) < 300:
        out.append(("fail", "Из файла почти не извлекается текст — для робота резюме пустое (скан или картинка). "
                            "Нужен PDF/DOCX с настоящим текстом."))
    else:
        out.append(("ok", f"Текст извлекается ({words} слов)"))

    out.append(("ok", "Email найден") if re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
               else ("fail", "Не найден email — ATS не сможет создать карточку кандидата"))
    out.append(("ok", "Телефон найден") if re.search(r"(\+?\d[\d\s().-]{7,}\d)", text)
               else ("warn", "Не найден телефон"))

    for name, pat in SECTION_PATTERNS.items():
        if not re.search(pat, text, re.I):
            out.append(("warn", f"Нет стандартного заголовка раздела «{name}» — ATS может не разложить текст по полям"))

    if not re.search(r"\b(19|20)\d{2}\b", text):
        out.append(("warn", "Не найдены даты (годы) — ATS не посчитает стаж"))
    if words < 200:
        out.append(("warn", "Резюме слишком короткое: мало мест для ключевых слов"))
    elif words > 1000:
        out.append(("warn", "Резюме длиннее ~2 страниц — HR обычно ждёт 1-2 страницы"))

    icons = sum(1 for ch in text if 0xE000 <= ord(ch) <= 0xF8FF or unicodedata.category(ch) == "So")
    if icons > 3:
        out.append(("warn", f"Найдено {icons} иконок/спецсимволов — ATS превращает их в мусор; лучше обычный текст"))

    if data and filename.lower().endswith(".docx"):
        import docx
        d = docx.Document(io.BytesIO(data))
        if d.tables:
            out.append(("warn", f"В DOCX {len(d.tables)} таблиц(ы) — многие ATS читают таблицы вперемешку"))
        hdr = " ".join(p.text for s in d.sections for p in [*s.header.paragraphs, *s.footer.paragraphs]).strip()
        if hdr:
            out.append(("warn", "Есть текст в колонтитулах — ATS часто их пропускает (не держите там контакты)"))
        if d.inline_shapes:
            out.append(("warn", "В документе есть картинки (фото/иконки/графики навыков) — роботу они не видны"))
    if data and filename.lower().endswith(".pdf"):
        from pypdf import PdfReader
        r = PdfReader(io.BytesIO(data))
        if len(r.pages) > 2:
            out.append(("warn", f"{len(r.pages)} страниц(ы) — лучше уложиться в 1-2"))
        try:
            imgs = sum(len(p.images) for p in r.pages)
        except Exception:
            imgs = 0
        if imgs:
            out.append(("warn", f"В PDF изображений: {imgs}. Фото допустимо, но графики навыков и иконки робот не прочтёт"))
        lines = [l for l in text.splitlines() if l.strip()]
        short = sum(1 for l in lines if len(l.split()) <= 2)
        if len(lines) > 10 and short / len(lines) > 0.45:
            out.append(("warn", "Много очень коротких строк — похоже на вёрстку в колонки; ATS может перемешать текст"))
    return out
