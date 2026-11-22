"""Модели данных: вакансия и структурированные ответы агентов."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


@dataclass
class Job:
    source: str
    external_id: str
    title: str
    company: str = ""
    location: str = ""
    url: str = ""
    description: str = ""
    salary: str = ""
    posted: str = ""

    @property
    def uid(self) -> str:
        return f"{self.source}:{self.external_id}"


# Все поля обязательные и extra="forbid" — так схема годится и для structured outputs Claude,
# и для параметра format в Ollama.
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResumeProfile(_Strict):
    full_name: str = Field(description="Имя кандидата, если указано, иначе пустая строка")
    headline: str = Field(description="Краткое позиционирование кандидата, 1 строка, на русском")
    target_roles: list[str] = Field(description="Подходящие должности на испанском, 3-6 штук")
    years_experience: float = Field(description="Общий релевантный опыт в годах")
    hard_skills: list[str]
    soft_skills: list[str]
    languages: list[str] = Field(description="Языки с уровнем, напр. 'Español B2'")
    education: list[str]
    experience_summary: str = Field(description="Сводка опыта, 3-5 предложений, на русском")
    strengths: list[str] = Field(description="Сильные стороны резюме, на русском")
    weaknesses: list[str] = Field(description="Слабые места резюме, на русском")
    improvement_tips: list[str] = Field(description="Конкретные советы по улучшению резюме для рынка Испании, на русском")
    search_keywords: list[str] = Field(description="Ключевые слова для поиска вакансий на испанском, 3-8 штук")


class JobAnalysis(_Strict):
    match_score: int = Field(description="Совпадение кандидата с вакансией, 0-100")
    verdict: Literal["подавать", "возможно", "не подходит"]
    summary: str = Field(description="Суть вакансии в 2-3 предложениях, на русском")
    seniority: Literal["junior", "middle", "senior", "lead", "не указан"] = Field(description="Уровень, который требует вакансия")
    key_requirements: list[str] = Field(description="Ключевые требования, на русском")
    matched_skills: list[str] = Field(description="Что у кандидата совпадает с требованиями")
    missing_skills: list[str] = Field(description="Чего кандидату не хватает")
    red_flags: list[str] = Field(description="Проблемы САМОЙ вакансии/работодателя: размытые обязанности, нереалистичные требования, признаки MLM, работа «за процент», нет зарплаты и т.п. Пробелы кандидата сюда НЕ писать. Пусто, если нет")
    language_requirements: str = Field(description="Требования к языкам")
    salary_info: str = Field(description="Зарплата, если указана, иначе 'не указана'")
    application_tips: str = Field(description="На что сделать упор при отклике, 1-2 предложения, на русском")


class ResumeTailoring(_Strict):
    summary_rewrite: str = Field(description="Новый вариант раздела 'Perfil/Resumen' резюме на испанском под эту вакансию")
    changes: list[str] = Field(description="Конкретные правки резюме под вакансию, на русском")
    keywords_to_add: list[str] = Field(description="Ключевые слова из вакансии, которые стоит добавить (на испанском)")
    skills_to_highlight: list[str] = Field(description="Что из опыта вынести на первый план")
    honest_gaps: list[str] = Field(description="Пробелы, которые нельзя закрыть правкой резюме — как о них сказать честно")


# ---------- ATS-аудит и улучшение резюме ----------

class AtsKeyword(_Strict):
    term: str = Field(description="Навык/инструмент/требование, 1-4 слова, как пишут в вакансиях. НЕ название вакансии и НЕ целое предложение")
    synonyms: list[str] = Field(description="Варианты написания и синонимы, которые ATS тоже засчитает (es/en, аббревиатуры)")
    category: Literal["hard_skill", "tool", "soft_skill", "certification", "language", "education", "domain"]
    importance: Literal[1, 2, 3] = Field(description="3 — обязательное или почти во всех вакансиях, 2 — часто, 1 — иногда")


class AtsProfile(_Strict):
    keywords: list[AtsKeyword] = Field(description="15-35 ключевых слов, по которым ATS и HR отбирают резюме на эти вакансии")
    knockout_criteria: list[str] = Field(description="Стоп-критерии, по которым резюме отсеивают сразу (языки, разрешение на работу, опыт, локация, диплом), на русском")
    hr_screening_notes: list[str] = Field(description="Что HR смотрит в первые 10 секунд именно для этих вакансий, на русском")
    job_titles: list[str] = Field(description="Как называются эти должности в вакансиях — для заголовка резюме")


class ResumeExperience(_Strict):
    role: str
    company: str
    location: str
    start: str = Field(description="Формат MM/YYYY или YYYY")
    end: str = Field(description="MM/YYYY, YYYY или 'Actualidad'")
    bullets: list[str] = Field(description="3-6 пунктов: глагол действия + что сделал + результат/цифра (только реальные факты)")


class ResumeEducation(_Strict):
    degree: str
    institution: str
    location: str
    start: str
    end: str
    details: str = Field(description="Доп. детали или пустая строка")


class ResumeDoc(_Strict):
    full_name: str
    headline: str = Field(description="Заголовок под именем: только целевая должность (2-6 слов), как в вакансиях, без компании и дат")
    email: str
    phone: str
    location: str
    links: list[str] = Field(description="LinkedIn, GitHub, портфолио — только если есть в исходном резюме")
    work_permit: str = Field(description="Разрешение на работу/NIE — ТОЛЬКО если прямо указано в резюме или заметках, иначе пустая строка")
    summary: str = Field(description="Раздел Perfil profesional: 3-4 предложения с главными ключевыми словами")
    skills: list[str] = Field(description="Навыки/инструменты — ровно в тех формулировках, что в вакансиях, если они есть у кандидата")
    experience: list[ResumeExperience]
    education: list[ResumeEducation]
    languages: list[str] = Field(description="ВСЕ языки из исходного резюме, ни одного не пропускать: язык — уровень, напр. 'Inglés — B2'")
    certifications: list[str]
    additional: list[str] = Field(description="Прочее: водительские права, доступность, переезд — если известно")


class ResumeRewrite(_Strict):
    resume: ResumeDoc
    changes_made: list[str] = Field(description="Что изменено и зачем, на русском")
    keywords_used: list[str] = Field(description="Какие ключевые слова из ATS-профиля вошли в резюме")
    keywords_not_used: list[str] = Field(description="Какие не вошли, потому что у кандидата этого нет (не выдумываем)")
    questions_for_user: list[str] = Field(description="Что уточнить у кандидата, чтобы резюме стало сильнее (цифры, результаты), на русском")
