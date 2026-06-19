"""📷 Earth Rover camera tools — inline Converse image returns.

The rover streams front (and rear on "zero" bots) cameras through the
Earth Rovers SDK headless browser. These tools fetch frames and return
them as PROPER Strands inline image content blocks so the model can SEE:

    {"image": {"format": "jpeg", "source": {"bytes": b"..."}}}

Tools:
  rover_see        → front (+rear if available) camera frame(s), inline
  rover_screenshot → composite views (front/rear/map) via /screenshot, inline
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from strands import tool

from ._rover_common import (
    b64_to_image_block,
    error_result,
    sdk_get,
)

logger = logging.getLogger(__name__)

CAPTURE_DIR = Path("captures")


@tool
def rover_see(
    camera: str = "front",
    save: bool = False,
) -> Dict[str, Any]:
    """Look through the rover's cameras. Returns frame(s) inline so you can SEE.

    Uses the SDK /v2 frame endpoints (fast, no disk I/O on the SDK side).

    Args:
        camera: "front" (default), "rear", or "both".
            Rear camera only exists on "zero" bot types — if unavailable,
            the tool degrades gracefully to front only.
        save: Also write JPEG(s) to ./captures/ and report the path(s).

    Returns:
        Dict with status and content: text summary + inline image block(s).
    """
    camera = camera.lower().strip()
    if camera not in ("front", "rear", "both"):
        return error_result(f"Invalid camera '{camera}'. Use: front, rear, both")

    frames: list[tuple[str, str]] = []  # (label, b64)
    errors: list[str] = []

    wanted = ["front", "rear"] if camera == "both" else [camera]
    for cam in wanted:
        try:
            resp = sdk_get(f"/v2/{cam}")
            if resp.status_code == 200:
                data = resp.json()
                b64 = data.get(f"{cam}_frame")
                if b64:
                    frames.append((cam, b64))
                else:
                    errors.append(f"{cam}: empty frame")
            else:
                errors.append(f"{cam}: HTTP {resp.status_code}")
        except Exception as e:
            errors.append(f"{cam}: {e}")

    if not frames:
        return error_result(
            "No frames available. Is the SDK running and the rover online? "
            f"Errors: {'; '.join(errors)}"
        )

    content: list[Dict[str, Any]] = []
    saved_paths = []
    for label, b64 in frames:
        block = b64_to_image_block(b64)
        if save:
            CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            path = CAPTURE_DIR / f"{label}-{ts}.jpg"
            path.write_bytes(block["image"]["source"]["bytes"])
            saved_paths.append(str(path))
        content.append({"text": f"[{label} camera]"})
        content.append(block)

    summary = f"Captured {len(frames)} frame(s): {', '.join(l for l, _ in frames)}"
    if saved_paths:
        summary += f" | saved: {', '.join(saved_paths)}"
    if errors:
        summary += f" | warnings: {'; '.join(errors)}"

    return {"status": "success", "content": [{"text": summary}, *content]}


@tool
def rover_screenshot(views: str = "front,rear,map") -> Dict[str, Any]:
    """Capture composite rover views (front/rear/map) via SDK /screenshot.

    Slower than rover_see (renders via headless browser screenshots) but
    includes the MAP view with the rover's position.

    Args:
        views: Comma-separated subset of: front, rear, map.

    Returns:
        Dict with status and content: text summary + inline image blocks.
    """
    valid = {"front", "rear", "map"}
    view_list = [v.strip() for v in views.split(",") if v.strip()]
    bad = [v for v in view_list if v not in valid]
    if bad:
        return error_result(f"Invalid views: {bad}. Valid: {sorted(valid)}")

    try:
        resp = sdk_get("/screenshot", view_types=",".join(view_list))
    except Exception as e:
        return error_result(f"SDK unreachable: {e}")

    if resp.status_code != 200:
        return error_result(f"/screenshot HTTP {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    content: list[Dict[str, Any]] = []
    got = []
    for view in view_list:
        b64 = data.get(f"{view}_frame")
        if b64:
            content.append({"text": f"[{view} view]"})
            content.append(b64_to_image_block(b64))
            got.append(view)

    if not got:
        return error_result("No views returned by SDK")

    return {
        "status": "success",
        "content": [{"text": f"Views captured: {', '.join(got)}"}, *content],
    }
