# Personas — who is running on Scout, and how the owner switches them

Scout is several agents sharing one rover. The dashboard's **Personas** control
(topbar pills + the ⚙️ sheet) lets the owner turn each one ON/OFF live, from a
phone, without SSH or `docker compose`.

| persona | kind | what it is | ON means | OFF means |
|---|---|---|---|---|
| 🎙 **rover-voice** | container `scout-slim-voice` | `voice_agent.py --audio rover`: bidirectional Realtime voice agent on the **rover's own mic + speaker** (`/rover-mic/*`, `/rover-speaker/*` on the SDK) | container running, model session open | container **removed** (`compose rm -sf`) — a dead bidi session never lingers |
| 🧠 **thinker** | container `scout-slim-thinker` | `thinker_loop.py`: slow-thinker that observes, explores, reports on Telegram every 60 s | running | stopped (`compose stop -t 10`) |
| 🕹 **thinker drive** | flag | may the thinker *move* the rover? | prompt says drive allowed | prompt says observe-only **and** `sdk_post("/control")` raises inside the thinker process (stop frames still pass) |
| ✈ **telegram** | container `scout-slim-telegram` | `telegram_listener.py` bot | running | stopped |
| ⏺ **rec** | flag | auto-record LeRobot episodes around agent turns (`_AutoRecorder`) | episodes saved | `begin_turn` returns early — nothing written |

The dashboard (`dashboard_server.py`) and the SDK proxy are **not** personas and
cannot be toggled from here — that is the whole point of the allow-list.

## Why two mechanisms

**Container personas** need the docker CLI, and the dashboard container has
neither docker nor the docker socket (mounting `/var/run/docker.sock` into an
internet-facing service is root on the host for anyone who finds an auth bug).
So a tiny host-side supervisor does it:

```
browser ─https─▶ dashboard (container) ─unix socket─▶ scout_supervisor.py (host, user unit)
                 /api/personas/*            /run/scout-supervisor/scout-supervisor.sock
                 (auth middleware)          fixed allow-list × {start,stop,status,logs}
                                            token header, audit log, docker compose …
```

* `deploy/scout_supervisor.py` — stdlib `http.server` on a unix socket, user
  unit `scout-supervisor.service` (WantedBy default.target, linger on). It runs
  **only** `docker compose -f docker-compose.slim.yml -f docker-compose.slim.override.yml`
  with the same files as the live stack, so a start never re-creates other
  services (`--no-deps`, and voice via `--profile voice`).
* Every request needs `X-Scout-Supervisor-Token` (`SCOUT_SUPERVISOR_TOKEN` in
  `.env`, shared with the dashboard) and is appended to `.supervisor/audit.log`
  with the requesting passkey subject (`X-Scout-Actor`).
* The socket dir `./.supervisor` is bind-mounted read/write into the dashboard
  only (`docker-compose.slim.override.yml`). Nothing else can reach it.
* Start/stop are asynchronous: the API answers `starting`/`stopping` at once;
  the UI polls `/api/personas` every 1.2 s until it settles (`running`,
  `stopped`, `missing`, `error` + `last_error`).

**Flag personas** are switches the processes already poll, so they need no
supervisor at all: `tools/persona_flags.py` reads/writes
`.memory/personas.json` (the `.memory` dir is bind-mounted into every service;
atomic tmp+rename; mtime-cached). The thinker re-reads `thinker_drive` at the
start of every cycle and the hard gate in `tools/_rover_common.sdk_post` refuses
`/control` moves while it is off (`SCOUT_PERSONA=thinker` marks the process).
`_AutoRecorder.begin_turn` checks `recording` per turn. The legacy env vars
`SCOUT_THINKER_DRIVE` / `SCOUT_AUTO_RECORD` remain the fallback when the file
has no entry, so an untouched deployment behaves exactly as before.

## API (all behind the dashboard's auth middleware — 401 without a passkey session)

```
GET  /api/personas                    → {personas:{name:{kind,label,icon,desc,state,on,last_error,…}}, supervisor:"ok"|"unavailable", voice_provider, flags_source}
POST /api/personas/{name}             body {"action":"start"|"on"|"stop"|"off"} → {ok, persona, state}   404 unknown persona · 400 unknown action · 503 supervisor down
GET  /api/personas/{name}/logs?n=20   → {lines:[…]}  (container personas only)
```

When the supervisor is down the container pills render struck-through and
disabled, the flag switches keep working, and the sheet shows the reason.

## Voice persona — what to expect

`voice_agent.py` on the rover mic streams **AEC @ 48 kHz → model @ 24 kHz** and
plays the reply on the rover speaker (echo-referenced). It uses
`VOICE_PROVIDER`/`OPENAI_API_KEY` (default openai Realtime). The browser-mic
voice (`/ws/voice`, the 🎤 button) is a separate path and unaffected by this
switch. Don't run both against the same room unless you like feedback.

## Operations

```bash
systemctl --user status scout-supervisor        # host side
tail -f ~/scout-the-rover/.supervisor/audit.log
cat ~/scout-the-rover/.memory/personas.json      # flag state
docker compose -f docker-compose.slim.yml -f docker-compose.slim.override.yml --profile all ps
```

Tests: `python3 -m pytest tests/test_personas.py` (allow-list, flag precedence,
thinker gate, state parsing — no docker needed).

## Proven 2026-09-17 (through https://scout.cagatay.my, bearer session)

* unauth → 401 on every route; unknown persona → 404; `restart` → 400
* thinker OFF → `stopping → stopped` (Exited 0) in 9 s, audit lines written; ON → running in 3 s, banner `drive=OFF` read from the flag file
* thinker drive OFF → next cycle: "No driving"; hard gate unit-tested
* voice OFF → container gone in 14 s (0 containers named voice); ON → running in 4 s, "rover mic → AEC@48000Hz → model @ 24000Hz", "model → rover speaker"
* 390 px phone: no horizontal scroll, 0 console errors; the owner toggled voice ON from the live UI two minutes after it shipped
