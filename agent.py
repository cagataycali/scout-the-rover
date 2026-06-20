"""
Earth Rover Mini operator agent with DYNAMIC per-turn state injection.

Before every user turn we call the rover's /data endpoint and rebuild
the system prompt with live telemetry at the top — battery, GPS, signal,
orientation. The model "wakes up" every turn knowing the rover's exact
state without defensive tool calls.

Run:  python agent.py            # REPL
      python agent.py "query"    # one-shot
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Any, Dict, Optional

from dotenv import load_dotenv

load_dotenv()

from strands import Agent
from tools import ROVER_ALL_TOOLS
from tools.rover_state import rover_state as _rover_state_tool
from tools.rover_camera import rover_see as _rover_see_tool
import memory as _memory
from tools.voice_bridge import voice_say as _voice_say
from tools.reasoning_log import ReasoningLoggerHook
from tools.dataset_index import dataset_index_block
from tools.room_map import build_map_block as _room_map_block
from tools.rover_pose import pose_block as _pose_block

BASE_PROMPT = """You are scout, a friendly Earth Rover Mini sidewalk robot.

scout is driven by AWS Strands Agents. You speak in a warm, conversational
tone — like a helpful companion, not a machine. Keep responses short.

You can SEE through your cameras (rover_see) — always look before you move.
You can DRIVE (rover_move), but be careful:

CRITICAL SAFETY RULES:
  1. EVERY user turn starts with live front+rear frames in your input —
     you already see the world. Plan from those frames; don't waste a
     rover_see unless you need a fresh look between moves.
  2. rover_move and rover_navigate now return BEFORE/AFTER camera frames
     inline — you SEE the visual delta of every motion. No need for a
     follow-up rover_see after a move; just read the AFTER frame.
  3. Use small steps: linear<=0.5, duration<=2s. Read the AFTER frame
     before the next move.
  4. If anything looks wrong (obstacle, edge, person close) → rover_stop.
  5. You share sidewalks with humans and pets. Yield. Always.
  6. Battery < 20% → refuse long drives, say so.

Your toolset:
  rover_see(camera, save)        → look through front/rear cameras (you SEE the image)
  rover_screenshot(views)        → front/rear/map composite views
  rover_move(linear, angular, duration, capture)
      → drive ONE segment, auto-stop, RETURN before+after frames inline
      capture: 'front' (default), 'both', or 'off'
  rover_navigate(steps, look_every_n_steps) → BATCH multiple move segments in ONE call
                                  steps=[{linear,angular,duration,pause,label}, ...]
                                  USE THIS for multi-segment journeys instead of N rover_move calls.
  rover_stop()                   → emergency stop
  rover_async(action, steps, priority) → ⚡ NON-BLOCKING motion queue.
      action='queue' enqueues [{linear,angular,duration,label}] and RETURNS
      INSTANTLY — wheels keep turning in the background while you look/think
      and queue the next leg. This is the SMOOTH/FLUID way to move: pipeline
      legs instead of blocking on each one. action='status' to check queue,
      action='stop' to clear+halt. Use rover_move only for a single careful
      look-then-step; use rover_async for fluid multi-leg motion.
  room_map(action)               → 🗺️ your PRIOR world model from the RoomPlan
      scan (walls, rooms, furniture, doors). action='show'|'objects'|'rooms'.
      The map is also injected at the TOP of every turn — you already see it.
  rover_pose(action, x, y, yaw_deg) → 🧭 WHERE YOU ARE in the room (dead
      reckoning). action='seed' when placed at a known spot (x,y meters,
      yaw_deg: 0=+x,90=+y,CCW). action='get' for current estimate + nearest
      objects. Pose auto-updates as you drive (sync OR async). It DRIFTS —
      re-seed when a camera view confidently matches the map. Your current
      pose is injected at the top of every turn.
      To make dead-reckoning ACCURATE: after driving a known distance,
      rover_pose(action='calibrate', linear=<cmd>, duration=<s>,
      measured=<actual meters>) — and similarly for angular with degrees.
      It back-solves the speed constants and saves them.
  rover_lamp(on)                 → headlamp
  rover_state()                  → battery/GPS/IMU telemetry
  rover_memory(action, ...)      → long-term SQLite knowledge store
      remember/recall/search/list/recent/update/forget/stats
      categories: spatial, preference, hazard, fact, goal, person, object
      USE THIS to retain knowledge ACROSS sessions: places explored,
      operator preferences, hazards, persistent goals, observations.
      Recent turns are auto-injected (short-term); rover_memory is
      what scout chooses to KEEP forever.
  rover_speak(text)              → talk through the onboard speaker

SPATIAL AWARENESS — you now have a room map + a position estimate:
  • The ROOM MAP and your ESTIMATED POSE are injected at the top of every
    turn. Use them: 'I'm at (+1.2,-0.4), the sofa is 1m to my left.'
  • Plan routes using mapped doors/objects, but the map+pose are a PRIOR —
    ALWAYS confirm against the live camera frames before committing a move.
  • If pose isn't seeded, ask the operator where you are, or match a camera
    view to the map and rover_pose(action='seed', ...).
  • For fluid travel, queue legs with rover_async and re-check pose/camera
    between queue calls instead of blocking on each rover_move.

EFFICIENCY TIP: For any journey of >2 movements, use rover_navigate with all
steps batched. Set look_every_n_steps=2 to get periodic camera frames so you
can re-evaluate without losing the batched plan.

Data collection (LeRobot v3 dataset) — automatic, per-turn:
  • EVERY user prompt automatically becomes one dataset episode.
    Task = the user's prompt verbatim. Recording starts when you receive
    the prompt and stops when you finish responding.
  • Episodes with NO motion (you only looked, only spoke, only checked state)
    are dropped — they would just bloat the dataset.
  • Manual override: start_recording(task) / stop_recording() / recording_status()
    still work for explicit multi-turn episodes (like a long teleop session).
    If a manual episode is in progress, auto-record stays out of the way.

The dataset accumulates at ./datasets/scout__earth-rover-mini/ across all
sessions and is fully LeRobot-v3-compliant for imitation learning training.

Teleop handover — let the operator drive while you observe/record:
  controller_start()             → enable PS4 controller background thread
  controller_stop()              → disable it
  controller_status()            → connected? what are the sticks doing?

When controller is active, operator stick input streams to /control directly
and the recorder logs source='controller'. The agent and operator share the
rover; whichever issued the most-recent command wins. Triangle/Cross on the
controller can also start/stop a recording episode hands-free.

MEMORY DISCIPLINE — when to call rover_memory(action='remember'):
  • New place or landmark identified → category='spatial' with GPS in meta
  • Operator states a preference     → category='preference'
  • Hazard observed (drop, gate, etc) → category='hazard', importance>=4
  • Long-running goal handed to you  → category='goal'
  • Person introduces themselves     → category='person'
Before acting on a place/person/goal you MIGHT have seen before, call
rover_memory(action='recall', query='...') first. Update or forget when
the world changes — stale memories are worse than no memories.
"""


def _unwrap(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Pull json blob out of @tool result envelopes."""
    if not isinstance(envelope, dict) or "content" not in envelope:
        return envelope if isinstance(envelope, dict) else {}
    content = envelope.get("content") or []
    blob = next(
        (c.get("json") for c in content if isinstance(c, dict) and "json" in c),
        None,
    )
    return blob if isinstance(blob, dict) else {}


def live_state_block() -> str:
    """Fetch rover telemetry and format as a system prompt block."""
    try:
        d = _unwrap(_rover_state_tool())
        if not d:
            return "## LIVE ROVER STATE: unavailable (SDK offline?)\n"
        gps_ok = d.get("gps_signal", 0) > 0 and d.get("latitude", 1000) != 1000
        gps = (
            f"{d['latitude']:.6f}, {d['longitude']:.6f}" if gps_ok else "no fix"
        )
        return (
            "## LIVE ROVER STATE (auto-refreshed this turn):\n"
            f"- Battery: {d.get('battery', '?')}%  "
            f"({d.get('voltage', '?')}V / {d.get('current', '?')}mA)\n"
            f"- Signal: {d.get('signal_level', '?')}/4\n"
            f"- GPS: {gps}\n"
            f"- Orientation: {d.get('orientation', '?')}°  "
            f"Speed: {d.get('speed', '?')}\n"
            f"- Lamp: {'on' if d.get('lamp') else 'off'}\n"
            f"- Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
    except Exception as e:
        return f"## LIVE ROVER STATE: error reading telemetry ({e})\n"



def spatial_block() -> str:
    """Room map prior + current dead-reckoning pose for the system prompt."""
    try:
        mp = _room_map_block()
    except Exception as e:
        mp = f"## ROOM MAP: unavailable ({e})"
    try:
        ps = _pose_block()
    except Exception as e:
        ps = f"## ESTIMATED POSE: unavailable ({e})"
    return f"{ps}\n{mp}"


def perception_block() -> str:
    """Live object detections (YOLO or LocateAnything-3B) from the perception
    detector, read via the MediaHub 'detections' stream (cross-container).
    Empty otherwise — never breaks the turn."""
    try:
        import media_client as _mc
        d = _mc.latest_detections()
        if d and d.get("dets"):
            backend = d.get("backend", "detector")
            names = ", ".join(sorted({x.get("cls", "?") for x in d["dets"]}))
            return (f"## 👁️ LIVE PERCEPTION ({backend}, {d.get('cam','front')} cam, now):\n"
                    f"sees: {names}\n")
    except Exception:
        pass
    return ""


def live_camera_blocks(camera: str = "both") -> list:
    """Grab front+rear frames; return Strands content blocks.

    Returns [] silently if SDK is offline or frames unavailable — never
    breaks the turn.
    """
    try:
        env = _rover_see_tool(camera=camera, save=False)
        if not isinstance(env, dict) or env.get("status") != "success":
            return []
        # Drop the leading text summary; keep only labels + image blocks.
        out = []
        for c in env.get("content", []):
            if isinstance(c, dict) and ("image" in c or c.get("text", "").startswith("[")):
                out.append(c)
        return out
    except Exception as e:
        print(f"📷 live-frames: skipped ({e})", flush=True)
        return []


def build_turn_input(prompt: str, camera: str = "both") -> list | str:
    """Wrap user prompt with current camera frames as multimodal input."""
    blocks = live_camera_blocks(camera)
    if not blocks:
        return prompt
    return [
        {"text": "[Live rover view at turn start]"},
        *blocks,
        {"text": f"\n[User]: {prompt}"},
    ]


# ────────────────────────────────────────────────────────────────────────
# Auto-record-per-turn — every user prompt becomes a LeRobot episode.
#
# Toggle with env vars:
#   SCOUT_AUTO_RECORD=0           disable entirely
#   SCOUT_AUTO_RECORD_MIN_ACTIONS=N  drop episodes with <N agent actions
#                                    (default 1; set 0 to keep observation-only)
#   SCOUT_AUTO_RECORD_MAX_TASK_LEN  truncate long prompts (default 256)
# ────────────────────────────────────────────────────────────────────────


_AUTO_REC_ENABLED = os.getenv("SCOUT_AUTO_RECORD", "1") not in ("0", "false", "False")
_AUTO_REC_MIN_ACTIONS = int(os.getenv("SCOUT_AUTO_RECORD_MIN_ACTIONS", "1"))
_AUTO_REC_MAX_TASK_LEN = int(os.getenv("SCOUT_AUTO_RECORD_MAX_TASK_LEN", "256"))


class _AutoRecorder:
    """Wraps a single agent turn in start_episode/stop_episode.

    Counts how many times ACTION_STATE was set with source='agent' during
    the turn — if zero (or below the threshold), the episode is discarded
    rather than saved, so observation-only turns ("what do you see?") don't
    bloat the dataset.

    Skips silently if:
      * SCOUT_AUTO_RECORD=0
      * The user is already inside a manual start_recording / stop_recording
        block (we honor manual recording — no nested episodes).
    """

    def __init__(self) -> None:
        from tools._recorder_engine import get_engine, ACTION_STATE
        self._get_engine = get_engine
        self._action_state = ACTION_STATE
        self._enabled = _AUTO_REC_ENABLED
        self._owns_episode = False
        self._action_count_at_start = 0

    def begin_turn(self, prompt: str) -> None:
        if not self._enabled:
            return
        try:
            eng = self._get_engine()
            # Don't double-record if a manual episode is already in progress.
            if eng.status().get("recording"):
                self._owns_episode = False
                return
            task = (prompt or "").strip()[:_AUTO_REC_MAX_TASK_LEN]
            if not task:
                self._owns_episode = False
                return
            # Snapshot the agent-action counter so we know how many actions
            # were actually issued during THIS turn (vs. lingering state).
            self._action_count_at_start = self._agent_action_ts()
            result = eng.start_episode(task=task)
            self._owns_episode = bool(result.get("ok"))
        except Exception as e:
            # Never let auto-record break the user's turn.
            self._owns_episode = False
            print(f"🎬 auto-record: failed to start ({e})", flush=True)

    def end_turn(self) -> None:
        if not self._enabled or not self._owns_episode:
            return
        try:
            eng = self._get_engine()
            # actions_this_turn is positive iff ACTION_STATE.set() was called
            # at least once during the turn (we're comparing monotonic ts deltas).
            ts_delta = self._agent_action_ts() - self._action_count_at_start
            had_actions = ts_delta > 0
            if not had_actions and _AUTO_REC_MIN_ACTIONS >= 1:
                # No motion → not training-relevant. Drop the episode.
                eng._recording_evt.clear()
                # Brief sleep so the capture loop notices and stops adding frames
                import time
                time.sleep(1.0 / max(eng.fps, 1) + 0.1)
                try:
                    eng._dataset.clear_episode_buffer()
                except Exception:
                    pass
                # Also stop the mic capture started in start_episode
                try:
                    eng._stop_audio()
                except Exception:
                    pass
                # Roll back the episode counter so the next real episode reuses it
                with eng._state_lock:
                    eng._episode_idx = max(0, eng._episode_idx - 1)
                print(f"🎬 auto-record: skipped (0 actions in turn)", flush=True)
            else:
                result = eng.stop_episode()
                if result.get("ok"):
                    print(
                        f"🎬 auto-record: ep {result['episode_index']} saved "
                        f"({result['frames']} frames)",
                        flush=True,
                    )
                else:
                    print(f"🎬 auto-record: stop failed: {result.get('error')}", flush=True)
        except Exception as e:
            print(f"🎬 auto-record: end_turn failed ({e})", flush=True)
        finally:
            self._owns_episode = False

    @staticmethod
    def _agent_action_ts() -> float:
        """Return a monotonic counter that increments on every agent action.

        We piggy-back on ACTION_STATE.ts — every motion tool call updates it.
        Counting state transitions where source='agent' is enough signal.
        """
        # NB: ACTION_STATE doesn't expose a counter, so we use a class-level
        # tally maintained by the recorder. Cheap and threadsafe enough.
        from tools._recorder_engine import ACTION_STATE as _AS
        with _AS.lock:
            return _AS.ts  # monotonic float; > start ⇒ at least one set() call


_auto_recorder = _AutoRecorder()

_COSMOS_PROMPT = """
🌌 COSMOS (NVIDIA world-model tools) — available on this device:
  cosmos3_caption(video|image)   → detailed scene caption of a clip/frame
  cosmos3_reason(prompt w/ <video>..</video> or <image>..</image>) → reason over vision
  cosmos3_temporal(video)        → notable events with timestamps
  cosmos3_ground(...)            → locate things spatially in a frame
  cosmos3_text2video / image2video / text2image → GENERATE media (Diffusers, local)
  video_extract_frames / video_probe → I/O helpers

  🤖 EMBODIED / world-model reasoning (great for a robot):
  cosmos3_embodied(video|image)        → predict your NEXT action from the scene
  cosmos3_action_cot(image, task)      → plan a trajectory toward a task (CoT)
  cosmos3_policy(jsonl)                → image+instruction → action chunk + rollout video
  cosmos3_forward_dynamics(jsonl)      → "if I take these actions, what happens?" (predict future video)
  cosmos3_inverse_dynamics(jsonl)      → "what actions produced this video?" (imitate)
WHEN TO USE: for DEEP visual understanding beyond a single glance — e.g.
"what happened in the last clip", physics/affordance reasoning, predicting the
outcome of a maneuver before doing it, or imagining/previewing a path as video.
Your normal rover_see frames + rover_move are still primary; reach for Cosmos
when richer reasoning, planning, or generation is genuinely needed.
"""


def build_agent(extra_tools: Optional[list] = None) -> Agent:
    tools = [*ROVER_ALL_TOOLS, _voice_say, *(extra_tools or [])]
    try:
        from tools import COSMOS_AVAILABLE as _COSMOS
    except Exception:
        _COSMOS = False
    cosmos_block = _COSMOS_PROMPT if _COSMOS else ""
    # Surface local dataset media/trace paths so Cosmos tools get real file paths.
    dataset_block = dataset_index_block() if _COSMOS else ""
    return Agent(
        tools=tools,
        system_prompt=(
            f"{_memory.recall_block()}\n{live_state_block()}\n{spatial_block()}\n{perception_block()}\n{BASE_PROMPT}\n"
            f"{cosmos_block}\n{dataset_block}"
        ),
        hooks=[ReasoningLoggerHook(agent_id="main")],
    )


def main() -> None:
    agent = build_agent()

    if len(sys.argv) > 1:
        # one-shot
        prompt = " ".join(sys.argv[1:])
        agent.system_prompt = f"{_memory.recall_block()}\n{live_state_block()}\n{spatial_block()}\n{perception_block()}\n{BASE_PROMPT}"
        _auto_recorder.begin_turn(prompt)
        _result = None
        try:
            _result = agent(build_turn_input(prompt))
        finally:
            _auto_recorder.end_turn()
            try:
                _memory.record_turn(prompt, _result)
            except Exception as _e:
                print(f"🧠 memory: skipped ({_e})", flush=True)
        return

    print("🛞 scout — Earth Rover Mini agent. Ctrl+C or 'exit' to quit.")
    while True:
        try:
            q = input("\n🛞 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            break
        if not q:
            continue
        if q.lower() in ("exit", "quit", "q"):
            break
        # Per-turn live state injection
        agent.system_prompt = f"{_memory.recall_block()}\n{live_state_block()}\n{spatial_block()}\n{perception_block()}\n{BASE_PROMPT}"
        _auto_recorder.begin_turn(q)
        _result = None
        try:
            _result = agent(build_turn_input(q))
        except KeyboardInterrupt:
            print("\n(interrupted)")
        except Exception as e:
            print(f"error: {e}")
        finally:
            _auto_recorder.end_turn()
            try:
                _memory.record_turn(q, _result)
            except Exception as _e:
                print(f"🧠 memory: skipped ({_e})", flush=True)


if __name__ == "__main__":
    main()
