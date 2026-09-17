---
title: Dashboard
description: "The glass cockpit: passkey gate, camera tiles, joystick, the live Ask stream, persona pills, dataset replay — and its API."
---

# Dashboard — the glass cockpit

`dashboard_server.py` is a FastAPI app that serves a vanilla-JS PWA from `docs/` (yes, that folder — it predates this site),
proxies the rover's cameras and telemetry, hosts the agent behind a WebSocket, and owns the passkey gate. It is the **only**
service the Cloudflare tunnel exposes.

<div class="rh-shots2" markdown>
<figure markdown>
<img src="../assets/media/joystick-360.gif" alt="cockpit — joystick then a 360" loading="lazy">
<figcaption>🕹️ unlock → joystick → "do a 360"</figcaption>
</figure>
<figure markdown>
<img src="../assets/media/agent-360.gif" alt="cockpit — asking the agent for a 360, tool calls streaming" loading="lazy">
<figcaption>🤖 Ask → tool calls stream live</figcaption>
</figure>
</div>

## The gate

Everything under `/api/*` and both WebSockets require a **passkey session** (a short-lived JWT the browser gets after a
WebAuthn login). A global middleware enforces it; the public allow-list is exactly:

- `GET /api/health` — for uptime checks,
- `/auth/*` — the WebAuthn ceremony itself,
- the page shells `/`, `/replay`, `/field-card`, `/trust`, `/ca` and the static `/css`, `/js`.

Anonymous `/api/*` → **401**; anonymous WebSocket → close **4401**. Multi-admin: enrol more passkeys from the ⚙️ drawer,
rename or delete them — the last one cannot be removed. Full story in [Auth](guide/auth.md); the route-by-route table is
generated from the code in the [Dashboard API](reference/api.md).

## What's on screen

| region | what it does | wired to |
|---|---|---|
| **cameras** | big front view, small rear PiP; tap to swap; 💡 lamp; 📷 snapshot | `GET /api/frame/{front\|rear}` polled (~700 ms), `POST /api/lamp`, `GET /api/screenshot` |
| **HUD** | battery, GPS fix, signal, heading, speed, lamp | `GET /api/telemetry` (the SDK's `/data`) |
| **joystick / WASD** | continuous `/control` frames while held, e-stop on release, blur or ++space++ | `POST /api/control {linear, angular, duration}` |
| **Ask** | chat with the agent; tokens, tool receipts, reasoning stream back | `WS /ws/chat` |
| **🎙 browser voice** | your phone's mic ↔ bidi model ↔ phone speaker (PCM16 frames) | `WS /ws/voice` |
| **🎭 persona pills** | rover-voice · thinker · telegram · rec — ON/OFF live, plus 🕹 thinker-drive in the sheet | `GET/POST /api/personas[/name]` |
| **⚙️ drawer** | system prompt, model id, voice provider/name, masked `.env` keys, passkeys — saves rebuild the agent | `GET/POST /api/config`, `/auth/credentials*` |
| **🎞 replay** (`/replay`) | dataset browser: episodes, per-episode video windows, reasoning trace, memory search | `/api/replay/*` |

## The agent behind the socket

One agent instance per dashboard process, rebuilt when config changes, **one turn at a time** (`_agent_busy` lock). Each turn
the server attaches the latest front/rear frames to the prompt (`build_turn_input`), streams the Strands callback events as
`token` / `tool` / `reasoning` / `done` messages, and — if the turn issued at least one action — the auto-recorder saves it as a
dataset episode.

`POST /api/chat` is the synchronous twin of the socket, built for machines: it is what tiny.technology's endpoint proxy calls
when a *sibling* robot asks Scout something. That path builds the agent with `fleet=True`, i.e. **no fleet tools**, which is how
the one-hop depth cap is enforced. → [Fleet](fleet.md)

## Running it outside docker

```bash
make dashboard        # http://localhost:8080 (auth still on; passkeys need HTTPS → use dashboard-tls)
make dashboard-tls    # https on :8443 with a self-signed cert so WebAuthn works on the LAN
make field-card       # printable QR card with the URL + CA fingerprint for a demo venue
```

The page's WebSocket target is a parameter (`?ws=…` → localStorage → page origin), so the static shell can be hosted anywhere
and pointed at any rover.

## Keyboard cheat-sheet

| keys | action |
|---|---|
| ++w++ ++s++ / ++a++ ++d++ | forward/back · turn left/right (hold several for arcs) |
| ++space++ | stop |
| ++bracket-left++ ++bracket-right++ · ++1++–++5++ | speed down/up · presets |
| ++shift++ | turbo while held |
| ++enter++ in Ask | send |
