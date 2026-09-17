# Autonomous perception/navigation test session — 2026-06-20

Branch: `feat/media-hub-perception`. Full docker stack (sdk + dashboard +
telegram + media-hub + LocateAnything detector). Agent run per-turn in a
container against the live rover.

## Stack verified working on hardware
- **MediaHub** fan-out: front/rear/mic/data → many subscribers, no contention.
  `/screenshot` fallback kicks in when the SDK `/v2/<cam>` fast-path 404s.
- **LocateAnything-3B** (open-vocab grounding) live as the perception backend:
  grounded `chair, table, cable×3` — `cable` is beyond YOLO's COCO classes.
  Isolated `.venv-locate` (transformers 4.57.1) so it doesn't clash with the
  image's transformers 5.x. ~5s/frame on GPU.
- **Detections fan-in**: detector → hub `detections` stream → agent
  `perception_block()` → "sees: cable, chair, table" in every turn's prompt.

## Test cycles
1. **Perception + pose awareness** — PASS. Agent described scene (chair, cables,
   Unitree G1, sofa, boxes), cross-referenced the room map. ⚠️ Surfaced a bug:
   12.8m stale odometer drift from a prior session.
   → FIX: pose auto-flags STALE (old/drifted) and instructs visual re-seed.
2. **Stale-pose re-seed** — PASS. Agent saw stale flag, used the blue sofa
   (mapped (0,-2.4)) as a landmark, triangulated, re-seeded to (0.4,-1.8)@315°,
   confirmed fresh.
3. **Perception-aware safety** — PASS. Asked to drive 1m forward; agent analyzed
   front (chair casters ~25cm, desk legs, cables) + rear (G1 robot), built a
   hazard table, and correctly REFUSED ("boxed in"), offered alternatives.

## Fixes shipped during the session
- media_hub `/screenshot` fallback for video drain
- detections fan-in (hub `detections` stream + `/publish`)
- rover_see `/screenshot` fallback (one tool call instead of 404+retry)
- telegram `_env_int` robust to quoted env values
- pose STALE detection + re-seed guidance
- Dockerfile: faster-whisper, webrtcvad, ultralytics, imageio, decord, lmdb

## Environment notes
- Rover battery telemetry read 0 while voltage 32V/current 95mA + control
  accepted → responsive, likely on charger. Camera served via /screenshot.
- Rover is physically boxed in (under a desk, G1 behind) — motion tests limited
  to reasoning/refusal rather than actual driving for safety.

## Additional cycles (4-5)
4. **Dataset recording probe** — FIXED probe to use hub/screenshot fallback
   (was refusing to start during /v2 outage).
5. **YOLO backend** — PASS. Loads on CUDA, 0.63s/frame inference, empty dets
   facing a wall. Both detector backends (yolo + locate) validated.

## ⚠️ Known issues (pre-existing, NOT part of perception work — for follow-up)
1. **LeRobot 0.5.1 + datasets 5.0.0 `action_age` save bug.** `add_frame`
   REQUIRES `action_age` as an `np.ndarray` shape `(1,)`, but `save_episode`'s
   `Dataset.from_dict` maps that feature to a scalar `Value('float32')` and
   raises `only 0-dimensional arrays can be converted to Python scalars`.
   Minimal repro confirmed. Fix options for a focused recorder PR:
     - fold `action_age` into `observation.state` (drop the `(1,)` feature), or
     - pin `datasets` to a version where `(1,)` features round-trip, or
     - patch LeRobot's writer to squeeze `(1,)` → scalar before `from_dict`.
   NOTE: `main/` has a 471-row episode saved earlier today, so the path worked
   under whatever datasets version was active at creation time.
2. **Capture loop yields 0 frames on the /screenshot fallback path** while the
   rover's /v2 fast-frame path is down (likely a timing issue at fps cadence
   with the slower screenshot render). The probe now succeeds but the per-frame
   grab during the episode needs the same robustness; worth a focused look when
   the rover's /v2 path is healthy (it self-heals on WebRTC reconnect).

These do not affect the perception pipeline (hub → detector → agent prompt),
which is fully verified.
