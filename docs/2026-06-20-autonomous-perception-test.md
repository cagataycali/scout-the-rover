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
