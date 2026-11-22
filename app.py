"""JobHunter — job search in Spain with AI agents. Run: streamlit run app.py"""
from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

from jobhunter import db, facts, pipeline
from jobhunter.agents import (AtsAuditor, CoverLetterWriter, JobAnalyst, ResumeAnalyst, ResumeTailor,
                              ResumeWriter, resume_language)
from jobhunter.ats import detect_language, format_checks, keyword_coverage
from jobhunter.config import DATA_DIR, ROOT
from jobhunter.render import to_docx, to_pdf
from jobhunter.config import SECRET_KEYS, get_secret, load_settings, save_secret, save_settings
from jobhunter.llm import LLMError, ollama_status
from jobhunter.resume import extract_text
from jobhunter.sources import REMOTE_SOURCES, RU_SOURCES, SOURCES, collect, job_from_text, job_from_url
from jobhunter.i18n import tr

st.set_page_config(page_title="JobHunter", page_icon="💼", layout="wide")

settings = load_settings()
resume_text, profile = db.load_resume()

SOURCE_TITLES = {name: cls.title for name, cls in SOURCES.items()} | {"manual": tr("Manual")}


VERDICTS = {"apply": "apply", "maybe": "maybe", "no_match": "not a fit"}  # older entries keep their text


def score_badge(score):
    if score is None:
        return "⚪ —"
    return f"{'🟢' if score >= 75 else '🟡' if score >= 55 else '🔴'} {score}%"


def bullet(items):
    return "\n".join(f"- {x}" for x in items) if items else "_—_"


# ---------- Sidebar ----------
with st.sidebar:
    st.title("💼 JobHunter")
    s = db.stats()
    st.metric(tr("Jobs in database"), s["total"], help=tr("Analysed: %d") % s["analyzed"])
    st.caption(" · ".join(f"{SOURCE_TITLES.get(k, k)}: {v}" for k, v in s["by_source"].items()) or tr("empty "
                                                                                                      "so far"))
    st.divider()
    ok, msg = ollama_status(settings)
    st.write(("✅ " if ok else "⚠️ ") + msg)
    st.write(("✅ " if get_secret("ANTHROPIC_API_KEY") else "⚠️ ") + "Claude API" +
             ("" if get_secret("ANTHROPIC_API_KEY") else tr(": key not set")))
    st.write(("✅ " if resume_text else "⚠️ ") + (tr("Resume uploaded") if resume_text else tr("No resume uploaded")))
    if not get_secret("TELEGRAM_BOT_TOKEN"):
        st.write(tr("⚠️ Telegram: token not set"))
    elif not settings.get("telegram_chat_id"):
        st.write(tr("⚠️ Telegram: send /start to the bot"))
    else:
        st.write(tr("✅ Telegram bot connected"))

tab_resume, tab_facts, tab_search, tab_jobs, tab_auto, tab_settings = st.tabs(
    [tr("📄 Resume"), tr("🧾 Fact base"), tr("🔎 Search"), tr("📋 Jobs"), tr("🤖 Autopilot"), tr("⚙️ Settings")])

# ---------- Resume ----------
with tab_resume:
    st.subheader(tr("Resume"))
    up = st.file_uploader(tr("Upload your resume (PDF, DOCX or TXT)"), type=["pdf", "docx", "txt", "md"])
    if up is not None and st.session_state.get("_uploaded") != up.file_id:
        resume_text = extract_text(up.name, up.getvalue())
        db.save_resume(resume_text, profile)
        # the original file is needed to check the format (tables, headers/footers, images)
        for old in DATA_DIR.glob("resume_original.*"):
            old.unlink()
        (DATA_DIR / f"resume_original{up.name[up.name.rfind('.'):].lower()}").write_bytes(up.getvalue())
        st.session_state["_uploaded"] = up.file_id
        st.success(tr("Text extracted: {0} characters", len(resume_text)))

    edited = st.text_area(tr("Resume text (you can edit it or paste it manually)"), resume_text, height=260)
    c1, c2 = st.columns([1, 1])
    if c1.button(tr("💾 Save text")):
        db.save_resume(edited, profile)
        st.rerun()
    if c2.button(tr("🤖 Agent 1: analyse the resume"), type="primary", disabled=not edited.strip()):
        with st.spinner(tr("Analysing the resume…")):
            try:
                profile = ResumeAnalyst(settings).run(edited)
                db.save_resume(edited, profile)
                if not settings["keywords"] and profile.get("search_keywords"):
                    settings["keywords"] = profile["search_keywords"][0]
                    save_settings(settings)
                st.rerun()
            except LLMError as e:
                st.error(str(e))

    if profile:
        st.divider()
        st.markdown(f"### {profile.get('full_name') or tr('Profile')}\n_{profile.get('headline', '')}_")
        m1, m2 = st.columns(2)
        m1.markdown(tr("**Experience:** {0} years", profile.get('years_experience')))
        m1.markdown(tr("**Languages:** {0}", ', '.join(profile.get('languages', []))))
        m2.markdown(tr("**Suitable positions:** {0}", ', '.join(profile.get('target_roles', []))))
        m2.markdown(tr("**Search keywords:** {0}", ', '.join(profile.get('search_keywords', []))))
        st.markdown(tr("**Skills:** {0}", ', '.join(profile.get('hard_skills', []))))
        st.markdown(profile.get("experience_summary", ""))
        a, b, c = st.columns(3)
        a.markdown(tr("**💪 Strengths**\n") + bullet(profile.get("strengths")))
        b.markdown(tr("**🩹 Weak spots**\n") + bullet(profile.get("weaknesses")))
        c.markdown(tr("**🛠 What to improve**\n") + bullet(profile.get("improvement_tips")))

    # ---------- ATS resume improvement ----------
    if edited.strip():
        st.divider()
        st.subheader(tr("🚀 Improve the resume for ATS and HR"))
        st.caption(tr("Agent 5 finds out which keywords and criteria the robot (ATS) and HR use to screen "
                      "resumes for your jobs. Agent 6 rewrites the resume around them — using only your real "
                      "facts — and builds a PDF/DOCX."))
        detected = detect_language(edited)
        names = {"es": tr("Spanish"), "en": tr("English"), "ru": tr("Russian")}
        lang_opts = ["auto", "es", "en", "ru"]
        lc, tc = st.columns([1, 2])
        new_lang = lc.selectbox(
            tr("New resume language"), lang_opts, index=lang_opts.index(settings.get("resume_language", "auto")),
            format_func=lambda x: tr("same as the source ({0})", names[detected]) if x == "auto" else names[x])
        if new_lang != settings.get("resume_language", "auto"):
            settings["resume_language"] = new_lang
            save_settings(settings)
        lang = resume_language(settings, edited)

        pool = db.list_jobs(statuses=["new", "interesting", "applied", "interview"])
        good = [j for j in pool if (j["score"] or 0) >= 45] or pool
        target_opts = ["__all__"] + [j["id"] for j in pool]
        by_id = {j["id"]: j for j in pool}
        target = tc.selectbox(
            tr("Optimise for"), target_opts,
            format_func=lambda x: (tr("All suitable jobs in the database ({0})", len(good)) if good else
                                   tr("Typical jobs for the profile (database is empty)")) if x == "__all__"
            else f"{score_badge(by_id[x]['score'])} {by_id[x]['title']} — {by_id[x]['company']}")
        target_job = by_id.get(target)

        orig = next(DATA_DIR.glob("resume_original.*"), None)
        issues = format_checks(edited, orig.name if orig else "", orig.read_bytes() if orig else None)

        if st.button(tr("🔍 Agent 5: ATS audit"), type="primary"):
            with st.spinner(tr("Studying the jobs and screening criteria…")):
                try:
                    ats = AtsAuditor(settings).run([target_job] if target_job else good, profile, lang)
                    db.kv_set("ats_profile", {"target": target, "lang": lang, "profile": ats})
                    st.rerun()
                except LLMError as e:
                    st.error(str(e))

        saved = db.kv_get("ats_profile")
        if saved and saved.get("target") != target:
            st.info(tr("The ATS audit was done for a different target — run agent 5 again."))
        elif saved:
            ats = saved["profile"]
            cov = keyword_coverage(edited, ats["keywords"])
            a1, a2 = st.columns([1, 2])
            a1.metric(tr("Keyword match"), f"{cov['score']}%",
                      help=tr("Weighted by importance: the most frequent/mandatory words count triple"))
            icon = {"ok": "✅", "warn": "⚠️", "fail": "❌"}
            a1.markdown(tr("**Readability for the robot**\n") + "\n".join(f"- {icon[l]} {m}" for l, m in issues))
            stars = {3: "★★★", 2: "★★", 1: "★"}
            a2.markdown(tr("**Keywords (★★★ — in almost every job)**"))
            a2.dataframe(
                [{"": "✅" if r["found"] else "❌", tr("Word"): r["term"], tr("Importance"): stars[r["importance"]],
                  tr("Found as"): r["as"], tr("Type"): r["category"]} for r in cov["rows"]],
                hide_index=True, width="stretch", height=300)
            k1, k2 = st.columns(2)
            k1.markdown(tr("**⛔ Knockout criteria — instant rejection**\n") + bullet(ats["knockout_criteria"]))
            k2.markdown(tr("**👀 What HR looks at in the first 10 seconds**\n") + bullet(ats["hr_screening_notes"]))

            if st.button(tr("✍️ Agent 6: rewrite the resume and build a PDF"), type="primary"):
                with st.spinner(tr("Rewriting the resume…")):
                    try:
                        rw = ResumeWriter(settings).run(edited, ats, cov, issues, lang, target_job)
                        db.kv_set("resume_rewrite", {"target": target, "lang": lang, "before": cov["score"], **rw})
                        st.rerun()
                    except LLMError as e:
                        st.error(str(e))

            rw = db.kv_get("resume_rewrite")
            if rw and rw.get("target") == target:
                doc, rlang = rw["resume"], rw["lang"]
                pdf_bytes = to_pdf(doc, rlang)
                # check the final PDF the way an ATS reads it: extract the text back
                pdf_text = extract_text("cv.pdf", pdf_bytes)
                after = keyword_coverage(pdf_text, ats["keywords"])
                after_issues = format_checks(pdf_text, "cv.pdf", pdf_bytes)
                n_before = sum(l != "ok" for l, _ in issues)
                n_after = sum(l != "ok" for l, _ in after_issues)
                st.success(tr("The new resume is ready"))
                r1, r2, r3 = st.columns(3)
                r1.metric(tr("Keywords"), f"{after['score']}%", tr("{0:+d} pp", after['score'] - rw['before']))
                r2.metric(tr("Format issues"), n_after, f"{n_after - n_before:+d}", delta_color="inverse")
                r3.metric(tr("Language"), names[rlang])
                fname = "CV_" + "_".join((doc.get("full_name") or "candidato").split())
                d1, d2, d3 = st.columns(3)
                d1.download_button(tr("⬇️ Download PDF"), pdf_bytes, f"{fname}.pdf", "application/pdf", type="primary")
                d2.download_button(tr("⬇️ Download DOCX"), to_docx(doc, rlang), f"{fname}.docx",
                                   "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
                if d3.button(tr("📌 Make it the main resume"), help=tr("New letters and analyses will use this text")):
                    db.save_resume(pdf_text, profile)
                    st.rerun()
                c1, c2 = st.columns(2)
                c1.markdown(tr("**What was changed**\n") + bullet(rw["changes_made"]))
                c2.markdown(tr("**❓ Clarify these — it will get stronger**\n") + bullet(rw["questions_for_user"]))
                if rw["keywords_not_used"]:
                    st.warning(tr("Not added because it is not in your experience (no inventing — it would "
                                  "come out in the interview): ") + ", ".join(rw["keywords_not_used"]))
                with st.expander(tr("New resume text as the robot sees it")):
                    st.text(pdf_text)

# ---------- Fact base ----------
with tab_facts:
    st.subheader(tr("Fact base — the source of truth for resumes"))
    st.caption(tr("An honest, detailed account of yourself. Resumes, letters and job scoring are built from "
                  "it, not from the old resume. Write it as it is, in your own language. If you're not sure "
                  "about something, mark it «TO CLARIFY»: the agent won't use it and will ask instead."))
    facts_text = st.text_area(tr("Fact base"), facts.get_facts(), height=480, label_visibility="collapsed")
    fb1, fb2 = st.columns(2)
    if fb1.button(tr("💾 Save the fact base"), type="primary"):
        facts.set_facts(facts_text)
        st.success(tr("Saved"))
    if fb2.button(tr("🔁 Agent 1: update the profile from the fact base"), disabled=not facts_text.strip(),
                  help=tr("The profile is needed to score jobs (agent 2)")):
        with st.spinner(tr("Analysing…")):
            try:
                db.save_resume(resume_text, ResumeAnalyst(settings).run(facts.source_text(resume_text)))
                st.success(tr("Profile updated"))
            except LLMError as e:
                st.error(str(e))

    st.markdown(tr("### ❓ Agent's questions"))
    st.caption(tr("The same questions arrive in Telegram — you can answer there (with a reply) or here."))
    oq = facts.open_questions()
    if not oq:
        st.info(tr("No open questions."))
    for q in oq:
        st.markdown(f"**{q['q']}**" + (f"  \n_{q['context']}_" if q.get("context") else ""))
        q1, q2, q3 = st.columns([5, 1, 1])
        ans = q1.text_input(tr("Answer"), key=f"qa_{q['id']}", label_visibility="collapsed")
        if q2.button("💾", key=f"qsv_{q['id']}", disabled=not ans.strip()):
            facts.answer(q["id"], ans)
            st.rerun()
        if q3.button("🤷", key=f"qsk_{q['id']}", help=tr("Don't know / skip")):
            facts.skip(q["id"])
            st.rerun()
    answered = [q for q in facts.get_qa() if q["status"] == "answered"]
    if answered:
        with st.expander(tr("Answered ({0}) — included in the fact base", len(answered))):
            for q in answered:
                st.markdown(f"- **{q['q']}**  \n  {q['a']}")

    st.markdown(tr("### 📄 Resumes by track (built entirely from scratch from the fact base)"))
    tcols = st.columns(len(facts.TRACKS))
    for col, (track, t) in zip(tcols, facts.TRACKS.items()):
        col.markdown(f"**{t['name']}**")
        if col.button(tr("✍️ Build: {0}", t['name']), key=f"mb_{track}", disabled=not facts.get_facts()):
            with st.spinner(tr("Writing the resume «{0}»…", t['name'])):
                try:
                    facts.build_master(settings, track)
                    st.rerun()
                except LLMError as e:
                    st.error(str(e))
        m = db.kv_get(f"master_{track}")
        if m and Path(m["pdf"]).exists():
            pdfp = Path(m["pdf"])
            col.download_button("⬇️ PDF", pdfp.read_bytes(), pdfp.name, "application/pdf", key=f"md_{track}")
            col.download_button("⬇️ DOCX", to_docx(m["resume"], m["lang"]), pdfp.stem + ".docx", key=f"mx_{track}")
            with col.popover(tr("What was done")):
                st.markdown(bullet(m["changes_made"]))

# ---------- Search ----------
with tab_search:
    st.subheader(tr("Job search"))
    suggestions = (profile or {}).get("search_keywords", [])
    if suggestions:
        st.caption(tr("Suggestions from the resume: ") + " · ".join(suggestions))
    c1, c2 = st.columns([2, 1])
    keywords = c1.text_input(tr("Keywords (in Spanish)"), settings["keywords"])
    location = c2.text_input(tr("City"), settings["location"])
    chosen = st.multiselect(tr("Sources"), list(SOURCES), default=[s for s in settings["sources"] if s in SOURCES],
                            format_func=lambda n: SOURCE_TITLES[n])
    for name in chosen:
        ok, why = SOURCES[name](settings).is_configured()
        if not ok:
            st.warning(f"{SOURCE_TITLES[name]}: {why}")

    auto_analyze = st.checkbox(tr("Analyse new jobs right away (agent 2)"), value=bool(resume_text))
    if st.button(tr("🔎 Find"), type="primary", disabled=not keywords.strip() or not chosen):
        settings.update(keywords=keywords, location=location, sources=chosen)
        save_settings(settings)
        status = st.status(tr("Searching for jobs…"), expanded=True)
        jobs, report = collect(settings, keywords, location, chosen,
                               on_progress=lambda n, m: status.write(f"**{SOURCE_TITLES[n]}**: {m}"))
        added = db.save_jobs(jobs)
        status.update(label=tr("Done: {0} found, {1} new", len(jobs), added), state="complete")
        st.session_state["_run_analysis"] = auto_analyze and added > 0

    with st.expander(tr("➕ Add a job manually (link or text)")):
        url = st.text_input(tr("Job link"))
        text = st.text_area(tr("…or the job text"), height=120)
        if st.button(tr("Add")):
            try:
                job = job_from_url(url) if url.strip() and not text.strip() else job_from_text(text, url=url)
                n = db.save_jobs([job])
                st.success(tr("Added") if n else tr("This job is already in the database"))
            except Exception as e:
                st.error(tr("Couldn't load: {0}", e))

    st.divider()
    pending = db.list_jobs(analyzed=False, statuses=["new", "interesting"])
    st.write(tr("Jobs not analysed yet: **{0}**", len(pending)))
    if (st.button(tr("🤖 Agent 2: analyse"), disabled=not pending or not resume_text)
            or (st.session_state.pop("_run_analysis", False) and pending)):
        bar = st.progress(0.0, tr("Analysis…"))
        analyst = JobAnalyst(settings)
        for i, job in enumerate(pending, 1):
            bar.progress(i / len(pending), f"{i}/{len(pending)}: {job['title'][:60]}")
            try:
                a = analyst.run(job, profile, resume_text)
                db.update_job(job["id"], analysis=a, score=a["match_score"])
            except LLMError as e:
                st.error(tr("Stopped: {0}", e))
                break
            except Exception as e:
                st.warning(f"{job['title']}: {e}")
        bar.empty()
        st.success(tr("Analysis finished — see the «Jobs» tab"))
    if not resume_text:
        st.info(tr("To analyse jobs, upload your resume first."))

# ---------- Jobs ----------
with tab_jobs:
    f1, f2, f3 = st.columns([1, 2, 2])
    min_score = f1.slider(tr("Min. match"), 0, 100, 0, 5)
    f_sources = f2.multiselect(tr("Source"), list(SOURCE_TITLES), format_func=lambda n: SOURCE_TITLES[n])
    f_status = f3.multiselect(tr("Status"), list(db.STATUSES), default=["new", "interesting", "applied", "interview"],
                              format_func=lambda s: db.STATUSES[s])
    jobs = db.list_jobs(min_score=min_score or None, sources=f_sources or None, statuses=f_status or None)
    st.caption(tr("Shown: {0}", len(jobs)))

    for job in jobs:
        a = job["analysis"]
        head = (f"{score_badge(job['score'])} · **{job['title']}** — {job['company'] or '?'} · "
                f"{job['location'] or ''} · _{SOURCE_TITLES.get(job['source'], job['source'])}_")
        with st.expander(head):
            top = st.columns([3, 2, 1])
            top[0].markdown(tr("[Open the job ↗]({0})", job['url']) if job["url"] else "")
            if job["salary"]:
                top[0].markdown(f"💶 {job['salary']}")
            new_status = top[1].selectbox(tr("Status"), list(db.STATUSES), index=list(db.STATUSES).index(job["status"]),
                                          format_func=lambda s: db.STATUSES[s], key=f"st_{job['id']}")
            if new_status != job["status"]:
                db.update_job(job["id"], status=new_status)
                st.rerun()
            if top[2].button("🗑", key=f"del_{job['id']}", help=tr("Delete from database")):
                db.delete_job(job["id"])
                st.rerun()

            if a:
                st.markdown(tr("**Verdict:** {0} · **Level:** {1} · **Languages:** {2}", tr(VERDICTS.get(a['verdict'], a['verdict'])), a['seniority'], a['language_requirements']))
                st.markdown(a["summary"])
                c1, c2, c3 = st.columns(3)
                c1.markdown(tr("**✅ Matches**\n") + bullet(a["matched_skills"]))
                c2.markdown(tr("**❗ Missing**\n") + bullet(a["missing_skills"]))
                c3.markdown(tr("**🚩 Red flags**\n") + bullet(a["red_flags"]))
                st.info("💡 " + a["application_tips"])
            elif resume_text and st.button(tr("🤖 Analyse"), key=f"an_{job['id']}"):
                with st.spinner(tr("Analysis…")):
                    try:
                        res = JobAnalyst(settings).run(job, profile, resume_text)
                        db.update_job(job["id"], analysis=res, score=res["match_score"])
                        st.rerun()
                    except LLMError as e:
                        st.error(str(e))

            with st.popover(tr("Job description")):
                st.text(job["description"] or "—")

            st.markdown("---")
            b1, b2 = st.columns(2)
            wishes = b1.text_input(tr("Wishes for the letter (optional)"), key=f"wish_{job['id']}",
                                   placeholder=tr("e.g. shorter, mention that I can start right away"))
            if b1.button(tr("✉️ Agent 3: cover letter"), key=f"cl_{job['id']}", disabled=not resume_text):
                with st.spinner(tr("Writing the letter…")):
                    try:
                        letter = CoverLetterWriter(settings).run(job, resume_text, profile, a, wishes)
                        db.update_job(job["id"], letter=letter)
                        st.rerun()
                    except LLMError as e:
                        st.error(str(e))
            if b2.button(tr("🎯 Agent 4: tailor the resume"), key=f"tl_{job['id']}", disabled=not resume_text):
                with st.spinner(tr("Preparing recommendations…")):
                    try:
                        db.update_job(job["id"], tailoring=ResumeTailor(settings).run(job, resume_text, a))
                        st.rerun()
                    except LLMError as e:
                        st.error(str(e))

            if job["letter"]:
                letter = st.text_area(tr("Cover letter"), job["letter"], height=300, key=f"lt_{job['id']}")
                l1, l2 = st.columns(2)
                if l1.button(tr("💾 Save changes"), key=f"sv_{job['id']}"):
                    db.update_job(job["id"], letter=letter)
                    st.toast(tr("Saved"))
                l2.download_button(tr("⬇️ Download .txt"), letter, file_name=f"carta_{job['company'] or 'empresa'}.txt",
                                   key=f"dl_{job['id']}")
            if t := job["tailoring"]:
                st.markdown(tr("**New «Perfil» section for the resume:**"))
                st.code(t["summary_rewrite"], language=None)
                c1, c2 = st.columns(2)
                c1.markdown(tr("**Edits**\n") + bullet(t["changes"]))
                c1.markdown(tr("**Bring to the front**\n") + bullet(t["skills_to_highlight"]))
                c2.markdown(tr("**Add keywords**\n") + ", ".join(t["keywords_to_add"]))
                c2.markdown(tr("**Honestly about the gaps**\n") + bullet(t["honest_gaps"]))

# ---------- Autopilot ----------
with tab_auto:
    st.subheader(tr("Autopilot"))
    st.caption(tr("Searches for jobs, filters out unsuitable ones, scores them and prepares a resume (PDF) "
                  "and a letter for the best. It never sends anything: ready applications wait for you below."))
    last = db.kv_get("last_run")
    running = pipeline.LOCK_PATH.exists()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric(tr("Ready to send"), len(db.list_jobs(statuses=["ready"])))
    if last:
        m2.metric(tr("Last run"), last["started"][5:16].replace("T", " "))
        m3.metric(tr("New / filtered out"), f"{last['new']} / {last['filtered']}")
        m4.metric(tr("Scored / prepared"), f"{last['analyzed']} / {last['prepared']}")
        if last.get("error"):
            st.error(tr("Last run error: {0}", last['error']))
    nxt = pipeline.schedule_info()
    r1, r2, r3 = st.columns(3)
    if r1.button(tr("▶️ Run now"), type="primary", disabled=running):
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen([str(ROOT / ".venv" / "Scripts" / "pythonw.exe"), "-m", "jobhunter.pipeline"],
                         cwd=ROOT, creationflags=flags)
        time.sleep(1)
        st.rerun()
    if running:
        r1.info(tr("⏳ Running… refresh the page later"))
    r2.write(tr("🗓 Scheduled: **{0}**, next run {1}", settings['auto_times'], nxt) if nxt
             else tr("🗓 Schedule is off"))
    if not nxt and r3.button(tr("Turn on the schedule")):
        save_settings(settings)
        pipeline.install_schedule(settings["auto_times"])
        st.rerun()
    if nxt and r3.button(tr("Turn off the schedule")):
        pipeline.remove_schedule()
        st.rerun()

    with st.expander(tr("Autopilot settings"), expanded=not settings["auto_queries"]):
        settings["auto_queries"] = st.text_area(
            tr("Search queries: one per line, «keywords | city» (the city is optional)"),
            settings["auto_queries"], height=120,
            placeholder=f"soporte técnico ruso | remoto\nhelpdesk | {settings['location']}\ntester QA junior")
        a1, a2, a3 = st.columns(3)
        settings["auto_min_score"] = a1.slider(tr("Prepare an application from match, %"), 40, 95, int(settings["auto_min_score"]), 5)
        settings["auto_max_prepare"] = a2.number_input(tr("Max. applications per run"), 0, 30, int(settings["auto_max_prepare"]),
                                                      help=tr("Each application = 2 Claude calls (resume + "
                                                              "letter)"))
        settings["auto_max_analyze"] = a3.number_input(tr("Max. scorings per run (Ollama)"), 0, 300,
                                                      int(settings["auto_max_analyze"]))
        settings["auto_skip_english_required"] = st.checkbox(
            tr("Filter out jobs that require high English (B2+, C1, fluent)"), settings["auto_skip_english_required"])
        settings["auto_skip_english_ads"] = st.checkbox(tr("Filter out job ads written in English"),
                                                        settings["auto_skip_english_ads"])
        settings["auto_remote_only"] = st.checkbox(tr("Remote only"), settings["auto_remote_only"])
        settings["auto_block_words"] = st.text_input(tr("Filter out if the text contains these words "
                                                        "(comma-separated)"),
                                                     settings["auto_block_words"], placeholder="autónomo, comisiones, puerta fría")
        settings["auto_times"] = st.text_input(tr("Scheduled run times (comma-separated)"), settings["auto_times"])
        st.markdown(tr("**🌍 Worldwide remote**"))
        remote_names = [n for n in SOURCES if n in REMOTE_SOURCES]
        settings["remote_sources"] = st.multiselect(
            tr("International job boards"), remote_names, default=[n for n in settings["remote_sources"] if n in remote_names],
            format_func=lambda n: SOURCE_TITLES[n])
        settings["remote_queries"] = st.text_area(
            tr("Queries for them — one per line, preferably in English (no city needed)"), settings["remote_queries"],
            height=140)
        settings["remote_regions"] = st.text_input(
            tr("Keep jobs open to these regions (comma-separated)"), settings["remote_regions"],
            help=tr("A «USA only» job will be filtered out; «Europe», «Worldwide», «Spain» will stay"))
        st.markdown(tr("**🇷🇺 Russian-language sources**"))
        ru_names = [n for n in SOURCES if n in RU_SOURCES]
        settings["ru_sources"] = st.multiselect(
            tr("Sources"), ru_names, default=[n for n in settings["ru_sources"] if n in ru_names],
            format_func=lambda n: SOURCE_TITLES[n], key="ru_src")
        settings["ru_queries"] = st.text_area(tr("Queries for them — one per line"), settings["ru_queries"], height=140)
        settings["tg_channels"] = st.text_input(tr("Telegram channels (comma-separated, without @)"), settings["tg_channels"])
        settings["skip_ru_by"] = st.checkbox(tr("Filter out companies from Russia and Belarus"), settings["skip_ru_by"],
                                             help=tr("By rubles, Russian labour code, cities, well-known "
                                                     "companies; «except Russia» doesn't count"))
        st.markdown(tr("**🏠 No relocation**"))
        settings["no_relocation"] = st.checkbox(tr("Only remote or close to home"), settings["no_relocation"])
        settings["home_area"] = st.text_input(tr("Close to home means (comma-separated)"), settings["home_area"])
        if st.button(tr("💾 Save autopilot settings")):
            save_settings(settings)
            if nxt:  # the time may have changed
                pipeline.install_schedule(settings["auto_times"])
            st.success(tr("Saved"))

    st.markdown(tr("### 📦 Ready to send"))
    ready = db.list_jobs(statuses=["ready"])
    if not ready:
        st.info(tr("Nothing yet. Run the autopilot or wait for the scheduled run."))
    for job in ready:
        with st.expander(f"{score_badge(job['score'])} · **{job['title']}** — {job['company'] or '?'} · "
                         f"_{SOURCE_TITLES.get(job['source'], job['source'])}_"):
            if job["url"]:
                st.markdown(tr("[Open the job and apply ↗]({0})", job['url']))
            if a := job["analysis"]:
                st.markdown(a["summary"])
                st.caption("💡 " + a["application_tips"])
            cv = job["cv"] or {}
            folder = Path(job["app_dir"]) if job["app_dir"] else None
            pdf = next(folder.glob("*.pdf"), None) if folder and folder.exists() else None
            c1, c2, c3 = st.columns(3)
            if pdf:
                c1.download_button(tr("⬇️ Resume PDF"), pdf.read_bytes(), pdf.name, "application/pdf", key=f"rp_{job['id']}")
            if folder and c2.button(tr("📂 Open folder"), key=f"of_{job['id']}"):
                os.startfile(folder)
            if cv.get("resume"):
                c3.download_button(tr("⬇️ Resume DOCX"), to_docx(cv["resume"], cv["lang"]),
                                   (pdf.stem if pdf else "CV") + ".docx", key=f"rd_{job['id']}")
            letter = st.text_area(tr("Cover letter"), job["letter"] or "", height=260, key=f"rl_{job['id']}")
            if letter != (job["letter"] or "") and st.button(tr("💾 Save letter"), key=f"rs_{job['id']}"):
                db.update_job(job["id"], letter=letter)
                if folder and folder.exists():
                    (folder / "carta.txt").write_text(letter, encoding="utf-8")
                st.toast(tr("Saved"))
            if cv.get("questions_for_user"):
                st.markdown(tr("**❓ The agent asks (add the answers in Settings → «What else should the "
                               "agent know about you» to make the resume stronger):**\n") + bullet(cv["questions_for_user"]))
            if cv.get("changes_made"):
                with st.popover(tr("What was changed in the resume")):
                    st.markdown(bullet(cv["changes_made"]))
            b1, b2 = st.columns(2)
            if b1.button(tr("✅ I sent the application"), key=f"ra_{job['id']}", type="primary"):
                db.update_job(job["id"], status="applied", applied_at=datetime.now().isoformat(timespec="seconds"))
                st.rerun()
            if b2.button(tr("🙈 Not a fit, remove"), key=f"rh_{job['id']}"):
                db.update_job(job["id"], status="hidden")
                st.rerun()

    filtered = [j for j in db.list_jobs(statuses=["hidden"]) if j["filtered"]]
    if filtered:
        with st.expander(tr("🚫 Filtered out: {0} (check for mistakes)", len(filtered))):
            for job in filtered[:100]:
                f1, f2 = st.columns([5, 1])
                f1.markdown(f"[{job['title']}]({job['url']}) — {job['company'] or '?'} · _{job['filtered']}_")
                if f2.button(tr("Restore"), key=f"fr_{job['id']}"):
                    db.update_job(job["id"], status="new", filtered=None)
                    st.rerun()

    if pipeline.LOG_PATH.exists():
        with st.expander(tr("Autopilot log")):
            lines = pipeline.LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
            st.code("\n".join(lines[-60:]), language=None)

# ---------- Settings ----------
with tab_settings:
    st.subheader(tr("API keys"))
    st.caption(tr("Stored only locally, in the project's .env file"))
    for key, label in SECRET_KEYS.items():
        cur = get_secret(key)
        val = st.text_input(f"{label}", type="password", placeholder=tr("set ✓") if cur else tr("not set"),
                            key=f"sec_{key}")
        if val:
            save_secret(key, val.strip())
            st.success(tr("{0} saved", key))

    st.subheader(tr("Models"))
    c1, c2 = st.columns(2)
    settings["ollama_model"] = c1.text_input(tr("Ollama model"), settings["ollama_model"])
    settings["claude_model"] = c2.text_input(tr("Claude model"), settings["claude_model"])
    roles = {"backend_resume_analyst": tr("Agent 1 — resume analysis"), "backend_job_analyst": tr("Agent 2 — job "
                                                                                            "analysis"),
             "backend_cover_letter": tr("Agent 3 — cover letters"), "backend_resume_tailor": tr("Agent "
                                                                                                      "4 — "
                                                                                                      "resume "
                                                                                                      "tailoring"),
             "backend_ats_auditor": tr("Agent 5 — ATS audit"), "backend_resume_writer": tr("Agent 6 — new resume")}
    cols = st.columns(3) * 2
    for col, (k, label) in zip(cols, roles.items()):
        settings[k] = col.radio(label, ["ollama", "claude"], index=["ollama", "claude"].index(settings[k]), key=k)

    st.subheader(tr("Search"))
    c1, c2, c3 = st.columns(3)
    settings["max_per_source"] = c1.number_input(tr("Max. jobs per source"), 1, 100, int(settings["max_per_source"]))
    settings["linkedin_days"] = c2.number_input(tr("LinkedIn: last N days (0 = all)"), 0, 30, int(settings["linkedin_days"]))
    settings["infojobs_province"] = c3.text_input(tr("InfoJobs: province (madrid, barcelona…)"), settings["infojobs_province"])
    settings["exclude_words"] = st.text_input(tr("Exclude jobs with these words in the title (comma-separated)"),
                                              settings["exclude_words"], placeholder="senior, comercial, prácticas")

    st.subheader(tr("Interface"))
    ui_opts = {"en": "English", "ru": "Русский"}
    settings["ui_lang"] = st.radio(tr("Interface language (restart the app and the bot after changing)"),
                                   list(ui_opts), horizontal=True, format_func=ui_opts.get,
                                   index=list(ui_opts).index(settings.get("ui_lang", "en")))

    st.subheader(tr("Letters"))
    settings["letter_language"] = st.radio(tr("Letter language"), ["es", "en", "ru"], horizontal=True,
                                           index=["es", "en", "ru"].index(settings["letter_language"]))
    settings["applicant_notes"] = st.text_area(
        tr("What else should the agent know about you (not in the resume)"), settings["applicant_notes"],
        placeholder=tr("e.g. I have an NIE and a work permit. Available in 2 weeks. Looking for hybrid work "
                       "in Madrid."))
    if st.button(tr("💾 Save settings"), type="primary"):
        save_settings(settings)
        st.success(tr("Saved"))
