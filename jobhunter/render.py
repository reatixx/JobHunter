"""Builds the resume as PDF and DOCX in an ATS-friendly format.

Layout rules: single column, real text (not an image), standard font, standard section
headings, contacts in the body (not in the header), no tables, icons or charts.
"""
from __future__ import annotations

import io
from pathlib import Path

import docx
from docx.shared import Pt, RGBColor
from fpdf import FPDF
from fpdf.enums import XPos, YPos

HEADINGS = {
    "es": {"summary": "PERFIL PROFESIONAL", "skills": "COMPETENCIAS", "experience": "EXPERIENCIA PROFESIONAL",
           "education": "FORMACIÓN", "languages": "IDIOMAS", "certifications": "CERTIFICACIONES",
           "additional": "INFORMACIÓN ADICIONAL", "permit": "Permiso de trabajo"},
    "en": {"summary": "PROFESSIONAL SUMMARY", "skills": "SKILLS", "experience": "WORK EXPERIENCE",
           "education": "EDUCATION", "languages": "LANGUAGES", "certifications": "CERTIFICATIONS",
           "additional": "ADDITIONAL INFORMATION", "permit": "Work permit"},
    "ru": {"summary": "О СЕБЕ", "skills": "КЛЮЧЕВЫЕ НАВЫКИ", "experience": "ОПЫТ РАБОТЫ",
           "education": "ОБРАЗОВАНИЕ", "languages": "ЯЗЫКИ", "certifications": "СЕРТИФИКАТЫ",
           "additional": "ДОПОЛНИТЕЛЬНО", "permit": "Разрешение на работу"},
}

FONT_DIR = Path("C:/Windows/Fonts")
FONT_SETS = [("calibri.ttf", "calibrib.ttf", "calibrii.ttf"), ("arial.ttf", "arialbd.ttf", "ariali.ttf")]
ACCENT = (31, 78, 121)


def _contact_line(r: dict, h: dict) -> str:
    parts = [r.get("location"), r.get("phone"), r.get("email"), *r.get("links", [])]
    line = "  |  ".join(p for p in parts if p)
    if r.get("work_permit"):
        line += f"\n{h['permit']}: {r['work_permit']}"
    return line


def _dates(a: str, b: str) -> str:
    return " – ".join(x for x in [a, b] if x)


class _PDF(FPDF):
    def __init__(self):
        super().__init__(format="A4")
        self.set_margins(18, 15, 18)
        self.set_auto_page_break(True, 15)
        for regular, bold, italic in FONT_SETS:
            if (FONT_DIR / regular).exists():
                self.add_font("Main", "", str(FONT_DIR / regular))
                self.add_font("Main", "B", str(FONT_DIR / bold))
                self.add_font("Main", "I", str(FONT_DIR / italic))
                return
        raise RuntimeError("Не найден шрифт Calibri или Arial в C:/Windows/Fonts")

    def heading(self, text: str):
        self.ln(3)
        self.set_font("Main", "B", 11.5)
        self.set_text_color(*ACCENT)
        self.cell(0, 6, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        y = self.get_y()
        self.set_draw_color(*ACCENT)
        self.line(self.l_margin, y, self.w - self.r_margin, y)
        self.ln(1.5)
        self.set_text_color(0, 0, 0)

    def para(self, text: str, size: float = 10.5, style: str = "", h: float = 5):
        self.set_font("Main", style, size)
        self.multi_cell(0, h, text, align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    def bullet(self, text: str):
        self.set_font("Main", "", 10.5)
        self.set_x(self.l_margin + 2)
        self.cell(4, 5, "•")
        self.multi_cell(0, 5, text, align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def to_pdf(r: dict, lang: str) -> bytes:
    h = HEADINGS.get(lang, HEADINGS["es"])
    pdf = _PDF()
    pdf.set_title(f"CV {r.get('full_name', '')}".strip())
    pdf.set_author(r.get("full_name", ""))
    pdf.add_page()

    pdf.set_font("Main", "B", 20)
    pdf.cell(0, 9, r.get("full_name", ""), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if r.get("headline"):
        pdf.set_text_color(*ACCENT)
        pdf.para(r["headline"], 12.5, "B", 6.5)
        pdf.set_text_color(0, 0, 0)
    pdf.para(_contact_line(r, h), 9.5, "", 4.8)

    if r.get("summary"):
        pdf.heading(h["summary"])
        pdf.para(r["summary"])
    if r.get("skills"):
        pdf.heading(h["skills"])
        pdf.para(" · ".join(r["skills"]))
    if r.get("experience"):
        pdf.heading(h["experience"])
        for e in r["experience"]:
            pdf.para(" — ".join(x for x in [e.get("role"), e.get("company")] if x), 11, "B", 5.5)
            pdf.para("  |  ".join(x for x in [e.get("location"), _dates(e.get("start", ""), e.get("end", ""))] if x),
                     9.5, "I", 4.8)
            for b in e.get("bullets", []):
                pdf.bullet(b)
            pdf.ln(1.5)
    if r.get("education"):
        pdf.heading(h["education"])
        for e in r["education"]:
            pdf.para(" — ".join(x for x in [e.get("degree"), e.get("institution")] if x), 11, "B", 5.5)
            meta = "  |  ".join(x for x in [e.get("location"), _dates(e.get("start", ""), e.get("end", ""))] if x)
            if meta:
                pdf.para(meta, 9.5, "I", 4.8)
            if e.get("details"):
                pdf.para(e["details"])
            pdf.ln(1)
    for key in ("languages", "certifications", "additional"):
        if r.get(key):
            pdf.heading(h[key])
            for item in r[key]:
                pdf.bullet(item)
    return bytes(pdf.output())


def to_docx(r: dict, lang: str) -> bytes:
    h = HEADINGS.get(lang, HEADINGS["es"])
    d = docx.Document()
    st = d.styles["Normal"]
    st.font.name, st.font.size = "Calibri", Pt(10.5)
    for s in d.sections:
        s.left_margin = s.right_margin = Pt(51)
        s.top_margin = s.bottom_margin = Pt(42)
    d.core_properties.title = f"CV {r.get('full_name', '')}"
    d.core_properties.author = r.get("full_name", "")

    def p(text, size=10.5, bold=False, italic=False, color=None, space_after=2):
        para = d.add_paragraph()
        run = para.add_run(text)
        run.bold, run.italic, run.font.size = bold, italic, Pt(size)
        if color:
            run.font.color.rgb = RGBColor(*color)
        para.paragraph_format.space_after = Pt(space_after)
        return para

    def heading(text):
        para = p(text, 11.5, bold=True, color=ACCENT, space_after=3)
        para.paragraph_format.space_before = Pt(8)

    def bullets(items):
        for it in items:
            d.add_paragraph(it, style="List Bullet").paragraph_format.space_after = Pt(1)

    p(r.get("full_name", ""), 20, bold=True)
    if r.get("headline"):
        p(r["headline"], 12.5, bold=True, color=ACCENT)
    p(_contact_line(r, h), 9.5)
    if r.get("summary"):
        heading(h["summary"]); p(r["summary"])
    if r.get("skills"):
        heading(h["skills"]); p(" · ".join(r["skills"]))
    if r.get("experience"):
        heading(h["experience"])
        for e in r["experience"]:
            p(" — ".join(x for x in [e.get("role"), e.get("company")] if x), 11, bold=True, space_after=0)
            p("  |  ".join(x for x in [e.get("location"), _dates(e.get("start", ""), e.get("end", ""))] if x),
              9.5, italic=True)
            bullets(e.get("bullets", []))
    if r.get("education"):
        heading(h["education"])
        for e in r["education"]:
            p(" — ".join(x for x in [e.get("degree"), e.get("institution")] if x), 11, bold=True, space_after=0)
            meta = "  |  ".join(x for x in [e.get("location"), _dates(e.get("start", ""), e.get("end", ""))] if x)
            if meta:
                p(meta, 9.5, italic=True)
            if e.get("details"):
                p(e["details"])
    for key in ("languages", "certifications", "additional"):
        if r.get(key):
            heading(h[key]); bullets(r[key])
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()
