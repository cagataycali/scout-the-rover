"""🛣️ Earth Rover navigation — batch sequence of move commands.

One tool call → many move steps. Massively reduces tool-call overhead
on longer journeys (room loops, hallway traversals, multi-segment paths).

Safety model (inherits from rover_move):
  * Each step's linear/angular clamped to [-1, 1]
  * Each step auto-stops at end of its duration window
  * Final auto-stop after the whole sequence
  * Optional `look_every_n_steps` returns camera frames mid-sequence
    so the agent can re-evaluate before continuing
  * Any exception → emergency stop, abort remaining steps
  * Per-step `stop_on_error` (default True) halts sequence on failure
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from strands import tool

from ._rover_common import error_result, ok_result, sdk_get, b64_to_image_block

logger = logging.getLogger(__name__)


def _clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(v)))


def _send_control(linear: float, angular: float) -> None:
    """Send a single /control frame. Raises on non-200."""
    from ._rover_common import sdk_post
    resp = sdk_post("/control", json={"command": {"linear": linear, "angular": angular}})
    if resp.status_code != 200:
        raise RuntimeError(f"/control HTTP {resp.status_code}: {resp.text[:200]}")
    # Publish to shared ACTION_STATE so the recorder logs the agent's command.
    from ._recorder_engine import ACTION_STATE
    ACTION_STATE.set(linear, angular, source="agent")


def _drive_step(linear: float, angular: float, duration: float) -> int:
    """Run one timed step with continuous command frames. Returns frame count."""
    deadline = time.time() + duration
    sends = 0
    while time.time() < deadline:
        _send_control(linear, angular)
        sends += 1
        remaining = deadline - time.time()
        if remaining > 0.4:
            time.sleep(0.4)
        elif remaining > 0:
            time.sleep(remaining)
    _send_control(0.0, 0.0)  # per-step auto-stop
    return sends


def _grab_camera_frame(camera: str = "front") -> Optional[Dict[str, Any]]:
    """Best-effort camera grab between steps. Returns image block or None."""
    try:
        resp = sdk_get(f"/v2/{camera}", timeout=3)
        if resp.status_code != 200:
            return None
        b64 = resp.json().get(f"{camera}_frame")
        if not b64:
            return None
        return b64_to_image_block(b64)
    except Exception as e:
        logger.debug(f"Mid-sequence camera grab failed: {e}")
        return None


@tool
def rover_navigate(
    steps: List[Dict[str, Any]],
    look_every_n_steps: int = 0,
    look_camera: str = "front",
    stop_on_error: bool = True,
) -> Dict[str, Any]:
    """Execute a batch sequence of move commands in ONE tool call.

    Drastically reduces tool-call overhead for multi-segment journeys
    (e.g. "go down hallway, turn right, continue 3m"). Each step is a
    rover_move-equivalent that auto-stops at its end; the whole sequence
    auto-stops cleanly after the last step.

    Args:
        steps: List of step dicts. Each step:
            {
                "linear":   float,  # -1..1, default 0.0
                "angular":  float,  # -1..1, default 0.0
                "duration": float,  # 0.1..10 seconds, default 1.0
                "pause":    float,  # optional post-step idle (default 0)
                "label":    str,    # optional human-readable name
            }
        look_every_n_steps: If > 0, capture a camera frame every N steps
            and inline it in the result so the agent can re-plan. 0 = off.
        look_camera: "front" or "rear" — which camera for periodic looks.
        stop_on_error: If True (default), abort remaining steps when one
            fails. If False, log and continue.

    Returns:
        Dict with overall status, per-step log, total duration, and any
        inlined camera frames.

    Example:
        rover_navigate(steps=[
            {"linear": 0.4, "angular": 0.0, "duration": 2, "label": "fwd"},
            {"linear": 0.0, "angular": 0.5, "duration": 1, "label": "turn left"},
            {"linear": 0.4, "angular": 0.0, "duration": 2, "label": "fwd"},
        ], look_every_n_steps=2)
    """
    if not isinstance(steps, list) or not steps:
        return error_result("rover_navigate requires a non-empty 'steps' list.")
    if len(steps) > 32:
        return error_result(f"Too many steps ({len(steps)}); cap at 32 per call.")

    log_lines: List[str] = []
    extra_content: List[Dict[str, Any]] = []
    t_start = time.time()
    completed = 0
    aborted = False
    abort_reason = ""

    # Bookend: grab a BEFORE frame so the model has visual context for the journey.
    _before = _grab_camera_frame(look_camera)
    if _before:
        extra_content.append({"text": "📷 BEFORE journey:"})
        extra_content.append(_before)

    try:
        for idx, raw in enumerate(steps, start=1):
            if not isinstance(raw, dict):
                msg = f"Step {idx}: not a dict, skipping."
                log_lines.append(f"⚠️  {msg}")
                if stop_on_error:
                    aborted = True
                    abort_reason = msg
                    break
                continue

            linear = _clamp(raw.get("linear", 0.0))
            angular = _clamp(raw.get("angular", 0.0))
            duration = max(0.1, min(10.0, float(raw.get("duration", 1.0))))
            pause = max(0.0, min(5.0, float(raw.get("pause", 0.0))))
            label = str(raw.get("label", "")).strip()
            tag = f" [{label}]" if label else ""

            try:
                frames = _drive_step(linear, angular, duration)
                completed += 1
                log_lines.append(
                    f"✅ Step {idx}/{len(steps)}{tag}: "
                    f"linear={linear:+.2f} angular={angular:+.2f} "
                    f"for {duration}s ({frames} frames)"
                )
            except Exception as e:
                msg = f"Step {idx}{tag} failed: {e}"
                log_lines.append(f"❌ {msg}")
                if stop_on_error:
                    aborted = True
                    abort_reason = msg
                    break

            if pause > 0:
                time.sleep(pause)

            # Periodic camera peek
            if look_every_n_steps > 0 and idx % look_every_n_steps == 0 and idx < len(steps):
                frame = _grab_camera_frame(look_camera)
                if frame:
                    extra_content.append({"text": f"📷 Look after step {idx}:"})
                    extra_content.append(frame)
                    log_lines.append(f"📷 Captured {look_camera} frame after step {idx}")

        # Final stop (defense in depth)
        try:
            _send_control(0.0, 0.0)
        except Exception:
            pass

    except Exception as e:
        try:
            _send_control(0.0, 0.0)
        except Exception:
            pass
        return error_result(f"Sequence aborted with error: {e}\n" + "\n".join(log_lines))

    # Bookend: AFTER frame.
    _after = _grab_camera_frame(look_camera)
    if _after:
        extra_content.append({"text": "📷 AFTER journey:"})
        extra_content.append(_after)

    elapsed = time.time() - t_start
    summary = (
        f"🛣️  rover_navigate: {completed}/{len(steps)} steps in {elapsed:.1f}s"
        + (f" — ABORTED: {abort_reason}" if aborted else " — all clear, stopped.")
    )
    full_text = summary + "\n\n" + "\n".join(log_lines)

    if aborted:
        return error_result(full_text)
    return ok_result(full_text, extra_content=extra_content or None)
