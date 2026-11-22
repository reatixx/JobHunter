"""Data models: job and structured agent responses."""
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


# All fields are required and extra="forbid" — so the schema works both for Claude structured outputs
# and for the Ollama format parameter.
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResumeProfile(_Strict):
    full_name: str = Field(description="Candidate's name if given, otherwise an empty string")
    headline: str = Field(description="Short positioning of the candidate, 1 line, in the interface language")
    target_roles: list[str] = Field(description="Suitable job titles in Spanish, 3-6 items")
    years_experience: float = Field(description="Total relevant experience in years")
    hard_skills: list[str]
    soft_skills: list[str]
    languages: list[str] = Field(description="Languages with level, e.g. 'Español B2'")
    education: list[str]
    experience_summary: str = Field(description="Experience summary, 3-5 sentences, in the interface language")
    strengths: list[str] = Field(description="Resume strengths, in the interface language")
    weaknesses: list[str] = Field(description="Resume weaknesses, in the interface language")
    improvement_tips: list[str] = Field(description="Concrete tips to improve the resume for the Spanish "
                                                    "market, in the interface language")
    search_keywords: list[str] = Field(description="Job search keywords in Spanish, 3-8 items")


class JobAnalysis(_Strict):
    match_score: int = Field(description="Candidate's match with the job, 0-100")
    verdict: Literal["apply", "maybe", "no_match"]
    summary: str = Field(description="Gist of the job in 2-3 sentences, in the interface language")
    seniority: Literal["junior", "middle", "senior", "lead", "not specified"] = Field(description="Level "
                                                                                              "required by "
                                                                                              "the job")
    key_requirements: list[str] = Field(description="Key requirements, in the interface language")
    matched_skills: list[str] = Field(description="What the candidate has that matches the requirements")
    missing_skills: list[str] = Field(description="What the candidate lacks")
    red_flags: list[str] = Field(description="Problems of the job/employer ITSELF: vague duties, unrealistic "
                                             "requirements, signs of MLM, commission-only work, no salary, "
                                             "etc. Do NOT put the candidate's gaps here. Empty if none")
    language_requirements: str = Field(description="Language requirements")
    salary_info: str = Field(description="Salary if stated, otherwise 'not specified'")
    application_tips: str = Field(description="What to emphasise when applying, 1-2 sentences, in the "
                                              "interface language")


class ResumeTailoring(_Strict):
    summary_rewrite: str = Field(description="New version of the resume's 'Perfil/Resumen' section in "
                                             "Spanish for this job")
    changes: list[str] = Field(description="Concrete resume edits for the job, in the interface language")
    keywords_to_add: list[str] = Field(description="Keywords from the job worth adding (in Spanish)")
    skills_to_highlight: list[str] = Field(description="Which experience to bring to the front")
    honest_gaps: list[str] = Field(description="Gaps that can't be closed by editing the resume — how to "
                                               "talk about them honestly")


# ---------- ATS audit and resume improvement ----------

class AtsKeyword(_Strict):
    term: str = Field(description="Skill/tool/requirement, 1-4 words, as written in job ads. NOT a job title "
                                  "and NOT a whole sentence")
    synonyms: list[str] = Field(description="Spelling variants and synonyms the ATS will also count (es/en, "
                                            "abbreviations)")
    category: Literal["hard_skill", "tool", "soft_skill", "certification", "language", "education", "domain"]
    importance: Literal[1, 2, 3] = Field(description="3 — mandatory or in almost every job, 2 — frequent, 1 "
                                                     "— occasional")


class AtsProfile(_Strict):
    keywords: list[AtsKeyword] = Field(description="15-35 keywords the ATS and HR use to screen resumes for "
                                                   "these jobs")
    knockout_criteria: list[str] = Field(description="Knockout criteria that get a resume rejected "
                                                     "immediately (languages, work permit, experience, "
                                                     "location, degree), in the interface language")
    hr_screening_notes: list[str] = Field(description="What HR looks at in the first 10 seconds for these "
                                                      "specific jobs, in the interface language")
    job_titles: list[str] = Field(description="What these positions are called in job ads — for the resume "
                                              "headline")


class ResumeExperience(_Strict):
    role: str
    company: str
    location: str
    start: str = Field(description="Format MM/YYYY or YYYY")
    end: str = Field(description="MM/YYYY, YYYY or 'Actualidad'")
    bullets: list[str] = Field(description="3-6 bullets: action verb + what was done + result/number (real "
                                           "facts only)")


class ResumeEducation(_Strict):
    degree: str
    institution: str
    location: str
    start: str
    end: str
    details: str = Field(description="Extra details or an empty string")


class ResumeDoc(_Strict):
    full_name: str
    headline: str = Field(description="Headline under the name: only the target position (2-6 words), as in "
                                      "job ads, no company or dates")
    email: str
    phone: str
    location: str
    links: list[str] = Field(description="LinkedIn, GitHub, portfolio — only if present in the source resume")
    work_permit: str = Field(description="Work permit/NIE — ONLY if explicitly stated in the resume or "
                                         "notes, otherwise an empty string")
    summary: str = Field(description="Perfil profesional section: 3-4 sentences with the main keywords")
    skills: list[str] = Field(description="Skills/tools — in exactly the wording used in job ads, if the "
                                          "candidate has them")
    experience: list[ResumeExperience]
    education: list[ResumeEducation]
    languages: list[str] = Field(description="ALL languages from the source resume, do not skip any: "
                                             "language — level, e.g. 'Inglés — B2'")
    certifications: list[str]
    additional: list[str] = Field(description="Other: driving licence, availability, relocation — if known")


class ResumeRewrite(_Strict):
    resume: ResumeDoc
    changes_made: list[str] = Field(description="What was changed and why, in the interface language")
    keywords_used: list[str] = Field(description="Which ATS profile keywords made it into the resume")
    keywords_not_used: list[str] = Field(description="Which did not, because the candidate doesn't have them "
                                                     "(we don't invent)")
    questions_for_user: list[str] = Field(description="What to ask the candidate to make the resume stronger "
                                                      "(numbers, results), in the interface language")
