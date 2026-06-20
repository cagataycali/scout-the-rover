"""📡 media_client — consumer-side access to rover media, hub-aware.

Consumers (recorder, listener, agent, YOLO, cosmos) use these helpers instead
of hitting the SDK directly. Resolution order:

  1. In-process HUB (same process imported media_hub) — zero-copy, fastest.
  2. Network hub (MEDIA_HUB_URL, default http://media:8090) — cross-container
     fan-out via REST /latest + WS /media.
  3. Direct SDK (ROVER_SDK_URL) — the original behavior, so nothing breaks if
     the hub isn't running.

This is what makes the fan-out OPTIONAL and SAFE to roll out incrementally.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
from typing import Any, Callable, Optional

import requests

SDK_URL = os.getenv("ROVER_SDK_URL", "http://localhost:8002").rstrip("/")
HUB_URL = os.getenv("MEDIA_HUB_URL", "").rstrip("/")  # e.g. http://media:8090
HUB_ENABLED = os.getenv("MEDIA_HUB_ENABLED", "auto")  # auto|on|off


def _inproc_hub():
    """Return the in-process HUB if media_hub is imported AND running."""
    try:
        import sys
        mod = sys.modules.get("media_hub")
        if mod and getattr(mod, "HUB", None) and mod.HUB._running.is_set():
            return mod.HUB
    except Exception:
        pass
    return None


def _hub_url() -> str:
    if HUB_URL:
        return HUB_URL
    # sensible default for docker-compose service name
    return os.getenv("MEDIA_HUB_DEFAULT_URL", "http://media:8090")


def hub_available() -> bool:
    if HUB_ENABLED == "off":
        return False
    if _inproc_hub() is not None:
        return True
    if HUB_ENABLED in ("on", "auto"):
        try:
            r = requests.get(f"{_hub_url()}/status", timeout=1.5)
            return r.status_code == 200
        except Exception:
            return False
    return False


# ── latest-sample pulls (agent / dashboard "what's now") ────────────────────
def latest_frame_b64(camera: str = "front") -> Optional[str]:
    """Newest JPEG base64 frame for 'front'/'rear'. Hub → SDK fallback."""
    h = _inproc_hub()
    if h:
        s = h.latest(camera)
        if s:
            return s.payload
    if HUB_ENABLED != "off":
        try:
            r = requests.get(f"{_hub_url()}/latest/{camera}", timeout=2)
            if r.status_code == 200:
                return r.json().get("payload")
        except Exception:
            pass
    # direct SDK fallback
    try:
        r = requests.get(f"{SDK_URL}/v2/{camera}", timeout=3)
        if r.status_code == 200:
            return r.json().get(f"{camera}_frame")
    except Exception:
        pass
    return None


def latest_data() -> Optional[dict]:
    """Newest telemetry dict. Hub → SDK fallback."""
    h = _inproc_hub()
    if h:
        s = h.latest("data")
        if s:
            return s.payload
    if HUB_ENABLED != "off":
        try:
            r = requests.get(f"{_hub_url()}/latest/data", timeout=2)
            if r.status_code == 200:
                return r.json().get("payload")
        except Exception:
            pass
    try:
        r = requests.get(f"{SDK_URL}/data", timeout=3)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


# ── streaming subscriptions (recorder / YOLO / cosmos / listener) ───────────
class Subscription:
    """A background subscription delivering every sample to a callback.

    Uses the in-process hub queue if available, else a network WS, else (for
    audio/video) a polling fallback against the SDK. Stop with .stop()."""

    def __init__(self, stream: str, on_sample: Callable[[dict], None],
                 poll_interval: float = 0.1) -> None:
        self.stream = stream
        self.on_sample = on_sample
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.mode = None

    def start(self) -> "Subscription":
        h = _inproc_hub()
        if h:
            self.mode = "inproc"
            self._thread = threading.Thread(target=self._inproc_loop, args=(h,),
                                            daemon=True)
        elif HUB_ENABLED != "off" and hub_available():
            self.mode = "http-stream"
            self._thread = threading.Thread(target=self._ws_loop, daemon=True)
        else:
            self.mode = "sdk-poll"
            self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _emit(self, sample: dict) -> None:
        try:
            self.on_sample(sample)
        except Exception:
            pass

    def _inproc_loop(self, hub) -> None:
        q = hub.subscribe(self.stream)
        try:
            while not self._stop.is_set():
                try:
                    s = q.get(timeout=0.25)
                except Exception:
                    continue
                self._emit({"stream": s.stream, "ts": s.ts, "seq": s.seq,
                            "payload": s.payload, "meta": s.meta})
        finally:
            hub.unsubscribe(self.stream, q)

    def _ws_loop(self) -> None:
        # HTTP chunked ndjson stream (robust across uvicorn/websockets gaps).
        url = f"{_hub_url()}/stream/{self.stream}"
        while not self._stop.is_set():
            try:
                with requests.get(url, stream=True, timeout=(5, 60)) as r:
                    if r.status_code != 200:
                        time.sleep(1); continue
                    for line in r.iter_lines(decode_unicode=True):
                        if self._stop.is_set():
                            break
                        if not line:
                            continue
                        try:
                            self._emit(json.loads(line))
                        except Exception:
                            continue
            except Exception:
                time.sleep(1)

    def _poll_loop(self) -> None:
        seq = 0
        while not self._stop.is_set():
            t0 = time.time()
            if self.stream in ("front", "rear"):
                b64 = latest_frame_b64(self.stream)
                if b64:
                    seq += 1
                    self._emit({"stream": self.stream, "ts": time.time(),
                                "seq": seq, "payload": b64, "meta": {"fmt": "jpeg_b64"}})
            elif self.stream == "data":
                d = latest_data()
                if d:
                    seq += 1
                    self._emit({"stream": "data", "ts": time.time(),
                                "seq": seq, "payload": d, "meta": {"fmt": "json"}})
            elif self.stream == "mic":
                # direct mic drain fallback (note: competes for the buffer!)
                try:
                    r = requests.get(f"{SDK_URL}/rover-mic", timeout=10)
                    if r.status_code == 200:
                        j = r.json(); rate = int(j.get("rate", 16000))
                        for c in (j.get("chunks", []) or []):
                            seq += 1
                            self._emit({"stream": "mic", "ts": time.time(),
                                        "seq": seq, "payload": c,
                                        "meta": {"rate": rate, "fmt": "pcm16_b64"}})
                except Exception:
                    pass
            dt = self.poll_interval - (time.time() - t0)
            if dt > 0:
                time.sleep(dt)


def subscribe(stream: str, on_sample: Callable[[dict], None],
              poll_interval: float = 0.1) -> Subscription:
    """Start a background subscription to a media stream. Returns a handle."""
    return Subscription(stream, on_sample, poll_interval).start()


def decode_jpeg_b64(b64: str):
    """Decode a JPEG base64 frame to an RGB numpy array (or None)."""
    try:
        import numpy as np, cv2
        raw = base64.b64decode(b64)
        arr = np.frombuffer(raw, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return None
        return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    except Exception:
        return None


# ── detections stream (perception fan-in) ───────────────────────────────────
def publish_detections(payload: dict) -> bool:
    """Detector → hub: publish a detections payload. In-proc or network."""
    h = _inproc_hub()
    if h:
        h.publish("detections", payload, {"fmt": "json"})
        return True
    if HUB_ENABLED != "off":
        try:
            requests.post(f"{_hub_url()}/publish/detections",
                          json={"payload": payload, "meta": {"fmt": "json"}}, timeout=2)
            return True
        except Exception:
            return False
    return False


def latest_detections() -> Optional[dict]:
    """Agent/dashboard → hub: newest detections payload (or None)."""
    h = _inproc_hub()
    if h:
        s = h.latest("detections")
        return s.payload if s else None
    if HUB_ENABLED != "off":
        try:
            r = requests.get(f"{_hub_url()}/latest/detections", timeout=2)
            if r.status_code == 200:
                return r.json().get("payload")
        except Exception:
            pass
    return None
