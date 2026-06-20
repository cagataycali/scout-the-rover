#!/usr/bin/env python3
"""
🛞 scout dashboard server — drive the Earth Rover Mini from the web.

A single FastAPI app that:
  • serves the glassmorphic dashboard in ./docs
  • streams the scout agent over WebSocket (/ws/chat) with a custom
    callback_handler (token + tool-use + telemetry events, à la devduck)
  • proxies the Earth Rovers SDK (camera frames, telemetry, control, speak)
    so the browser never needs CORS gymnastics
  • exposes a live config API (system prompt, model id, env credentials)
    so you can retune scout without touching files
  • bridges browser mic ↔ bidi voice model over /ws/voice (PCM16)

Run:  python dashboard_server.py            # :8080
      DASH_PORT=9000 python dashboard_server.py
      make dashboard

The WS URL is a *client-side* parameter — the dashboard asks where to
connect (defaults to its own origin), so you can host the static page
anywhere and point it at a rover-side server.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv, dotenv_values, set_key
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

# 🔐 WebAuthn auth guard (passkey passwordless)
try:
    import auth as scout_auth
    _AUTH_OK = True
except Exception as _e:
    print(f"⚠️  auth module not loaded: {_e}", flush=True)
    scout_auth = None
    _AUTH_OK = False

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
ENV_FILE = ROOT / ".env"

load_dotenv(ENV_FILE)

def _resolve_sdk_url() -> str:
    """Live SDK base URL. Re-reads env each call so the dashboard can change
    the rover SDK port/host at runtime via /api/config without a restart."""
    return os.getenv("ROVER_SDK_URL", "http://localhost:8002").rstrip("/")


# Back-compat module global (snapshot at import; prefer _resolve_sdk_url()).
ROVER_SDK_URL = _resolve_sdk_url()
DASH_PORT = int(os.getenv("DASH_PORT", "8080"))
DASH_HOST = os.getenv("DASH_HOST", "0.0.0.0")

app = FastAPI(title="scout dashboard")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 🎞️ Dataset replay API (episode browser + timeline scrubber + reasoning overlay)
try:
    import dashboard_replay
    dashboard_replay.mount(app)
except Exception as _e:
    print(f"⚠️  replay API not mounted: {_e}", flush=True)


# 🔏 On-device CA-trust page (iOS / Android). Served at /trust. Detects the OS
# and shows the exact steps to install + trust scout's mkcert root CA so the
# browser shows NO certificate warning. {placeholders} filled by trust_page().
_TRUST_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>scout · trust this device</title>
<style>
  body {{ font-family:system-ui,-apple-system,sans-serif; background:#0a0a0f; color:#e8e8f0;
         margin:0; padding:24px; display:flex; justify-content:center; }}
  .card {{ width:min(460px,92vw); background:linear-gradient(160deg,#13131c,#0b0b12);
          border:1px solid rgba(255,255,255,.12); border-radius:24px; padding:28px;
          box-shadow:0 24px 70px rgba(0,0,0,.5); }}
  h1 {{ font-size:24px; margin:0 0 2px; text-align:center; }}
  .sub {{ color:#9aa; font-size:13px; margin:0 0 18px; text-align:center; }}
  .mode {{ display:inline-block; font-size:11px; padding:3px 9px; border-radius:999px;
          background:rgba(118,185,0,.12); color:#9be319; border:1px solid rgba(118,185,0,.3); }}
  .qr {{ background:#fff; border-radius:18px; padding:14px; display:block; width:max-content;
        margin:14px auto; }}
  .qr img {{ display:block; width:220px; height:220px; image-rendering:pixelated; }}
  .dl {{ display:block; text-align:center; margin:6px 0 18px; }}
  .dl a {{ color:#9be319; font-weight:600; font-size:15px; text-decoration:none; }}
  .seg {{ display:flex; gap:8px; margin:0 0 14px; }}
  .seg button {{ flex:1; padding:10px; border-radius:11px; border:1px solid rgba(255,255,255,.14);
    background:rgba(255,255,255,.05); color:#cdd; font-size:14px; cursor:pointer; }}
  .seg button.on {{ background:#76b900; color:#07120a; border-color:#76b900; font-weight:600; }}
  .steps {{ display:none; }} .steps.on {{ display:block; }}
  ol {{ font-size:13.5px; line-height:1.75; color:#cdd; padding-left:20px; margin:6px 0 0; }}
  code {{ background:rgba(255,255,255,.08); padding:1px 6px; border-radius:6px; font-size:12px; }}
  .warn {{ background:rgba(255,180,84,.1); border:1px dashed rgba(255,180,84,.4); color:#ffce9a;
          border-radius:12px; padding:12px; font-size:13px; margin:0 0 16px; }}
  .foot {{ margin-top:18px; font-size:11px; color:#556; text-align:center; }}
</style></head><body>
<div class="card">
  <h1>🛞🔏 trust scout</h1>
  <p class="sub">install the rover CA → no more certificate warnings<br>
     <span class="mode">{mode}</span></p>
  {selfsigned_note}
  <div class="qr"><img src="data:image/png;base64,{qr_b64}" alt="CA QR"></div>
  <div class="dl"><a href="{ca_url}" download="scout-rootCA.crt">⬇︎ download scout-rootCA.crt</a></div>

  <div class="seg">
    <button id="biOS" class="on" onclick="seg('iOS')">📱 iPhone / iPad</button>
    <button id="bAnd" onclick="seg('And')">🤖 Android</button>
  </div>

  <div id="siOS" class="steps on">
    <ol>
      <li>Tap the QR / download link above in <b>Safari</b> — allow the profile.</li>
      <li><b>Settings → Profile Downloaded → Install</b> (top-right), enter passcode.</li>
      <li><b>Settings → General → About → Certificate Trust Settings</b>.</li>
      <li>Toggle <b>ON</b> for <code>scout</code> / <code>mkcert</code>. Done — open the dashboard.</li>
    </ol>
  </div>
  <div id="sAnd" class="steps">
    <ol>
      <li>Tap the download link above to save <code>scout-rootCA.crt</code>.</li>
      <li><b>Settings → Security → Encryption &amp; credentials → Install a certificate → CA certificate</b>.</li>
      <li>Pick the downloaded file, confirm <b>Install anyway</b>.</li>
      <li>Open the dashboard — no warning. (Some apps need <i>user CA</i> trust enabled.)</li>
    </ol>
  </div>
  <div class="foot">After trusting, return to the field card or open the dashboard directly.</div>
</div>
<script>
  function seg(k){{
    document.getElementById('biOS').classList.toggle('on', k==='iOS');
    document.getElementById('bAnd').classList.toggle('on', k==='And');
    document.getElementById('siOS').classList.toggle('on', k==='iOS');
    document.getElementById('sAnd').classList.toggle('on', k==='And');
  }}
  // auto-pick based on UA
  if (/android/i.test(navigator.userAgent)) seg('And');
</script>
</body></html>"""

# 🔐 Auth routes (WebAuthn passkey) + guards

def _auth_guard_http(request: "Request"):
    """Raise 401 unless authed. No-op if auth module/feature disabled."""
    if not _AUTH_OK or scout_auth is None:
        return None
    return scout_auth.require_auth(request)


@app.get("/auth/status")
async def auth_status(request: Request):
    if not _AUTH_OK:
        return {"enabled": False, "setup_required": False, "available": False}
    return {**scout_auth.status(request), "available": True}


@app.post("/auth/register/begin")
async def auth_register_begin(request: Request):
    if not _AUTH_OK:
        raise HTTPException(503, "auth unavailable")
    body = await request.json()
    # If credentials already exist, adding another passkey requires a session.
    if scout_auth.has_credentials():
        scout_auth.require_auth(request)
    return scout_auth.begin_registration(
        request,
        label=body.get("label", "admin passkey"),
        bootstrap=body.get("bootstrap", ""),
    )


@app.post("/auth/register/finish")
async def auth_register_finish(request: Request):
    if not _AUTH_OK:
        raise HTTPException(503, "auth unavailable")
    body = await request.json()
    res = scout_auth.finish_registration(
        request, body.get("challenge_id", ""), body.get("credential", {})
    )
    resp = JSONResponse(res)
    resp.set_cookie("scout_session", res["token"], httponly=True, samesite="lax", max_age=scout_auth.TOKEN_TTL)
    return resp


@app.post("/auth/login/begin")
async def auth_login_begin(request: Request):
    if not _AUTH_OK:
        raise HTTPException(503, "auth unavailable")
    return scout_auth.begin_authentication(request)


@app.post("/auth/login/finish")
async def auth_login_finish(request: Request):
    if not _AUTH_OK:
        raise HTTPException(503, "auth unavailable")
    body = await request.json()
    res = scout_auth.finish_authentication(
        request, body.get("challenge_id", ""), body.get("credential", {})
    )
    resp = JSONResponse(res)
    resp.set_cookie("scout_session", res["token"], httponly=True, samesite="lax", max_age=scout_auth.TOKEN_TTL)
    return resp


@app.post("/auth/logout")
async def auth_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("scout_session")
    return resp


@app.get("/auth/credentials")
async def auth_credentials(request: Request):
    """List enrolled passkeys (admin only)."""
    if not _AUTH_OK:
        raise HTTPException(503, "auth unavailable")
    scout_auth.require_auth(request)
    me = None
    try:
        me = scout_auth.require_auth(request).get("sub")
    except Exception:
        pass
    creds = scout_auth.list_credentials()
    for c in creds:
        c["current"] = (c["id"] == me)
    return {"credentials": creds}


@app.post("/auth/credentials/rename")
async def auth_credentials_rename(request: Request):
    if not _AUTH_OK:
        raise HTTPException(503, "auth unavailable")
    scout_auth.require_auth(request)
    body = await request.json()
    return scout_auth.rename_credential(body.get("id", ""), body.get("name", ""))


@app.post("/auth/credentials/delete")
async def auth_credentials_delete(request: Request):
    if not _AUTH_OK:
        raise HTTPException(503, "auth unavailable")
    scout_auth.require_auth(request)
    body = await request.json()
    return scout_auth.delete_credential(body.get("id", ""))


# 🔐 Global auth middleware — seals every /api/* route (incl. replay) behind a
# valid session. Public allowlist: the auth ceremony, static assets, the page
# shell (so the login screen can load), and nothing that touches the rover.
_PUBLIC_PREFIXES = ("/auth/", "/css/", "/js/")
_PUBLIC_EXACT = {"/", "/replay", "/field-card", "/trust", "/ca", "/favicon.ico", "/api/health"}


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    if not (_AUTH_OK and scout_auth and scout_auth.AUTH_ENABLED):
        return await call_next(request)
    path = request.url.path
    if path in _PUBLIC_EXACT or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
        return await call_next(request)
    # everything else under /api requires a session
    if path.startswith("/api/"):
        try:
            scout_auth.require_auth(request)
        except HTTPException as e:
            return JSONResponse({"error": e.detail}, status_code=e.status_code)
    return await call_next(request)


# Agent session — lazily built so the server boots even if creds are missing.
# Rebuilt whenever config (system prompt / model) changes via the dashboard.
_agent_lock = threading.Lock()
_agent = None
_agent_busy = threading.Lock()  # only one agent turn at a time


def _build_agent():
    """Construct the scout agent (imports are deferred to keep boot fast)."""
    import agent as scout_agent  # noqa
    import importlib
    importlib.reload(scout_agent)  # pick up any env changes
    a = scout_agent.build_agent()
    return a, scout_agent


def get_agent():
    global _agent
    with _agent_lock:
        if _agent is None:
            _agent, _ = _build_agent()
        return _agent


def reset_agent():
    """Drop the cached agent so the next turn rebuilds with fresh config."""
    global _agent
    with _agent_lock:
        _agent = None


# WS streaming callback handler — pushes structured events into a thread-safe
# queue that the websocket coroutine drains and forwards to the browser.
# Mirrors the devduck callback_handler kwargs contract.
class WSStreamHandler:
    def __init__(self, q: "queue.Queue[dict]"):
        self.q = q
        self._tool_ids: set[str] = set()

    def __call__(self, **kwargs: Any) -> None:
        data = kwargs.get("data")
        complete = kwargs.get("complete", False)
        current_tool_use = kwargs.get("current_tool_use") or {}
        reasoning_text = kwargs.get("reasoningText")
        message = kwargs.get("message") or {}

        if reasoning_text:
            self.q.put({"type": "reasoning", "data": reasoning_text})

        if data:
            self.q.put({"type": "token", "data": data, "complete": bool(complete)})

        # tool-use start (announce once per tool id)
        if current_tool_use and current_tool_use.get("name"):
            tid = current_tool_use.get("toolUseId", "")
            if tid and tid not in self._tool_ids:
                self._tool_ids.add(tid)
                self.q.put({
                    "type": "tool",
                    "status": "start",
                    "name": current_tool_use.get("name"),
                    "id": tid,
                })

        # tool results (from user-role messages)
        if isinstance(message, dict) and message.get("role") == "user":
            for c in message.get("content", []):
                if isinstance(c, dict) and "toolResult" in c:
                    tr = c["toolResult"]
                    self.q.put({
                        "type": "tool",
                        "status": tr.get("status", "done"),
                        "id": tr.get("toolUseId", ""),
                    })


def _run_turn_blocking(prompt: str, q: "queue.Queue[dict]") -> None:
    """Run ONE agent turn in a worker thread, streaming events into q."""
    try:
        import agent as scout_agent
        import memory as _memory
        with _agent_busy:
            a = get_agent()
            # Per-turn live state injection (same as REPL).
            try:
                a.system_prompt = (
                    f"{_memory.recall_block()}\n"
                    f"{scout_agent.live_state_block()}\n"
                    f"{_get_active_prompt()}"
                )
            except Exception:
                pass
            a.callback_handler = WSStreamHandler(q)

            scout_agent._auto_recorder.begin_turn(prompt)
            result = None
            try:
                result = a(scout_agent.build_turn_input(prompt))
            finally:
                scout_agent._auto_recorder.end_turn()
                try:
                    _memory.record_turn(prompt, result)
                except Exception:
                    pass

            # Final text
            text = ""
            try:
                msg = getattr(result, "message", None)
                if msg and isinstance(msg, dict):
                    text = "\n".join(
                        c.get("text", "") for c in msg.get("content", []) if "text" in c
                    )
                else:
                    text = str(result)
            except Exception:
                text = str(result)
            q.put({"type": "done", "text": text})
    except Exception as e:
        q.put({"type": "error", "error": str(e)})
    finally:
        q.put({"type": "__END__"})


# Active system prompt override (set via dashboard). None → use agent.BASE_PROMPT
_active_prompt_override: Optional[str] = None


def _get_active_prompt() -> str:
    import agent as scout_agent
    return _active_prompt_override or scout_agent.BASE_PROMPT


# WebSocket: chat (streaming agent)
@app.websocket("/ws/chat")
async def ws_chat(ws: WebSocket):
    await ws.accept()
    if _AUTH_OK and scout_auth and scout_auth.AUTH_ENABLED:
        if scout_auth.require_ws_auth(ws) is None:
            await ws.send_text(json.dumps({"type": "error", "error": "auth required"}))
            await ws.close(code=4401)
            return
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                msg = {"type": "chat", "text": raw}

            mtype = msg.get("type", "chat")

            if mtype == "chat":
                prompt = (msg.get("text") or "").strip()
                if not prompt:
                    continue
                q: "queue.Queue[dict]" = queue.Queue()
                # run agent turn in a thread
                threading.Thread(
                    target=_run_turn_blocking, args=(prompt, q), daemon=True
                ).start()
                # drain queue → ws
                while True:
                    ev = await asyncio.to_thread(q.get)
                    if ev.get("type") == "__END__":
                        break
                    await ws.send_text(json.dumps(ev))

            elif mtype == "control":
                # direct manual drive (bypasses agent)
                await _sdk_control(
                    float(msg.get("linear", 0)),
                    float(msg.get("angular", 0)),
                    float(msg.get("duration", 0.5)),
                )
                await ws.send_text(json.dumps({"type": "control_ack"}))

            elif mtype == "stop":
                await _sdk_control(0, 0, 0.1)
                await ws.send_text(json.dumps({"type": "control_ack", "stop": True}))

            elif mtype == "ping":
                await ws.send_text(json.dumps({"type": "pong", "t": time.time()}))

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await ws.send_text(json.dumps({"type": "error", "error": str(e)}))
        except Exception:
            pass


# SDK proxy helpers + endpoints (camera, telemetry, control, speak)
def _sdk_get(path: str, **params) -> dict:
    r = requests.get(f"{_resolve_sdk_url()}{path}", params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def _sdk_post(path: str, payload: dict) -> dict:
    r = requests.post(f"{_resolve_sdk_url()}{path}", json=payload, timeout=20)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return {"ok": True}


# Manual-drive (WASD/joystick) turn sign. Observed reversed on this rover, so
# default -1. INDEPENDENT from the agent tools' ROVER_TURN_SIGN because the two
# control paths were observed to behave differently. Override via DASH_TURN_SIGN.
TURN_SIGN = float(os.getenv("DASH_TURN_SIGN", "-1"))


async def _sdk_control(linear: float, angular: float, duration: float):
    """Stream a clamped control command to the rover for `duration`s, then stop."""
    linear = max(-1.0, min(1.0, linear))
    angular = max(-1.0, min(1.0, angular)) * TURN_SIGN
    cmd = {"command": {"linear": linear, "angular": angular}}
    end = time.time() + max(0.05, min(duration, 3.0))
    try:
        while time.time() < end:
            await asyncio.to_thread(_sdk_post, "/control", cmd)
            await asyncio.sleep(0.1)
    finally:
        await asyncio.to_thread(
            _sdk_post, "/control", {"command": {"linear": 0, "angular": 0}}
        )


@app.get("/api/telemetry")
async def api_telemetry(request: Request):
    _auth_guard_http(request)
    try:
        return JSONResponse(await asyncio.to_thread(_sdk_get, "/data"))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.get("/api/frame/{view}")
async def api_frame(view: str, request: Request):
    _auth_guard_http(request)
    """Proxy a single camera frame. view ∈ {front, rear}."""
    if view not in ("front", "rear"):
        raise HTTPException(400, "view must be front or rear")
    try:
        data = await asyncio.to_thread(_sdk_get, f"/v2/{view}")
        return JSONResponse(data)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.get("/api/screenshot")
async def api_screenshot(request: Request, views: str = "front,rear,map"):
    _auth_guard_http(request)
    try:
        return JSONResponse(
            await asyncio.to_thread(lambda: _sdk_get("/screenshot", view_types=views))
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.post("/api/control")
async def api_control(request: Request):
    _auth_guard_http(request)
    body = await request.json()
    await _sdk_control(
        float(body.get("linear", 0)),
        float(body.get("angular", 0)),
        float(body.get("duration", 0.5)),
    )
    return {"ok": True}


@app.post("/api/lamp")
async def api_lamp(request: Request):
    _auth_guard_http(request)
    body = await request.json()
    on = bool(body.get("on", True))
    # SDK control accepts a lamp field in command on many firmwares
    await asyncio.to_thread(_sdk_post, "/control", {"command": {"lamp": 1 if on else 0}})
    return {"ok": True, "lamp": on}


@app.post("/api/speak")
async def api_speak(request: Request):
    _auth_guard_http(request)
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text required")
    try:
        await asyncio.to_thread(_sdk_post, "/speak", {"text": text})
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


# Config API — system prompt, model id, env credentials (live retune)
_SENSITIVE = ("TOKEN", "KEY", "SECRET", "PASSWORD", "CREDENTIAL")


def _mask(k: str, v: str) -> str:
    if any(s in k.upper() for s in _SENSITIVE) and v:
        return v[:4] + "…" + v[-2:] if len(v) > 8 else "••••"
    return v


@app.get("/api/config")
async def api_config(request: Request):
    _auth_guard_http(request)
    import agent as scout_agent
    env = dotenv_values(ENV_FILE) if ENV_FILE.exists() else {}
    masked = {k: _mask(k, v or "") for k, v in env.items()}
    return {
        "system_prompt": _active_prompt_override or scout_agent.BASE_PROMPT,
        "is_default_prompt": _active_prompt_override is None,
        "model_id": os.getenv("STRANDS_MODEL_ID", ""),
        "voice_provider": os.getenv("VOICE_PROVIDER", "openai"),
        "voice_name": os.getenv("VOICE_NAME", ""),
        "env": masked,
        "rover_sdk_url": _resolve_sdk_url(),
    }


@app.post("/api/config")
async def api_config_update(request: Request):
    """Update system prompt / model / env vars. Rebuilds the agent."""
    global _active_prompt_override
    _auth_guard_http(request)
    body = await request.json()

    if "system_prompt" in body:
        sp = body["system_prompt"]
        _active_prompt_override = sp if sp and sp.strip() else None

    if body.get("reset_prompt"):
        _active_prompt_override = None

    if body.get("model_id"):
        os.environ["STRANDS_MODEL_ID"] = body["model_id"]
        if ENV_FILE.exists():
            set_key(str(ENV_FILE), "STRANDS_MODEL_ID", body["model_id"])

    # env var updates (only persist non-empty, non-masked values)
    for k, v in (body.get("env") or {}).items():
        if v is None or v == "" or "…" in str(v) or "••" in str(v):
            continue  # skip masked/empty — user didn't change it
        os.environ[k] = str(v)
        if ENV_FILE.exists():
            set_key(str(ENV_FILE), k, str(v))

    # rover SDK url / port — lets you point the dashboard at an SDK on any port
    # (e.g. 8003) or host without a restart. Accepts a full URL or a bare port.
    if body.get("rover_sdk_url"):
        val = str(body["rover_sdk_url"]).strip()
        if val.isdigit():
            val = f"http://localhost:{val}"
        val = val.rstrip("/")
        os.environ["ROVER_SDK_URL"] = val
        if ENV_FILE.exists():
            set_key(str(ENV_FILE), "ROVER_SDK_URL", val)

    if body.get("voice_provider"):
        os.environ["VOICE_PROVIDER"] = body["voice_provider"]
        if ENV_FILE.exists():
            set_key(str(ENV_FILE), "VOICE_PROVIDER", body["voice_provider"])
    if body.get("voice_name"):
        os.environ["VOICE_NAME"] = body["voice_name"]
        if ENV_FILE.exists():
            set_key(str(ENV_FILE), "VOICE_NAME", body["voice_name"])

    reset_agent()  # next turn rebuilds with new config
    return {"ok": True}


@app.get("/api/health")
async def api_health():
    sdk_ok = False
    try:
        await asyncio.to_thread(_sdk_get, "/data")
        sdk_ok = True
    except Exception:
        pass
    return {"ok": True, "sdk": sdk_ok, "sdk_url": _resolve_sdk_url()}


# Voice: browser mic ↔ bidi model (PCM16 over WS). Pragmatic streaming bridge.
@app.websocket("/ws/voice")
async def ws_voice(ws: WebSocket):
    await ws.accept()
    if _AUTH_OK and scout_auth and scout_auth.AUTH_ENABLED:
        if scout_auth.require_ws_auth(ws) is None:
            await ws.send_text(json.dumps({"type": "error", "error": "auth required"}))
            await ws.close(code=4401)
            return
    loop = asyncio.get_event_loop()
    in_q: "asyncio.Queue[bytes]" = asyncio.Queue()
    stop_evt = threading.Event()

    try:
        from voice_agent import build_voice_agent
        from strands.experimental.bidi.types.events import (
            BidiAudioInputEvent, BidiAudioStreamEvent,
        )
    except Exception as e:
        await ws.send_text(json.dumps({"type": "error", "error": f"voice deps: {e}"}))
        await ws.close()
        return

    provider = os.getenv("VOICE_PROVIDER", "openai")
    voice = os.getenv("VOICE_NAME") or None

    # Browser-backed audio IO adapters
    class _BrowserInput:
        async def start(self, agent):
            self._cfg = agent.model.config["audio"]
        async def stop(self):
            pass
        async def __call__(self):
            data = await in_q.get()
            return BidiAudioInputEvent(
                audio=base64.b64encode(data).decode(),
                channels=self._cfg.get("channels", 1),
                format=self._cfg.get("format", "pcm"),
                sample_rate=self._cfg.get("input_rate", 16000),
            )

    class _BrowserOutput:
        async def start(self, agent):
            self._rate = agent.model.config["audio"]["output_rate"]
            await ws.send_text(json.dumps({"type": "voice_meta", "rate": self._rate}))
        async def stop(self):
            pass
        async def __call__(self, event):
            if isinstance(event, BidiAudioStreamEvent):
                await ws.send_text(json.dumps({
                    "type": "audio", "data": event["audio"],
                }))

    agent, _ = build_voice_agent(provider, voice, audio="laptop")
    bin_, bout = _BrowserInput(), _BrowserOutput()

    async def _reader():
        """Read PCM16 from browser → in_q."""
        while not stop_evt.is_set():
            try:
                raw = await ws.receive()
            except WebSocketDisconnect:
                break
            if "bytes" in raw and raw["bytes"] is not None:
                await in_q.put(raw["bytes"])
            elif "text" in raw and raw["text"]:
                try:
                    m = json.loads(raw["text"])
                    if m.get("type") == "stop":
                        break
                except Exception:
                    pass
        stop_evt.set()

    reader_task = asyncio.create_task(_reader())
    try:
        runner = asyncio.create_task(
            agent.run(inputs=[bin_], outputs=[bout])
        )
        await stop_evt.wait()
        runner.cancel()
    except Exception as e:
        try:
            await ws.send_text(json.dumps({"type": "error", "error": str(e)}))
        except Exception:
            pass
    finally:
        reader_task.cancel()
        try:
            await ws.close()
        except Exception:
            pass


# Static dashboard
if DOCS.exists():
    app.mount("/css", StaticFiles(directory=str(DOCS / "css")), name="css")
    app.mount("/js", StaticFiles(directory=str(DOCS / "js")), name="js")


@app.get("/replay")
async def replay_page():
    pg = DOCS / "replay.html"
    if pg.exists():
        return FileResponse(str(pg))
    return JSONResponse({"error": "docs/replay.html not found"}, status_code=404)


@app.get("/ca")
async def ca_cert():
    """Serve the mkcert root CA so iOS/Android can trust scout's HTTPS.
    Returns 404 in self-signed mode (nothing to install — just click through)."""
    try:
        import tls as scout_tls
        ca = scout_tls.mkcert_ca_path()
        if not ca:
            return JSONResponse(
                {"error": "no mkcert CA (self-signed mode). Accept the browser warning instead, "
                          "or run mkcert + DASH_TLS_MKCERT=auto."},
                status_code=404,
            )
        # iOS wants the profile downloaded; .crt/.pem with the right mime triggers install
        return FileResponse(
            ca,
            media_type="application/x-x509-ca-cert",
            filename="scout-rootCA.crt",
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/trust")
async def trust_page(request: Request):
    """On-device CA-trust helper: QR to /ca + iOS/Android step-by-step."""
    try:
        import tls as scout_tls, field_setup, base64 as _b64
        ca = scout_tls.mkcert_ca_path()
        host = request.headers.get("host", "localhost")
        scheme = "https" if request.url.scheme == "https" else "http"
        ca_url = f"{scheme}://{host}/ca"
        out_dir = Path(os.getenv("DASH_TLS_DIR", "./.scout_tls")).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        png = out_dir / "ca_qr.png"
        field_setup.png_qr(ca_url, png)
        qr_b64 = _b64.b64encode(png.read_bytes()).decode()
        selfsigned = ca is None
        note = (
            "<p class='warn'>⚠️ This rover is in <b>self-signed</b> mode — there's no CA to "
            "install. Just tap <b>Advanced → Proceed</b> on the browser warning when you open "
            "the dashboard. To remove warnings entirely, run mkcert on the rover host "
            "(<code>make mkcert-install</code>).</p>"
            if selfsigned else ""
        )
        html = _TRUST_HTML.format(
            qr_b64=qr_b64, ca_url=ca_url,
            mode=("self-signed (no CA)" if selfsigned else "mkcert trusted CA"),
            selfsigned_note=note,
        )
        return HTMLResponse(html)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/field-card")
async def field_card():
    """Printable QR field-setup card (public — it's just the access URL).
    Regenerated on each request so it always reflects the live host/port."""
    try:
        import field_setup
        out_dir = Path(os.getenv("DASH_TLS_DIR", "./.scout_tls"))
        url = field_setup.default_url()
        bootstrap = os.getenv("SCOUT_AUTH_BOOTSTRAP_TOKEN", "")
        trusted = os.getenv("DASH_TLS_MKCERT", "auto").lower() not in ("0", "false", "no", "off")
        res = field_setup.build(url, out_dir.resolve(), bootstrap=bootstrap, trusted=trusted)
        return FileResponse(res["html"], media_type="text/html")
    except Exception as e:
        return JSONResponse({"error": f"field card unavailable: {e}"}, status_code=500)


@app.get("/")
async def index():
    idx = DOCS / "index.html"
    if idx.exists():
        return FileResponse(str(idx))
    return JSONResponse({"error": "docs/index.html not found"}, status_code=404)


if __name__ == "__main__":
    import uvicorn

    # 🔒 TLS for WebAuthn over LAN/IP. Passkeys require a secure context (HTTPS
    # or http://localhost); plain http://<ip>:<port> blocks the ceremony. When
    # DASH_TLS=true we serve HTTPS with an auto self-signed cert (or a supplied
    # DASH_TLS_CERT/KEY). See tls.py.
    ssl_kwargs = {}
    scheme = "http"
    try:
        import tls as scout_tls
        cert_key = scout_tls.ensure_cert()
        if cert_key:
            ssl_kwargs = {"ssl_certfile": cert_key[0], "ssl_keyfile": cert_key[1]}
            scheme = "https"
    except Exception as _e:
        print(f"⚠️  TLS setup skipped: {_e}", flush=True)

    if scheme == "https":
        try:
            trusted = scout_tls.mkcert_ca_path() is not None
            for u in scout_tls.access_urls(DASH_PORT):
                print(f"🛞 scout dashboard → {u}  (SDK: {ROVER_SDK_URL})")
            if trusted:
                print("   ↑ mkcert cert: NO warning on machines that trust the scout CA.")
                print(f"   ↑ phones (iOS/Android): open https://<host>:{DASH_PORT}/trust to install the CA.")
            else:
                print("   ↑ self-signed cert: accept the one-time browser warning (Advanced → Proceed).")
                print(f"   ↑ phones: open https://<host>:{DASH_PORT}/trust for CA-trust help.")
            print("   ↑ passkeys/WebAuthn require this HTTPS origin to work over LAN/IP.")
            print(f"   🎫 field card: https://<host>:{DASH_PORT}/field-card")
        except Exception:
            print(f"🛞 scout dashboard → https://localhost:{DASH_PORT}  (SDK: {ROVER_SDK_URL})")
    else:
        print(f"🛞 scout dashboard → http://localhost:{DASH_PORT}  (SDK: {ROVER_SDK_URL})")
        print("   ↑ HTTP: passkeys only work on localhost. For LAN/IP access set DASH_TLS=true.")

    # 📡 Advertise scout.local on the LAN so phones/laptops resolve a hostname
    # (WebAuthn rpId needs a name, not a raw IP). Best-effort.
    try:
        import scout_mdns
        scout_mdns.start()
    except Exception as _e:
        print(f"⚠️  mDNS skipped: {_e}", flush=True)

    uvicorn.run(app, host=DASH_HOST, port=DASH_PORT, log_level="info", **ssl_kwargs)
