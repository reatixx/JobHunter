"""JobHunter — поиск вакансий в Испании с ИИ-агентами. Запуск: streamlit run app.py"""
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

st.set_page_config(page_title="JobHunter", page_icon="💼", layout="wide")

settings = load_settings()
resume_text, profile = db.load_resume()

SOURCE_TITLES = {name: cls.title for name, cls in SOURCES.items()} | {"manual": "Вручную"}


def score_badge(score):
    if score is None:
        return "⚪ —"
    return f"{'🟢' if score >= 75 else '🟡' if score >= 55 else '🔴'} {score}%"


def bullet(items):
    return "\n".join(f"- {x}" for x in items) if items else "_—_"


# ---------- Боковая панель ----------
with st.sidebar:
    st.title("💼 JobHunter")
    s = db.stats()
    st.metric("Вакансий в базе", s["total"], help="Проанализировано: %d" % s["analyzed"])
    st.caption(" · ".join(f"{SOURCE_TITLES.get(k, k)}: {v}" for k, v in s["by_source"].items()) or "пока пусто")
    st.divider()
    ok, msg = ollama_status(settings)
    st.write(("✅ " if ok else "⚠️ ") + msg)
    st.write(("✅ " if get_secret("ANTHROPIC_API_KEY") else "⚠️ ") + "Claude API" +
             ("" if get_secret("ANTHROPIC_API_KEY") else ": ключ не задан"))
    st.write(("✅ " if resume_text else "⚠️ ") + ("Резюме загружено" if resume_text else "Резюме не загружено"))
    if not get_secret("TELEGRAM_BOT_TOKEN"):
        st.write("⚠️ Telegram: токен не задан")
    elif not settings.get("telegram_chat_id"):
        st.write("⚠️ Telegram: напиши боту /start")
    else:
        st.write("✅ Telegram-бот привязан")

tab_resume, tab_facts, tab_search, tab_jobs, tab_auto, tab_settings = st.tabs(
    ["📄 Резюме", "🧾 База фактов", "🔎 Поиск", "📋 Вакансии", "🤖 Автопилот", "⚙️ Настройки"])

# ---------- Резюме ----------
with tab_resume:
    st.subheader("Резюме")
    up = st.file_uploader("Загрузите резюме (PDF, DOCX или TXT)", type=["pdf", "docx", "txt", "md"])
    if up is not None and st.session_state.get("_uploaded") != up.file_id:
        resume_text = extract_text(up.name, up.getvalue())
        db.save_resume(resume_text, profile)
        # оригинал файла нужен для проверки формата (таблицы, колонтитулы, картинки)
        for old in DATA_DIR.glob("resume_original.*"):
            old.unlink()
        (DATA_DIR / f"resume_original{up.name[up.name.rfind('.'):].lower()}").write_bytes(up.getvalue())
        st.session_state["_uploaded"] = up.file_id
        st.success(f"Текст извлечён: {len(resume_text)} символов")

    edited = st.text_area("Текст резюме (можно поправить или вставить вручную)", resume_text, height=260)
    c1, c2 = st.columns([1, 1])
    if c1.button("💾 Сохранить текст"):
        db.save_resume(edited, profile)
        st.rerun()
    if c2.button("🤖 Агент 1: проанализировать резюме", type="primary", disabled=not edited.strip()):
        with st.spinner("Анализирую резюме…"):
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
        st.markdown(f"### {profile.get('full_name') or 'Профиль'}\n_{profile.get('headline', '')}_")
        m1, m2 = st.columns(2)
        m1.markdown(f"**Опыт:** {profile.get('years_experience')} лет")
        m1.markdown(f"**Языки:** {', '.join(profile.get('languages', []))}")
        m2.markdown(f"**Подходящие должности:** {', '.join(profile.get('target_roles', []))}")
        m2.markdown(f"**Ключевые слова для поиска:** {', '.join(profile.get('search_keywords', []))}")
        st.markdown(f"**Навыки:** {', '.join(profile.get('hard_skills', []))}")
        st.markdown(profile.get("experience_summary", ""))
        a, b, c = st.columns(3)
        a.markdown("**💪 Сильные стороны**\n" + bullet(profile.get("strengths")))
        b.markdown("**🩹 Слабые места**\n" + bullet(profile.get("weaknesses")))
        c.markdown("**🛠 Что улучшить**\n" + bullet(profile.get("improvement_tips")))

    # ---------- Улучшение резюме под ATS ----------
    if edited.strip():
        st.divider()
        st.subheader("🚀 Улучшить резюме для ATS и HR")
        st.caption("Агент 5 выясняет, по каким словам и критериям робот (ATS) и HR отсеивают резюме на ваши вакансии. "
                   "Агент 6 переписывает резюме под них — только на ваших реальных фактах — и собирает PDF/DOCX.")
        detected = detect_language(edited)
        names = {"es": "испанский", "en": "английский", "ru": "русский"}
        lang_opts = ["auto", "es", "en", "ru"]
        lc, tc = st.columns([1, 2])
        new_lang = lc.selectbox(
            "Язык нового резюме", lang_opts, index=lang_opts.index(settings.get("resume_language", "auto")),
            format_func=lambda x: f"как в исходном ({names[detected]})" if x == "auto" else names[x])
        if new_lang != settings.get("resume_language", "auto"):
            settings["resume_language"] = new_lang
            save_settings(settings)
        lang = resume_language(settings, edited)

        pool = db.list_jobs(statuses=["new", "interesting", "applied", "interview"])
        good = [j for j in pool if (j["score"] or 0) >= 45] or pool
        target_opts = ["__all__"] + [j["id"] for j in pool]
        by_id = {j["id"]: j for j in pool}
        target = tc.selectbox(
            "Под что оптимизировать", target_opts,
            format_func=lambda x: (f"Под все подходящие вакансии из базы ({len(good)})" if good else
                                   "Под типичные вакансии по профилю (база пуста)") if x == "__all__"
            else f"{score_badge(by_id[x]['score'])} {by_id[x]['title']} — {by_id[x]['company']}")
        target_job = by_id.get(target)

        orig = next(DATA_DIR.glob("resume_original.*"), None)
        issues = format_checks(edited, orig.name if orig else "", orig.read_bytes() if orig else None)

        if st.button("🔍 Агент 5: ATS-аудит", type="primary"):
            with st.spinner("Изучаю вакансии и критерии отбора…"):
                try:
                    ats = AtsAuditor(settings).run([target_job] if target_job else good, profile, lang)
                    db.kv_set("ats_profile", {"target": target, "lang": lang, "profile": ats})
                    st.rerun()
                except LLMError as e:
                    st.error(str(e))

        saved = db.kv_get("ats_profile")
        if saved and saved.get("target") != target:
            st.info("ATS-аудит сделан для другой цели — запустите агента 5 заново.")
        elif saved:
            ats = saved["profile"]
            cov = keyword_coverage(edited, ats["keywords"])
            a1, a2 = st.columns([1, 2])
            a1.metric("Совпадение с ключевыми словами", f"{cov['score']}%",
                      help="Взвешено по важности слова: самые частые/обязательные считаются втрое")
            icon = {"ok": "✅", "warn": "⚠️", "fail": "❌"}
            a1.markdown("**Читаемость для робота**\n" + "\n".join(f"- {icon[l]} {m}" for l, m in issues))
            stars = {3: "★★★", 2: "★★", 1: "★"}
            a2.markdown("**Ключевые слова (★★★ — почти во всех вакансиях)**")
            a2.dataframe(
                [{"": "✅" if r["found"] else "❌", "Слово": r["term"], "Важность": stars[r["importance"]],
                  "Найдено как": r["as"], "Тип": r["category"]} for r in cov["rows"]],
                hide_index=True, width="stretch", height=300)
            k1, k2 = st.columns(2)
            k1.markdown("**⛔ Стоп-критерии — отсеивают сразу**\n" + bullet(ats["knockout_criteria"]))
            k2.markdown("**👀 Что HR смотрит в первые 10 секунд**\n" + bullet(ats["hr_screening_notes"]))

            if st.button("✍️ Агент 6: переписать резюме и собрать PDF", type="primary"):
                with st.spinner("Переписываю резюме…"):
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
                # проверяем готовый PDF так же, как его прочтёт ATS: извлекаем текст обратно
                pdf_text = extract_text("cv.pdf", pdf_bytes)
                after = keyword_coverage(pdf_text, ats["keywords"])
                after_issues = format_checks(pdf_text, "cv.pdf", pdf_bytes)
                n_before = sum(l != "ok" for l, _ in issues)
                n_after = sum(l != "ok" for l, _ in after_issues)
                st.success("Новое резюме готово")
                r1, r2, r3 = st.columns(3)
                r1.metric("Ключевые слова", f"{after['score']}%", f"{after['score'] - rw['before']:+d} п.п.")
                r2.metric("Проблем формата", n_after, f"{n_after - n_before:+d}", delta_color="inverse")
                r3.metric("Язык", names[rlang])
                fname = "CV_" + "_".join((doc.get("full_name") or "candidato").split())
                d1, d2, d3 = st.columns(3)
                d1.download_button("⬇️ Скачать PDF", pdf_bytes, f"{fname}.pdf", "application/pdf", type="primary")
                d2.download_button("⬇️ Скачать DOCX", to_docx(doc, rlang), f"{fname}.docx",
                                   "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
                if d3.button("📌 Сделать основным резюме", help="Новые письма и анализы будут по этому тексту"):
                    db.save_resume(pdf_text, profile)
                    st.rerun()
                c1, c2 = st.columns(2)
                c1.markdown("**Что изменено**\n" + bullet(rw["changes_made"]))
                c2.markdown("**❓ Уточните — станет сильнее**\n" + bullet(rw["questions_for_user"]))
                if rw["keywords_not_used"]:
                    st.warning("Не добавлено, потому что этого нет в вашем опыте (выдумывать нельзя — "
                               "на собеседовании это вскроется): " + ", ".join(rw["keywords_not_used"]))
                with st.expander("Текст нового резюме так, как его видит робот"):
                    st.text(pdf_text)

# ---------- База фактов ----------
with tab_facts:
    st.subheader("База фактов — эталон для резюме")
    st.caption("Честный подробный рассказ о себе. Резюме, письма и оценка вакансий строятся по нему, а не по старому "
               "резюме. Пиши как есть, по-русски. Что не уверен — помечай «УТОЧНИТЬ»: агент это не возьмёт, а спросит.")
    facts_text = st.text_area("База фактов", facts.get_facts(), height=480, label_visibility="collapsed")
    fb1, fb2 = st.columns(2)
    if fb1.button("💾 Сохранить базу фактов", type="primary"):
        facts.set_facts(facts_text)
        st.success("Сохранено")
    if fb2.button("🔁 Агент 1: обновить профиль по базе фактов", disabled=not facts_text.strip(),
                  help="Профиль нужен для оценки вакансий (агент 2)"):
        with st.spinner("Анализирую…"):
            try:
                db.save_resume(resume_text, ResumeAnalyst(settings).run(facts.source_text(resume_text)))
                st.success("Профиль обновлён")
            except LLMError as e:
                st.error(str(e))

    st.markdown("### ❓ Вопросы агента")
    st.caption("Те же вопросы приходят в Telegram — отвечать можно и там (реплаем), и здесь.")
    oq = facts.open_questions()
    if not oq:
        st.info("Открытых вопросов нет.")
    for q in oq:
        st.markdown(f"**{q['q']}**" + (f"  \n_{q['context']}_" if q.get("context") else ""))
        q1, q2, q3 = st.columns([5, 1, 1])
        ans = q1.text_input("Ответ", key=f"qa_{q['id']}", label_visibility="collapsed")
        if q2.button("💾", key=f"qsv_{q['id']}", disabled=not ans.strip()):
            facts.answer(q["id"], ans)
            st.rerun()
        if q3.button("🤷", key=f"qsk_{q['id']}", help="Не знаю / пропустить"):
            facts.skip(q["id"])
            st.rerun()
    answered = [q for q in facts.get_qa() if q["status"] == "answered"]
    if answered:
        with st.expander(f"Отвеченные ({len(answered)}) — входят в базу фактов"):
            for q in answered:
                st.markdown(f"- **{q['q']}**  \n  {q['a']}")

    st.markdown("### 📄 Резюме по трекам (собирается из базы фактов полностью заново)")
    tcols = st.columns(len(facts.TRACKS))
    for col, (track, t) in zip(tcols, facts.TRACKS.items()):
        col.markdown(f"**{t['name']}**")
        if col.button(f"✍️ Собрать: {t['name']}", key=f"mb_{track}", disabled=not facts.get_facts()):
            with st.spinner(f"Пишу резюме «{t['name']}»…"):
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
            with col.popover("Что сделано"):
                st.markdown(bullet(m["changes_made"]))

# ---------- Поиск ----------
with tab_search:
    st.subheader("Поиск вакансий")
    suggestions = (profile or {}).get("search_keywords", [])
    if suggestions:
        st.caption("Подсказки из резюме: " + " · ".join(suggestions))
    c1, c2 = st.columns([2, 1])
    keywords = c1.text_input("Ключевые слова (на испанском)", settings["keywords"])
    location = c2.text_input("Город", settings["location"])
    chosen = st.multiselect("Источники", list(SOURCES), default=[s for s in settings["sources"] if s in SOURCES],
                            format_func=lambda n: SOURCE_TITLES[n])
    for name in chosen:
        ok, why = SOURCES[name](settings).is_configured()
        if not ok:
            st.warning(f"{SOURCE_TITLES[name]}: {why}")

    auto_analyze = st.checkbox("Сразу проанализировать новые вакансии (агент 2)", value=bool(resume_text))
    if st.button("🔎 Найти", type="primary", disabled=not keywords.strip() or not chosen):
        settings.update(keywords=keywords, location=location, sources=chosen)
        save_settings(settings)
        status = st.status("Ищу вакансии…", expanded=True)
        jobs, report = collect(settings, keywords, location, chosen,
                               on_progress=lambda n, m: status.write(f"**{SOURCE_TITLES[n]}**: {m}"))
        added = db.save_jobs(jobs)
        status.update(label=f"Готово: найдено {len(jobs)}, новых {added}", state="complete")
        st.session_state["_run_analysis"] = auto_analyze and added > 0

    with st.expander("➕ Добавить вакансию вручную (ссылка или текст)"):
        url = st.text_input("Ссылка на вакансию")
        text = st.text_area("…или текст вакансии", height=120)
        if st.button("Добавить"):
            try:
                job = job_from_url(url) if url.strip() and not text.strip() else job_from_text(text, url=url)
                n = db.save_jobs([job])
                st.success("Добавлено" if n else "Такая вакансия уже есть")
            except Exception as e:
                st.error(f"Не удалось загрузить: {e}")

    st.divider()
    pending = db.list_jobs(analyzed=False, statuses=["new", "interesting"])
    st.write(f"Непроанализированных вакансий: **{len(pending)}**")
    if (st.button("🤖 Агент 2: проанализировать", disabled=not pending or not resume_text)
            or (st.session_state.pop("_run_analysis", False) and pending)):
        bar = st.progress(0.0, "Анализ…")
        analyst = JobAnalyst(settings)
        for i, job in enumerate(pending, 1):
            bar.progress(i / len(pending), f"{i}/{len(pending)}: {job['title'][:60]}")
            try:
                a = analyst.run(job, profile, resume_text)
                db.update_job(job["id"], analysis=a, score=a["match_score"])
            except LLMError as e:
                st.error(f"Остановлено: {e}")
                break
            except Exception as e:
                st.warning(f"{job['title']}: {e}")
        bar.empty()
        st.success("Анализ завершён — смотрите вкладку «Вакансии»")
    if not resume_text:
        st.info("Чтобы анализировать вакансии, сначала загрузите резюме.")

# ---------- Вакансии ----------
with tab_jobs:
    f1, f2, f3 = st.columns([1, 2, 2])
    min_score = f1.slider("Мин. совпадение", 0, 100, 0, 5)
    f_sources = f2.multiselect("Источник", list(SOURCE_TITLES), format_func=lambda n: SOURCE_TITLES[n])
    f_status = f3.multiselect("Статус", list(db.STATUSES), default=["new", "interesting", "applied", "interview"],
                              format_func=lambda s: db.STATUSES[s])
    jobs = db.list_jobs(min_score=min_score or None, sources=f_sources or None, statuses=f_status or None)
    st.caption(f"Показано: {len(jobs)}")

    for job in jobs:
        a = job["analysis"]
        head = (f"{score_badge(job['score'])} · **{job['title']}** — {job['company'] or '?'} · "
                f"{job['location'] or ''} · _{SOURCE_TITLES.get(job['source'], job['source'])}_")
        with st.expander(head):
            top = st.columns([3, 2, 1])
            top[0].markdown(f"[Открыть вакансию ↗]({job['url']})" if job["url"] else "")
            if job["salary"]:
                top[0].markdown(f"💶 {job['salary']}")
            new_status = top[1].selectbox("Статус", list(db.STATUSES), index=list(db.STATUSES).index(job["status"]),
                                          format_func=lambda s: db.STATUSES[s], key=f"st_{job['id']}")
            if new_status != job["status"]:
                db.update_job(job["id"], status=new_status)
                st.rerun()
            if top[2].button("🗑", key=f"del_{job['id']}", help="Удалить из базы"):
                db.delete_job(job["id"])
                st.rerun()

            if a:
                st.markdown(f"**Вердикт:** {a['verdict']} · **Уровень:** {a['seniority']} · "
                            f"**Языки:** {a['language_requirements']}")
                st.markdown(a["summary"])
                c1, c2, c3 = st.columns(3)
                c1.markdown("**✅ Совпадает**\n" + bullet(a["matched_skills"]))
                c2.markdown("**❗ Не хватает**\n" + bullet(a["missing_skills"]))
                c3.markdown("**🚩 Настораживает**\n" + bullet(a["red_flags"]))
                st.info("💡 " + a["application_tips"])
            elif resume_text and st.button("🤖 Проанализировать", key=f"an_{job['id']}"):
                with st.spinner("Анализ…"):
                    try:
                        res = JobAnalyst(settings).run(job, profile, resume_text)
                        db.update_job(job["id"], analysis=res, score=res["match_score"])
                        st.rerun()
                    except LLMError as e:
                        st.error(str(e))

            with st.popover("Описание вакансии"):
                st.text(job["description"] or "—")

            st.markdown("---")
            b1, b2 = st.columns(2)
            wishes = b1.text_input("Пожелания к письму (необязательно)", key=f"wish_{job['id']}",
                                   placeholder="напр. короче, упомянуть переезд в Мадрид")
            if b1.button("✉️ Агент 3: сопроводительное письмо", key=f"cl_{job['id']}", disabled=not resume_text):
                with st.spinner("Пишу письмо…"):
                    try:
                        letter = CoverLetterWriter(settings).run(job, resume_text, profile, a, wishes)
                        db.update_job(job["id"], letter=letter)
                        st.rerun()
                    except LLMError as e:
                        st.error(str(e))
            if b2.button("🎯 Агент 4: адаптировать резюме", key=f"tl_{job['id']}", disabled=not resume_text):
                with st.spinner("Готовлю рекомендации…"):
                    try:
                        db.update_job(job["id"], tailoring=ResumeTailor(settings).run(job, resume_text, a))
                        st.rerun()
                    except LLMError as e:
                        st.error(str(e))

            if job["letter"]:
                letter = st.text_area("Сопроводительное письмо", job["letter"], height=300, key=f"lt_{job['id']}")
                l1, l2 = st.columns(2)
                if l1.button("💾 Сохранить правки", key=f"sv_{job['id']}"):
                    db.update_job(job["id"], letter=letter)
                    st.toast("Сохранено")
                l2.download_button("⬇️ Скачать .txt", letter, file_name=f"carta_{job['company'] or 'empresa'}.txt",
                                   key=f"dl_{job['id']}")
            if t := job["tailoring"]:
                st.markdown("**Новый раздел «Perfil» для резюме:**")
                st.code(t["summary_rewrite"], language=None)
                c1, c2 = st.columns(2)
                c1.markdown("**Правки**\n" + bullet(t["changes"]))
                c1.markdown("**Вынести на первый план**\n" + bullet(t["skills_to_highlight"]))
                c2.markdown("**Добавить ключевые слова**\n" + ", ".join(t["keywords_to_add"]))
                c2.markdown("**Честно о пробелах**\n" + bullet(t["honest_gaps"]))

# ---------- Автопилот ----------
with tab_auto:
    st.subheader("Автопилот")
    st.caption("Сам ищет вакансии, отсеивает неподходящие, оценивает и готовит под лучшие резюме (PDF) и письмо. "
               "Ничего не отправляет: готовые отклики ждут тебя ниже.")
    last = db.kv_get("last_run")
    running = pipeline.LOCK_PATH.exists()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Готово к отправке", len(db.list_jobs(statuses=["ready"])))
    if last:
        m2.metric("Последний запуск", last["started"][5:16].replace("T", " "))
        m3.metric("Новых / отсеяно", f"{last['new']} / {last['filtered']}")
        m4.metric("Оценено / подготовлено", f"{last['analyzed']} / {last['prepared']}")
        if last.get("error"):
            st.error(f"Ошибка последнего запуска: {last['error']}")
    nxt = pipeline.schedule_info()
    r1, r2, r3 = st.columns(3)
    if r1.button("▶️ Запустить сейчас", type="primary", disabled=running):
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen([str(ROOT / ".venv" / "Scripts" / "pythonw.exe"), "-m", "jobhunter.pipeline"],
                         cwd=ROOT, creationflags=flags)
        time.sleep(1)
        st.rerun()
    if running:
        r1.info("⏳ Работает… обнови страницу позже")
    r2.write(f"🗓 По расписанию: **{settings['auto_times']}**, следующий запуск {nxt}" if nxt
             else "🗓 Расписание не включено")
    if not nxt and r3.button("Включить расписание"):
        save_settings(settings)
        pipeline.install_schedule(settings["auto_times"])
        st.rerun()
    if nxt and r3.button("Выключить расписание"):
        pipeline.remove_schedule()
        st.rerun()

    with st.expander("Настройки автопилота", expanded=not settings["auto_queries"]):
        settings["auto_queries"] = st.text_area(
            "Поисковые запросы: по одному в строке, «ключевые слова | город» (город можно не писать)",
            settings["auto_queries"], height=120,
            placeholder=f"soporte técnico ruso | remoto\nhelpdesk | {settings['location']}\ntester QA junior")
        a1, a2, a3 = st.columns(3)
        settings["auto_min_score"] = a1.slider("Готовить отклик от совпадения, %", 40, 95, int(settings["auto_min_score"]), 5)
        settings["auto_max_prepare"] = a2.number_input("Макс. откликов за запуск", 0, 30, int(settings["auto_max_prepare"]),
                                                      help="Каждый отклик — 2 запроса к Claude (резюме + письмо)")
        settings["auto_max_analyze"] = a3.number_input("Макс. оценок за запуск (Ollama)", 0, 300,
                                                      int(settings["auto_max_analyze"]))
        settings["auto_skip_english_required"] = st.checkbox(
            "Отсеивать вакансии с обязательным высоким английским (B2+, C1, fluent)", settings["auto_skip_english_required"])
        settings["auto_skip_english_ads"] = st.checkbox("Отсеивать вакансии, написанные по-английски",
                                                        settings["auto_skip_english_ads"])
        settings["auto_remote_only"] = st.checkbox("Только удалённые", settings["auto_remote_only"])
        settings["auto_block_words"] = st.text_input("Отсеивать, если в тексте есть слова (через запятую)",
                                                     settings["auto_block_words"], placeholder="autónomo, comisiones, puerta fría")
        settings["auto_times"] = st.text_input("Время запуска по расписанию (через запятую)", settings["auto_times"])
        st.markdown("**🌍 Удалёнка по всему миру**")
        remote_names = [n for n in SOURCES if n in REMOTE_SOURCES]
        settings["remote_sources"] = st.multiselect(
            "Международные площадки", remote_names, default=[n for n in settings["remote_sources"] if n in remote_names],
            format_func=lambda n: SOURCE_TITLES[n])
        settings["remote_queries"] = st.text_area(
            "Запросы для них — по одному в строке, лучше по-английски (город не нужен)", settings["remote_queries"],
            height=140)
        settings["remote_regions"] = st.text_input(
            "Оставлять вакансии, доступные для регионов (через запятую)", settings["remote_regions"],
            help="Вакансия «только для USA» будет отсеяна; «Europe», «Worldwide», «Spain» — останутся")
        st.markdown("**🇷🇺 Русскоязычные источники**")
        ru_names = [n for n in SOURCES if n in RU_SOURCES]
        settings["ru_sources"] = st.multiselect(
            "Источники", ru_names, default=[n for n in settings["ru_sources"] if n in ru_names],
            format_func=lambda n: SOURCE_TITLES[n], key="ru_src")
        settings["ru_queries"] = st.text_area("Запросы для них — по одному в строке", settings["ru_queries"], height=140)
        settings["tg_channels"] = st.text_input("Telegram-каналы (через запятую, без @)", settings["tg_channels"])
        settings["skip_ru_by"] = st.checkbox("Отсеивать компании из России и Беларуси", settings["skip_ru_by"],
                                             help="По рублям, ТК РФ, городам, известным компаниям; «кроме РФ» не считается")
        st.markdown("**🏠 Без переезда**")
        settings["no_relocation"] = st.checkbox("Только удалённо или рядом с домом", settings["no_relocation"])
        settings["home_area"] = st.text_input("Рядом с домом — это (через запятую)", settings["home_area"])
        if st.button("💾 Сохранить настройки автопилота"):
            save_settings(settings)
            if nxt:  # время могло поменяться
                pipeline.install_schedule(settings["auto_times"])
            st.success("Сохранено")

    st.markdown("### 📦 Готово к отправке")
    ready = db.list_jobs(statuses=["ready"])
    if not ready:
        st.info("Пока пусто. Запусти автопилот или дождись запуска по расписанию.")
    for job in ready:
        with st.expander(f"{score_badge(job['score'])} · **{job['title']}** — {job['company'] or '?'} · "
                         f"_{SOURCE_TITLES.get(job['source'], job['source'])}_"):
            if job["url"]:
                st.markdown(f"[Открыть вакансию и откликнуться ↗]({job['url']})")
            if a := job["analysis"]:
                st.markdown(a["summary"])
                st.caption("💡 " + a["application_tips"])
            cv = job["cv"] or {}
            folder = Path(job["app_dir"]) if job["app_dir"] else None
            pdf = next(folder.glob("*.pdf"), None) if folder and folder.exists() else None
            c1, c2, c3 = st.columns(3)
            if pdf:
                c1.download_button("⬇️ Резюме PDF", pdf.read_bytes(), pdf.name, "application/pdf", key=f"rp_{job['id']}")
            if folder and c2.button("📂 Открыть папку", key=f"of_{job['id']}"):
                os.startfile(folder)
            if cv.get("resume"):
                c3.download_button("⬇️ Резюме DOCX", to_docx(cv["resume"], cv["lang"]),
                                   (pdf.stem if pdf else "CV") + ".docx", key=f"rd_{job['id']}")
            letter = st.text_area("Сопроводительное письмо", job["letter"] or "", height=260, key=f"rl_{job['id']}")
            if letter != (job["letter"] or "") and st.button("💾 Сохранить письмо", key=f"rs_{job['id']}"):
                db.update_job(job["id"], letter=letter)
                if folder and folder.exists():
                    (folder / "carta.txt").write_text(letter, encoding="utf-8")
                st.toast("Сохранено")
            if cv.get("questions_for_user"):
                st.markdown("**❓ Агент спрашивает (ответы впиши в Настройки → «Что ещё знать о тебе», "
                            "резюме станет сильнее):**\n" + bullet(cv["questions_for_user"]))
            if cv.get("changes_made"):
                with st.popover("Что изменено в резюме"):
                    st.markdown(bullet(cv["changes_made"]))
            b1, b2 = st.columns(2)
            if b1.button("✅ Я отправил отклик", key=f"ra_{job['id']}", type="primary"):
                db.update_job(job["id"], status="applied", applied_at=datetime.now().isoformat(timespec="seconds"))
                st.rerun()
            if b2.button("🙈 Не подходит, убрать", key=f"rh_{job['id']}"):
                db.update_job(job["id"], status="hidden")
                st.rerun()

    filtered = [j for j in db.list_jobs(statuses=["hidden"]) if j["filtered"]]
    if filtered:
        with st.expander(f"🚫 Отсеяно фильтрами: {len(filtered)} (проверь, нет ли ошибок)"):
            for job in filtered[:100]:
                f1, f2 = st.columns([5, 1])
                f1.markdown(f"[{job['title']}]({job['url']}) — {job['company'] or '?'} · _{job['filtered']}_")
                if f2.button("Вернуть", key=f"fr_{job['id']}"):
                    db.update_job(job["id"], status="new", filtered=None)
                    st.rerun()

    if pipeline.LOG_PATH.exists():
        with st.expander("Журнал автопилота"):
            lines = pipeline.LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
            st.code("\n".join(lines[-60:]), language=None)

# ---------- Настройки ----------
with tab_settings:
    st.subheader("Ключи API")
    st.caption("Хранятся только локально в файле D:\\JobHunter\\.env")
    for key, label in SECRET_KEYS.items():
        cur = get_secret(key)
        val = st.text_input(f"{label}", type="password", placeholder="задан ✓" if cur else "не задан",
                            key=f"sec_{key}")
        if val:
            save_secret(key, val.strip())
            st.success(f"{key} сохранён")

    st.subheader("Модели")
    c1, c2 = st.columns(2)
    settings["ollama_model"] = c1.text_input("Модель Ollama", settings["ollama_model"])
    settings["claude_model"] = c2.text_input("Модель Claude", settings["claude_model"])
    roles = {"backend_resume_analyst": "Агент 1 — анализ резюме", "backend_job_analyst": "Агент 2 — анализ вакансий",
             "backend_cover_letter": "Агент 3 — сопроводительные письма", "backend_resume_tailor": "Агент 4 — адаптация резюме",
             "backend_ats_auditor": "Агент 5 — ATS-аудит", "backend_resume_writer": "Агент 6 — новое резюме"}
    cols = st.columns(3) * 2
    for col, (k, label) in zip(cols, roles.items()):
        settings[k] = col.radio(label, ["ollama", "claude"], index=["ollama", "claude"].index(settings[k]), key=k)

    st.subheader("Поиск")
    c1, c2, c3 = st.columns(3)
    settings["max_per_source"] = c1.number_input("Макс. вакансий с источника", 1, 100, int(settings["max_per_source"]))
    settings["linkedin_days"] = c2.number_input("LinkedIn: за последние N дней (0 = все)", 0, 30, int(settings["linkedin_days"]))
    settings["infojobs_province"] = c3.text_input("InfoJobs: провинция (madrid, barcelona…)", settings["infojobs_province"])
    settings["exclude_words"] = st.text_input("Исключить вакансии со словами в названии (через запятую)",
                                              settings["exclude_words"], placeholder="senior, comercial, prácticas")

    st.subheader("Письма")
    settings["letter_language"] = st.radio("Язык писем", ["es", "en", "ru"], horizontal=True,
                                           index=["es", "en", "ru"].index(settings["letter_language"]))
    settings["applicant_notes"] = st.text_area(
        "Что ещё агенту стоит знать о вас (не из резюме)", settings["applicant_notes"],
        placeholder="Есть NIE и разрешение на работу. Могу выйти через 2 недели. Ищу гибрид в Мадриде.")
    if st.button("💾 Сохранить настройки", type="primary"):
        save_settings(settings)
        st.success("Сохранено")
