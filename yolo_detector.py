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



def _parse_locate_output(text: str, W: int, H: int) -> list:
    """Parse LocateAnything-3B output.

    Format: <ref>LABEL</ref><box><x1><y1><x2><y2></box>  (coords quantized 0-1000),
    with <box>None</box> meaning 'not present'. Multiple boxes per ref allowed.
    """
    import re
    dets = []
    # Each <ref>..</ref> followed by one or more <box>..</box>
    for rm in re.finditer(r"<ref>(.*?)</ref>\s*((?:<box>.*?</box>\s*)+)", text, re.DOTALL):
        label = rm.group(1).strip() or "object"
        boxes_blob = rm.group(2)
        for bm in re.finditer(r"<box>(.*?)</box>", boxes_blob, re.DOTALL):
            inner = bm.group(1)
            if "none" in inner.lower():
                continue
            nums = re.findall(r"-?\d+\.?\d*", inner)
            if len(nums) < 4:
                continue
            x1, y1, x2, y2 = (float(n) for n in nums[:4])
            # coords are quantized to 0-1000 → rescale to pixels
            if max(x1, y1, x2, y2) <= 1000:
                x1, x2 = x1 / 1000.0 * W, x2 / 1000.0 * W
                y1, y2 = y1 / 1000.0 * H, y2 / 1000.0 * H
            dets.append({"cls": label, "conf": 1.0,
                         "xyxy": [round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)]})
    return dets


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
        """Load LocateAnything-3B via its official batch_utils runtime.

        The model ships custom inference code (batch_utils/) in its HF snapshot;
        we add that dir to sys.path and call generate_batch_hybrid().
        Query format uses '</c>' between categories, e.g. "person</c>chair</c>door".
        """
        try:
            import os, sys
            from huggingface_hub import snapshot_download
            snap = snapshot_download(LOCATE_MODEL,
                                     ignore_patterns=["assets/*", "*.mp4", "training_args.bin"])
            if snap not in sys.path:
                sys.path.insert(0, snap)
            os.environ.setdefault("LA_FLASH_MODEL", snap)
            os.environ.setdefault("LA_FLASH_ATTN", os.getenv("LOCATE_ATTN", "sdpa"))
            from batch_utils import generate_batch_hybrid, load as _la_load
            from batch_utils.hybrid_runtime import load_pil  # noqa: F401 (api parity)
            _la_load()
            self._la_generate = generate_batch_hybrid
            self.backend_name = "locate"
            import torch
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
            print(f"[{_now()}] 👁️ LocateAnything-3B loaded (official batch_utils, {self.device})", flush=True)
            return True
        except Exception as e:
            print(f"[{_now()}] 👁️ LocateAnything load failed ({e}); falling back to YOLO", flush=True)
            return False

    def _locate_query(self) -> str:
        # accept either '. ' separated or '</c>' separated env; normalize to '</c>'
        q = LOCATE_QUERY
        if "</c>" in q:
            return q
        cats = [c.strip().rstrip(".").strip() for c in q.replace(".", ",").split(",")]
        cats = [c for c in cats if c]
        return "</c>".join(cats) if cats else "object"

    def _predict_locate(self, img) -> list:
        """Run LocateAnything-3B grounding for the query. Returns dets list."""
        import re
        try:
            from PIL import Image
            pil = Image.fromarray(img)
            query = self._locate_query()
            texts = self._la_generate([(pil, query)], max_new_tokens=1024,
                                      temperature=0.0)
            text = texts[0] if texts else ""
            H, W = img.shape[:2]
            return _parse_locate_output(text, W, H)
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
        self._publish_detections(cam, sample.get("ts"), dets)

        # Align into the dataset episode if recording.
        self._write_aligned(cam, sample, dets)

    def _publish_detections(self, cam, ts, dets):
        """Push detections to the hub 'detections' stream so OTHER containers
        (the agent) can read live perception via media_client."""
        try:
            mc.publish_detections({"cam": cam, "ts": ts,
                                   "backend": getattr(self, 'backend_name', 'yolo'),
                                   "dets": dets})
        except Exception:
            pass

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
