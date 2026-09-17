"""personas — dashboard-side view of Scout's personas (who is running, toggle them).

Two kinds of persona, two mechanisms, one API:

* CONTAINER personas (voice, thinker, telegram) — started/stopped by the
  host-side `deploy/scout_supervisor.py` over a unix socket that is
  bind-mounted into this container. The dashboard never touches docker.
* FLAG personas (thinker_drive, recording) — a shared JSON file
  (`tools/persona_flags.py`) that the thinker/recorder poll on every cycle.

    >>> snapshot()          -> {"personas": {...}, "supervisor": "ok"|"unavailable", ...}
    >>> toggle("voice", "start")
    >>> toggle("recording", "off")
    >>> logs("thinker", 20)

Pure stdlib besides the repo's own tools package, so tests can run anywhere.
"""
from __future__ import annotations

import http.client
import json
import os
import socket
from typing import Any, Dict, List, Optional

from tools import persona_flags

SUP_SOCK = os.getenv("SCOUT_SUPERVISOR_SOCK", "/run/scout-supervisor/scout-supervisor.sock")
SUP_TOKEN = os.getenv("SCOUT_SUPERVISOR_TOKEN", "")

CONTAINER_PERSONAS: Dict[str, Dict[str, str]] = {
    "voice":    {"label": "rover-voice", "icon": "🎙", "desc": "Bidi voice agent on the rover's own mic + speaker (voice_agent.py)"},
    "thinker":  {"label": "thinker",     "icon": "🧠", "desc": "Slow-thinker persona: observes, explores, reports on Telegram (thinker_loop.py)"},
    "telegram": {"label": "telegram",    "icon": "✈",  "desc": "Telegram listener bot (telegram_listener.py)"},
}
FLAG_PERSONAS: Dict[str, Dict[str, str]] = {
    "thinker_drive": {"label": "thinker drive", "icon": "🕹", "desc": "Allow the thinker to move the rover (prompt + hard /control gate)"},
    "recording":     {"label": "rec",           "icon": "⏺", "desc": "Auto-record LeRobot episodes around agent turns"},
}
ALL_PERSONAS = tuple(CONTAINER_PERSONAS) + tuple(FLAG_PERSONAS)

START_WORDS = ("start", "on", "true", "1", "enable")
STOP_WORDS = ("stop", "off", "false", "0", "disable")
VALID_STATES = ("running", "stopped", "starting", "stopping", "error", "missing", "unavailable")


class SupervisorError(RuntimeError):
    pass


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 8.0):
        super().__init__("scout-supervisor", timeout=timeout)
        self._path = path

    def connect(self):  # noqa: D401
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._path)
        self.sock = s


def _sup(method: str, path: str, actor: str = "dashboard", timeout: float = 8.0) -> Dict[str, Any]:
    if not SUP_TOKEN:
        raise SupervisorError("SCOUT_SUPERVISOR_TOKEN not set")
    conn = _UnixHTTPConnection(SUP_SOCK, timeout=timeout)
    try:
        conn.request(method, path, headers={
            "X-Scout-Supervisor-Token": SUP_TOKEN,
            "X-Scout-Actor": actor[:64],
            "Content-Length": "0",
        })
        r = conn.getresponse()
        raw = r.read()
    except (OSError, http.client.HTTPException) as e:
        raise SupervisorError(f"supervisor unreachable: {e}") from e
    finally:
        conn.close()
    try:
        body = json.loads(raw or b"{}")
    except Exception:
        body = {"raw": raw[:200].decode(errors="replace")}
    if r.status >= 400:
        raise SupervisorError(body.get("error") or f"HTTP {r.status}")
    return body


def normalize_state(raw: Any) -> str:
    """Coerce whatever the supervisor said into one of VALID_STATES."""
    s = str((raw or {}).get("state", "") if isinstance(raw, dict) else raw or "").lower()
    return s if s in VALID_STATES else "error"


def parse_action(action: Any) -> str:
    a = str(action or "").strip().lower()
    if a in START_WORDS:
        return "start"
    if a in STOP_WORDS:
        return "stop"
    raise ValueError(f"unknown action {action!r} (use start/on or stop/off)")


def flag_state(name: str) -> Dict[str, Any]:
    on = persona_flags.flag(name)
    return {"kind": "flag", "state": "running" if on else "stopped", "on": on}


def snapshot(actor: str = "dashboard") -> Dict[str, Any]:
    out: Dict[str, Any] = {"personas": {}, "supervisor": "ok", "flags_source": None}
    try:
        sup = _sup("GET", "/status", actor=actor)
        raw = sup.get("personas") or {}
    except SupervisorError as e:
        out["supervisor"] = "unavailable"
        out["supervisor_error"] = str(e)
        raw = {}
    for name, meta in CONTAINER_PERSONAS.items():
        r = raw.get(name) or {}
        st = normalize_state(r) if r else "unavailable"
        out["personas"][name] = {
            "kind": "container", **meta, "state": st, "on": st in ("running", "starting"),
            "last_error": r.get("last_error") or r.get("error"),
            "started_at": r.get("started_at"), "exit_code": r.get("exit_code"),
        }
    flags = persona_flags.all_flags()
    out["flags_source"] = flags.get("_source")
    for name, meta in FLAG_PERSONAS.items():
        out["personas"][name] = {**meta, **flag_state(name)}
    # honest hint for the voice persona: which provider/key it will use
    out["voice_provider"] = os.getenv("VOICE_PROVIDER", "openai")
    return out


def toggle(name: str, action: Any, actor: str = "dashboard") -> Dict[str, Any]:
    if name not in ALL_PERSONAS:
        raise KeyError(f"unknown persona {name!r}; known: {list(ALL_PERSONAS)}")
    act = parse_action(action)
    if name in FLAG_PERSONAS:
        persona_flags.set_flag(name, act == "start", actor=actor)
        return {"ok": True, "persona": name, **flag_state(name)}
    res = _sup("POST", f"/{name}/{act}", actor=actor)
    return {"ok": bool(res.get("ok")), "persona": name, "kind": "container",
            "state": normalize_state(res), **({"error": res.get("error")} if res.get("error") else {})}


def logs(name: str, n: int = 20, actor: str = "dashboard") -> Dict[str, Any]:
    if name not in CONTAINER_PERSONAS:
        return {"lines": [], "error": "no container logs for a flag persona"}
    n = max(1, min(200, int(n)))
    res = _sup("GET", f"/{name}/logs?n={n}", actor=actor)
    lines: List[str] = [str(x) for x in res.get("lines") or []]
    return {"persona": name, "lines": lines, **({"error": res["error"]} if res.get("error") else {})}
