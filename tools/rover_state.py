"""📊 Earth Rover telemetry tools — battery, GPS, IMU, speaker.

Wraps SDK endpoints:
  /data    → full sensor snapshot (battery, GPS, IMU, RPMs, ...)
  /speak   → onboard speaker TTS
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from strands import tool

from ._rover_common import error_result, sdk_get, sdk_post

logger = logging.getLogger(__name__)


@tool
def rover_state() -> Dict[str, Any]:
    """Read full rover telemetry: battery, signal, GPS, orientation, IMU.

    Returns:
        Dict with status, human-readable summary, and full JSON telemetry.
    """
    try:
        resp = sdk_get("/data")
    except Exception as e:
        return error_result(f"SDK unreachable: {e}")

    if resp.status_code != 200:
        return error_result(f"/data HTTP {resp.status_code}: {resp.text[:200]}")

    d = resp.json()
    gps_ok = d.get("gps_signal", 0) > 0 and d.get("latitude", 1000) != 1000
    summary = (
        f"🔋 {d.get('battery', '?')}% | 📶 signal {d.get('signal_level', '?')}/4 | "
        f"🧭 {d.get('orientation', '?')}° | 🚗 speed {d.get('speed', '?')} | "
        f"💡 lamp {'on' if d.get('lamp') else 'off'} | "
        f"📍 GPS {'%.6f, %.6f' % (d['latitude'], d['longitude']) if gps_ok else 'no fix'} | "
        f"⚡ {d.get('voltage', '?')}V {d.get('current', '?')}mA"
    )
    return {
        "status": "success",
        "content": [{"text": summary}, {"json": d}],
    }


@tool
def rover_speak(text: str) -> Dict[str, Any]:
    """Make the rover speak through its onboard speaker (TTS).

    Args:
        text: What the rover should say.

    Returns:
        Dict with status.
    """
    if not text or not text.strip():
        return error_result("text is required")
    try:
        resp = sdk_post("/speak", json={"text": text}, timeout=120)
    except Exception as e:
        return error_result(f"SDK unreachable: {e}")

    if resp.status_code != 200:
        return error_result(f"/speak HTTP {resp.status_code}: {resp.text[:200]}")
    return {"status": "success", "content": [{"text": f"Rover said: {text}"}]}
