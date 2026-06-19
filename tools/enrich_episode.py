"""🔬 Background episode enricher — populates the multimodal memory index.

Runs OFFLINE (after stop_episode, or via CLI) so it never competes with the
4 FPS live recorder for the SDK/CPU. For one episode it:

  1. picks KEYFRAMES (every N frames, or on motion/scene change)
  2. CLIP-embeds each keyframe image           → memory_index (image)
  3. embeds every reasoning event's text       → memory_index (text)
  4. [optional] YOLO object detection on keyframes → memory_index (object)
  5. [optional] Whisper transcribe the episode WAV → memory_index (audio)

Everything is keyed on frame_index → recall hits seek the replay scrubber.

CLI:
    python -m tools.enrich_episode <dataset_dir> <episode_index> [--yolo] [--whisper]
    python -m tools.enrich_episode <dataset_dir> --all [--yolo] [--whisper]

Env:
    SCOUT_KEYFRAME_STRIDE   keyframe every N frames (default 4 → ~1 FPS @ 4fps)
    SCOUT_KEYFRAME_MOTION   also keyframe when |action|>0 changes (default 1)
    SCOUT_YOLO_MODEL        ultralytics model (default yolov8n.pt)
    SCOUT_WHISPER_MODEL     faster-whisper size (default base)
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from .memory_index import get_index, embed_image, embed_text

KEYFRAME_STRIDE = int(os.getenv("SCOUT_KEYFRAME_STRIDE", "4"))
KEYFRAME_MOTION = os.getenv("SCOUT_KEYFRAME_MOTION", "1") == "1"


# ── frame extraction from the packed mp4 via PyAV (no full decode) ───────────
def _episode_window(dataset_dir: Path, episode_index: int):
    """Return (fps, video_from, video_to, from_idx, to_idx, video_path)."""
    import pandas as pd
    files = sorted(glob.glob(str(dataset_dir / "meta" / "episodes" / "*" / "*.parquet")))
    df = pd.concat([pd.read_parquet(f) for f in files])
    row = df[df["episode_index"] == episode_index].iloc[0]
    info = json.loads((dataset_dir / "meta" / "info.json").read_text())
    fps = float(info.get("fps", 4))
    vfrom = float(row["videos/observation.images.front/from_timestamp"])
    vto = float(row["videos/observation.images.front/to_timestamp"])
    vids = sorted(glob.glob(str(dataset_dir / "videos" / "observation.images.front" / "*" / "*.mp4")))
    return fps, vfrom, vto, int(row["dataset_from_index"]), int(row["dataset_to_index"]), Path(vids[0])


def _decode_keyframes(video_path: Path, vfrom: float, vto: float, fps: float,
                      keyframe_local: List[int]):
    """Yield (local_frame_index, PIL.Image) for requested local frames.

    local_frame_index is the frame within the EPISODE (0-based). Absolute
    video time = vfrom + local/fps.
    """
    import av
    from PIL import Image
    want = set(keyframe_local)
    container = av.open(str(video_path))
    stream = container.streams.video[0]
    # seek to episode start
    if vfrom > 0:
        container.seek(int(vfrom / stream.time_base), stream=stream, any_frame=False, backward=True)
    emitted = {}
    for frame in container.decode(stream):
        t = float(frame.pts * stream.time_base)
        if t < vfrom - 0.05:
            continue
        if t > vto + 0.05:
            break
        local = int(round((t - vfrom) * fps))
        if local in want and local not in emitted:
            emitted[local] = True
            yield local, frame.to_image()
            if len(emitted) >= len(want):
                break
    container.close()


def _select_keyframes(series: Dict[str, Any], stride: int) -> List[int]:
    """Pick keyframe LOCAL indices: every `stride`, plus motion transitions."""
    fi = series.get("frame_index", [])
    n = len(fi)
    if not n:
        return []
    # series frame_index are dataset-global within episode but start at 0 here
    base = fi[0]
    locals_ = [f - base for f in fi]
    picks = set(range(0, n, max(1, stride)))
    if KEYFRAME_MOTION and series.get("action"):
        acts = series["action"]
        prev_moving = False
        for i, a in enumerate(acts):
            moving = (abs(a[0]) + abs(a[1])) > 0.05 if len(a) >= 2 else False
            if moving != prev_moving:   # transition → keyframe
                picks.add(i)
            prev_moving = moving
    return sorted(locals_[i] for i in picks if i < n)


def _load_series(dataset_dir: Path, episode_index: int) -> Dict[str, Any]:
    import pandas as pd
    pqs = sorted(glob.glob(str(dataset_dir / "data" / "*" / "*.parquet")))
    frames = []
    for f in pqs:
        df = pd.read_parquet(f)
        if "episode_index" in df.columns:
            df = df[df["episode_index"] == episode_index]
        if len(df):
            frames.append(df)
    if not frames:
        return {"frame_index": [], "action": []}
    e = pd.concat(frames).sort_values("frame_index")
    return {
        "frame_index": [int(x) for x in e["frame_index"].tolist()],
        "action": [list(map(float, a)) for a in e["action"].tolist()] if "action" in e.columns else [],
    }


def _reasoning_rows(dataset_dir: Path, episode_index: int):
    db = dataset_dir / "reasoning" / "events.sqlite"
    if not db.exists():
        return []
    c = sqlite3.connect(str(db)); c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT wall_ts,frame_index,t_in_episode,type,tool_name,text,tool_input "
        "FROM reasoning_events WHERE episode_index=? ORDER BY seq", (episode_index,)
    ).fetchall()
    c.close()
    return rows


# ── optional: YOLO ───────────────────────────────────────────────────────────
_yolo = {"model": None}
def _get_yolo():
    if _yolo["model"] is None:
        from ultralytics import YOLO
        _yolo["model"] = YOLO(os.getenv("SCOUT_YOLO_MODEL", "yolov8n.pt"))
    return _yolo["model"]

def _detect(pil_image) -> List[str]:
    m = _get_yolo()
    res = m.predict(pil_image, verbose=False)
    labels = []
    for r in res:
        for c in r.boxes.cls.tolist():
            labels.append(m.names[int(c)])
    return labels


# ── optional: Whisper ────────────────────────────────────────────────────────
def _transcribe(wav_path: Path):
    """Yield (segment_text, start_s, end_s) using faster-whisper."""
    from faster_whisper import WhisperModel
    model = WhisperModel(os.getenv("SCOUT_WHISPER_MODEL", "base"),
                         device="cpu", compute_type="int8")
    segs, _ = model.transcribe(str(wav_path))
    for s in segs:
        yield s.text.strip(), float(s.start), float(s.end)


# ── main enrichment ──────────────────────────────────────────────────────────
def enrich_episode(dataset_dir, episode_index: int,
                   yolo: bool = False, whisper: bool = False,
                   verbose: bool = True) -> Dict[str, Any]:
    dataset_dir = Path(dataset_dir).resolve()
    idx = get_index(dataset_dir)
    # Idempotent re-enrich: clear any prior rows for this episode.
    try:
        idx.delete_episode(episode_index)
    except Exception:
        pass
    fps, vfrom, vto, fidx, tidx, vpath = _episode_window(dataset_dir, episode_index)
    series = _load_series(dataset_dir, episode_index)
    kf = _select_keyframes(series, KEYFRAME_STRIDE)
    counts = {"image": 0, "text": 0, "object": 0, "audio": 0}

    # episode start wall_ts (for absolute time on rows)
    anc = sqlite3.connect(str(dataset_dir / "reasoning" / "events.sqlite"))
    arow = anc.execute("SELECT start_wall_ts FROM episode_anchors WHERE episode_index=?",
                       (episode_index,)).fetchone()
    anc.close()
    ep_start = float(arow[0]) if arow else 0.0

    # 1+2 (+3) images / objects
    rows_batch = []
    if kf:
        if verbose: print(f"  decoding {len(kf)} keyframes from {vpath.name}…")
        for local, img in _decode_keyframes(vpath, vfrom, vto, fps, kf):
            wall = ep_start + local / fps
            objs = ""
            if yolo:
                try:
                    labels = _detect(img)
                    objs = ",".join(labels)
                    if labels:
                        idx.add_objects(labels, episode=episode_index, frame_index=local,
                                        wall_ts=wall, t_in_episode=local/fps)
                        counts["object"] += 1
                except Exception as e:
                    if verbose: print(f"    yolo fail f{local}: {e}")
            try:
                idx.add_image(local, img, episode=episode_index, wall_ts=wall,
                              t_in_episode=local/fps, objects=objs)
                counts["image"] += 1
            except Exception as e:
                if verbose: print(f"    clip fail f{local}: {e}")

    # 3-text: reasoning events (skip UI placeholder noise)
    _NOISE = {"[rear camera]", "[front camera]", "[image]",
              "[live rover view at turn start]"}
    for r in _reasoning_rows(dataset_dir, episode_index):
        txt = r["text"]
        if r["type"] == "tool_use":
            txt = f"{r['tool_name']}({(r['tool_input'] or '')[:80]})"
        ts = (txt or "").strip()
        if not ts or ts.lower() in _NOISE:
            continue
        try:
            idx.add_text(txt, episode=episode_index,
                         frame_index=r["frame_index"] if r["frame_index"] is not None else -1,
                         wall_ts=r["wall_ts"], source=f"reasoning:{r['type']}",
                         t_in_episode=r["t_in_episode"])
            counts["text"] += 1
        except Exception:
            pass

    # 5: whisper audio
    if whisper:
        # Match the episode's WAV. New datasets: episode_{idx:06d}.wav.
        # Legacy datasets (pre off-by-one fix) are 1-indexed → fall back +1.
        wav = dataset_dir / "audio" / f"episode_{episode_index:06d}.wav"
        if not wav.exists():
            alt = dataset_dir / "audio" / f"episode_{episode_index + 1:06d}.wav"
            if alt.exists():
                wav = alt
        if wav.exists():
            if verbose: print(f"  transcribing {wav.name}…")
            try:
                for seg_text, st, en in _transcribe(wav):
                    if not seg_text:
                        continue
                    local = int(round(st * fps))
                    idx.add_text(seg_text, episode=episode_index, frame_index=local,
                                 wall_ts=ep_start + st, source="whisper",
                                 t_in_episode=st, meta={"end_s": en})
                    counts["audio"] += 1
            except Exception as e:
                if verbose: print(f"    whisper fail: {e}")

    if verbose:
        print(f"✅ ep{episode_index} enriched: {counts}")
    return {"episode": episode_index, "counts": counts, "keyframes": len(kf)}


def enrich_all(dataset_dir, yolo=False, whisper=False, verbose=True):
    dataset_dir = Path(dataset_dir).resolve()
    info = json.loads((dataset_dir / "meta" / "info.json").read_text())
    total = int(info.get("total_episodes", 0))
    out = []
    for ep in range(total):
        out.append(enrich_episode(dataset_dir, ep, yolo, whisper, verbose))
    return out


def main():
    ap = argparse.ArgumentParser(description="enrich episode(s) into memory index")
    ap.add_argument("dataset_dir")
    ap.add_argument("episode", nargs="?", type=int)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--yolo", action="store_true")
    ap.add_argument("--whisper", action="store_true")
    a = ap.parse_args()
    if a.all:
        enrich_all(a.dataset_dir, a.yolo, a.whisper)
    elif a.episode is not None:
        enrich_episode(a.dataset_dir, a.episode, a.yolo, a.whisper)
    else:
        ap.error("pass an episode index or --all")
    print("stats:", json.dumps(get_index(a.dataset_dir).stats(), indent=2))


if __name__ == "__main__":
    main()
