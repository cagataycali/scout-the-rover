---
title: Hardware & 3D files
description: "The Earth Rover Mini+ (sensors, actuators, the SDK surface), the Jetson AGX Thor brain, and where the official CAD lives — with the licensing honesty that goes with it."
---

# Hardware & 3D files

<div class="scout-3d-slot" data-scout-3d="../assets/models/scout.glb" data-orbit="-30deg 70deg 0.8m" data-rps="15deg"><div class="scout-3d"></div></div>

## The rover — FrodoBots Earth Rover Mini+

A skid-steer four-wheel platform designed for FrodoBots' *Earth Rovers* network (people teleoperate them across the world; the
Mini+ is the developer edition you can own). What scout uses, as seen from the SDK's `/data` and media endpoints:

| subsystem | what the code reads / drives |
|---|---|
| **drive** | 4 hub motors, skid steer — `POST /control {linear, angular}` in `[-1, 1]`, continuous frames |
| **cameras** | front + rear, JPEG frames via `/v2/front`, `/v2/rear` (Agora WebRTC underneath) |
| **audio** | mic (`/rover-mic`, PCM) and speaker (`/rover-speaker`, `/speak` TTS) |
| **lamp** | headlamp — `rover_lamp`, `POST /api/lamp` |
| **telemetry** | battery level/voltage/current, GPS lat/lng/signal, cellular signal, IMU accel/gyro (burst arrays), magnetometer,
  per-wheel RPM (fl/fr/rl/rr), vibration, power & network state — the 27-D `observation.state` ([schema](reference/schema.md)) |
| **link** | 4G / Wi-Fi to FrodoBots' cloud; the SDK joins the rover's Agora channel from wherever it runs |

Internals (from the official repo): an RK3588-class Linux SoC handles media, an STM32 (RT-Thread) drives motors and sensors,
a Quectel EC2x/EG9x-series modem provides cellular. A community project, [sssynk/earth-rover-mini-firmware](https://github.com/sssynk/earth-rover-mini-firmware),
documents a drop-in firmware with a local HTTP API — interesting for a future no-cloud mode; scout does not use it.

## The brain — NVIDIA Jetson AGX Thor

The reference deployment runs the slim (CPU) stack on a Jetson AGX Thor over Wi-Fi, next to the rover. The GPU is used only if you
build `scout:latest` for Cosmos; everything on this site works on any arm64/amd64 docker host — a Raspberry Pi 5 with 8 GB
runs it (YOLO n on CPU, ~2–4 fps). Disk: the slim image is ~16 GB with lerobot/torch-cpu.

## 3D files — where they are, and why they are not here

The official CAD is published by FrodoBots in
**[frodobots-org/earth-rover-mini → `3DPrint/`](https://github.com/frodobots-org/earth-rover-mini/tree/main/3DPrint)** as STEP files:

| file | size |
|---|---|
| [`body.stp`](https://github.com/frodobots-org/earth-rover-mini/blob/main/3DPrint/body.stp) | 1.3 MB |
| [`front_top.stp`](https://github.com/frodobots-org/earth-rover-mini/blob/main/3DPrint/front_top.stp) | 3.2 MB |
| [`back_top.stp`](https://github.com/frodobots-org/earth-rover-mini/blob/main/3DPrint/back_top.stp) | 1.3 MB |
| [`back_cover.stp`](https://github.com/frodobots-org/earth-rover-mini/blob/main/3DPrint/back_cover.stp) | 124 KB |
| [`camera.stp`](https://github.com/frodobots-org/earth-rover-mini/blob/main/3DPrint/camera.stp) | 521 KB |
| [`mini_cover.stp`](https://github.com/frodobots-org/earth-rover-mini/blob/main/3DPrint/mini_cover.stp) | 521 KB |

The same repo carries the **schematic** (`Hardware/Schematic/SCH_Earth_Rover_Car_2025-07-29.pdf`), **Gerbers** (`Hardware/Gerber/V6.2.4.zip`),
PCB photos, the modem AT-command manual, and the STM32 + Linux SDKs.

!!! warning "No license = all rights reserved"
    As of September 2026 that repository has **no LICENSE file**, no license field, and `3DPrint/README.md` says `TODO`. Under
    copyright law that means *all rights reserved* by default — so scout **links** to the files and does not redistribute, convert
    or remix them. If FrodoBots adds a permissive license (CC-BY, MIT, CERN-OHL…) the plan is to convert `body.stp` + tops to a
    ≤ 3 MB `.glb` and put the real rover on the home page. Please open an issue if you see that happen.

    We also looked for community models on Printables, Thingiverse, Cults3D and GrabCAD: nothing for "Earth Rover Mini" /
    "FrodoBots" at the time of writing.

### The model on this site

`website/assets/models/scout.glb` (30 KB, 1 388 faces) is an **original, stylised stand-in** built from boxes and cylinders with
`trimesh` in the cockpit's colours — not derived from any FrodoBots CAD, photo or mesh, and not dimensionally accurate. MIT, like the
repo; see `website/assets/models/LICENSE.md`. It exists so the home page has something to spin.

### The room scan

`cagatay_lab.usdz` (1.4 MB, tracked) is an **Apple RoomPlan** scan of the lab the rover drives in — semantically named meshes that
`tools/room_map.py` turns into the spatial prompt. Scan your own room with any RoomPlan app and point `SCOUT_ROOM_SCAN` at it.

## Adding hardware

- **A different rover** — implement the SDK surface (`/control`, `/data`, `/v2/front`, `/speak`, `/rover-mic`, `/rover-speaker`) and
  everything above the SDK works unchanged; `ROVER_SDK_URL` is the only pointer.
- **A PS4 controller** — plug in, `controller_start`; `ROVER_CONTROLLER_HZ`, `_LINEAR_MAX`, `_ANGULAR_MAX`, `_DEADZONE`.
- **A GPU** — `docker-compose.yml` + `Dockerfile.scout` for Cosmos; `DET_BACKEND=locate` for open-vocabulary grounding.
