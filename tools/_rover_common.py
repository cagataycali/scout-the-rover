"""Shared helpers for Earth Rover Mini tools.

All tools talk to the local Earth Rovers SDK (frodobots earth-rovers-sdk),
which must be running (default: http://localhost:8001).

The SDK proxies commands to the physical rover over WebRTC/RTM via a
headless Chrome session, so tools here are simple HTTP calls.
"""
from __future__ import annotations

import base64
import os
from typing import Any, Dict, Optional

import requests

DEFAULT_SDK_URL = os.getenv("ROVER_SDK_URL", "http://localhost:8001")
DEFAULT_TIMEOUT = float(os.getenv("ROVER_HTTP_TIMEOUT", "60"))

# Agent turn-direction sign. The SDK/hardware already matches the agent's
# +angular=left convention, so NO inversion by default (1). The dashboard's
# manual WASD/joystick path uses its own DASH_TURN_SIGN (it was observed
# reversed there). Override here with ROVER_TURN_SIGN=-1 only if the AGENT's
# turns are reversed.
TURN_SIGN = float(os.getenv("ROVER_TURN_SIGN", "1"))


def apply_turn_sign(angular: float) -> float:
    """Correct angular for this rover's physical turn direction."""
    return angular * TURN_SIGN


def sdk_url(path: str) -> str:
    base = os.getenv("ROVER_SDK_URL", DEFAULT_SDK_URL).rstrip("/")
    return f"{base}{path}"


def sdk_get(path: str, timeout: float = DEFAULT_TIMEOUT, **params) -> requests.Response:
    return requests.get(sdk_url(path), params=params or None, timeout=timeout)


def sdk_post(path: str, json: Optional[Dict[str, Any]] = None,
             timeout: float = DEFAULT_TIMEOUT) -> requests.Response:
    return requests.post(sdk_url(path), json=json, timeout=timeout)


def detect_image_format(data: bytes) -> str:
    """Detect image format from magic bytes (SDK may emit png/jpeg/webp)."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "jpeg"  # safe default


def b64_to_image_block(b64_data: str) -> Dict[str, Any]:
    """Decode base64 frame → Strands/Converse inline image content block."""
    raw = base64.b64decode(b64_data)
    return {"image": {"format": detect_image_format(raw), "source": {"bytes": raw}}}


def error_result(msg: str) -> Dict[str, Any]:
    return {"status": "error", "content": [{"text": msg}]}


def ok_result(text: str, extra_content: Optional[list] = None) -> Dict[str, Any]:
    content = [{"text": text}]
    if extra_content:
        content.extend(extra_content)
    return {"status": "success", "content": content}
