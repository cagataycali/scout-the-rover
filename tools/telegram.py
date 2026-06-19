"""Telegram tool — rich actions + per-chat conversation memory.

Modeled after devduck/tools/telegram.py + DevDuck's get_last_messages() pattern.

Features:
- 14 actions: send_message, send_photo, send_document, send_poll, edit_message,
  delete_message, get_me, get_chat, get_updates, get_history, ping, ...
- Per-chat conversation history persisted to SQLite (.memory/mem.db, table: tg_history)
- Long-poll listener that stores both directions and injects last N messages
  into the agent's system prompt for context-aware multi-turn conversations.

Env:
    TELEGRAM_BOT_TOKEN          required
    TELEGRAM_DEFAULT_CHAT_ID    optional default route
    TELEGRAM_ALLOWED_USERS      comma-separated username/id allowlist
    TELEGRAM_HISTORY_LIMIT      messages to inject per chat (default 20)
"""
from __future__ import annotations
import os
import time
import json
import sqlite3
import threading
from pathlib import Path
from typing import Optional, Callable, List, Dict
import requests
from strands import tool

# ── config ─────────────────────────────────────────────────────────────
TOKEN = os.getenv("SCOUT_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN", "")
DEFAULT_CHAT_ID = os.getenv("SCOUT_TELEGRAM_DEFAULT_CHAT_ID") or os.getenv("TELEGRAM_DEFAULT_CHAT_ID", "")
ALLOWED = {u.strip() for u in (os.getenv("SCOUT_TELEGRAM_ALLOWED_USERS") or os.getenv("TELEGRAM_ALLOWED_USERS", "")).split(",") if u.strip()}
HISTORY_LIMIT = int(os.getenv("SCOUT_TELEGRAM_HISTORY_LIMIT") or os.getenv("TELEGRAM_HISTORY_LIMIT", "20"))
API = lambda: f"https://api.telegram.org/bot{TOKEN}"

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / ".memory" / "mem.db"
OFFSET_FILE = ROOT / ".telegram_offset"
ROOT.joinpath(".memory").mkdir(parents=True, exist_ok=True)


# ── conversation history (per-chat) ────────────────────────────────────
def _conn():
    c = sqlite3.connect(DB)
    c.execute("""
        CREATE TABLE IF NOT EXISTS tg_history(
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id   TEXT NOT NULL,
            role      TEXT NOT NULL,         -- 'user' | 'assistant'
            username  TEXT,
            text      TEXT NOT NULL,
            msg_id    INTEGER,
            ts        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_tg_chat ON tg_history(chat_id, ts DESC)")
    c.commit()
    return c


def record_message(chat_id: str | int, role: str, text: str,
                   username: Optional[str] = None, msg_id: Optional[int] = None):
    """Persist a single message to chat history."""
    if not text:
        return
    c = _conn()
    c.execute(
        "INSERT INTO tg_history(chat_id, role, username, text, msg_id) VALUES(?,?,?,?,?)",
        (str(chat_id), role, username, text[:8000], msg_id),
    )
    c.commit()
    c.close()


def get_history(chat_id: str | int, limit: int = HISTORY_LIMIT) -> List[Dict]:
    """Get last N messages for a chat, oldest-first."""
    c = _conn()
    rows = c.execute(
        "SELECT role, username, text, ts FROM tg_history "
        "WHERE chat_id=? ORDER BY id DESC LIMIT ?",
        (str(chat_id), limit),
    ).fetchall()
    c.close()
    rows.reverse()
    return [{"role": r, "username": u, "text": t, "ts": ts} for r, u, t, ts in rows]


def format_history_for_prompt(chat_id: str | int, limit: int = HISTORY_LIMIT) -> str:
    """Format chat history as a system-prompt context block."""
    msgs = get_history(chat_id, limit)
    if not msgs:
        return ""
    lines = [f"\n## 💬 Conversation History (chat {chat_id}, last {len(msgs)} msgs):"]
    for m in msgs:
        who = f"@{m['username']}" if m["role"] == "user" else "you"
        lines.append(f"[{m['ts']}] {who}: {m['text'][:300]}")
    return "\n".join(lines) + "\n"


# ── HTTP wrapper ───────────────────────────────────────────────────────
def _api(method: str, **params):
    if not TOKEN:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN not set"}
    try:
        r = requests.post(f"{API()}/{method}", json=params, timeout=15)
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _api_form(method: str, files=None, **data):
    """For multipart uploads (sendPhoto/sendDocument)."""
    if not TOKEN:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN not set"}
    try:
        r = requests.post(f"{API()}/{method}", data=data, files=files, timeout=60)
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── the tool ───────────────────────────────────────────────────────────
@tool
def telegram(
    action: str = "send_message",
    chat_id: Optional[str] = None,
    text: Optional[str] = None,
    parse_mode: str = "Markdown",
    disable_notification: bool = False,
    reply_to_message_id: Optional[int] = None,
    file_path: Optional[str] = None,
    caption: Optional[str] = None,
    message_id: Optional[int] = None,
    new_text: Optional[str] = None,
    question: Optional[str] = None,
    options: Optional[List[str]] = None,
    is_anonymous: bool = True,
    limit: int = 10,
) -> str:
    """
    Telegram bot — rich set of actions.

    Actions:
      Messages:
        - "send_message":   chat_id + text       → send text (auto-records to history)
        - "edit_message":   chat_id + message_id + new_text
        - "delete_message": chat_id + message_id
        - "send_photo":     chat_id + file_path [+ caption]
        - "send_document":  chat_id + file_path [+ caption]
        - "send_poll":      chat_id + question + options[]
      Info:
        - "get_me":         bot identity
        - "get_chat":       chat_id → chat info
        - "get_updates":    last N updates (no listener needed)
        - "ping":           connectivity check
      History:
        - "get_history":    chat_id [+ limit] → last N msgs from local DB
        - "clear_history":  chat_id → wipe local history for that chat

    chat_id falls back to TELEGRAM_DEFAULT_CHAT_ID if omitted.
    """
    if not TOKEN:
        return "TELEGRAM_BOT_TOKEN not set"

    cid = str(chat_id or DEFAULT_CHAT_ID) if chat_id is not None or DEFAULT_CHAT_ID else None

    # ── messages ──
    if action == "send_message":
        if not cid:
            return "chat_id required (set TELEGRAM_DEFAULT_CHAT_ID or pass chat_id)"
        if not text:
            return "text required"
        r = _api("sendMessage", chat_id=cid, text=text,
                 parse_mode=parse_mode, disable_notification=disable_notification,
                 reply_to_message_id=reply_to_message_id)
        if r.get("ok"):
            mid = r["result"]["message_id"]
            record_message(cid, "assistant", text, msg_id=mid)
            return f"✓ sent to {cid} (msg_id={mid})"
        return f"✗ {r.get('description', r.get('error'))}"

    if action == "edit_message":
        if not (cid and message_id and new_text):
            return "chat_id + message_id + new_text required"
        r = _api("editMessageText", chat_id=cid, message_id=message_id,
                 text=new_text, parse_mode=parse_mode)
        return f"✓ edited" if r.get("ok") else f"✗ {r.get('description')}"

    if action == "delete_message":
        if not (cid and message_id):
            return "chat_id + message_id required"
        r = _api("deleteMessage", chat_id=cid, message_id=message_id)
        return f"✓ deleted" if r.get("ok") else f"✗ {r.get('description')}"

    if action == "send_photo":
        if not (cid and file_path):
            return "chat_id + file_path required"
        with open(file_path, "rb") as f:
            r = _api_form("sendPhoto",
                          files={"photo": f},
                          chat_id=cid,
                          caption=caption or "",
                          parse_mode=parse_mode)
        return f"✓ photo sent" if r.get("ok") else f"✗ {r.get('description')}"

    if action == "send_document":
        if not (cid and file_path):
            return "chat_id + file_path required"
        with open(file_path, "rb") as f:
            r = _api_form("sendDocument",
                          files={"document": f},
                          chat_id=cid,
                          caption=caption or "",
                          parse_mode=parse_mode)
        return f"✓ document sent" if r.get("ok") else f"✗ {r.get('description')}"

    if action == "send_poll":
        if not (cid and question and options):
            return "chat_id + question + options[] required"
        r = _api("sendPoll", chat_id=cid, question=question,
                 options=json.dumps(options), is_anonymous=is_anonymous)
        return f"✓ poll sent" if r.get("ok") else f"✗ {r.get('description')}"

    # ── info ──
    if action == "get_me":
        r = _api("getMe")
        return json.dumps(r.get("result", r), indent=2)

    if action == "get_chat":
        if not cid:
            return "chat_id required"
        r = _api("getChat", chat_id=cid)
        return json.dumps(r.get("result", r), indent=2)

    if action == "get_updates":
        r = _api("getUpdates", limit=limit)
        if not r.get("ok"):
            return f"✗ {r.get('description', r.get('error'))}"
        out = []
        for u in r.get("result", []):
            msg = u.get("message", {})
            out.append(f"[{msg.get('date')}] @{msg.get('from',{}).get('username','?')}: {msg.get('text','')[:200]}")
        return "\n".join(out) if out else "no updates"

    if action == "ping":
        r = _api("getMe")
        if r.get("ok"):
            bot = r["result"]
            return f"✓ online as @{bot.get('username')} ({bot.get('first_name')})"
        return f"✗ {r.get('error') or r}"

    # ── history (local) ──
    if action == "get_history":
        if not cid:
            return "chat_id required"
        msgs = get_history(cid, limit=limit)
        if not msgs:
            return "no history"
        return "\n".join(
            f"[{m['ts']}] {'@'+m['username'] if m['role']=='user' else 'assistant'}: {m['text'][:200]}"
            for m in msgs
        )

    if action == "clear_history":
        if not cid:
            return "chat_id required"
        c = _conn()
        n = c.execute("DELETE FROM tg_history WHERE chat_id=?", (str(cid),)).rowcount
        c.commit(); c.close()
        return f"✓ cleared {n} messages for chat {cid}"

    return f"unknown action: {action}"


# ── listener (long-polling) ────────────────────────────────────────────
def _load_offset() -> int:
    if OFFSET_FILE.exists():
        try:
            return int(OFFSET_FILE.read_text().strip())
        except Exception:
            pass
    return 0


def _save_offset(off: int):
    OFFSET_FILE.write_text(str(off))


def _user_allowed(user: dict) -> bool:
    if not ALLOWED:
        return True
    return user.get("username", "") in ALLOWED or str(user.get("id", "")) in ALLOWED


def listen(callback: Callable[[dict], None], stop_event: Optional[threading.Event] = None):
    """Long-poll Telegram, dispatch each NEW allowed message to callback.

    Records every incoming message to chat history (role='user') BEFORE
    invoking callback, so callbacks always see fresh history.
    """
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")

    offset = _load_offset()
    print(f"[telegram] listening (offset={offset}, allowed={ALLOWED or 'all'})")

    while not (stop_event and stop_event.is_set()):
        try:
            r = requests.get(
                f"{API()}/getUpdates",
                params={"offset": offset, "timeout": 25, "limit": 30},
                timeout=30,
            ).json()

            print(f"[telegram] poll → ok={r.get('ok')} count={len(r.get('result', []))} desc={r.get('description', '')}", flush=True)

            if not r.get("ok"):
                print(f"[telegram] ERROR: {r}", flush=True)
                time.sleep(5)
                continue

            for u in r.get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message") or u.get("edited_message")
                if not msg:
                    continue
                # Accept text, photo, voice, video, document — skip everything else
                has_payload = any(
                    msg.get(k) for k in ("text", "photo", "voice", "video", "document", "sticker", "caption")
                )
                if not has_payload:
                    continue
                user = msg.get("from", {})
                if not _user_allowed(user):
                    continue

                chat_id = str(msg["chat"]["id"])
                username = user.get("username", "") or user.get("first_name", "?")
                # 1. Persist a textual representation to history
                text_repr = msg.get("text") or msg.get("caption") or ""
                if msg.get("photo"):
                    text_repr = (text_repr + "  [photo]").strip()
                elif msg.get("voice"):
                    text_repr = (text_repr + "  [voice]").strip()
                elif msg.get("video"):
                    text_repr = (text_repr + "  [video]").strip()
                elif msg.get("document"):
                    text_repr = (text_repr + "  [document]").strip()
                record_message(chat_id, "user", text_repr or "(media)",
                               username=username, msg_id=msg.get("message_id"))
                # 2. Dispatch to callback
                try:
                    callback(msg)
                except Exception as e:
                    print(f"[callback err] {e}")

            _save_offset(offset)

        except requests.RequestException:
            time.sleep(5)
        except Exception as e:
            print(f"[telegram listen err] {e}")
            time.sleep(5)


# ─── file download (bot photos / docs) ─────────────────────────────────
def download_file(file_id: str, dest_dir: Optional[Path] = None) -> Optional[Path]:
    """Download a file (photo, document, voice…) by file_id.

    Returns the local Path on success, None on failure. Files are saved
    under <repo>/.memory/tg_files/ by default.
    """
    if not TOKEN:
        return None
    info = _api("getFile", file_id=file_id)
    if not info.get("ok"):
        return None
    file_path = info["result"].get("file_path")
    if not file_path:
        return None
    dest_dir = dest_dir or (ROOT / ".memory" / "tg_files")
    dest_dir.mkdir(parents=True, exist_ok=True)
    fname = file_path.split("/")[-1]
    out = dest_dir / fname
    try:
        r = requests.get(
            f"https://api.telegram.org/file/bot{TOKEN}/{file_path}",
            timeout=30, stream=True,
        )
        r.raise_for_status()
        with open(out, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        return out
    except Exception:
        return None

