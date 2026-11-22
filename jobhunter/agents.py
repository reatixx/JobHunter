"""Агенты. У каждого своя роль, системный промпт и бэкенд (ollama / claude) из настроек.

1. ResumeAnalyst    — разбирает резюме: профиль, сильные/слабые стороны, советы, ключевые слова для поиска.
2. JobAnalyst       — оценивает вакансию против профиля: % совпадения, требования, пробелы, red flags.
3. CoverLetterWriter — пишет сопроводительное письмо (carta de presentación) под конкретную вакансию.
4. ResumeTailor     — подсказывает, как адаптировать резюме под конкретную вакансию.
5. AtsAuditor       — по пачке вакансий выясняет, по каким словам и критериям ATS/HR отсеивают резюме.
6. ResumeWriter     — переписывает резюме под эти слова (только реальные факты) → PDF/DOCX.
"""
from __future__ import annotations

import json
import re

from .llm import ask_json, ask_text
from .ats import detect_language, norm
from .models import AtsProfile, JobAnalysis, ResumeProfile, ResumeRewrite, ResumeTailoring

LANG_NAMES = {"es": "испанском (España)", "en": "английском", "ru": "русском"}


def resume_language(settings: dict, resume_text: str) -> str:
    """Язык нового резюме: из настроек или, если там auto, — как у исходного резюме."""
    lang = settings.get("resume_language", "auto")
    return detect_language(resume_text) if lang == "auto" else lang


def _job_block(job: dict, max_chars: int = 12000) -> str:
    desc = (job.get("description") or "").strip() or "(описание недоступно, есть только название)"
    return (
        f"Должность: {job.get('title', '')}\n"
        f"Компания: {job.get('company') or 'не указана'}\n"
        f"Локация: {job.get('location') or 'не указана'}\n"
        f"Зарплата: {job.get('salary') or 'не указана'}\n"
        f"Источник: {job.get('source')}\n\n"
        f"Описание:\n{desc[:max_chars]}"
    )


def _profile_block(profile: dict | None) -> str:
    if not profile:
        return "(профиль не построен)"
    keep = ["headline", "target_roles", "years_experience", "hard_skills", "soft_skills",
            "languages", "education", "experience_summary"]
    return json.dumps({k: profile.get(k) for k in keep}, ensure_ascii=False, indent=1)


class ResumeAnalyst:
    SYSTEM = (
        "Ты опытный карьерный консультант и рекрутер на рынке труда Испании. "
        "Ты внимательно читаешь резюме и даёшь честную, конкретную оценку без воды. "
        "Не выдумывай факты, которых нет в резюме. Поля с пояснениями пиши на русском, "
        "должности и ключевые слова для поиска — на испанском, как их пишут в испанских вакансиях."
    )

    def __init__(self, settings: dict):
        self.settings = settings

    def run(self, resume_text: str) -> dict:
        user = (
            "Проанализируй резюме кандидата, который ищет работу в Испании.\n"
            "Оцени его глазами испанского рекрутера: структура, понятность, достижения, "
            "соответствие местным ожиданиям (фото, длина, язык, формат Europass и т.п.).\n\n"
            f"<resume>\n{resume_text}\n</resume>"
        )
        return ask_json(self.settings, self.settings["backend_resume_analyst"], self.SYSTEM, user,
                        ResumeProfile, effort="high").model_dump()


class JobAnalyst:
    SYSTEM = (
        "Ты аналитик вакансий. Сравниваешь вакансию с профилем кандидата и даёшь трезвую оценку. "
        "match_score: 80-100 — почти всё совпадает; 60-79 — подходит с небольшими пробелами; "
        "40-59 — частичное совпадение; ниже 40 — не подходит. Не завышай оценку. "
        "Обязательные требования важнее желательных. Учитывай языки и уровень. "
        "Чего не хватает кандидату — пиши ТОЛЬКО в missing_skills. "
        "red_flags — это ТОЛЬКО проблемы самой вакансии или работодателя (размытые обязанности, "
        "нереалистичные требования, оплата только процентом, признаки MLM, подозрительные условия). "
        "Если таких проблем нет — red_flags пустой список []. "
        "Все пояснения пиши на русском. Отвечай строго в формате JSON по схеме."
    )

    def __init__(self, settings: dict):
        self.settings = settings

    def run(self, job: dict, profile: dict | None, resume_text: str) -> dict:
        user = (
            f"<candidate_profile>\n{_profile_block(profile)}\n</candidate_profile>\n\n"
            f"<resume_excerpt>\n{resume_text[:5000]}\n</resume_excerpt>\n\n"
            f"<vacancy>\n{_job_block(job)}\n</vacancy>\n\n"
            "Оцени, насколько кандидат подходит на эту вакансию."
        )
        a = ask_json(self.settings, self.settings["backend_job_analyst"], self.SYSTEM, user,
                     JobAnalysis, effort="low").model_dump()
        score = a["match_score"] = max(0, min(100, int(a["match_score"])))
        # вердикт выводим из оценки — так он всегда согласован с ней
        a["verdict"] = "подавать" if score >= 65 else "возможно" if score >= 45 else "не подходит"
        return a


class CoverLetterWriter:
    SYSTEM = (
        "Ты пишешь сопроводительные письма, которые читают живые рекрутеры в Испании. "
        "Письмо короткое (180-280 слов), конкретное и человеческое: без штампов вроде "
        "«me dirijo a ustedes con gran interés», без пересказа всего резюме. "
        "Структура: зацепка, почему именно эта компания/роль; 2-3 конкретных факта из опыта, "
        "которые закрывают главные требования вакансии; готовность к собеседованию. "
        "Используй только факты из резюме — ничего не выдумывай. Если чего-то не хватает, "
        "не упоминай это или подай честно через готовность учиться. "
        "Верни только текст письма, без пояснений и без markdown."
    )

    def __init__(self, settings: dict):
        self.settings = settings

    def run(self, job: dict, resume_text: str, profile: dict | None, analysis: dict | None,
            extra_instructions: str = "") -> str:
        lang = LANG_NAMES.get(self.settings.get("letter_language", "es"), "испанском")
        notes = self.settings.get("applicant_notes", "").strip()
        user = (
            f"<resume>\n{resume_text}\n</resume>\n\n"
            f"<vacancy>\n{_job_block(job)}\n</vacancy>\n\n"
            + (f"<analysis>\n{json.dumps(analysis, ensure_ascii=False)}\n</analysis>\n\n" if analysis else "")
            + (f"<applicant_notes>\n{notes}\n</applicant_notes>\n\n" if notes else "")
            + f"Напиши сопроводительное письмо на {lang} языке. Обращение — к компании "
              f"{job.get('company') or '(название неизвестно — нейтральное обращение)'}. "
              "Подпись — именем кандидата из резюме."
            + (f"\n\nДополнительные пожелания: {extra_instructions}" if extra_instructions else "")
        )
        return ask_text(self.settings, self.settings["backend_cover_letter"], self.SYSTEM, user, effort="medium")


class ResumeTailor:
    SYSTEM = (
        "Ты помогаешь адаптировать резюме под конкретную вакансию в Испании так, чтобы оно "
        "проходило ATS-фильтры и цепляло рекрутера. Предлагай конкретные правки, но не придумывай "
        "опыт, которого нет. Пояснения — на русском, текст для резюме — на испанском."
    )

    def __init__(self, settings: dict):
        self.settings = settings

    def run(self, job: dict, resume_text: str, analysis: dict | None) -> dict:
        user = (
            f"<resume>\n{resume_text}\n</resume>\n\n"
            f"<vacancy>\n{_job_block(job)}\n</vacancy>\n\n"
            + (f"<analysis>\n{json.dumps(analysis, ensure_ascii=False)}\n</analysis>\n\n" if analysis else "")
            + "Как адаптировать это резюме под эту вакансию?"
        )
        return ask_json(self.settings, self.settings["backend_resume_tailor"], self.SYSTEM, user,
                        ResumeTailoring, effort="medium").model_dump()


class AtsAuditor:
    SYSTEM = (
        "Ты эксперт по ATS (Applicant Tracking Systems: InfoJobs, Workday, SuccessFactors, Taleo, Personio) "
        "и по тому, как рекрутеры отсеивают резюме. По набору вакансий ты определяешь: "
        "ключевые слова, по которым робот ранжирует кандидатов (навыки, инструменты, сертификаты, языки, "
        "домен), их варианты написания, и стоп-критерии, по которым резюме отбрасывают сразу. "
        "Важность ставь по тому, как часто термин встречается в вакансиях и обязателен ли он. "
        "Ключевые слова — это навыки, инструменты, сертификаты, языки и домены (1-4 слова), "
        "а не названия вакансий и не целые фразы из текста. Требование, которое есть только в одной "
        "вакансии из многих, получает важность 1. "
        "Не придумывай требования, которых нет в вакансиях. Пояснения — на русском."
    )

    def __init__(self, settings: dict):
        self.settings = settings

    def run(self, jobs: list[dict], profile: dict | None, lang: str) -> dict:
        backend = self.settings["backend_ats_auditor"]
        n, per = (20, 3500) if backend == "claude" else (8, 2000)
        if jobs:
            body = "\n\n".join(f"<vacancy n={i}>\n{_job_block(j, per)}\n</vacancy>"
                               for i, j in enumerate(jobs[:n], 1))
        else:
            roles = ", ".join((profile or {}).get("target_roles", [])) or "(не указаны)"
            body = (f"Вакансий в базе нет. Опиши типичные требования испанских вакансий для должностей: {roles}. "
                    "Опирайся на свои знания рынка.")
        user = (
            f"{body}\n\n"
            f"Резюме кандидата будет на {LANG_NAMES[lang]} языке — в synonyms добавь и варианты на этом языке.\n"
            "Составь ATS-профиль этих вакансий."
        )
        return ask_json(self.settings, backend, self.SYSTEM, user, AtsProfile, effort="medium").model_dump()


class ResumeWriter:
    SYSTEM = (
        "Ты профессиональный автор резюме, который знает, как работают ATS и рекрутеры. "
        "Ты переписываешь резюме так, чтобы оно проходило автоматический отбор и цепляло HR, "
        "но оставалось ЧЕСТНЫМ. Правила:\n"
        "1. Используй только факты из исходного резюме и заметок кандидата. Не добавляй навыки, "
        "инструменты, должности, цифры и достижения, которых там нет. Если навыка нет — он идёт в keywords_not_used.\n"
        "2. Если навык у кандидата есть, называй его ровно так, как в вакансиях (напр. 'análisis de datos' вместо "
        "'trabajo con datos'; 'Power BI', если в резюме речь именно о Power BI).\n"
        "3. Ключевые слова вписывай естественно: в заголовок, Perfil, навыки и пункты опыта. "
        "Никаких списков слов ради слов, скрытого текста и повторов одного слова — ATS и HR это ловят.\n"
        "4. Пункты опыта: глагол действия + что сделано + результат. Цифры — только реальные; "
        "если цифры не хватает, спроси в questions_for_user.\n"
        "5. Обратная хронология, единый формат дат, 1-2 страницы. Без фото, даты рождения, семейного положения.\n"
        "6. Стоп-критерии (языки, разрешение на работу, локация), если кандидат им соответствует, — сделай заметными вверху.\n"
        "7. Весь текст резюме — на языке, который указан в задании; названия инструментов не переводи.\n"
        "8. Ничего не дописывай сверх исходника: ни разрешения на работу, ни городов, стран, вузов, дат, "
        "если их нет в резюме или заметках. Пустое поле лучше выдуманного.\n"
        "9. Сохрани ВСЕ языки, места работы и образование из исходного резюме.\n"
        "10. В questions_for_user спрашивай только о фактах, которые могут быть у кандидата (цифры, результаты, "
        "проекты). Никогда не предлагай указать навыки, которых у кандидата нет.\n"
        "changes_made, keywords_not_used и questions_for_user пиши на русском."
    )

    def __init__(self, settings: dict):
        self.settings = settings

    FACTS_RULES = (
        "Источник — не резюме, а БАЗА ФАКТОВ о кандидате (на русском, с ответами на уточняющие вопросы). "
        "Собери резюме ПОЛНОСТЬЮ заново: своя структура, формулировки и порядок, старое резюме не копируй. "
        "Бери только факты из базы. Пункты с пометкой «УТОЧНИТЬ» в резюме НЕ включай, а задай по каждому "
        "вопрос в questions_for_user. Если в ответах кандидата факт уточнён — используй уточнённую версию. "
        "Должности называй так, как они реально назывались или как точно описывают реальные обязанности "
        "(например, «Técnico de soporte IT»), но не повышай уровень и не меняй суть работы. "
        "Вопросы в questions_for_user — конкретные, по одному факту, чтобы на них можно было ответить в одном "
        "сообщении (даты, системы, объёмы, результаты). Обращайся к кандидату на «ты»."
    )

    def run(self, resume_text: str, ats_profile: dict, coverage: dict, issues: list, lang: str,
            job: dict | None = None, track: str | None = None) -> dict:
        from .facts import TRACKS
        notes = self.settings.get("applicant_notes", "").strip()
        missing = [r["term"] for r in coverage.get("rows", []) if not r["found"]]
        problems = "; ".join(m for lvl, m in issues if lvl != "ok") or "нет"
        source = (f"<candidate_facts>\n{resume_text}\n</candidate_facts>\n\n<rules_for_facts>\n{self.FACTS_RULES}\n"
                  f"{TRACKS[track]['focus']}\n</rules_for_facts>\n\n") if track else f"<resume>\n{resume_text}\n</resume>\n\n"
        user = (
            source
            + (f"<applicant_notes>\n{notes}\n</applicant_notes>\n\n" if notes else "")
            + f"<ats_profile>\n{json.dumps(ats_profile, ensure_ascii=False)}\n</ats_profile>\n\n"
            + f"<ats_check>\nПокрытие ключевых слов: {coverage.get('score')}%. "
              f"Не найдены в резюме: {', '.join(missing) or 'нет'}.\n"
              f"Проблемы формата: {problems}\n</ats_check>\n\n"
            + (f"<target_vacancy>\n{_job_block(job)}\n</target_vacancy>\n\n" if job else "")
            + f"Перепиши резюме на {LANG_NAMES[lang]} языке"
            + (" под эту конкретную вакансию." if job else " под этот тип вакансий.")
        )
        rw = ask_json(self.settings, self.settings["backend_resume_writer"], self.SYSTEM, user,
                      ResumeRewrite, effort="high").model_dump()
        return _guard_facts(rw, resume_text + "\n" + notes)


_PERMIT = re.compile(r"permiso|nie\b|nif\b|residencia|work permit|visa|right to work|разрешени|вид на жительство|резиденц",
                     re.I)


def _guard_facts(rw: dict, source: str) -> dict:
    """Проверки в коде поверх модели: убираем то, чего точно нет в исходнике."""
    doc, warnings = rw["resume"], []
    if doc.get("work_permit") and not _PERMIT.search(source):
        doc["work_permit"] = ""
        warnings.append("Убрано «разрешение на работу» — его нет в резюме. Если оно есть, впишите в Настройки → «Что ещё знать о вас».")
    src = norm(source)
    for field in ("email", "phone"):
        val = doc.get(field, "")
        digits = re.sub(r"\D", "", val) if field == "phone" else norm(val)
        if val and digits and digits not in (re.sub(r"\D", "", source) if field == "phone" else src):
            doc[field] = ""
            warnings.append(f"Убран {field}, которого нет в исходном резюме.")
    rw["changes_made"] = warnings + rw["changes_made"]
    return rw
