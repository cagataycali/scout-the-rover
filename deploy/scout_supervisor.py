#!/usr/bin/env python3
"""scout-supervisor — least-privilege persona control plane for Scout.

Runs ON THE HOST (Thor) as a user systemd unit, outside every container.
The public dashboard container has NO docker socket (a socket = root on the
host behind a public tunnel). Instead it talks to this tiny supervisor over a
UNIX socket bind-mounted read/write into the dashboard container:

    ./.supervisor/scout-supervisor.sock   (host)  ->  /run/scout-supervisor/ (container)

Surface (deliberately tiny, no arbitrary compose args ever reach a shell):

    GET  /status                         -> {personas:{name:{state,...}}}
    GET  /<persona>/status
    GET  /<persona>/logs?n=20            -> {lines:[...]}  (docker logs --tail n)
    POST /<persona>/start | /stop        -> 202 {state:"starting"|"stopping"}

    persona ∈ PERSONAS (voice, thinker, telegram)  — fixed allow-list
    action  ∈ {start, stop, status, logs}

Every request must carry `X-Scout-Supervisor-Token: <SCOUT_SUPERVISOR_TOKEN>`
(read from the repo .env; the dashboard has the same .env via env_file).
Every start/stop is appended to .supervisor/audit.log as one JSON line.

stdlib only — no pip on the host.
"""
from __future__ import annotations

import hmac
import json
import os
import socket
import socketserver
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

REPO = Path(os.environ.get("SCOUT_REPO", Path(__file__).resolve().parent.parent))
SUP_DIR = Path(os.environ.get("SCOUT_SUPERVISOR_DIR", REPO / ".supervisor"))
SOCK = SUP_DIR / "scout-supervisor.sock"
AUDIT = SUP_DIR / "audit.log"

COMPOSE = [
    "docker", "compose",
    "-f", "docker-compose.slim.yml",
    "-f", "docker-compose.slim.override.yml",
]

# persona -> (compose service, container name, profile, stop verb)
#   stop verb: "stop"  keeps the container (logs survive, `restart: unless-stopped`
#                      keeps it down until asked)
#              "rm"    removes it (voice: a dead bidi session must not linger)
PERSONAS = {
    "voice":    {"service": "voice",    "container": "scout-slim-voice",    "profile": "voice", "stop": "rm"},
    "thinker":  {"service": "thinker",  "container": "scout-slim-thinker",  "profile": "all",   "stop": "stop"},
    "telegram": {"service": "telegram", "container": "scout-slim-telegram", "profile": "all",   "stop": "stop"},
}
ACTIONS = ("start", "stop", "status", "logs")
LOG_MAX = 200


def _load_token() -> str:
    tok = os.environ.get("SCOUT_SUPERVISOR_TOKEN", "")
    if tok:
        return tok
    env = REPO / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line.startswith("SCOUT_SUPERVISOR_TOKEN="):
                return line.split("=", 1)[1].strip().strip("'\"")
    return ""


TOKEN = _load_token()
if not TOKEN:
    print("scout-supervisor: SCOUT_SUPERVISOR_TOKEN missing (env or .env) — refusing to start", file=sys.stderr)
    sys.exit(2)

_pending: dict[str, dict] = {}      # persona -> {"state": "starting"|"stopping", "since": ts}
_last_error: dict[str, str] = {}
_lock = threading.Lock()


def _audit(entry: dict) -> None:
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **entry}
    try:
        with AUDIT.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:  # pragma: no cover
        print("audit write failed:", e, file=sys.stderr)


def _run(args: list[str], timeout: int = 120) -> tuple[int, str]:
    p = subprocess.run(args, cwd=str(REPO), capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout + p.stderr).strip()


def container_state(name: str) -> dict:
    """docker inspect -> compact state. state ∈ running|stopped|missing|error|starting|stopping."""
    rc, out = _run(["docker", "inspect", "-f", "{{json .State}}", name], timeout=20)
    if rc != 0:
        return {"state": "missing", "container": name}
    try:
        st = json.loads(out)
    except Exception:
        return {"state": "error", "container": name, "error": out[:200]}
    status = st.get("Status", "")
    health = (st.get("Health") or {}).get("Status")
    if status == "running":
        state = "running"
    elif status in ("exited", "created", "dead"):
        state = "stopped"
        if st.get("ExitCode", 0) not in (0, 137, 143):
            state = "error"
    elif status == "restarting":
        state = "error"
    else:
        state = status or "unknown"
    return {
        "state": state,
        "container": name,
        "docker_status": status,
        "health": health,
        "exit_code": st.get("ExitCode"),
        "started_at": st.get("StartedAt"),
        "finished_at": st.get("FinishedAt"),
        "error": st.get("Error") or None,
    }


def persona_status(name: str) -> dict:
    spec = PERSONAS[name]
    s = container_state(spec["container"])
    with _lock:
        pend = _pending.get(name)
        err = _last_error.get(name)
    if pend and time.time() - pend["since"] < 180:
        s["state"] = pend["state"]
    if err:
        s["last_error"] = err
    return s


def _do(name: str, action: str) -> None:
    spec = PERSONAS[name]
    if action == "start":
        args = COMPOSE + ["--profile", spec["profile"], "up", "-d", "--no-deps", spec["service"]]
    elif spec["stop"] == "rm":
        args = COMPOSE + ["--profile", spec["profile"], "rm", "-sf", spec["service"]]
    else:
        args = COMPOSE + ["--profile", spec["profile"], "stop", "-t", "10", spec["service"]]
    t0 = time.time()
    try:
        rc, out = _run(args, timeout=170)
    except subprocess.TimeoutExpired:
        rc, out = 124, "timeout"
    with _lock:
        _pending.pop(name, None)
        if rc == 0:
            _last_error.pop(name, None)
        else:
            _last_error[name] = out[-300:]
    _audit({"persona": name, "action": action, "rc": rc, "secs": round(time.time() - t0, 1),
            "tail": out[-160:]})


def act(name: str, action: str, who: str) -> dict:
    with _lock:
        if name in _pending:
            return {"ok": False, "error": "busy", **_pending[name]}
        _pending[name] = {"state": "starting" if action == "start" else "stopping", "since": time.time()}
    _audit({"persona": name, "action": action, "requested_by": who})
    threading.Thread(target=_do, args=(name, action), daemon=True).start()
    return {"ok": True, "persona": name, "state": _pending[name]["state"]}


def logs(name: str, n: int) -> dict:
    n = max(1, min(LOG_MAX, n))
    rc, out = _run(["docker", "logs", "--tail", str(n), PERSONAS[name]["container"]], timeout=20)
    if rc != 0:
        return {"lines": [], "error": out[-200:]}
    return {"lines": out.splitlines()[-n:]}


class Handler(BaseHTTPRequestHandler):
    server_version = "scout-supervisor/1"

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authed(self) -> bool:
        tok = self.headers.get("X-Scout-Supervisor-Token", "")
        return bool(tok) and hmac.compare_digest(tok, TOKEN)

    def _route(self):
        u = urlsplit(self.path)
        parts = [p for p in u.path.split("/") if p]
        q = parse_qs(u.query)
        return parts, q

    def do_GET(self):
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        parts, q = self._route()
        if parts == ["status"]:
            return self._send(200, {"personas": {n: persona_status(n) for n in PERSONAS}})
        if len(parts) == 2 and parts[0] in PERSONAS:
            if parts[1] == "status":
                return self._send(200, persona_status(parts[0]))
            if parts[1] == "logs":
                try:
                    n = int(q.get("n", ["20"])[0])
                except ValueError:
                    n = 20
                return self._send(200, logs(parts[0], n))
        return self._send(404, {"error": "unknown route", "allow": {"personas": list(PERSONAS), "actions": list(ACTIONS)}})

    def do_POST(self):
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        parts, _ = self._route()
        if len(parts) == 2 and parts[0] in PERSONAS and parts[1] in ("start", "stop"):
            who = self.headers.get("X-Scout-Actor", "dashboard")[:64]
            res = act(parts[0], parts[1], who)
            return self._send(202 if res.get("ok") else 409, res)
        return self._send(404, {"error": "unknown route", "allow": {"personas": list(PERSONAS), "actions": list(ACTIONS)}})


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    # BaseHTTPRequestHandler expects client_address[0] to be a str
    def get_request(self):
        req, _ = super().get_request()
        return req, ("unix", 0)


def main() -> None:
    SUP_DIR.mkdir(parents=True, exist_ok=True)
    if SOCK.exists():
        SOCK.unlink()
    srv = UnixHTTPServer(str(SOCK), Handler)
    os.chmod(SOCK, 0o660)
    print(f"scout-supervisor listening on {SOCK} (personas={list(PERSONAS)})", flush=True)
    try:
        srv.serve_forever()
    finally:
        try:
            SOCK.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()
