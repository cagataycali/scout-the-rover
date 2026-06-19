"""🕹️ Earth Rover motion tools — drive, turn, stop, lamp.

Safety model:
  * linear/angular clamped to [-1, 1]
  * Timed moves auto-send a STOP command after `duration` seconds —
    the rover never keeps driving after the tool returns.
  * rover_stop is always available as an immediate kill switch.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict

from strands import tool

from ._rover_common import b64_to_image_block, error_result, ok_result, sdk_get, sdk_post
from ._recorder_engine import ACTION_STATE

logger = logging.getLogger(__name__)


def _clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(v)))


def _send(linear: float, angular: float, lamp: int | None = None) -> Dict[str, Any]:
    command: Dict[str, Any] = {"linear": linear, "angular": angular}
    if lamp is not None:
        command["lamp"] = 1 if lamp else 0
    resp = sdk_post("/control", json={"command": command})
    if resp.status_code != 200:
        raise RuntimeError(f"/control HTTP {resp.status_code}: {resp.text[:200]}")
    # Publish to shared ACTION_STATE so the recorder logs the agent's command.
    # Controller teleop also writes here; whoever called _send most recently wins.
    ACTION_STATE.set(linear, angular, lamp=float(lamp) if lamp is not None else None,
                     source="agent")
    return resp.json()




def _capture_frame(camera: str = "front") -> tuple[str, dict] | None:
    """Best-effort camera grab. Returns (label, image_block) or None."""
    try:
        resp = sdk_get(f"/v2/{camera}", timeout=3)
        if resp.status_code != 200:
            return None
        b64 = resp.json().get(f"{camera}_frame")
        if not b64:
            return None
        return camera, b64_to_image_block(b64)
    except Exception:
        return None


def _capture_views(both: bool = False) -> list[dict]:
    """Snap front (and optionally rear) frames. Returns list of content blocks."""
    cams = ["front", "rear"] if both else ["front"]
    out: list[dict] = []
    for cam in cams:
        snap = _capture_frame(cam)
        if snap:
            out.append({"text": f"[{snap[0]}]"})
            out.append(snap[1])
    return out


@tool
def rover_move(
    linear: float = 0.0,
    angular: float = 0.0,
    duration: float = 1.0,
    capture: str = "front",
) -> Dict[str, Any]:
    """Drive the rover with velocity control, then auto-stop.

    Returns BEFORE and AFTER camera frames inline so the model can SEE
    the visual delta of the move (no follow-up rover_see needed).

    Args:
        linear: Forward/backward speed, -1.0 (full reverse) to 1.0 (full forward).
        angular: Turn rate, -1.0 (full right) to 1.0 (full left).
        duration: Seconds to apply the command before auto-stop (0.1-10).
            Commands are re-sent every ~0.4s during the window because the
            rover's firmware expects a continuous command stream.
        capture: "front" (default), "both" (front+rear), or "off" (skip frames).

    Returns:
        Dict with status, summary text, and before/after image blocks.
    """
    linear = _clamp(linear)
    angular = _clamp(angular)
    duration = max(0.1, min(10.0, float(duration)))
    capture = (capture or "off").lower()
    want_frames = capture in ("front", "both")
    both = capture == "both"

    before = _capture_views(both=both) if want_frames else []

    try:
        deadline = time.time() + duration
        sends = 0
        while time.time() < deadline:
            _send(linear, angular)
            sends += 1
            remaining = deadline - time.time()
            if remaining > 0.4:
                time.sleep(0.4)
            elif remaining > 0:
                time.sleep(remaining)
        _send(0.0, 0.0)  # auto-stop

        after = _capture_views(both=both) if want_frames else []
        extra: list[dict] = []
        if before:
            extra.append({"text": "📷 BEFORE move:"})
            extra.extend(before)
        if after:
            extra.append({"text": "📷 AFTER move:"})
            extra.extend(after)

        return ok_result(
            f"Moved linear={linear} angular={angular} for {duration}s "
            f"({sends} command frames), then stopped.",
            extra_content=extra or None,
        )
    except Exception as e:
        # best-effort stop even on failure
        try:
            _send(0.0, 0.0)
        except Exception:
            pass
        return error_result(f"Move failed: {e}")


@tool
def rover_stop() -> Dict[str, Any]:
    """EMERGENCY STOP — immediately halt all rover motion.

    Returns:
        Dict with status.
    """
    try:
        _send(0.0, 0.0)
        return ok_result("Rover stopped.")
    except Exception as e:
        return error_result(f"Stop failed (rover may still be moving!): {e}")


@tool
def rover_lamp(on: bool = True) -> Dict[str, Any]:
    """Toggle the rover's headlamp.

    Args:
        on: True to switch lamp on, False to switch off.

    Returns:
        Dict with status.
    """
    try:
        _send(0.0, 0.0, lamp=1 if on else 0)
        return ok_result(f"Lamp {'ON' if on else 'OFF'}.")
    except Exception as e:
        return error_result(f"Lamp command failed: {e}")
