---
title: Dataset schema
description: "LeRobot v3 features scout records — 27-D state, 2-D action, two cameras, audio — plus the ECoT and detections sidecars."
---

# Dataset schema — a superset that stays LeRobot-safe

The first 10 dims of `observation.state` match the official `earthrover_mini_plus` / `lilkm/earthrover-navigation` layout
(same dotted names, same order) so policies and checkpoints transfer — a standard model slices `[:10]`. scout then **appends**
richer raw telemetry.

## `observation.state` — 27-D

| idx | fields | source |
|---|---|---|
| 0–9 | `linear.vel · angular.vel · battery.level · orientation.deg · gps.{lat,lng,signal} · signal.level · vibration · lamp.state` | official core (transfer-compatible) |
| 10–11 | `voltage · current` | electrical |
| 12–17 | `imu.{accel,gyro}.{x,y,z}` | latest IMU sample |
| 18–20 | `imu.mag.{x,y,z}` | magnetometer — absolute heading |
| 21–24 | `rpm.{fl,fr,rl,rr}` | per-wheel odometry — ground-truth proprioception (commanded ≠ executed) |
| 25–26 | `power · network_state` | system |

## `action` — 2-D, normalized `[-1, 1]`

`linear.vel · angular.vel` — the reference formulation; the lamp lives in state.

## Video & audio

`observation.images.front` and `observation.images.rear` (rear on by default; `ROVER_RECORD_REAR=0` for front-only) at `ROVER_RECORD_FPS` (10) and `ROVER_RECORD_WIDTH×HEIGHT`
(640×480), encoded `SCOUT_VCODEC` (h264); `observation.audio` at `ROVER_AUDIO_RATE` (16 kHz) from the hub's mic stream.

!!! note "Burst arrays"
    The SDK exposes IMU/mag/rpm as bursts (several samples per poll with a unix timestamp). scout takes the freshest sample per
    10 Hz frame.

## Sidecars, aligned by frame index

```
datasets/scout__earth-rover-mini-YYYYMMDD/<persona>/
├── meta/info.json  meta/episodes/…  meta/tasks.parquet
├── data/chunk-000/file-000.parquet            # one file per sealed episode
├── videos/observation.images.front/chunk-000/file-000.mp4
├── reasoning/events.sqlite                    # ECoT: prompt → tool calls → results, frame_index = round((ts - t0) * fps)
└── detections/episode_000000.jsonl            # YOLO per frame: {frame, ts, cam, dets:[{cls, conf, xyxy}]}
```

- **ECoT** (`tools/reasoning_log.py`, `tools/ecot_export.py`) binds every reasoning step to the video spine; the export to
  🤗 [`cagataydev/scout-earthrover-ecot`](https://huggingface.co/datasets/cagataydev/scout-earthrover-ecot) flattens it.
- **Detections** (`yolo_detector.py`) are keyed by the recorder's frame index — same captured frame, no drift.
- **Merging** — `tools/merge_datasets.py` folds per-persona datasets into a daily corpus; `tools/dataset_index.py` lists them for
  the cockpit and the agent prompt (`SCOUT_INJECT_DATASETS`).

Why one parquet + one mp4 per episode, and how to repair an index: [Datasets & replay](../datasets.md).
