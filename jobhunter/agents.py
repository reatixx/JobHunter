"""Agents. Each has its own role, system prompt and backend (ollama / claude) from settings.

1. ResumeAnalyst    — reviews the resume: profile, strengths/weaknesses, tips, search keywords.
2. JobAnalyst       — scores a job against the profile: % match, requirements, gaps, red flags.
3. CoverLetterWriter — writes a cover letter (carta de presentación) for a specific job.
4. ResumeTailor     — suggests how to adapt the resume to a specific job.
5. AtsAuditor       — from a batch of jobs, finds the keywords and criteria ATS/HR use to filter resumes.
6. ResumeWriter     — rewrites the resume around those keywords (real facts only) → PDF/DOCX.
"""
from __future__ import annotations

import json
import re

from .llm import ask_json, ask_text
from .ats import detect_language, norm
from .models import AtsProfile, JobAnalysis, ResumeProfile, ResumeRewrite, ResumeTailoring
from .i18n import tr

LANG_NAMES = {"es": "Spanish (Spain)", "en": "English", "ru": "Russian"}


def resume_language(settings: dict, resume_text: str) -> str:
    """Language of the new resume: from settings or, if 'auto', the same as the source resume."""
    lang = settings.get("resume_language", "auto")
    return detect_language(resume_text) if lang == "auto" else lang


def _job_block(job: dict, max_chars: int = 12000) -> str:
    desc = (job.get("description") or "").strip() or "(description not available, title only)"
    return (
        f"Position: {job.get('title', '')}\nCompany: {job.get('company') or 'not specified'}\nLocation: {job.get('location') or 'not specified'}\nSalary: {job.get('salary') or 'not specified'}\nSource: {job.get('source')}\n\nDescription:\n{desc[:max_chars]}"
    )


def _profile_block(profile: dict | None) -> str:
    if not profile:
        return "(profile not built yet)"
    keep = ["headline", "target_roles", "years_experience", "hard_skills", "soft_skills",
            "languages", "education", "experience_summary"]
    return json.dumps({k: profile.get(k) for k in keep}, ensure_ascii=False, indent=1)


class ResumeAnalyst:
    SYSTEM = (
        "You are an experienced career consultant and recruiter on the Spanish job market. You read resumes "
        "carefully and give an honest, specific assessment with no fluff. Do not invent facts that are not "
        "in the resume. Write explanation fields in the interface language; job titles and search keywords "
        "in Spanish, as they appear in Spanish job ads."
    )

    def __init__(self, settings: dict):
        self.settings = settings

    def run(self, resume_text: str) -> dict:
        user = (
            f"Analyse the resume of a candidate looking for a job in Spain.\nAssess it through the eyes of a Spanish recruiter: structure, clarity, achievements, fit with local expectations (photo, length, language, Europass format, etc.).\n\n<resume>\n{resume_text}\n</resume>"
        )
        return ask_json(self.settings, self.settings["backend_resume_analyst"], self.SYSTEM, user,
                        ResumeProfile, effort="high").model_dump()


class JobAnalyst:
    SYSTEM = (
        "You are a job analyst. You compare a job ad with the candidate's profile and give a sober "
        "assessment. match_score: 80-100 — almost everything matches; 60-79 — fits with small gaps; 40-59 — "
        "partial match; below 40 — not a fit. Do not inflate the score. Mandatory requirements matter more "
        "than nice-to-haves. Take languages and levels into account. What the candidate lacks goes ONLY into "
        "missing_skills. red_flags are ONLY problems of the job or the employer itself (vague duties, "
        "unrealistic requirements, commission-only pay, signs of MLM, suspicious conditions). If there are "
        "none, red_flags is an empty list []. Write all explanations in the interface language. Answer "
        "strictly as JSON following the schema."
    )

    def __init__(self, settings: dict):
        self.settings = settings

    def run(self, job: dict, profile: dict | None, resume_text: str) -> dict:
        user = (
            f"<candidate_profile>\n{_profile_block(profile)}\n</candidate_profile>\n\n<resume_excerpt>\n{resume_text[:5000]}\n</resume_excerpt>\n\n<vacancy>\n{_job_block(job)}\n</vacancy>\n\nAssess how well the candidate fits this job."
        )
        a = ask_json(self.settings, self.settings["backend_job_analyst"], self.SYSTEM, user,
                     JobAnalysis, effort="low").model_dump()
        score = a["match_score"] = max(0, min(100, int(a["match_score"])))
        # derive the verdict from the score so they always agree
        a["verdict"] = "apply" if score >= 65 else "maybe" if score >= 45 else "no_match"
        return a


class CoverLetterWriter:
    SYSTEM = (
        "You write cover letters that are read by real recruiters in Spain. The letter is short (180-280 "
        "words), specific and human: no clichés like «me dirijo a ustedes con gran interés», no retelling of "
        "the whole resume. Structure: a hook — why this company/role in particular; 2-3 concrete facts from "
        "experience that cover the job's main requirements; availability for an interview. Use only facts "
        "from the resume — do not invent anything. If something is missing, don't mention it, or present it "
        "honestly as willingness to learn. Return only the letter text, with no explanations and no markdown."
    )

    def __init__(self, settings: dict):
        self.settings = settings

    def run(self, job: dict, resume_text: str, profile: dict | None, analysis: dict | None,
            extra_instructions: str = "") -> str:
        lang = LANG_NAMES.get(self.settings.get("letter_language", "es"), "Spanish")
        notes = self.settings.get("applicant_notes", "").strip()
        user = (
            f"<resume>\n{resume_text}\n</resume>\n\n"
            f"<vacancy>\n{_job_block(job)}\n</vacancy>\n\n"
            + (f"<analysis>\n{json.dumps(analysis, ensure_ascii=False)}\n</analysis>\n\n" if analysis else "")
            + (f"<applicant_notes>\n{notes}\n</applicant_notes>\n\n" if notes else "")
            + f"Write the cover letter in {lang}. Address it to the company {job.get('company') or '(name unknown — use a neutral greeting)'}. Sign it with the candidate's name from the resume."
            + (f"\n\nAdditional wishes: {extra_instructions}" if extra_instructions else "")
        )
        return ask_text(self.settings, self.settings["backend_cover_letter"], self.SYSTEM, user, effort="medium")


class ResumeTailor:
    SYSTEM = (
        "You help adapt a resume to a specific job in Spain so that it passes ATS filters and catches the "
        "recruiter's eye. Suggest concrete edits, but do not invent experience the candidate doesn't have. "
        "Explanations in the interface language, resume text in Spanish."
    )

    def __init__(self, settings: dict):
        self.settings = settings

    def run(self, job: dict, resume_text: str, analysis: dict | None) -> dict:
        user = (
            f"<resume>\n{resume_text}\n</resume>\n\n"
            f"<vacancy>\n{_job_block(job)}\n</vacancy>\n\n"
            + (f"<analysis>\n{json.dumps(analysis, ensure_ascii=False)}\n</analysis>\n\n" if analysis else "")
            + "How should this resume be adapted for this job?"
        )
        return ask_json(self.settings, self.settings["backend_resume_tailor"], self.SYSTEM, user,
                        ResumeTailoring, effort="medium").model_dump()


class AtsAuditor:
    SYSTEM = (
        "You are an expert in ATS (Applicant Tracking Systems: InfoJobs, Workday, SuccessFactors, Taleo, "
        "Personio) and in how recruiters screen resumes. From a set of job ads you determine: the keywords "
        "the robot uses to rank candidates (skills, tools, certifications, languages, domain), their "
        "spelling variants, and the knockout criteria that get a resume rejected immediately. Set importance "
        "by how often a term appears in the ads and whether it is mandatory. Keywords are skills, tools, "
        "certifications, languages and domains (1-4 words), not job titles and not whole phrases from the "
        "text. A requirement that appears in only one ad out of many gets importance 1. Do not invent "
        "requirements that are not in the ads. Explanations in the interface language."
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
            roles = ", ".join((profile or {}).get("target_roles", [])) or "(not specified)"
            body = (f"There are no jobs in the database. Describe the typical requirements of Spanish job ads for these positions: {roles}. Rely on your knowledge of the market.")
        user = (
            f"{body}\n\nThe candidate's resume will be in {LANG_NAMES[lang]} — add variants in that language to synonyms as well.\nBuild an ATS profile of these jobs."
        )
        return ask_json(self.settings, backend, self.SYSTEM, user, AtsProfile, effort="medium").model_dump()


class ResumeWriter:
    SYSTEM = (
        "You are a professional resume writer who knows how ATS and recruiters work. You rewrite resumes so "
        "that they pass automated screening and catch HR's eye, but stay HONEST. Rules:\n1. Use only facts "
        "from the source resume and the candidate's notes. Do not add skills, tools, positions, numbers or "
        "achievements that are not there. If a skill is missing, it goes into keywords_not_used.\n2. If the "
        "candidate has a skill, name it exactly as job ads do (e.g. 'análisis de datos' instead of 'trabajo "
        "con datos'; 'Power BI' if the resume really is about Power BI).\n3. Work keywords in naturally: into "
        "the headline, Perfil, skills and experience bullets. No keyword lists for their own sake, no hidden "
        "text, no repeating one word — ATS and HR catch this.\n4. Experience bullets: action verb + what was "
        "done + result. Numbers — only real ones; if a number is missing, ask in questions_for_user.\n5. "
        "Reverse chronological order, one date format, 1-2 pages. No photo, date of birth or marital "
        "status.\n6. Knockout criteria (languages, work permit, location), if the candidate meets them, "
        "should be visible at the top.\n7. All resume text is in the language given in the task; do not "
        "translate tool names.\n8. Do not add anything beyond the source: no work permit, cities, countries, "
        "universities or dates that are not in the resume or notes. An empty field is better than an "
        "invented one.\n9. Keep ALL languages, workplaces and education from the source resume.\n10. In "
        "questions_for_user ask only about facts the candidate may actually have (numbers, results, "
        "projects). Never suggest listing skills the candidate doesn't have.\nWrite changes_made, "
        "keywords_not_used and questions_for_user in the interface language."
    )

    def __init__(self, settings: dict):
        self.settings = settings

    FACTS_RULES = (
        "The source is not a resume but a FACT BASE about the candidate (possibly in another language, with "
        "answers to follow-up questions). Build the resume COMPLETELY from scratch: your own structure, "
        "wording and order; do not copy the old resume. Use only facts from the base. Items marked «TO "
        "CLARIFY» (or its equivalent in the fact base's language) must NOT go into the resume; ask a "
        "question about each of them in questions_for_user instead. If the candidate's answers clarify a "
        "fact, use the clarified version. Name positions as they were actually called or as they accurately "
        "describe the real duties (e.g. «Técnico de soporte IT»), but do not raise the level or change the "
        "nature of the work. Questions in questions_for_user must be specific, one fact each, so that each "
        "can be answered in a single message (dates, systems, volumes, results). Address the candidate "
        "informally."
    )

    def run(self, resume_text: str, ats_profile: dict, coverage: dict, issues: list, lang: str,
            job: dict | None = None, track: str | None = None) -> dict:
        from .facts import TRACKS
        notes = self.settings.get("applicant_notes", "").strip()
        missing = [r["term"] for r in coverage.get("rows", []) if not r["found"]]
        problems = "; ".join(m for lvl, m in issues if lvl != "ok") or "none"
        source = (f"<candidate_facts>\n{resume_text}\n</candidate_facts>\n\n<rules_for_facts>\n{self.FACTS_RULES}\n"
                  f"{TRACKS[track]['focus']}\n</rules_for_facts>\n\n") if track else f"<resume>\n{resume_text}\n</resume>\n\n"
        user = (
            source
            + (f"<applicant_notes>\n{notes}\n</applicant_notes>\n\n" if notes else "")
            + f"<ats_profile>\n{json.dumps(ats_profile, ensure_ascii=False)}\n</ats_profile>\n\n"
            + f"<ats_check>\nKeyword coverage: {coverage.get('score')}%. Not found in the resume: {', '.join(missing) or 'none'}.\nFormat issues: {problems}\n</ats_check>\n\n"
            + (f"<target_vacancy>\n{_job_block(job)}\n</target_vacancy>\n\n" if job else "")
            + f"Rewrite the resume in {LANG_NAMES[lang]}"
            + (" for this specific job." if job else " for this type of job.")
        )
        rw = ask_json(self.settings, self.settings["backend_resume_writer"], self.SYSTEM, user,
                      ResumeRewrite, effort="high").model_dump()
        return _guard_facts(rw, resume_text + "\n" + notes)


_PERMIT = re.compile(r"permiso|nie\b|nif\b|residencia|work permit|visa|right to work|разрешени|вид на жительство|резиденц",
                     re.I)


def _guard_facts(rw: dict, source: str) -> dict:
    """Code checks on top of the model: remove anything that is definitely not in the source."""
    doc, warnings = rw["resume"], []
    if doc.get("work_permit") and not _PERMIT.search(source):
        doc["work_permit"] = ""
        warnings.append(tr("Removed «work permit» — it is not in the resume. If you have one, add it in "
                           "Settings → «What else should the agent know about you»."))
    src = norm(source)
    for field in ("email", "phone"):
        val = doc.get(field, "")
        digits = re.sub(r"\D", "", val) if field == "phone" else norm(val)
        if val and digits and digits not in (re.sub(r"\D", "", source) if field == "phone" else src):
            doc[field] = ""
            warnings.append(tr("Removed the {0} that is not in the source resume.", field))
    rw["changes_made"] = warnings + rw["changes_made"]
    return rw
