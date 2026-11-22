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
from .i18n import tr

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
        raise TelegramError(tr("TELEGRAM_BOT_TOKEN is not set"))
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
        [{"text": tr("✅ Sent"), "callback_data": f"ok:{rid}"},
         {"text": tr("🙈 Skip"), "callback_data": f"no:{rid}"}],
        [{"text": tr("✏️ Edit letter"), "callback_data": f"ed:{rid}"}],
    ]}


def card_text(job: dict) -> str:
    a = job.get("analysis") or {}
    meta = " · ".join(x for x in [job.get("company"), job.get("location"), job.get("source")] if x)
    lines = [f"<b>{e(job['title'])}</b>", e(meta), tr("Match: <b>{0}%</b>", job.get('score'))]
    if job.get("salary"):
        lines.append(f"💶 {e(job['salary'])}")
    if a.get("summary"):
        lines += ["", e(a["summary"])]
    if a.get("missing_skills"):
        lines.append(tr("❗ Missing: ") + e(", ".join(a["missing_skills"][:5])))
    if a.get("application_tips"):
        lines.append("💡 " + e(a["application_tips"]))
    if job.get("url"):
        lines += ["", tr("<a href=\"{0}\">👉 Open the job and apply</a>", e(job["url"]))]
    return "\n".join(lines)


def send_application(job: dict, chat_id: int | None = None) -> None:
    send(card_text(job), chat_id)
    folder = Path(job["app_dir"]) if job.get("app_dir") else None
    pdf = next(folder.glob("*.pdf"), None) if folder and folder.exists() else None
    if pdf:
        send_file(pdf, tr("Resume for this job"), chat_id)
    send_letter(job, chat_id)


def send_letter(job: dict, chat_id: int | None = None) -> None:
    letter = job.get("letter") or tr("(no letter)")
    send(tr("✉️ <b>Letter</b> (tap to copy):\n<code>{0}</code>", e(letter)), chat_id, buttons(job["rid"]))


# --- fact-base follow-up questions -----------------------------------------------
def send_questions(only_new: bool = True) -> int:
    qs = [q for q in facts.open_questions() if not (only_new and q.get("tg_msg"))]
    if qs:
        send(tr("❓ <b>The agent has {0} question(s) for the resume.</b>\nAnswer each one with a <b>reply</b> "
                "(swipe left on the message). The answers go into the fact base and make the resume more "
                "accurate.", len(qs)))
    for q in qs:
        ctx = f"\n<i>({e(q['context'])})</i>" if q.get("context") else ""
        m = send(f"❓ {e(q['q'])}{ctx}",
                 markup={"inline_keyboard": [[{"text": tr("🤷 Don't know / skip"), "callback_data": f"qs:{q['id']}"}]]})
        facts.update_question(q["id"], tg_msg=m["message_id"])
    return len(qs)


def rebuild_masters_async(chat_id: int | None = None) -> None:
    """Rebuilds the master resumes for both tracks in the background (~1-2 min of Claude calls) and sends the PDFs."""
    def work():
        settings = load_settings()
        for track, t in facts.TRACKS.items():
            try:
                rw = facts.build_master(settings, track)
                send_file(Path(rw["pdf"]), tr("📄 Resume: <b>{0}</b>", t['name']), chat_id)
            except Exception as ex:
                send(tr("Couldn't build the resume «{0}»: {1}", t['name'], e(str(ex))), chat_id)
        send_questions()
    send(tr("🔄 Rebuilding the resumes from the fact base (both tracks), this takes a couple of minutes…"), chat_id)
    threading.Thread(target=work, daemon=True).start()


def notify_after_run(summary: dict) -> None:
    """Called by the autopilot at the end of a run. Sends new prepared applications and new questions; silent if there's nothing."""
    if not configured():
        return
    fresh = [j for j in db.list_jobs(statuses=["ready"]) if not j["notified_at"]]
    if summary.get("error"):
        send(tr("⚠️ Autopilot: error\n<code>{0}</code>", e(summary['error'][:1500])))
    if not fresh:
        send_questions()
        return
    send(tr("🤖 <b>Autopilot</b>: {0} new found, {1} filtered out, {2} scored.\n📦 Ready to send: <b>{3}</b>", summary.get('new', 0), summary.get('filtered', 0), summary.get('analyzed', 0), len(fresh)))
    for job in fresh:
        try:
            send_application(job)
            db.update_job(job["id"], notified_at=datetime.now().isoformat(timespec="seconds"))
        except Exception as ex:
            log.warning("Failed to send to Telegram (%s): %s", job["title"], ex)
    send_questions()


# --- command and button handling ------------------------------------------------------
HELP = (tr("I send the applications prepared by the JobHunter autopilot.\n\n/ready — show everything ready to "
           "send\n/questions — open questions for the resume\n/cv — rebuild the resumes from the fact base "
           "(support and frontend)\n/status — last autopilot run\n/run — run the autopilot now\n\nAnswer the "
           "agent's questions with a reply — the answer goes into the fact base.\nAny plain text (not a "
           "command) is a message for Claude; it will read it in its memory.\nUnder each letter there are "
           "buttons: ✅ Sent (marks the application), 🙈 Skip, ✏️ Edit letter."))


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
            send(tr("Hi! The bot is now bound to this chat. ") + HELP, chat_id)
            send_questions()
        return
    if chat_id != owner:
        return  # ignore other chats

    reply_to = (msg.get("reply_to_message") or {}).get("message_id")
    if reply_to and text and (q := facts.by_tg_message(reply_to)):
        facts.answer(q["id"], text)
        left = len(facts.open_questions())
        send(tr("✅ Saved to the fact base. Questions left: {0}", left),
             markup={"inline_keyboard": [[{"text": tr("🔄 Rebuild resume"), "callback_data": "rb:0"}]]}
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
            send(tr("The autopilot hasn't run yet. Ready to send: {0}", ready))
        else:
            send(tr("Last run: {0}\n{1} new, {2} filtered out, {3} scored, {4} prepared.\n📦 Ready to send: "
                    "<b>{5}</b>", last['started'].replace('T', ' '), last['new'], last['filtered'], last['analyzed'], last['prepared'], ready)
                 + (f"\n⚠️ {e(last['error'])}" if last.get("error") else ""))
    elif cmd == "/ready":
        ready = db.list_jobs(statuses=["ready"])
        if not ready:
            send(tr("No applications ready."))
        for job in ready:
            send_application(job)
    elif cmd == "/questions":
        if not send_questions(only_new=False):
            send(tr("No open questions 👍"))
    elif cmd == "/cv":
        if not facts.get_facts():
            send(tr("The fact base is empty — fill it in on the «🧾 Fact base» tab."))
        else:
            rebuild_masters_async()
    elif cmd == "/run":
        from .pipeline import LOCK_PATH
        if LOCK_PATH.exists():
            send(tr("The autopilot is already running; I'll send the results when it's done."))
        else:
            _run_pipeline()
            send(tr("Autopilot started. I'll send the applications when they're ready (usually 5–20 minutes)."))
    elif text and not text.startswith("/"):
        if to_claude(text, reply_to):
            send(tr("📝 Passed to Claude — it'll read it when it wakes up."))
        else:
            send(tr("Couldn't save the message for Claude. ") + HELP)
    else:
        send(tr("Didn't get that. ") + HELP)


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
        mem.save(tr("{0} wrote to Claude in Telegram ({1}{2}): «{3}»", load_settings().get('user_name') or tr('User'), when, tr(', replying to my message') if is_reply else '', text), kind="fact", project="telegram", importance=3)
        return True
    except Exception:
        log.exception("to_claude failed")
        return False


def rewrite_letter(rid: int, wishes: str) -> None:
    from .agents import CoverLetterWriter
    job = db.get_job(rid)
    if not job:
        send(tr("Job not found."))
        return
    send(tr("✍️ Rewriting the letter…"))
    resume_text, profile = db.load_resume()
    try:
        letter = CoverLetterWriter(load_settings()).run(job, facts.source_text(resume_text), profile,
                                                        job["analysis"], wishes)
    except Exception as ex:
        send(tr("Didn't work: {0}", e(str(ex))))
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
        call("answerCallbackQuery", callback_query_id=cb["id"], text=tr("Skipped"))
        call("editMessageReplyMarkup", chat_id=owner, message_id=msg["message_id"],
             reply_markup={"inline_keyboard": [[{"text": tr("🤷 Skipped"), "callback_data": "noop"}]]})
        return
    if action == "rb":
        call("answerCallbackQuery", callback_query_id=cb["id"])
        rebuild_masters_async()
        return
    job = db.get_job(int(rid)) if rid.isdigit() else None
    if not job:
        call("answerCallbackQuery", callback_query_id=cb["id"], text=tr("Job not found"))
        return
    note = ""
    if action == "ok":
        db.update_job(job["id"], status="applied", applied_at=datetime.now().isoformat(timespec="seconds"))
        note = tr("✅ Marked: application sent")
    elif action == "no":
        db.update_job(job["id"], status="hidden")
        note = tr("🙈 Skipped")
    elif action == "ed":
        db.kv_set("tg_pending_edit", job["rid"])
        call("answerCallbackQuery", callback_query_id=cb["id"])
        send(tr("What should I change in the letter for «{0}»? Write it in one message (e.g. shorter, more "
                "formal, mention availability).", e(job['title'])))
        return
    call("answerCallbackQuery", callback_query_id=cb["id"], text=note)
    # replace the buttons under the letter with the outcome, so it's clear what was done
    call("editMessageReplyMarkup", chat_id=owner, message_id=msg["message_id"],
         reply_markup={"inline_keyboard": [[{"text": note, "callback_data": "noop"}]]})


def poll_forever() -> None:
    offset = None
    call("setMyCommands", commands=[
        {"command": "ready", "description": tr("Applications ready to send")},
        {"command": "questions", "description": tr("Questions for the resume")},
        {"command": "cv", "description": tr("Rebuild the resumes from the fact base")},
        {"command": "status", "description": tr("Last autopilot run")},
        {"command": "run", "description": tr("Run the autopilot now")},
        {"command": "help", "description": tr("What the bot can do")},
    ])
    log.info("Bot started")
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
                log.exception("Error handling update")


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
        log.info("Bot is already running — exiting")
        return
    poll_forever()


if __name__ == "__main__":
    main()
