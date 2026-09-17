---
title: scout
description: "A Strands agent that sees, thinks, talks and drives a FrodoBots Earth Rover Mini+ — every drive recorded as a LeRobot dataset."
hide:
  - toc
---

<div class="rh-hero" markdown>

<p class="rh-eyebrow">🛞 <b>scout</b> · a small robot with a big curiosity</p>

# sees · thinks · drives · remembers

<p class="rh-lead"><strong>scout</strong> is a <a href="https://strandsagents.com">Strands</a> agent living on a Jetson AGX Thor that drives a
<a href="https://www.frodobots.com/">FrodoBots Earth Rover Mini+</a>. It looks through the rover's two cameras, talks through its speaker,
listens on its mic, takes orders from a passkey-gated web cockpit, Telegram or your voice — and <em>records every drive</em>
as a LeRobot dataset with the agent's own reasoning aligned frame-by-frame.</p>

<div class="scout-badges" markdown>
<a href="https://github.com/cagataycali/scout-the-rover/actions/workflows/docs.yml"><img alt="docs" src="https://github.com/cagataycali/scout-the-rover/actions/workflows/docs.yml/badge.svg"></a>
<a href="https://github.com/cagataycali/scout-the-rover/commits/main"><img alt="last commit" src="https://img.shields.io/github/last-commit/cagataycali/scout-the-rover?style=flat-square&color=2ee6d6"></a>
<a href="https://github.com/cagataycali/scout-the-rover/blob/main/LICENSE"><img alt="license" src="https://img.shields.io/badge/license-MIT-16b9c4?style=flat-square"></a>
<a href="https://huggingface.co/datasets/cagataydev/scout-earthrover-ecot"><img alt="dataset" src="https://img.shields.io/badge/🤗_dataset-scout--earthrover--ecot-ffc24a?style=flat-square"></a>
<a href="https://strandsagents.com"><img alt="strands" src="https://img.shields.io/badge/built_with-strands_agents-30d1a8?style=flat-square"></a>
</div>

<div class="scout-3d-slot" data-scout-3d="assets/models/scout.glb" data-alt="a stylised low-poly scout rover, spinning"><div class="scout-3d"></div></div>
<p style="text-align:center;font-size:.7rem;opacity:.7;margin-top:.3rem">a stylised stand-in, drawn from primitives — the real
Earth Rover Mini+ CAD is linked on the <a href="hardware/">hardware page</a> (not redistributed: no license on the source repo)</p>

<div class="buttons" markdown>
[First drive in 60 s](start/first-drive.md){ .md-button .md-button--primary }
[Open the cockpit](dashboard.md){ .md-button }
[Tools reference](reference/tools/index.md){ .md-button }
</div>

</div>

## What is scout, in five bullets

- **One agent, many faces.** The same Strands agent answers in the REPL, the web cockpit's Ask box, Telegram, a bidirectional
  voice session over the rover's own mic and speaker, and a slow background *thinker* that wakes every minute to do one useful thing.
  → [Personas](personas.md)
- **Real vision, real safety.** Camera tools return actual image blocks the model sees; every motion tool clamps to `[-1, 1]`,
  re-streams `/control` frames (the firmware wants a continuous stream) and **always auto-stops** — even on exceptions.
  → [Tools](reference/tools/index.md)
- **A cockpit you can trust from anywhere.** FastAPI + vanilla JS PWA, sealed behind WebAuthn passkeys and HTTPS, exposed through a
  Cloudflare Tunnel. Joystick, cameras, live agent stream, persona switches, dataset replay. → [Dashboard](dashboard.md)
- **Every drive is data.** A LeRobot v3 recorder captures 10 Hz video + 27-D state + 2-D action, with the agent's reasoning trace
  as an Embodied-Chain-of-Thought sidecar and YOLO detections aligned to the same frames. → [Datasets](datasets.md)
- **Fleet-aware.** With `TINY_MCP=1` the agents mount tiny.technology's MCP tools and can ask sibling robots (Reachy, Fomo, Q…)
  for help — allow-listed, self-invoke refused, one hop deep. → [Fleet](fleet.md)

## How it fits together

```mermaid
flowchart LR
    subgraph clients["🌐 clients"]
        PWA[📱 cockpit PWA]; TG[✈ Telegram]; MIC[🎙 voice]; REPL[🧠 REPL / MCP]
    end
    subgraph edge["🛡 edge"]
        CF[☁️ cloudflared tunnel]; PK[🔐 passkey session]
    end
    subgraph thor["🧠 Jetson AGX Thor · docker compose slim"]
        DASH["dashboard :8080 TLS<br/>FastAPI · agent · replay · personas"]
        AG[strands agent<br/>ROVER_ALL_TOOLS]
        SDK["earth-rovers-sdk :8002<br/>headless Chromium · Agora"]
        HUB["media hub :8090<br/>one drainer, N subscribers"]
        YOLO[yolo detector]
        THK[🐢 thinker]; TEL[telegram listener]; VOX[voice agent]
        SUP["scout-supervisor<br/>(host, unix socket)"]
        REC[(LeRobot v3 datasets<br/>+ ECoT + detections)]
        MEM[(.memory SQLite)]
    end
    ROVER((🛞 Earth Rover Mini+))
    PWA -. HTTPS / WS .-> CF --> PK --> DASH
    TG --> TEL --> AG; MIC --> VOX --> AG; REPL --> AG; DASH --> AG
    AG <--> SDK; HUB --> SDK; YOLO --> HUB; VOX --> HUB
    AG --> REC; YOLO --> REC; AG <--> MEM
    DASH -. start/stop .-> SUP -. docker compose .-> THK & TEL & VOX
    SDK <==>|WebRTC| ROVER
```

Ports and services come straight from `docker-compose.slim.yml` + `docker-compose.slim.override.yml` — see [Architecture](guide/architecture.md).

<div class="scout-ports" markdown>
<div><b>sdk :8002</b>earth-rovers-sdk — headless Chromium joined to the rover's Agora channel; `/control` `/data` `/v2/front` `/speak`</div>
<div><b>dashboard :8080</b>FastAPI cockpit (TLS) — agent, cameras, joystick, personas, replay; the only thing the tunnel exposes</div>
<div><b>media :8090</b>MediaHub — single drainer for mic + cameras, fan-out to recorder / YOLO / voice</div>
<div><b>yolo</b>Ultralytics on the hub's front stream → `detections/episode_N.jsonl` sidecars</div>
<div><b>telegram · thinker · voice</b>persona containers (profiles), switched live from the cockpit via the host supervisor</div>
<div><b>cloudflared</b>public hostname → `https://localhost:8080`, real TLS at the edge</div>
</div>

## Where to next

<div class="grid cards" markdown>

-   :material-rocket-launch:{ .lg .middle } **Start**

    ---

    What you need, installing on the brain, docker compose slim, the `.env` walkthrough, first drive.

    [:octicons-arrow-right-24: Start here](start/hardware.md)

-   :material-monitor-dashboard:{ .lg .middle } **Dashboard**

    ---

    Passkey gate, camera tiles, glass joystick, Ask stream, persona pills, dataset replay.

    [:octicons-arrow-right-24: The cockpit](dashboard.md)

-   :material-eye:{ .lg .middle } **Perception**

    ---

    MediaHub fan-out, YOLO sidecars, the Locate-Anything "who to follow" idea.

    [:octicons-arrow-right-24: Perception](perception.md)

-   :material-database:{ .lg .middle } **Datasets & replay**

    ---

    LeRobot v3, ECoT traces, why episodes are sealed, how to repair an index.

    [:octicons-arrow-right-24: Datasets](datasets.md)

-   :material-tools:{ .lg .middle } **Tools reference**

    ---

    Every `@tool`, generated from the code at build time — signatures and docstrings.

    [:octicons-arrow-right-24: Reference](reference/tools/index.md)

-   :material-wrench:{ .lg .middle } **Operations**

    ---

    Bring-up order, restart recipes, tunnel, watchdogs, what to check before a demo.

    [:octicons-arrow-right-24: Runbook](guide/operations.md)

</div>
