"""👁️ yolo_detector — live object detection on the MediaHub video stream,
annotated INTO the dataset episode (aligned sidecar).

Subscribes to the MediaHub 'front' (and optionally 'rear') stream, runs an
Ultralytics YOLO model on each frame, and — when the recorder is actively
recording an episode — writes per-frame detections to a sidecar aligned with
the LeRobot dataset:

    <dataset_root>/detections/episode_<NNNNNN>.jsonl
        {"frame": <idx>, "ts": <capt_ts>, "cam": "front",
         "dets": [{"cls": "person", "conf": 0.91, "xyxy": [..]}, ...]}

This gives PARALLEL, time-aligned data from the SAME captured frame the
recorder writes to the episode video — no extra SDK round-trips, no drift.
Detections can later be merged into training as an auxiliary supervision
signal (open-vocab grounding, affordance, safety masking, etc.).

It also exposes the latest detections in-process (get_detector().latest) so
the agent / dashboard can show "what YOLO sees right now".

Env:
  MEDIA_HUB_URL / ROVER_SDK_URL    media source (via media_client)
  YOLO_MODEL          ultralytics weights (default yolov8n.pt — auto-downloads)
  YOLO_CONF           confidence threshold (default 0.35)
  YOLO_CAMERA         front|rear|both (default front)
  YOLO_DEVICE         cuda|cpu (default auto)
  YOLO_EVERY_N        run on every Nth frame (default 1 = all)
  YOLO_IMGSZ          inference size (default 640)
"""
from __future__ import annotations

import json
import os
import signal
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import media_client as mc

YOLO_MODEL = os.getenv("YOLO_MODEL", "yolov8n.pt")
YOLO_CONF = float(os.getenv("YOLO_CONF", "0.35"))
YOLO_CAMERA = os.getenv("YOLO_CAMERA", "front")
YOLO_DEVICE = os.getenv("YOLO_DEVICE", "auto")
YOLO_EVERY_N = int(os.getenv("YOLO_EVERY_N", "1"))
YOLO_IMGSZ = int(os.getenv("YOLO_IMGSZ", "640"))

# Detector backend: "yolo" (Ultralytics, fixed COCO classes, fast) or
# "locate" (NVIDIA LocateAnything-3B: open-vocabulary, language-prompted grounding).
DET_BACKEND = os.getenv("DET_BACKEND", "yolo").lower()
# For the locate backend: the natural-language grounding query (what to find).
LOCATE_QUERY = os.getenv("LOCATE_QUERY", "person. chair. sofa. table. door. plant. tv. cup. bottle.")
LOCATE_MODEL = os.getenv("LOCATE_MODEL", "nvidia/LocateAnything-3B")


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _recorder_status() -> Optional[dict]:
    """Best-effort: ask the recorder engine if it's recording + where."""
    try:
        from tools._recorder_engine import get_engine
        return get_engine().status()
    except Exception:
        return None


class YoloDetector:
    def __init__(self) -> None:
        self.model = None
        self.device = None
        self.names: Dict[int, str] = {}
        self._subs: List[Any] = []
        self._stop = threading.Event()
        self._frame_no = 0
        self.latest: Dict[str, Any] = {}      # cam -> last detections list
        self._jsonl_paths: Dict[str, Path] = {}
        self._open_episode: Optional[int] = None

    def _load(self) -> bool:
        try:
            from ultralytics import YOLO
            self.model = YOLO(YOLO_MODEL)
            dev = YOLO_DEVICE
            if dev == "auto":
                try:
                    import torch
                    dev = "cuda" if torch.cuda.is_available() else "cpu"
                except Exception:
                    dev = "cpu"
            self.device = dev
            self.names = self.model.names
            return True
        except Exception as e:
            print(f"[{_now()}] 👁️ YOLO load failed: {e}", flush=True)
            return False


    # ── LocateAnything-3B backend (open-vocab, language-prompted grounding) ──
    def _load_locate(self) -> bool:
        try:
            import torch
            from transformers import AutoModel, AutoProcessor, AutoModelForCausalLM
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
            dtype = torch.bfloat16 if self.device == "cuda" else torch.float32
            # LocateAnything ships as a custom architecture → trust_remote_code.
            try:
                self.model = AutoModelForCausalLM.from_pretrained(
                    LOCATE_MODEL, torch_dtype=dtype, trust_remote_code=True,
                    device_map=self.device)
            except Exception:
                self.model = AutoModel.from_pretrained(
                    LOCATE_MODEL, torch_dtype=dtype, trust_remote_code=True,
                    device_map=self.device)
            self.processor = AutoProcessor.from_pretrained(
                LOCATE_MODEL, trust_remote_code=True)
            self.backend_name = "locate"
            return True
        except Exception as e:
            print(f"[{_now()}] 👁️ LocateAnything load failed ({e}); falling back to YOLO", flush=True)
            return False

    def _predict_locate(self, img) -> list:
        """Run LocateAnything-3B grounding for LOCATE_QUERY. Returns dets list."""
        import re
        try:
            from PIL import Image
            import numpy as _np
            pil = Image.fromarray(img)
            prompt = f"Detect: {LOCATE_QUERY}"
            inputs = self.processor(images=pil, text=prompt, return_tensors="pt").to(self.device)
            out = self.model.generate(**inputs, max_new_tokens=1024)
            text = self.processor.batch_decode(out, skip_special_tokens=False)[0]
            # Parse <box> x1, y1, x2, y2 </box> (coords may be normalized 0-1000)
            H, W = img.shape[:2]
            dets = []
            for m in re.finditer(r"<box>\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)\s*</box>", text):
                x1, y1, x2, y2 = (float(v) for v in m.groups())
                # if quantized to 0-1000, rescale to pixels
                if max(x1, y1, x2, y2) <= 1000 and max(x1,y1,x2,y2) > 1.5:
                    x1, x2 = x1/1000*W, x2/1000*W
                    y1, y2 = y1/1000*H, y2/1000*H
                dets.append({"cls": "object", "conf": 1.0,
                             "xyxy": [round(x1,1), round(y1,1), round(x2,1), round(y2,1)]})
            return dets
        except Exception as e:
            return []

    def start(self) -> "YoloDetector":
        self.backend_name = "yolo"
        loaded = self._load_locate() if DET_BACKEND == "locate" else False
        if not loaded:
            if not self._load():
                print(f"[{_now()}] 👁️ detector disabled (no model)", flush=True)
                return self
        cams = ["front", "rear"] if YOLO_CAMERA == "both" else [YOLO_CAMERA]
        for cam in cams:
            self._subs.append(mc.subscribe(cam, (lambda c: (lambda s: self._on_frame(c, s)))(cam)))
        print(f"[{_now()}] 👁️ yolo_detector started (model={YOLO_MODEL}, dev={self.device}, "
              f"cams={cams}, conf={YOLO_CONF})", flush=True)
        return self

    def stop(self) -> None:
        self._stop.set()
        for s in self._subs:
            try:
                s.stop()
            except Exception:
                pass

    def _on_frame(self, cam: str, sample: dict) -> None:
        if self._stop.is_set():
            return
        self._frame_no += 1
        if YOLO_EVERY_N > 1 and (self._frame_no % YOLO_EVERY_N) != 0:
            return
        b64 = sample.get("payload")
        if not b64:
            return
        img = mc.decode_jpeg_b64(b64)
        if img is None:
            return
        if getattr(self, "backend_name", "yolo") == "locate":
            dets = self._predict_locate(img)
        else:
            try:
                res = self.model.predict(img, conf=YOLO_CONF, imgsz=YOLO_IMGSZ,
                                         device=self.device, verbose=False)[0]
            except Exception:
                return
            dets = []
            try:
                for b in res.boxes:
                    cls = int(b.cls[0])
                    dets.append({
                        "cls": self.names.get(cls, str(cls)),
                        "conf": round(float(b.conf[0]), 3),
                        "xyxy": [round(float(x), 1) for x in b.xyxy[0].tolist()],
                    })
            except Exception:
                pass
        self.latest[cam] = {"ts": sample.get("ts"), "dets": dets, "n": len(dets)}

        # Align into the dataset episode if recording.
        self._write_aligned(cam, sample, dets)

    def _write_aligned(self, cam: str, sample: dict, dets: list) -> None:
        st = _recorder_status()
        if not st or not st.get("recording"):
            self._open_episode = None
            return
        ep = st.get("current_episode")
        frame_idx = st.get("current_frames", 0)
        root = st.get("dataset_root")
        if ep is None or not root:
            return
        det_dir = Path(root) / "detections"
        try:
            det_dir.mkdir(parents=True, exist_ok=True)
            path = det_dir / f"episode_{int(ep):06d}.jsonl"
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "frame": frame_idx, "ts": sample.get("ts"),
                    "cam": cam, "dets": dets,
                }) + "\n")
        except Exception:
            pass


_DET: Optional[YoloDetector] = None
def get_detector() -> YoloDetector:
    global _DET
    if _DET is None:
        _DET = YoloDetector()
    return _DET


def latest_context() -> str:
    """One-line summary of current detections for prompt injection (optional)."""
    d = get_detector()
    if not d.latest:
        return ""
    parts = []
    for cam, info in d.latest.items():
        names = ", ".join(sorted({x["cls"] for x in info.get("dets", [])})) or "nothing"
        parts.append(f"{cam}: {names}")
    return "👁️ YOLO sees → " + " | ".join(parts)


def main() -> None:
    stop = {"flag": False}
    def _sig(*_):
        stop["flag"] = True
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    det = get_detector().start()
    last_print = 0.0
    while not stop["flag"]:
        time.sleep(0.5)
        if time.time() - last_print > 5 and det.latest:
            print(f"[{_now()}] {latest_context()}", flush=True)
            last_print = time.time()
    det.stop()
    print("👁️ yolo_detector stopped")


if __name__ == "__main__":
    main()
