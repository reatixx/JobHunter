"""JobHunter Telegram bot: notifications about prepared applications with "Sent / Skip / Edit letter" buttons.

Button and command listener:  .venv\\Scripts\\pythonw.exe -m jobhunter.telegram_bot
Post-run notifications are sent by the pipeline (notify_after_run) — no listener needed for those.

The bot binds to the first chat that sends /start; messages from other chats are ignored.
No dependencies: Bot API via requests.
"""
from __future__ import annotations

import html
import json
import logging
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

from . import db, facts
from .config import DATA_DIR, ROOT, get_secret, load_settings, save_settings

log = logging.getLogger("jobhunter.telegram")
LOG_PATH = DATA_DIR / "telegram.log"
LOCK_PORT = 47831  # port already taken = bot already running (simple dependency-free mutex)


class TelegramError(Exception):
    pass


def configured() -> bool:
    return bool(get_secret("TELEGRAM_BOT_TOKEN") and load_settings().get("telegram_chat_id"))


def call(method: str, files: dict | None = None, http_timeout: int = 30, **params):
    token = get_secret("TELEGRAM_BOT_TOKEN")
    if not token:
        raise TelegramError("не задан TELEGRAM_BOT_TOKEN")
    data = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
            for k, v in params.items() if v is not None}
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=data, files=files, timeout=http_timeout)
    body = r.json()
    if not body.get("ok"):
        raise TelegramError(body.get("description", r.text[:200]))
    return body["result"]


def send(text: str, chat_id: int | None = None, markup: dict | None = None, **kw):
    chat_id = chat_id or load_settings().get("telegram_chat_id")
    return call("sendMessage", chat_id=chat_id, text=text[:4096], parse_mode="HTML",
                disable_web_page_preview=True, reply_markup=markup, **kw)


def send_file(path: Path, caption: str = "", chat_id: int | None = None):
    chat_id = chat_id or load_settings().get("telegram_chat_id")
    with path.open("rb") as f:
        return call("sendDocument", files={"document": (path.name, f)}, http_timeout=120,
                    chat_id=chat_id, caption=caption[:1024], parse_mode="HTML")


e = html.escape


# --- application card -------------------------------------------------------------
def buttons(rid: int) -> dict:
    return {"inline_keyboard": [
        [{"text": "✅ Отправил", "callback_data": f"ok:{rid}"},
         {"text": "🙈 Пропустить", "callback_data": f"no:{rid}"}],
        [{"text": "✏️ Поправить письмо", "callback_data": f"ed:{rid}"}],
    ]}


def card_text(job: dict) -> str:
    a = job.get("analysis") or {}
    meta = " · ".join(x for x in [job.get("company"), job.get("location"), job.get("source")] if x)
    lines = [f"<b>{e(job['title'])}</b>", e(meta), f"Совпадение: <b>{job.get('score')}%</b>"]
    if job.get("salary"):
        lines.append(f"💶 {e(job['salary'])}")
    if a.get("summary"):
        lines += ["", e(a["summary"])]
    if a.get("missing_skills"):
        lines.append("❗ Не хватает: " + e(", ".join(a["missing_skills"][:5])))
    if a.get("application_tips"):
        lines.append("💡 " + e(a["application_tips"]))
    if job.get("url"):
        lines += ["", f'<a href="{e(job["url"])}">👉 Открыть вакансию и откликнуться</a>']
    return "\n".join(lines)


def send_application(job: dict, chat_id: int | None = None) -> None:
    send(card_text(job), chat_id)
    folder = Path(job["app_dir"]) if job.get("app_dir") else None
    pdf = next(folder.glob("*.pdf"), None) if folder and folder.exists() else None
    if pdf:
        send_file(pdf, "Резюме под эту вакансию", chat_id)
    send_letter(job, chat_id)


def send_letter(job: dict, chat_id: int | None = None) -> None:
    letter = job.get("letter") or "(письма нет)"
    send(f"✉️ <b>Письмо</b> (нажми, чтобы скопировать):\n<code>{e(letter)}</code>", chat_id, buttons(job["rid"]))


# --- fact-base follow-up questions -----------------------------------------------
def send_questions(only_new: bool = True) -> int:
    qs = [q for q in facts.open_questions() if not (only_new and q.get("tg_msg"))]
    if qs:
        send(f"❓ <b>У агента {len(qs)} вопрос(ов) для резюме.</b>\nОтвечай <b>реплаем</b> на каждый "
             "(свайп влево по сообщению). Ответы попадут в базу фактов, и резюме станет точнее.")
    for q in qs:
        ctx = f"\n<i>({e(q['context'])})</i>" if q.get("context") else ""
        m = send(f"❓ {e(q['q'])}{ctx}",
                 markup={"inline_keyboard": [[{"text": "🤷 Не знаю / пропустить", "callback_data": f"qs:{q['id']}"}]]})
        facts.update_question(q["id"], tg_msg=m["message_id"])
    return len(qs)


def rebuild_masters_async(chat_id: int | None = None) -> None:
    """Rebuilds the master resumes for both tracks in the background (~1-2 min of Claude calls) and sends the PDFs."""
    def work():
        settings = load_settings()
        for track, t in facts.TRACKS.items():
            try:
                rw = facts.build_master(settings, track)
                send_file(Path(rw["pdf"]), f"📄 Резюме: <b>{t['name']}</b>", chat_id)
            except Exception as ex:
                send(f"Не получилось собрать резюме «{t['name']}»: {e(str(ex))}", chat_id)
        send_questions()
    send("🔄 Пересобираю резюме по базе фактов (оба трека), это пара минут…", chat_id)
    threading.Thread(target=work, daemon=True).start()


def notify_after_run(summary: dict) -> None:
    """Called by the autopilot at the end of a run. Sends new prepared applications and new questions; silent if there's nothing."""
    if not configured():
        return
    fresh = [j for j in db.list_jobs(statuses=["ready"]) if not j["notified_at"]]
    if summary.get("error"):
        send(f"⚠️ Автопилот: ошибка\n<code>{e(summary['error'][:1500])}</code>")
    if not fresh:
        send_questions()
        return
    send(f"🤖 <b>Автопилот</b>: найдено новых {summary.get('new', 0)}, отсеяно {summary.get('filtered', 0)}, "
         f"оценено {summary.get('analyzed', 0)}.\n📦 Готово к отправке: <b>{len(fresh)}</b>")
    for job in fresh:
        try:
            send_application(job)
            db.update_job(job["id"], notified_at=datetime.now().isoformat(timespec="seconds"))
        except Exception as ex:
            log.warning("Не удалось отправить в Telegram (%s): %s", job["title"], ex)
    send_questions()


# --- command and button handling ------------------------------------------------------
HELP = ("Я присылаю отклики, которые подготовил автопилот JobHunter.\n\n"
        "/ready — показать все готовые к отправке\n"
        "/questions — открытые вопросы для резюме\n"
        "/cv — пересобрать резюме по базе фактов (поддержка и фронтенд)\n"
        "/status — последний запуск автопилота\n"
        "/run — запустить автопилот сейчас\n\n"
        "На вопросы агента отвечай реплаем — ответ попадёт в базу фактов.\n"
        "Любой обычный текст (не команда) — сообщение для Claude, он прочитает его в своей памяти.\n"
        "Под письмом кнопки: ✅ Отправил (отмечу отклик), 🙈 Пропустить, ✏️ Поправить письмо.")


def _run_pipeline() -> None:
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen([str(ROOT / ".venv" / "Scripts" / "pythonw.exe"), "-m", "jobhunter.pipeline"],
                     cwd=ROOT, creationflags=flags)


def handle_message(msg: dict) -> None:
    settings = load_settings()
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    owner = settings.get("telegram_chat_id")

    if not owner:
        if text.startswith("/start"):
            settings["telegram_chat_id"] = chat_id
            save_settings(settings)
            send("Привет! Бот привязан к этому чату. " + HELP, chat_id)
            send_questions()
        return
    if chat_id != owner:
        return  # ignore other chats

    reply_to = (msg.get("reply_to_message") or {}).get("message_id")
    if reply_to and text and (q := facts.by_tg_message(reply_to)):
        facts.answer(q["id"], text)
        left = len(facts.open_questions())
        send(f"✅ Записал в базу фактов. Осталось вопросов: {left}",
             markup={"inline_keyboard": [[{"text": "🔄 Пересобрать резюме", "callback_data": "rb:0"}]]}
             if left == 0 else None)
        return

    pending = db.kv_get("tg_pending_edit")
    if pending and text and not text.startswith("/"):
        db.kv_set("tg_pending_edit", None)
        rewrite_letter(int(pending), text)
        return

    cmd = text.split()[0].split("@")[0].lower() if text else ""
    if cmd in ("/start", "/help"):
        send(HELP)
    elif cmd == "/status":
        last = db.kv_get("last_run")
        ready = len(db.list_jobs(statuses=["ready"]))
        if not last:
            send(f"Автопилот ещё не запускался. Готово к отправке: {ready}")
        else:
            send(f"Последний запуск: {last['started'].replace('T', ' ')}\n"
                 f"Новых {last['new']}, отсеяно {last['filtered']}, оценено {last['analyzed']}, "
                 f"подготовлено {last['prepared']}.\n📦 Готово к отправке: <b>{ready}</b>"
                 + (f"\n⚠️ {e(last['error'])}" if last.get("error") else ""))
    elif cmd == "/ready":
        ready = db.list_jobs(statuses=["ready"])
        if not ready:
            send("Готовых откликов нет.")
        for job in ready:
            send_application(job)
    elif cmd == "/questions":
        if not send_questions(only_new=False):
            send("Открытых вопросов нет 👍")
    elif cmd == "/cv":
        if not facts.get_facts():
            send("База фактов пустая — заполни её во вкладке «🧾 База фактов».")
        else:
            rebuild_masters_async()
    elif cmd == "/run":
        from .pipeline import LOCK_PATH
        if LOCK_PATH.exists():
            send("Автопилот уже работает, пришлю результат, когда закончит.")
        else:
            _run_pipeline()
            send("Запустил автопилот. Пришлю отклики, когда будут готовы (обычно 5–20 минут).")
    elif text and not text.startswith("/"):
        if to_claude(text, reply_to):
            send("📝 Передал Claude — прочитает, когда проснётся.")
        else:
            send("Не получилось сохранить сообщение для Claude. " + HELP)
    else:
        send("Не понял. " + HELP)


CLAUDE_MEMORY = Path(load_settings().get("claude_memory_dir") or r"D:\ClaudeMemory")  # optional Claude long-term memory


def to_claude(text: str, reply_to: int | None) -> bool:
    """Free text is a message from the user to Claude: store it in Claude's long-term memory."""
    try:
        if str(CLAUDE_MEMORY) not in sys.path:
            sys.path.insert(0, str(CLAUDE_MEMORY))
        import memory_core as mem
        import sleep as mem_sleep
        con = mem_sleep._con()
        row = con.execute("SELECT v FROM kv WHERE k='claude_tg_ids'").fetchone()
        con.close()
        is_reply = bool(reply_to and row and reply_to in json.loads(row["v"]))
        when = datetime.now().strftime("%Y-%m-%d %H:%M")
        mem.save(f"{load_settings().get('user_name') or 'Пользователь'} написал Claude в Telegram ({when}{', ответом на моё сообщение' if is_reply else ''}): "
                 f"«{text}»", kind="fact", project="telegram", importance=3)
        return True
    except Exception:
        log.exception("to_claude failed")
        return False


def rewrite_letter(rid: int, wishes: str) -> None:
    from .agents import CoverLetterWriter
    job = db.get_job(rid)
    if not job:
        send("Вакансия не найдена.")
        return
    send("✍️ Переписываю письмо…")
    resume_text, profile = db.load_resume()
    try:
        letter = CoverLetterWriter(load_settings()).run(job, facts.source_text(resume_text), profile,
                                                        job["analysis"], wishes)
    except Exception as ex:
        send(f"Не получилось: {e(str(ex))}")
        return
    db.update_job(job["id"], letter=letter)
    if job.get("app_dir") and Path(job["app_dir"]).exists():
        (Path(job["app_dir"]) / "carta.txt").write_text(letter, encoding="utf-8")
    job["letter"] = letter
    send_letter(job)


def handle_callback(cb: dict) -> None:
    owner = load_settings().get("telegram_chat_id")
    msg = cb.get("message") or {}
    if msg.get("chat", {}).get("id") != owner:
        call("answerCallbackQuery", callback_query_id=cb["id"])
        return
    action, _, rid = (cb.get("data") or "").partition(":")
    if action == "noop":
        call("answerCallbackQuery", callback_query_id=cb["id"])
        return
    if action == "qs":
        facts.skip(rid)
        call("answerCallbackQuery", callback_query_id=cb["id"], text="Пропущено")
        call("editMessageReplyMarkup", chat_id=owner, message_id=msg["message_id"],
             reply_markup={"inline_keyboard": [[{"text": "🤷 Пропущено", "callback_data": "noop"}]]})
        return
    if action == "rb":
        call("answerCallbackQuery", callback_query_id=cb["id"])
        rebuild_masters_async()
        return
    job = db.get_job(int(rid)) if rid.isdigit() else None
    if not job:
        call("answerCallbackQuery", callback_query_id=cb["id"], text="Вакансия не найдена")
        return
    note = ""
    if action == "ok":
        db.update_job(job["id"], status="applied", applied_at=datetime.now().isoformat(timespec="seconds"))
        note = "✅ Отмечено: отклик отправлен"
    elif action == "no":
        db.update_job(job["id"], status="hidden")
        note = "🙈 Пропущено"
    elif action == "ed":
        db.kv_set("tg_pending_edit", job["rid"])
        call("answerCallbackQuery", callback_query_id=cb["id"])
        send(f"Что поправить в письме для «{e(job['title'])}»? Напиши одним сообщением "
             "(например: короче, упомянуть переезд, более формально).")
        return
    call("answerCallbackQuery", callback_query_id=cb["id"], text=note)
    # replace the buttons under the letter with the outcome, so it's clear what was done
    call("editMessageReplyMarkup", chat_id=owner, message_id=msg["message_id"],
         reply_markup={"inline_keyboard": [[{"text": note, "callback_data": "noop"}]]})


def poll_forever() -> None:
    offset = None
    call("setMyCommands", commands=[
        {"command": "ready", "description": "Готовые к отправке отклики"},
        {"command": "questions", "description": "Вопросы для резюме"},
        {"command": "cv", "description": "Пересобрать резюме по базе фактов"},
        {"command": "status", "description": "Последний запуск автопилота"},
        {"command": "run", "description": "Запустить автопилот сейчас"},
        {"command": "help", "description": "Что умеет бот"},
    ])
    log.info("Бот запущен")
    while True:
        try:
            updates = call("getUpdates", http_timeout=65, offset=offset, timeout=50,
                           allowed_updates=["message", "callback_query"])
        except (requests.RequestException, TelegramError) as ex:
            log.warning("getUpdates: %s", ex)
            time.sleep(10)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            try:
                if "callback_query" in u:
                    handle_callback(u["callback_query"])
                elif "message" in u:
                    handle_message(u["message"])
            except Exception:
                log.exception("Ошибка обработки апдейта")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8")]
                        + ([logging.StreamHandler(sys.stdout)] if sys.stdout and sys.stdout.isatty() else []))
    for noisy in ("urllib3", "httpx", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        log.info("Бот уже запущен — выхожу")
        return
    poll_forever()


if __name__ == "__main__":
    main()
