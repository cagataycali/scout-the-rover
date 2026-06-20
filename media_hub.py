"""📡 MediaHub — single-drainer fan-out for the rover's mic + cameras.

The Earth Rover Mini exposes ONE mic and ONE camera pair through the SDK, but
many consumers want them at once: the dataset recorder, the voice listener, the
voice agent, YOLO detection, Cosmos reasoning, the dashboard, the agent itself.

If each polls the SDK independently they (a) duplicate WebRTC round-trips,
(b) get time-misaligned frames, and (c) — for the mic — STEAL each other's
audio (the /rover-mic buffer empties on read).

MediaHub fixes this: ONE drainer per stream pulls from the SDK at a steady
rate and fans the data out to all subscribers. Consumers either:
  • pull the LATEST sample (agent/dashboard: "what do you see right now"), or
  • SUBSCRIBE to every sample (recorder/YOLO/Cosmos: aligned stream).

Because every subscriber sees the SAME frame at the SAME timestamp, we can
generate PARALLEL aligned data from one capture: the recorder writes the frame
to the LeRobot episode while YOLO annotates it and Cosmos buffers the clip —
all perfectly synchronized with the action/telemetry.

Transport:
  • In-process: import and use the singleton HUB directly.
  • Network (cross-container): a WebSocket server (/media/<stream>) streams
    base64 frames/audio to remote subscribers; a REST /latest/<stream> serves
    the newest sample.

Backward-compatible: if the hub isn't running, helpers in media_client.py fall
back to direct SDK calls so nothing breaks.

Streams:
  front   JPEG base64 frames   (from /v2/front)
  rear    JPEG base64 frames   (from /v2/rear, if present)
  mic     PCM16 base64 chunks  (from /rover-mic)
  data    telemetry JSON       (from /data)  — cheap, lets subscribers align
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

import requests

SDK_URL = os.getenv("ROVER_SDK_URL", "http://localhost:8002").rstrip("/")
HUB_PORT = int(os.getenv("MEDIA_HUB_PORT", "8090"))

# Per-stream capture cadence (seconds between SDK pulls).
VIDEO_INTERVAL = float(os.getenv("MEDIA_HUB_VIDEO_INTERVAL", "0.1"))   # ~10 fps
MIC_INTERVAL = float(os.getenv("MEDIA_HUB_MIC_INTERVAL", "0.1"))
DATA_INTERVAL = float(os.getenv("MEDIA_HUB_DATA_INTERVAL", "0.1"))
ENABLE_REAR = os.getenv("MEDIA_HUB_REAR", "1") in ("1", "true", "True")
MIC_RATE = int(os.getenv("MEDIA_HUB_MIC_RATE", "16000"))


@dataclass
class Sample:
    """One captured media sample with a capture timestamp."""
    stream: str
    ts: float
    seq: int
    payload: Any            # b64 str (front/rear/mic) or dict (data)
    meta: Dict[str, Any] = field(default_factory=dict)


class _Stream:
    """A single fan-out stream: latest-sample cache + per-subscriber queues."""

    def __init__(self, name: str, ring: int = 64) -> None:
        self.name = name
        self.lock = threading.Lock()
        self.latest: Optional[Sample] = None
        self.ring: Deque[Sample] = deque(maxlen=ring)
        self._subs: List["queue_like"] = []   # list of thread-safe queues
        self._seq = 0

    def publish(self, payload: Any, meta: Optional[dict] = None) -> Sample:
        with self.lock:
            self._seq += 1
            s = Sample(self.name, time.time(), self._seq, payload, meta or {})
            self.latest = s
            self.ring.append(s)
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(s)
            except Exception:
                pass  # slow/full subscriber: drop rather than block the drainer
        return s

    def get_latest(self) -> Optional[Sample]:
        with self.lock:
            return self.latest

    def subscribe(self, maxsize: int = 128):
        import queue as _q
        q = _q.Queue(maxsize=maxsize)
        with self.lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q) -> None:
        with self.lock:
            if q in self._subs:
                self._subs.remove(q)

    def sub_count(self) -> int:
        with self.lock:
            return len(self._subs)


class MediaHub:
    """Owns the SDK streams; drains each on its own thread; fans out."""

    def __init__(self) -> None:
        self.streams: Dict[str, _Stream] = {
            "front": _Stream("front"),
            "rear": _Stream("rear"),
            "mic": _Stream("mic"),
            "data": _Stream("data"),
        }
        self._threads: Dict[str, threading.Thread] = {}
        self._running = threading.Event()
        self._has_rear = False
        self._mic_started = False
        self.stats: Dict[str, int] = {k: 0 for k in self.streams}

    # ── lifecycle ──────────────────────────────────────────────────────
    def start(self) -> None:
        if self._running.is_set():
            return
        self._running.set()
        self._start_mic()
        self._has_rear = self._probe_rear()
        targets = {
            "front": self._drain_video_loop,
            "mic": self._drain_mic_loop,
            "data": self._drain_data_loop,
        }
        if self._has_rear and ENABLE_REAR:
            targets["rear"] = self._drain_video_loop
        for name, fn in targets.items():
            t = threading.Thread(target=fn, args=(name,), daemon=True,
                                 name=f"mediahub-{name}")
            t.start()
            self._threads[name] = t
        print(f"📡 MediaHub started (rear={self._has_rear}, sdk={SDK_URL})", flush=True)

    def stop(self) -> None:
        self._running.clear()
        try:
            requests.post(f"{SDK_URL}/rover-mic/stop", timeout=5)
        except Exception:
            pass

    # ── SDK helpers ────────────────────────────────────────────────────
    def _probe_rear(self) -> bool:
        try:
            r = requests.get(f"{SDK_URL}/v2/rear", timeout=5)
            return r.status_code == 200 and bool(r.json().get("rear_frame"))
        except Exception:
            return False

    def _start_mic(self) -> None:
        try:
            r = requests.post(f"{SDK_URL}/rover-mic/start",
                              json={"rate": MIC_RATE}, timeout=10)
            self._mic_started = r.status_code == 200
        except Exception:
            self._mic_started = False

    # ── drain loops ────────────────────────────────────────────────────
    def _drain_video_loop(self, cam: str) -> None:
        url = f"{SDK_URL}/v2/{cam}"
        key = f"{cam}_frame"
        while self._running.is_set():
            t0 = time.time()
            try:
                r = requests.get(url, timeout=3)
                if r.status_code == 200:
                    b64 = r.json().get(key)
                    if b64:
                        self.streams[cam].publish(b64, {"fmt": "jpeg_b64"})
                        self.stats[cam] += 1
            except Exception:
                pass
            self._sleep_to(t0, VIDEO_INTERVAL)

    def _drain_mic_loop(self, _name: str) -> None:
        while self._running.is_set():
            t0 = time.time()
            try:
                if not self._mic_started:
                    self._start_mic()
                r = requests.get(f"{SDK_URL}/rover-mic", timeout=10)
                if r.status_code == 200:
                    j = r.json()
                    rate = int(j.get("rate", MIC_RATE))
                    for c in (j.get("chunks", []) or []):
                        self.streams["mic"].publish(c, {"rate": rate, "fmt": "pcm16_b64"})
                        self.stats["mic"] += 1
            except Exception:
                pass
            self._sleep_to(t0, MIC_INTERVAL)

    def _drain_data_loop(self, _name: str) -> None:
        while self._running.is_set():
            t0 = time.time()
            try:
                r = requests.get(f"{SDK_URL}/data", timeout=3)
                if r.status_code == 200:
                    self.streams["data"].publish(r.json(), {"fmt": "json"})
                    self.stats["data"] += 1
            except Exception:
                pass
            self._sleep_to(t0, DATA_INTERVAL)

    @staticmethod
    def _sleep_to(t0: float, interval: float) -> None:
        dt = interval - (time.time() - t0)
        if dt > 0:
            time.sleep(dt)

    # ── public consumer API (in-process) ───────────────────────────────
    def latest(self, stream: str) -> Optional[Sample]:
        st = self.streams.get(stream)
        return st.get_latest() if st else None

    def subscribe(self, stream: str, maxsize: int = 128):
        st = self.streams.get(stream)
        return st.subscribe(maxsize) if st else None

    def unsubscribe(self, stream: str, q) -> None:
        st = self.streams.get(stream)
        if st:
            st.unsubscribe(q)

    def status(self) -> Dict[str, Any]:
        return {
            "running": self._running.is_set(),
            "has_rear": self._has_rear,
            "mic_started": self._mic_started,
            "subscribers": {k: v.sub_count() for k, v in self.streams.items()},
            "counts": dict(self.stats),
            "sdk": SDK_URL,
        }


# Singleton
HUB = MediaHub()


# ── Network server (cross-container fan-out) ────────────────────────────────
def _build_app():
    """FastAPI app: REST /latest + WebSocket /media for remote subscribers."""
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.responses import JSONResponse

    app = FastAPI(title="scout-media-hub")

    @app.on_event("startup")
    def _startup():
        HUB.start()

    @app.get("/status")
    def status():
        return HUB.status()

    @app.get("/latest/{stream}")
    def latest(stream: str):
        s = HUB.latest(stream)
        if not s:
            return JSONResponse({"error": f"no sample for {stream}"}, status_code=404)
        return {"stream": s.stream, "ts": s.ts, "seq": s.seq,
                "payload": s.payload, "meta": s.meta}

    @app.get("/stream/{stream}")
    def stream_ndjson(stream: str):
        """HTTP chunked ndjson stream — one JSON sample per line. Robust across
        uvicorn/websockets version gaps; consumable with plain requests(stream=True)."""
        from fastapi.responses import StreamingResponse
        if stream not in HUB.streams:
            return JSONResponse({"error": f"unknown stream {stream}"}, status_code=404)

        def gen():
            q = HUB.subscribe(stream)
            try:
                while True:
                    try:
                        smp = q.get(timeout=5)
                    except Exception:
                        # keep-alive comment line
                        yield "\n"
                        continue
                    yield json.dumps({"stream": smp.stream, "ts": smp.ts,
                                      "seq": smp.seq, "payload": smp.payload,
                                      "meta": smp.meta}) + "\n"
            finally:
                HUB.unsubscribe(stream, q)

        return StreamingResponse(gen(), media_type="application/x-ndjson")

    @app.websocket("/media/{stream}")
    async def media_ws(ws: WebSocket, stream: str):
        await ws.accept()
        if stream not in HUB.streams:
            await ws.close(code=1003)
            return
        q = HUB.subscribe(stream)
        loop = asyncio.get_event_loop()
        try:
            while True:
                # block in a thread so we don't stall the event loop
                s = await loop.run_in_executor(None, q.get)
                await ws.send_text(json.dumps(
                    {"stream": s.stream, "ts": s.ts, "seq": s.seq,
                     "payload": s.payload, "meta": s.meta}))
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            HUB.unsubscribe(stream, q)

    return app


app = None
def _ensure_app():
    global app
    if app is None:
        app = _build_app()
    return app


def main() -> None:
    import uvicorn
    _ensure_app()
    print(f"📡 MediaHub serving on :{HUB_PORT} (ws /media/<stream>, GET /latest/<stream>, /status)", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=HUB_PORT, log_level="warning")


if __name__ == "__main__":
    main()
