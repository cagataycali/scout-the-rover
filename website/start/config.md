---
title: Configuration (.env)
description: "A walkthrough of .env.example — what each block does and which service reads it. No values, only names."
---

# Configuration — the `.env` walkthrough

`cp .env.example .env` and fill it in. The file is `.gitignore`d; **never** commit it, never paste it into a gist
(GitHub secret scanning revokes OpenAI keys within minutes — we learned this the hard way). The names below are the ones
`.env.example` ships; the [reference](../reference/env.md) lists all 150+ variables the code can read.

## Rover link

| variable | what |
|---|---|
| `ROVER_SDK_URL` | where the agent finds the Earth Rovers SDK. `http://localhost:8002` locally; compose overrides it to `http://sdk:8002`. |
| `SDK_API_TOKEN`, `BOT_SLUG` | your FrodoBots credentials and the rover's slug — used by the SDK container (`earth-rovers-sdk/.env` locally). |
| `CHROME_EXECUTABLE_PATH` | the browser the SDK drives headlessly; `/usr/bin/chromium` in the image. |
| `MAP_ZOOM_LEVEL`, `IMAGE_QUALITY`, `IMAGE_FORMAT` | SDK frame settings — `jpeg` at `0.8` is the bandwidth-friendly default. |

## Model

| variable | what |
|---|---|
| `STRANDS_MODEL_ID` | the Bedrock model id every text persona uses (the cockpit's ⚙️ drawer can swap it live). |
| `STRANDS_MAX_TOKENS` | max output tokens per turn. |
| `AWS_BEARER_TOKEN_BEDROCK` *(optional)* | otherwise the standard AWS credential chain (`AWS_REGION` etc.). |
| `OPENAI_API_KEY` | the OpenAI Realtime voice provider (and any OpenAI text model). |
| `GOOGLE_API_KEY` / `GEMINI_API_KEY` | Gemini Live voice. |

## Voice

| variable | what |
|---|---|
| `VOICE_PROVIDER` | `openai` · `nova_sonic` · `gemini` |
| `VOICE_MODEL` | e.g. `gpt-realtime-2` |
| `VOICE_NAME` | provider voice — `marin`, `shimmer`, `matthew`, `Kore`… |
| `TTS_PROVIDER`, `TTS_API_KEY`, `TTS_VOICE` | the SDK-side text-to-speech behind `/speak` (`edge` is free). |

## Telegram

`SCOUT_TELEGRAM_BOT_TOKEN`, `SCOUT_TELEGRAM_DEFAULT_CHAT_ID`, `SCOUT_TELEGRAM_ALLOWED_USERS` (comma-separated user ids —
only these can command the rover), `SCOUT_TELEGRAM_HISTORY_LIMIT`.

## Datasets

`ROVER_REPO_ID` pins one growing dataset; unset → one dataset **per day per persona** (`scout__earth-rover-mini-YYYYMMDD/<persona>`).
`ROVER_DATASET_ROOT` (default `datasets`). Recording knobs — `ROVER_RECORD_FPS` 10, `SCOUT_AUTO_RECORD`, `SCOUT_SEAL_EPISODES`,
`SCOUT_VCODEC` — are covered in [Datasets](../datasets.md).

## Cockpit & auth (compose sets sane defaults)

`DASH_PORT` 8080 · `DASH_TLS` true · `DASH_TLS_HOSTS` extra SANs · `SCOUT_AUTH_ENABLED` true · `SCOUT_AUTH_RP_ID` /
`SCOUT_AUTH_ORIGIN` (the public hostname when behind a tunnel) · `SCOUT_AUTH_BOOTSTRAP_TOKEN` (one-time enrol token;
without it the **first** passkey enrols freely, then the door closes) · `SCOUT_PUBLIC_URL` (printed on the field card / QR) ·
`SCOUT_MDNS_NAME`. Details in [Auth](../guide/auth.md).

## Personas

`THINKER_INTERVAL` (s), `THINKER_TELEGRAM_CHAT_ID`, `SCOUT_THINKER_DRIVE` (fallback when `.memory/personas.json` has no flag),
`SCOUT_SUPERVISOR_TOKEN` (shared secret between the dashboard and the host supervisor), `SCOUT_BRIEFING_MUTE_SOURCES`
(which personas' briefings the voice agent should **not** speak aloud; default `thinker`).

## Fleet (separate file)

`.tiny-mcp.env` — `TINY_MCP=1`, `TINY_TOKEN`, `TINY_MCP_PERSONAS`, `TINY_SELF_DEVICE_IDS`. Only the agent containers read it.
→ [Fleet](../fleet.md)

!!! danger "Secrets hygiene"
    `.env`, `.tiny-mcp.env`, `earth-rovers-sdk/`, `*.creds.json`, `.scout_auth.json`, `.supervisor/` and `notes.md` are all
    in `.gitignore`. Keep them there. Rotate any key that ever touched a public place.
