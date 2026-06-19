"""🎬 LeRobot dataset recording + 🎮 PS4 controller — agent-facing @tool wrappers.

Tools:
    start_recording(task)   → begin a new dataset episode
    stop_recording()        → save the current episode to disk
    recording_status()      → engine + episode state
    controller_start()      → enable PS4 teleop background thread
    controller_stop()       → disable PS4 teleop
    controller_status()     → connection + axis state

The recorder and controller are designed to coexist:
    * agent issues rover_move() → ACTION_STATE.source = "agent"
    * operator nudges PS4 stick → ACTION_STATE.source = "controller" (overrides)
    * recorder writes whichever is current as the "action" feature

Triangle/Cross on the controller can also start/stop recording when both
are running — handy for one-handed data collection.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from strands import tool

from ._controller_engine import get_controller
from ._recorder_engine import get_engine
from ._rover_common import error_result, ok_result

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────
# Recording
# ────────────────────────────────────────────────────────────────────────


@tool
def start_recording(task: str = "drive") -> Dict[str, Any]:
    """Start recording a new LeRobot dataset episode.

    Captures front (+ rear, if present) cameras, full telemetry state vector,
    last-commanded action, and microphone audio at the configured FPS
    (default 10) into a versioned LeRobot v3 dataset on disk.

    Each call to start_recording → stop_recording produces ONE episode.
    The dataset is created on the first run and APPENDED on subsequent runs,
    so you can build it up over many sessions.

    Args:
        task: Short natural-language label for what the rover is doing in
            this episode (e.g. "navigate to crosswalk", "follow sidewalk").
            Stored as the LeRobot 'task' string and used for task-conditioned
            training later.

    Returns:
        Dict with status and: episode_index, fps, dataset_root.
    """
    try:
        eng = get_engine()
        result = eng.start_episode(task=task)
        if not result.get("ok"):
            return error_result(f"start_recording: {result.get('error')}")
        return {
            "status": "success",
            "content": [
                {"text": (
                    f"🎬 Recording episode #{result['episode_index']} "
                    f"(task={task!r}, fps={result['fps']}). "
                    f"Dataset: {result['dataset_root']}"
                )},
                {"json": result},
            ],
        }
    except Exception as e:
        logger.exception("start_recording")
        return error_result(f"start_recording: {e}")


@tool
def stop_recording() -> Dict[str, Any]:
    """Stop the current recording episode and save it to the dataset.

    Encodes captured frames into MP4 video files, writes the parquet rows
    for state/action, and persists episode metadata. Audio (if captured)
    is written as a WAV sidecar at <dataset>/audio/episode_NNNNNN.wav.

    Returns:
        Dict with status and: frames captured, duration, dataset_root.
    """
    try:
        eng = get_engine()
        result = eng.stop_episode()
        if not result.get("ok"):
            return error_result(f"stop_recording: {result.get('error')}")
        return {
            "status": "success",
            "content": [
                {"text": (
                    f"💾 Saved episode #{result['episode_index']}: "
                    f"{result['frames']} frames in {result['duration_s']}s. "
                    + (f"Audio: {result['audio_path']}. " if result.get("audio_path") else "")
                    + f"Dataset: {result['dataset_root']}"
                )},
                {"json": result},
            ],
        }
    except Exception as e:
        logger.exception("stop_recording")
        return error_result(f"stop_recording: {e}")


@tool
def recording_status() -> Dict[str, Any]:
    """Get the current recording engine status (running, episode, frames).

    Returns:
        Dict with status and full engine state JSON (recording, total_episodes,
        current_frames, current_duration_s, dataset_root, …).
    """
    try:
        eng = get_engine()
        s = eng.status()
        active = "🔴 RECORDING" if s["recording"] else "⚪ idle"
        text = (
            f"{active} · episodes saved: {s['total_episodes']} · "
            f"current: {s['current_frames']} frames / {s['current_duration_s']}s · "
            f"audio: {'on' if s['audio_capture'] else 'off'} · rear: {s['has_rear_camera']}"
        )
        return ok_result(text, [{"json": s}])
    except Exception as e:
        return error_result(f"recording_status: {e}")


# ────────────────────────────────────────────────────────────────────────
# Controller
# ────────────────────────────────────────────────────────────────────────


def _ctl_record_start(task: str = "controller-teleop") -> None:
    try:
        get_engine().start_episode(task=task)
    except Exception as e:
        logger.warning(f"controller record-start: {e}")


def _ctl_record_stop() -> None:
    try:
        get_engine().stop_episode()
    except Exception as e:
        logger.warning(f"controller record-stop: {e}")


@tool
def controller_start() -> Dict[str, Any]:
    """Enable the PS4 controller teleop background thread.

    Starts polling a connected DualShock 4 (or compatible SDL2 joystick)
    and forwarding commands to the rover. Updates ACTION_STATE so any
    active recording captures the human action stream tagged
    source='controller'. The agent and the controller share the rover —
    whichever issues the most-recent /control wins.

    Mapping:
        Left stick Y    → linear  (forward / reverse, scaled to ±0.7)
        Right stick X   → angular (turn, scaled to ±0.9)
        L2 trigger      → soft brake
        R2 trigger      → boost (allows up to ±1.0)
        Square          → toggle headlamp
        Circle          → emergency stop (zero velocities)
        Triangle        → start_recording('controller-teleop')
        Cross (X)       → stop_recording

    Tune limits with env vars:
        ROVER_CONTROLLER_LINEAR_MAX, ROVER_CONTROLLER_ANGULAR_MAX,
        ROVER_CONTROLLER_DEADZONE, ROVER_CONTROLLER_HZ.

    Returns:
        Dict with status and connection state. If no joystick is found,
        status='error' with a hint to plug one in.
    """
    try:
        ctl = get_controller(
            on_record_start=_ctl_record_start,
            on_record_stop=_ctl_record_stop,
        )
        result = ctl.start()
        if not result.get("ok"):
            return error_result(
                f"controller_start: {result.get('error') or 'no controller detected'}"
            )
        return {
            "status": "success",
            "content": [
                {"text": (
                    f"🎮 Controller active: {result.get('device')} · "
                    f"linear≤{result['linear_limit']}, angular≤{result['angular_limit']}, "
                    f"deadzone={result['deadzone']}. "
                    f"Triangle=start-rec, Cross=stop-rec, Circle=e-stop."
                )},
                {"json": result},
            ],
        }
    except Exception as e:
        logger.exception("controller_start")
        return error_result(f"controller_start: {e}")


@tool
def controller_stop() -> Dict[str, Any]:
    """Disable the PS4 controller teleop and stop the rover.

    Kills the background polling thread and sends a final zero-velocity
    /control to the rover so it doesn't keep moving on the last command.

    Returns:
        Dict with status.
    """
    try:
        ctl = get_controller()
        result = ctl.stop()
        return ok_result("🎮 Controller disabled. Rover stopped.", [{"json": result}])
    except Exception as e:
        logger.exception("controller_stop")
        return error_result(f"controller_stop: {e}")


@tool
def controller_status() -> Dict[str, Any]:
    """Get the current PS4 controller status (connected, axis values).

    Returns:
        Dict with status, device name, current axis values (linear/angular),
        lamp state, and any error message.
    """
    try:
        ctl = get_controller()
        s = ctl.status()
        if not s["running"]:
            return ok_result("🎮 Controller off.", [{"json": s}])
        if not s["connected"]:
            return ok_result(
                f"🎮 Controller running but not connected ({s.get('error')}).",
                [{"json": s}],
            )
        text = (
            f"🎮 {s['device']} · linear={s['linear']:+.2f} angular={s['angular']:+.2f} · "
            f"lamp={'on' if s['lamp'] else 'off'}"
        )
        return ok_result(text, [{"json": s}])
    except Exception as e:
        return error_result(f"controller_status: {e}")
