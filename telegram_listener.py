#!/usr/bin/env python3
"""🛞 scout — Telegram listener.

Lets the operator chat with scout (Earth Rover Mini) from anywhere over
Telegram. Each incoming message:
  1. is recorded in tg_history (the @tool listener does that)
  2. spawns a fresh Strands agent via build_agent("telegram", chat_id, username)
  3. agent uses rover tools to drive/look/speak + telegram tool to reply
     with text + photos

Slash commands (handled directly, no LLM round-trip):
  /start    register chat_id, show capabilities
  /clear    wipe this chat's history
  /history  dump last N messages
  /state    live rover telemetry (battery, GPS, signal, IMU)
  /battery  quick battery check
  /photo    grab front+rear camera frames and send them
  /lamp_on  /lamp_off  toggle headlamp
  /stop     emergency stop (rover_stop)

Run:
    make telegram                      # foreground REPL
    sudo systemctl start scout-telegram  # service mode
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Any, Dict

from dotenv import load_dotenv

load_dotenv(override=True)

from strands import Agent

from agent import (
    BASE_PROMPT,
    build_turn_input,
    live_state_block,
    _auto_recorder,
)
from tools import ROVER_ALL_TOOLS
from tools.rover_state import rover_state as _rover_state_tool, rover_speak as _rover_speak_tool
from tools.rover_camera import rover_see as _rover_see_tool
from tools.rover_motion import rover_lamp as _rover_lamp_tool, rover_stop as _rover_stop_tool
from tools.voice_bridge import push as voice_push
from tools.telegram import (
    listen as telegram_listen,
    format_history_for_prompt,
    record_message,
    download_file,
    _api,
    _api_form,
    _conn,
)
import memory as _memory
from tools.reasoning_log import ReasoningLoggerHook


# Telegram persona prompt — extends BASE_PROMPT with chat-aware preamble.
def _telegram_prompt(chat_id: str, username: str) -> str:
    history = format_history_for_prompt(chat_id, limit=20) or "(no history yet)"
    extra = f"""
## Channel: Telegram DM
You are talking to **@{username}** (chat_id `{chat_id}`) over Telegram.

To reply, USE THE telegram TOOL — DO NOT just emit text:
  telegram(action='send_message', chat_id='{chat_id}', text='your reply')
  telegram(action='send_photo',   chat_id='{chat_id}', file_path='/tmp/scout_view.jpg', caption='...')

Telegram-specific behavior:
  • Be terse — Telegram is mobile, so ≤2 short sentences per reply.
  • Whenever you take a meaningful action (drove somewhere, found something,
    saw something cool), send a PHOTO via send_photo so the operator can
    see what you saw. Save snapshots to /tmp/scout_*.jpg and attach.
  • Use markdown sparingly (Telegram supports basic markdown).
  • For long lists / json, prefer telegram(action='send_document') over message.

## Recent telegram history with @{username}
{history}
"""
    return f"{_memory.recall_block()}\n{live_state_block()}\n{BASE_PROMPT}\n{extra}"


def build_agent_for_chat(chat_id: str, username: str) -> Agent:
    return Agent(
        tools=ROVER_ALL_TOOLS,
        system_prompt=_telegram_prompt(chat_id, username),
        hooks=[ReasoningLoggerHook(agent_id="telegram")],
    )


# Helpers
def _send(chat_id: str, text: str, parse_mode: str = "Markdown") -> dict:
    r = _api("sendMessage", chat_id=chat_id, text=text, parse_mode=parse_mode)
    if r.get("ok"):
        record_message(chat_id, "assistant", text, msg_id=r["result"]["message_id"])
    return r


def _send_photo(chat_id: str, file_path: str, caption: str = "") -> dict:
    try:
        with open(file_path, "rb") as f:
            r = _api_form(
                "sendPhoto",
                files={"photo": (os.path.basename(file_path), f, "image/jpeg")},
                chat_id=chat_id,
                caption=caption[:1000],
            )
        return r
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _unwrap(env: Dict[str, Any]) -> Dict[str, Any]:
    """Pull JSON out of @tool envelopes."""
    if not isinstance(env, dict) or "content" not in env:
        return env if isinstance(env, dict) else {}
    for c in env.get("content") or []:
        if isinstance(c, dict) and "json" in c:
            return c["json"]
    return {}


def _grab_and_send_view(chat_id: str, caption: str = "👀") -> None:
    """Capture front+rear, save jpegs, send to Telegram."""
    try:
        env = _rover_see_tool(camera="both", save=True)
        # rover_see saves to /tmp/scout_front_*.jpg etc; pull the paths from text content
        paths = []
        for c in env.get("content", []):
            if isinstance(c, dict):
                t = c.get("text", "")
                # rover_see prints "saved → /tmp/..." or similar
                for line in t.split("\n"):
                    if "/tmp/" in line and ".jpg" in line:
                        for tok in line.split():
                            if tok.startswith("/tmp/") and tok.endswith(".jpg"):
                                paths.append(tok.rstrip(",.;)"))
        # If we couldn't parse, just iterate over /tmp for the freshest
        if not paths:
            import glob, os.path
            cands = sorted(glob.glob("/tmp/scout_*.jpg") + glob.glob("/tmp/rover_*.jpg"),
                           key=os.path.getmtime, reverse=True)[:2]
            paths = cands
        if not paths:
            _send(chat_id, "📷 no frames available (SDK offline?)")
            return
        for p in paths[:2]:
            r = _send_photo(chat_id, p, caption=f"{caption} — {os.path.basename(p)}")
            if not r.get("ok"):
                _send(chat_id, f"⚠️ photo send failed: {r.get('error', 'unknown')}")
    except Exception as e:
        _send(chat_id, f"⚠️ camera error: {e}")


# Slash commands
def _handle_slash(chat_id: str, text: str, username: str) -> bool:
    """Return True if handled; False to fall through to LLM."""
    cmd = text.split()[0].lower()

    if cmd == "/start":
        _send(chat_id,
            f"🛞 *scout* — Earth Rover Mini — is online.\n\n"
            f"Your chat_id: `{chat_id}`\n\n"
            f"I can see, drive, remember, and chat. Try:\n"
            f"  • `look around` — I'll send photos\n"
            f"  • `drive forward 1m` — I'll move + show before/after\n"
            f"  • `where are you?` — telemetry\n\n"
            f"Slash commands:\n"
            f"  /photo /state /battery\n"
            f"  /lamp\\_on /lamp\\_off /stop\n"
            f"  /history /clear")
        return True

    if cmd == "/clear":
        c = _conn()
        n = c.execute("DELETE FROM tg_history WHERE chat_id=?", (chat_id,)).rowcount
        c.commit(); c.close()
        _send(chat_id, f"🧹 cleared {n} messages from chat memory")
        return True

    if cmd == "/history":
        block = format_history_for_prompt(chat_id, limit=30) or "(empty)"
        _send(chat_id, f"```\n{block[:3500]}\n```")
        return True

    if cmd == "/state":
        d = _unwrap(_rover_state_tool())
        if not d:
            _send(chat_id, "⚠️ rover SDK unreachable")
            return True
        gps_ok = d.get("gps_signal", 0) > 0 and d.get("latitude", 1000) != 1000
        gps = f"`{d['latitude']:.6f}, {d['longitude']:.6f}`" if gps_ok else "no fix"
        _send(chat_id,
              f"🛞 *scout state*\n"
              f"Battery: *{d.get('battery','?')}%*  ({d.get('voltage','?')}V)\n"
              f"Signal: {d.get('signal_level','?')}/4\n"
              f"GPS: {gps}\n"
              f"Heading: {d.get('orientation','?')}°\n"
              f"Lamp: {'🔆' if d.get('lamp') else '⚫'}")
        return True

    if cmd == "/battery":
        d = _unwrap(_rover_state_tool())
        b = d.get("battery", "?")
        v = d.get("voltage", "?")
        _send(chat_id, f"🔋 *{b}%* — {v}V")
        return True

    if cmd == "/photo":
        _grab_and_send_view(chat_id, caption=f"📸 on demand for @{username}")
        return True

    if cmd in ("/lamp_on", "/lampon"):
        _rover_lamp_tool(on=True)
        _send(chat_id, "🔆 lamp on")
        return True

    if cmd in ("/lamp_off", "/lampoff"):
        _rover_lamp_tool(on=False)
        _send(chat_id, "⚫ lamp off")
        return True

    if cmd == "/stop":
        _rover_stop_tool()
        _send(chat_id, "🛑 *EMERGENCY STOP* issued")
        return True

    return False


# Main message handler
def handle_message(msg: dict) -> None:
    chat_id = str(msg["chat"]["id"])
    text = (msg.get("text") or "").strip()
    user = msg.get("from", {})
    username = user.get("username") or user.get("first_name") or "?"

    print(f"[{datetime.now():%H:%M:%S}] @{username} ({chat_id}): {text[:80] or '(media)'}")

    # Photo from operator → save it (could be useful for vision context later)
    photos = msg.get("photo") or []
    if photos:
        biggest = max(photos, key=lambda p: p.get("file_size", 0))
        path = download_file(biggest["file_id"])
        caption = msg.get("caption", "")
        if path:
            _send(chat_id,
                  f"📥 got your photo — saved to `{path.name}`"
                  + (f"\ncaption: _{caption}_" if caption else ""))
        else:
            _send(chat_id, "⚠️ photo download failed")
        return

    if not text:
        return

    # Slash commands
    if text.startswith("/") and _handle_slash(chat_id, text, username):
        return

    try:
        # Relay to voice agent: operator is talking via Telegram, Scout should hear it
        try:
            voice_push("telegram", f"@{username}: {text}", importance=1)
        except Exception as _e:
            print(f"🌉 voice_bridge push skipped: {_e}", flush=True)
        agent = build_agent_for_chat(chat_id, username)
        # Wrap the input with live camera frames (same as agent.py REPL).
        prompt = f"@{username} (telegram): {text}"
        _auto_recorder.begin_turn(prompt)
        _result = None
        try:
            _result = agent(build_turn_input(prompt, camera="both"))
        finally:
            _auto_recorder.end_turn()
            try:
                _memory.record_turn(prompt, _result)
            except Exception as e:
                print(f"🧠 memory: skipped ({e})", flush=True)

        # Belt-and-suspenders: if the agent forgot to call telegram tool,
        # send the final text via Telegram so the operator gets a reply.
        text_out = str(_result or "").strip()
        if text_out and not _last_outbound_was_recent(chat_id, ttl_seconds=30):
            _send(chat_id, text_out[:3500])
    except Exception as e:
        err = f"⚠️ scout error: {type(e).__name__}: {e}"
        print(err)
        _send(chat_id, err[:3500])


def _last_outbound_was_recent(chat_id: str, ttl_seconds: int = 30) -> bool:
    """Did we send an assistant message in this chat in the last N seconds?
    Used to avoid double-replying when the agent already used telegram tool."""
    c = _conn()
    row = c.execute(
        "SELECT ts FROM tg_history WHERE chat_id=? AND role='assistant' "
        "ORDER BY id DESC LIMIT 1",
        (str(chat_id),),
    ).fetchone()
    c.close()
    if not row:
        return False
    try:
        last = datetime.fromisoformat(row[0]) if isinstance(row[0], str) else row[0]
    except Exception:
        return False
    delta = (datetime.utcnow() - last).total_seconds()
    return delta < ttl_seconds


def main() -> None:
    print("🛞 scout — Telegram listener starting…")
    if not (os.getenv("SCOUT_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")):
        print("✗ TELEGRAM_BOT_TOKEN not set", file=sys.stderr)
        sys.exit(1)
    print(f"   ALLOWED_USERS = {os.getenv('SCOUT_TELEGRAM_ALLOWED_USERS') or os.getenv('TELEGRAM_ALLOWED_USERS','(any)')}")
    print(f"   HISTORY_LIMIT = {os.getenv('SCOUT_TELEGRAM_HISTORY_LIMIT') or os.getenv('TELEGRAM_HISTORY_LIMIT','20')}")
    try:
        telegram_listen(handle_message)
    except KeyboardInterrupt:
        print("\n👋 listener stopped")


if __name__ == "__main__":
    main()
