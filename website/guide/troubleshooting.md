---
title: Troubleshooting
description: "Symptoms → causes → fixes, learned in the field."
---

# Troubleshooting

## Cameras

**`/v2/front` → 404 "Front frame not available"; `/data` fine.**
The SDK's headless Chromium is a participant in the rover's Agora channel. When the rover reboots, Agora fires
`user-unpublished`, the page removes the `#player-1000/1001` video elements and — in the upstream SDK — nothing recreates
them. Our fork adds `is_ready()/video_alive()/rejoin()` and a watchdog loop (`VIDEO_WATCHDOG=true`, 5 s poll, 6 misses → fresh
tokens + rejoin, backoff 30→300 s). If you run the upstream SDK, `restart sdk` is the manual equivalent.

**Frames are black / tiny.** `ROVER_REJECT_BLACK_FRAMES`, `ROVER_BLACK_FRAME_STD`, `ROVER_MIN_FRAME_DIM` govern what the recorder
keeps; the SDK's `IMAGE_QUALITY`/`IMAGE_FORMAT` govern what it sends.

## Driving

**`rover_move` returns but nothing happens.** The firmware needs a *continuous* `/control` stream; the tools re-send frames for
the whole duration. If a single frame is sent (e.g. from curl) the rover twitches and stops — that's expected.

**Turns go the wrong way.** `ROVER_TURN_SIGN` / `DASH_TURN_SIGN` flip the angular sign for the tools / the cockpit joystick.

**The thinker never moves.** Its drive flag is off (`.memory/personas.json` → `thinker_drive`, or `SCOUT_THINKER_DRIVE`). This is a
feature — flip it in the cockpit's persona sheet.

## Auth

**"Passkey not allowed" / ceremony fails.** WebAuthn's relying-party id must be a **hostname** and the page must be HTTPS. Use
`scout.local` (mDNS), an `/etc/hosts` name, or the tunnel hostname; set `SCOUT_AUTH_RP_ID` and `SCOUT_AUTH_ORIGIN` explicitly when
behind a proxy.

**Locked out.** The store is `auth.json` inside the `scout-auth-slim` volume. Remove the volume and the first visitor re-enrols
(set `SCOUT_AUTH_BOOTSTRAP_TOKEN` first if the rover is reachable from the internet).

**401 on everything from a script.** Mint a session with the auth module inside the dashboard container and send it as a Bearer:

```bash
TOKEN=$(docker exec scout-slim-dashboard python3 -c "import auth; print(auth.issue_token('cli','ops'))")
curl -sk -H "Authorization: Bearer $TOKEN" https://localhost:8080/api/telemetry
```

## Voice

**`401 invalid_api_key` in the voice logs.** The provider key was revoked (GitHub secret scanning does this to any key that
touches a public place — or a "secret" gist). Rotate it in `.env`, `--force-recreate voice`.

**Two things fighting over the mic.** Only one voice persona; the listener and recorder must go through the MediaHub. Check
`scout-compose ps` for a stray `voice` container and `curl :8090/status`.

**Robot interrupts itself.** Raise `ROVER_GATE_TAIL_S` (default 0.6 s) or tune `ROVER_AEC_DELAY_MS`.

## Datasets

**Every episode replays the same video.** lerobot 0.6 streams parquet and only writes the footer on `finalize()`. A recorder
killed mid-session left the index footerless. Recent versions seal after every episode; for old datasets run the repair
(`tools/repair_episode_index.py --scan datasets`, see [Datasets](../datasets.md)).

**"vcodec" TypeError / no dataset created.** lerobot ≥ 0.6 moved video encoding to `RGBEncoderConfig`; `_recorder_engine` picks
the right call per version (`SCOUT_VCODEC`, default `h264`). Make sure `lerobot[dataset]` (the extra) is installed.

**torchcodec warnings.** Harmless: it falls back to pyav.

## Fleet

**No `use_device` in the agent.** `TINY_MCP=1` set? persona in `TINY_MCP_PERSONAS`? `.tiny-mcp.env` present and mounted?
The bridge is fail-open: missing node/token/server → one warning, tools absent, persona still runs. The turn arrived from a
sibling (`/api/chat`)? Then no fleet tools *by design*.

## Containers

**Stopping the thinker truncated a parquet.** Use the cockpit switch (checked between cycles) rather than `docker rm -sf` while
an episode is open. Only the in-flight episode is lost since sealing.

**`docker compose` says "no such service" for `media`/`yolo`.** You forgot the override file. Use the `scout-compose` alias.

**Ad-hoc commands in the image.** The entrypoint dispatches on the service name, so for a shell use
`docker run --rm -it --entrypoint bash scout:slim` or `docker exec -it scout-slim-dashboard bash`.
