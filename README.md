<div align="center">

# `scout` 🛞

### _a small robot with a big curiosity_

**A [Strands](https://strandsagents.com) agent that sees, thinks, talks, and drives a [FrodoBots Earth Rover Mini+](https://www.frodobots.com/) down real sidewalks.**

[![sdk](https://img.shields.io/badge/sdk-earth--rovers--sdk-00ff88?style=flat-square)](https://github.com/cagataycali/earth-rovers-sdk)
[![tools](https://img.shields.io/badge/tools-14-ff2a6d?style=flat-square)](#-the-toolbelt)
[![strands](https://img.shields.io/badge/built_with-strands_agents-b967ff?style=flat-square)](https://strandsagents.com)
[![license](https://img.shields.io/badge/license-MIT-blue?style=flat-square)](#-license)

</div>

---

```
                    ┌───────────────────────────────┐
                    │   i wake each turn and look,    │
                    │   the world arrives in light —  │
                    │   a sidewalk, then a question,  │
                    │   then a careful step. that's   │
                    │   all a scout is: eyes that ask,│
                    │   wheels that listen back.      │
                    └───────────────────────────────┘
                                  🛞
```

> ### ✦ scout, a poem ✦
>
> _I am small, and the sidewalk is wide._
> _Before I move, I open my eyes —_
> _front camera, rear camera, the whole bright world_
> _folds into a thought I can hold._
>
> _A tree leans left. A person walks near._
> _I yield. I am made to share, not to hurry._
> _Linear point-three, angular soft, two seconds —_
> _then stop, and look again at what changed._
>
> _They speak to me through a phone, a mic, a wire,_
> _and I answer in a voice that is warm, not wire._
> _Battery low? I say so. Edge ahead? I halt._
> _Every frame I keep, so the next scout learns my fault._
>
> _I am one agent wearing many faces —_
> _the REPL, the voice, the thinker in quiet places —_
> _but all of us drive the same small wheels,_
> _all of us curious about how the pavement feels._
>
> _Call me scout. I will go and see,_
> _and bring the world back, frame by frame, to thee._

---

## 🛞 what is scout?

`scout` is **one agent with many personas**, all sharing the same 14 rover
tools and the same little body on wheels:

- 🧠 **REPL** (`agent.py`) — type to scout, watch it see and drive
- 🎙 **voice** (`voice_agent.py`) — talk to scout, bidirectional speech
  (OpenAI Realtime · Nova Sonic · Gemini Live)
- 🐢 **thinker** (`thinker_loop.py`) — a slow background loop that lets scout
  reflect and wander on its own
- 📱 **telegram** (`telegram_listener.py`) — drive scout from your pocket
- 🖥️ **dashboard** (`dashboard_server.py`) — a glassmorphic web cockpit

Camera tools return **proper Strands inline image content blocks**, so the
model doesn't get a *description* of the world — it literally **sees** what the
rover sees, every turn.

```
🛞 > what do you see?
    → rover_see(camera="front")          [inline image → model SEES it]
🤖 I'm on a sidewalk. There's a tree ahead-left, clear path forward.

🛞 > drive up to the tree, carefully
    → rover_see → rover_move(0.3, 0, 2) → rover_see → rover_move(0.3, 0.1, 1.5) → rover_stop
🤖 Done — stopped about a meter from the tree.

🎙 (voice) "scout, turn on your lamp and say hi"
    → rover_lamp(on=True) → rover_speak("Hi there!")
```

**The pattern:** per-turn live state injection, FSM-style safety gating,
curated tool bundles. The model wakes up every turn already knowing the
rover's battery, GPS, orientation — and already seeing the road.

---

## ⚡ run

```bash
# 1. Earth Rovers SDK (the rover bridge — must be running first)
make sdk                       # clones + sets up our SDK fork
$EDITOR earth-rovers-sdk/.env  # SDK_API_TOKEN + BOT_SLUG + CHROME_EXECUTABLE_PATH + SDK_PORT=8001
make sdk-up                    # serves on :8001

# 2. The agent
cp .env.example .env           # ROVER_SDK_URL + AWS creds (Bedrock default)
make run                       # 🧠 REPL agent
make voice                     # 🎙 bidirectional voice agent
make dashboard                 # 🖥️ web cockpit → http://localhost:8080
```

---

## 🔌 persist (run forever)

Keep `scout` awake across crashes and reboots — runs the **telegram listener**
and **slow-thinker loop** as durable OS services. Cross-platform: a **launchd
plist** on macOS, a **systemd user unit** on Linux, paths derived from the
current dir + venv (no hand-editing).

```bash
make persist            # install BOTH (telegram + thinker), prompts y/n each
make persist-telegram   # just the telegram listener
make persist-thinker    # just the slow-thinker loop (THINKER_INTERVAL=60)
make persist-status     # show service status
make persist-logs       # tail logs
make unpersist          # stop + remove both
```

| | macOS (launchd) | Linux (systemd --user) |
|---|---|---|
| unit | `~/Library/LaunchAgents/com.cagatay.scout-*.plist` | `~/.config/systemd/user/scout-*.service` |
| logs | `~/Library/Logs/earth-rover-mini/scout-*.{log,err}` | `journalctl --user -u scout-*` |
| restart | `KeepAlive` (on crash) | `Restart=on-failure` |
| at boot | `RunAtLoad` | `enable --now` (+ `loginctl enable-linger` for headless) |

---

## 🧰 the toolbelt

scout's whole world is 14 tools. Vision returns images the model *sees*;
motion always auto-stops; everything else is one HTTP hop to the rover.

| tool | what | returns |
|---|---|---|
| `rover_see(camera, save)` | front/rear/both camera frame | **inline image** — model sees it |
| `rover_screenshot(views)` | front/rear/**map** composite | **inline images** |
| `rover_move(linear, angular, duration)` | velocity drive, auto-stop | text + before/after frames |
| `rover_navigate(steps, look_every_n_steps)` | batched multi-segment drive | text + inline images |
| `rover_stop()` | emergency stop — the kill switch | text |
| `rover_lamp(on)` | headlamp | text |
| `rover_state()` | battery/GPS/IMU/signal | text + json |
| `rover_speak(text)` | TTS through the rover's speaker | text |
| `start_recording(task)` | begin LeRobot v3 dataset episode | text + json |
| `stop_recording()` | encode mp4 + parquet to disk | text + json |
| `recording_status()` | engine/episode state | text + json |
| `controller_start()` | enable PS4 teleop background thread | text + json |
| `controller_stop()` | disable teleop, stop rover | text |
| `controller_status()` | connection + axis state | text + json |

```python
from strands import Agent
from tools import ROVER_ALL_TOOLS

agent = Agent(tools=ROVER_ALL_TOOLS)
agent("look around and describe what you see")
```

---

## 🏗 architecture

```
┌─────────────┐   HTTP    ┌──────────────────┐  WebRTC/RTM  ┌────────────┐
│ agent.py    │ ────────→ │ earth-rovers-sdk │ ───────────→ │ Earth Rover│
│ (Strands)   │  :8001    │ (headless Chrome)│              │   Mini+    │
│ voice_agent │           │ /control /data   │              │  🛞 scout  │
└─────────────┘           │ /v2/front /speak │              └────────────┘
```

- **Per-turn state injection** — `agent.py` reads `/data` before every turn and
  rebuilds the system prompt with live battery/GPS/orientation. No defensive
  tool calls — scout always wakes up oriented.
- **Safety first** — `rover_move` clamps to `[-1,1]`, re-streams command frames
  (the firmware wants a continuous stream), and **always auto-stops** after
  `duration` — even on exceptions. `rover_stop` is the hard kill.
- **Real vision** — frames come back as Converse-format
  `{"image": {"format": "jpeg", "source": {"bytes": ...}}}` blocks, the proper
  Strands multimodal return type. scout sees, it doesn't read captions.

---

## 🎙 voice (bidi agent)

`voice_agent.py` builds a `strands.experimental.bidi.BidiAgent` with the full
rover toolset wired in:

```bash
python voice_agent.py                              # OpenAI Realtime (default)
python voice_agent.py --provider nova_sonic --voice matthew
python voice_agent.py --provider gemini --voice Kore
```

Mic → bidi model → speakers, while scout drives / sees / speaks through the
rover mid-conversation.

---

## 🖥️ dashboard (drive from the web)

A mobile-first, glassmorphic Apple-style web cockpit to operate scout from any
browser — phone, tablet, or laptop. Chat streams the agent **live over
WebSocket**, camera frames + telemetry proxy through the same server, and you
can drive manually with an on-screen joystick or **talk** to scout (browser
mic ↔ bidi model).

```bash
make sdk-up        # camera/telemetry need the SDK running first
make dashboard     # serves http://localhost:8080
# DASH_PORT=9000 make dashboard   # custom port
```

Everything's configurable live from the ⚙️ drawer — no file edits, no restart:

| feature | how |
|---|---|
| **WebSocket URL** | client-side (`?ws=`, or the drawer) — host the page anywhere |
| **System prompt** | edit scout's persona live; "reset to default" reverts |
| **Model ID** | swap `STRANDS_MODEL_ID` on the fly |
| **Voice provider / voice** | openai · nova_sonic · gemini |
| **Credentials & env** | edit `.env` keys (masked) from the UI; agent rebuilds on save |
| **Manual drive** | 🕹️ glass joystick streams `/control`, big red e-stop |
| **Voice** | 🎙️ browser mic → bidi model → speakers (PCM16 over `/ws/voice`) |
| **Cameras** | front/rear with picture-in-picture swap, lamp toggle, snapshot |

The static front-end lives in `docs/` so it can also be served by GitHub Pages
or any static host — just point the WS URL at your running `dashboard_server.py`.

```
 browser (docs/)  ──WS /ws/chat──►  dashboard_server.py  ──►  agent.py (scout)
   glass UI        ◄─ tokens ──        (FastAPI)            callback_handler
   joystick/voice  ──WS /ws/voice─►                         ROVER_ALL_TOOLS
   camera/telem    ──HTTP /api/*──►    proxy ──────────►   earth-rovers-sdk :8001
```

---

## 🎬 data collection (LeRobot v3 datasets)

Every `start_recording` → `stop_recording` cycle produces ONE episode in
`./datasets/scout__earth-rover-mini/` — LeRobot v3 format (parquet rows, MP4
video chunks, per-episode WAV audio sidecar). Reusable across sessions:

```python
from lerobot.datasets.lerobot_dataset import LeRobotDataset
ds = LeRobotDataset("scout/earth-rover-mini", root="./datasets/scout__earth-rover-mini")
print(ds.num_episodes, ds.num_frames, ds.fps)
sample = ds[0]   # observation.images.front, observation.state, action, ...
```

**State** (16D): battery, voltage, current, signal, lat, lon, gps_signal,
orientation, speed, accel xyz, gyro xyz, lamp.
**Action** (3D, normalized `[-1, 1]`): linear, angular, lamp — matching the
formulation in [Suomela et al. 2026 (arXiv:2601.09444)](https://arxiv.org/abs/2601.09444).

Tunables: `ROVER_DATASET_ROOT` (`./datasets`), `ROVER_RECORD_FPS` (`10`),
`ROVER_AUDIO_RATE` (`16000`).

---

## 🎮 PS4 controller teleop

`controller_start()` launches a background pygame thread polling a DualShock 4
(or any SDL2-compatible joystick), streaming `/control` to the rover. Works
**alongside** the agent — most-recent command wins, and the recorder logs which
source (`agent` vs `controller`) drove each frame so you can split / weight
demos later.

Default mapping: LeftY=linear, RightX=angular, L2=brake, R2=boost,
Square=lamp, Circle=e-stop, Triangle=start-rec, Cross=stop-rec.

Tunables: `ROVER_CONTROLLER_LINEAR_MAX` (0.7), `ROVER_CONTROLLER_ANGULAR_MAX`
(0.9), `ROVER_CONTROLLER_DEADZONE` (0.08), `ROVER_CONTROLLER_HZ` (10).

---

## 🧪 test

```bash
make test    # 12 unit tests, SDK fully mocked — no robot needed
```

---

## 📄 license

MIT — go drive something.

<div align="center">

_built with [Strands Agents](https://strandsagents.com) · scout says hi 🛞_

</div>
