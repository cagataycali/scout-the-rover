"""🌌 cosmos_buffer — rolling video clips from the MediaHub for Cosmos reasoning.

Subscribes to the MediaHub 'front' (and optionally 'rear') stream and writes
rolling MP4 clips to disk so the Cosmos world-model tools (cosmos3_reason /
caption / temporal / embodied) can run on EXACTLY what the rover saw — time
aligned with the dataset the recorder is writing from the SAME hub frames.

Two products:
  • clips/      rolling N-second MP4 segments (default 4s), newest kept.
  • latest.mp4  symlink/copy to the most recent finished clip — a stable path
    Cosmos tools can point at: cosmos3_reason("... <video>cosmos_clips/latest.mp4</video>").

Run as a peer service (make cosmos-buffer) or import get_buffer() in-process.

Env:
  ROVER_SDK_URL / MEDIA_HUB_URL    media source (via media_client)
  COSMOS_BUFFER_DIR     output dir (default ./cosmos_clips)
  COSMOS_CLIP_SECONDS   clip length (default 4)
  COSMOS_CLIP_FPS       encode fps (default 10)
  COSMOS_KEEP_CLIPS     how many finished clips to retain (default 10)
  COSMOS_BUFFER_CAMERA  front|rear (default front)
"""
from __future__ import annotations

import os
import shutil
import signal
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Deque, Optional, Tuple

import numpy as np

import media_client as mc

OUT_DIR = Path(os.getenv("COSMOS_BUFFER_DIR", "cosmos_clips"))
CLIP_SECONDS = float(os.getenv("COSMOS_CLIP_SECONDS", "4"))
CLIP_FPS = int(os.getenv("COSMOS_CLIP_FPS", "10"))
KEEP_CLIPS = int(os.getenv("COSMOS_KEEP_CLIPS", "10"))
CAMERA = os.getenv("COSMOS_BUFFER_CAMERA", "front")


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


class CosmosBuffer:
    """Rolling clip writer fed by a MediaHub subscription."""

    def __init__(self) -> None:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / "clips").mkdir(exist_ok=True)
        self._frames: Deque[Tuple[float, np.ndarray]] = deque()
        self._lock = threading.Lock()
        self._sub = None
        self._writer_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._clip_idx = 0
        self.latest_clip: Optional[str] = None

    def start(self) -> "CosmosBuffer":
        self._sub = mc.subscribe(CAMERA, self._on_frame)
        self._writer_thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._writer_thread.start()
        print(f"🌌 cosmos_buffer started (cam={CAMERA}, {CLIP_SECONDS}s @ {CLIP_FPS}fps, "
              f"keep={KEEP_CLIPS}) → {OUT_DIR}/ via {getattr(self._sub,'mode','?')}", flush=True)
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._sub:
            try:
                self._sub.stop()
            except Exception:
                pass

    def _on_frame(self, sample: dict) -> None:
        b64 = sample.get("payload")
        if not b64:
            return
        img = mc.decode_jpeg_b64(b64)
        if img is None:
            return
        with self._lock:
            self._frames.append((sample.get("ts", time.time()), img))

    def _drain_window(self) -> list:
        """Pop frames covering ~CLIP_SECONDS."""
        with self._lock:
            if not self._frames:
                return []
            frames = list(self._frames)
            self._frames.clear()
        return frames

    def _writer_loop(self) -> None:
        try:
            import imageio.v2 as imageio  # ships with the image stack
        except Exception:
            imageio = None
        while not self._stop.is_set():
            time.sleep(CLIP_SECONDS)
            frames = self._drain_window()
            if len(frames) < 2:
                continue
            self._clip_idx += 1
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            clip_path = OUT_DIR / "clips" / f"clip_{self._clip_idx:06d}_{ts}.mp4"
            imgs = [f[1] for f in frames]
            try:
                if imageio is not None:
                    imageio.mimwrite(str(clip_path), imgs, fps=CLIP_FPS,
                                     codec="libx264", quality=7)
                else:
                    self._write_mp4_cv2(str(clip_path), imgs)
                # update stable latest pointer
                latest = OUT_DIR / "latest.mp4"
                try:
                    shutil.copyfile(clip_path, latest)
                except Exception:
                    pass
                self.latest_clip = str(clip_path)
                print(f"[{_now()}] 🌌 clip {clip_path.name} ({len(imgs)} frames) → latest.mp4", flush=True)
                self._prune()
            except Exception as e:
                print(f"[{_now()}] 🌌 clip write error: {e}", flush=True)

    @staticmethod
    def _write_mp4_cv2(path: str, imgs: list) -> None:
        import cv2
        h, w = imgs[0].shape[:2]
        vw = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), CLIP_FPS, (w, h))
        for im in imgs:
            vw.write(cv2.cvtColor(im, cv2.COLOR_RGB2BGR))
        vw.release()

    def _prune(self) -> None:
        clips = sorted((OUT_DIR / "clips").glob("clip_*.mp4"))
        for old in clips[:-KEEP_CLIPS]:
            try:
                old.unlink()
            except Exception:
                pass


_BUF: Optional[CosmosBuffer] = None
def get_buffer() -> CosmosBuffer:
    global _BUF
    if _BUF is None:
        _BUF = CosmosBuffer()
    return _BUF


def main() -> None:
    stop = {"flag": False}
    def _sig(*_):
        stop["flag"] = True
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    buf = get_buffer().start()
    while not stop["flag"]:
        time.sleep(0.5)
    buf.stop()
    print("🌌 cosmos_buffer stopped")


if __name__ == "__main__":
    main()
