"""🎞️ Dataset replay API — browse recorded episodes, scrub the timeline,
overlay reasoning events. Mounts onto the existing dashboard FastAPI app.

Bridges three things on one timeline:
  • the packed LeRobot mp4 (front/rear) via per-episode from/to_timestamp
  • per-frame action + state from the parquet
  • the ECoT reasoning events (events.sqlite) bound by frame_index

LeRobot v3 packs MANY episodes into ONE mp4. meta/episodes/*.parquet gives each
episode its [from_timestamp, to_timestamp] window inside that file — that window
IS the scrubber range. Frame f within episode e maps to video time:
    video_t = from_timestamp + (f / fps)
"""
from __future__ import annotations

import glob
import json
import os
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

ROOT = Path(__file__).resolve().parent
DATASETS = Path(os.getenv("ROVER_DATASET_ROOT", str(ROOT / "datasets"))).resolve()


# ── dataset discovery ────────────────────────────────────────────────────────
def _list_datasets() -> List[Dict[str, Any]]:
    """Discover LeRobot datasets at ANY depth under DATASETS.

    Supports all layouts:
      • flat legacy:   datasets/<repo>/
      • per-agent:     datasets/<parent>/<agent>/   (current standard)
      • merged:        datasets/<parent>__merged/
    A dataset is any dir containing meta/info.json. The returned `id` is the
    path RELATIVE to DATASETS (e.g. "scout__...-20260619/main"), used verbatim
    by _dataset_dir().
    """
    out = []
    seen = set()
    for info in DATASETS.rglob("meta/info.json"):
        d = info.parent.parent  # <dataset>/meta/info.json → <dataset>
        if d in seen:
            continue
        seen.add(d)
        try:
            meta = json.loads(info.read_text())
        except Exception:
            continue
        rel = d.relative_to(DATASETS).as_posix().replace("/", "::")  # "::" = path-safe nest sep
        out.append({
            "id": rel,
            "fps": meta.get("fps"),
            "total_episodes": meta.get("total_episodes"),
            "total_frames": meta.get("total_frames"),
            "robot_type": meta.get("robot_type"),
            "has_reasoning": (d / "reasoning" / "events.sqlite").exists(),
            "mtime": d.stat().st_mtime,
        })
    out.sort(key=lambda x: x.get("mtime", 0), reverse=True)
    for o in out:
        o.pop("mtime", None)
    return out


def _dataset_dir(ds_id: str) -> Path:
    # ds_id may be a nested id encoded with "::" ("<parent>::<agent>") so it
    # survives as a single FastAPI path segment. Decode back to a real path.
    ds_id = ds_id.replace("::", "/")
    p = (DATASETS / ds_id).resolve()
    try:
        p.relative_to(DATASETS)  # raises if escaping DATASETS (path traversal)
    except ValueError:
        raise HTTPException(404, f"invalid dataset id: {ds_id}")
    if not p.exists() or not (p / "meta" / "info.json").exists():
        raise HTTPException(404, f"dataset not found: {ds_id}")
    return p


def _episodes_meta(ds_id: str) -> List[Dict[str, Any]]:
    """Per-episode timeline rows from meta/episodes/*.parquet.

    If the parquet is unreadable (dataset still LIVE — footer not flushed),
    fall back to episode_anchors in the reasoning DB so the UI can still list
    episodes + tasks (video scrubbing just won't have precise from/to until the
    agent stops and footers flush).
    """
    import pandas as pd
    d = _dataset_dir(ds_id)
    files = sorted(glob.glob(str(d / "meta" / "episodes" / "*" / "*.parquet")))
    if files:
        try:
            frames = [pd.read_parquet(f) for f in files]
            df = pd.concat(frames).sort_values("episode_index")
            FK_FROM = "videos/observation.images.front/from_timestamp"
            FK_TO = "videos/observation.images.front/to_timestamp"
            rows = []
            for _, r in df.iterrows():
                tasks = r.get("tasks")
                if hasattr(tasks, "tolist"):
                    tasks = tasks.tolist()
                rows.append({
                    "episode_index": int(r["episode_index"]),
                    "tasks": tasks if isinstance(tasks, list) else [str(tasks)],
                    "length": int(r["length"]),
                    "from_index": int(r["dataset_from_index"]),
                    "to_index": int(r["dataset_to_index"]),
                    "video_from": float(r.get(FK_FROM, 0.0) or 0.0),
                    "video_to": float(r.get(FK_TO, 0.0) or 0.0),
                    "live": False,
                })
            return rows
        except Exception:
            pass  # live/unfinalized → fall through to reasoning-anchor fallback

    # Fallback: reconstruct a coarse episode list from episode_anchors (the
    # reasoning DB is readable even while the parquet is being written).
    db = d / "reasoning" / "events.sqlite"
    if not db.exists():
        return []
    try:
        c = sqlite3.connect(str(db))
        c.row_factory = sqlite3.Row
        anchors = c.execute(
            "SELECT episode_index,fps,start_wall_ts,stop_wall_ts,task "
            "FROM episode_anchors WHERE episode_index IS NOT NULL "
            "ORDER BY episode_index"
        ).fetchall()
        c.close()
    except Exception:
        return []
    rows = []
    for a in anchors:
        dur = ((a["stop_wall_ts"] or a["start_wall_ts"]) - a["start_wall_ts"]) \
              if a["start_wall_ts"] else 0.0
        rows.append({
            "episode_index": int(a["episode_index"]),
            "tasks": [a["task"]] if a["task"] else [],
            "length": None,
            "from_index": None, "to_index": None,
            # without the parquet we don't know the packed-mp4 offset; assume
            # this episode starts at 0 in its own (not-yet-finalized) video.
            "video_from": 0.0,
            "video_to": float(dur),
            "live": True,
        })
    return rows


def _video_path(ds_id: str, view: str) -> Path:
    d = _dataset_dir(ds_id)
    key = f"observation.images.{view}"
    cands = sorted(glob.glob(str(d / "videos" / key / "*" / "*.mp4")))
    if not cands:
        raise HTTPException(404, f"no {view} video in {ds_id}")
    return Path(cands[0])


# ── reasoning events for an episode ──────────────────────────────────────────
def _reasoning_for_episode(ds_id: str, episode_index: int, fps: float) -> List[Dict[str, Any]]:
    db = _dataset_dir(ds_id) / "reasoning" / "events.sqlite"
    if not db.exists():
        return []
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    try:
        rows = c.execute(
            "SELECT seq,wall_ts,frame_index,frame_span_lo,frame_span_hi,agent_id,"
            "role,type,tool_name,tool_use_id,text,tool_input "
            "FROM reasoning_events WHERE episode_index=? ORDER BY wall_ts,seq",
            (episode_index,),
        ).fetchall()
    except Exception:
        rows = []
    c.close()
    out = []
    for r in rows:
        fi = r["frame_index"]
        out.append({
            "seq": r["seq"],
            "frame_index": fi,
            "video_t": (fi / fps) if fi is not None else None,  # relative to episode start
            "frame_span": [r["frame_span_lo"], r["frame_span_hi"]]
                          if r["frame_span_lo"] is not None else None,
            "agent_id": r["agent_id"],
            "role": r["role"],
            "type": r["type"],
            "tool_name": r["tool_name"],
            "tool_use_id": r["tool_use_id"],
            "text": (r["text"] or "")[:500],
            "tool_input": r["tool_input"],
        })
    return out


# ── per-frame action/state series (for the scrubber graph) ───────────────────
def _state_names(ds_id: str) -> List[str]:
    """Ordered names of the observation.state vector from meta/info.json."""
    try:
        info = json.loads((_dataset_dir(ds_id) / "meta" / "info.json").read_text())
        return list(info["features"]["observation.state"]["names"])
    except Exception:
        return []


def _series(ds_id: str, episode_index: int) -> Dict[str, Any]:
    import pandas as pd
    d = _dataset_dir(ds_id)
    pqs = sorted(glob.glob(str(d / "data" / "*" / "*.parquet")))
    if not pqs:
        return {"frame_index": [], "action": [], "speed": []}
    frames = []
    for f in pqs:
        try:
            df = pd.read_parquet(f)
        except Exception:
            # live/unfinalized parquet (agent still recording) → no series yet
            return {"frame_index": [], "action": [], "speed": [], "live": True}
        if "episode_index" in df.columns:
            df = df[df["episode_index"] == episode_index]
        if len(df):
            frames.append(df)
    if not frames:
        return {"frame_index": [], "action": [], "speed": []}
    e = pd.concat(frames).sort_values("frame_index")
    action = [list(map(float, a)) for a in e["action"].tolist()] if "action" in e.columns else []
    # state[0] == linear.vel (earthrover_mini_plus aligned schema; was [8] pre-align)
    speed = []
    # Full named telemetry: expose the WHOLE observation.state vector keyed by
    # its names (battery, gps, imu, voltage, …) so the UI can render a live
    # telemetry strip + extra graph channels, not just speed. Backward-compat:
    # `speed` (state[0]) is still returned exactly as before.
    names = _state_names(ds_id)
    state_cols: Dict[str, List[float]] = {n: [] for n in names}
    if "observation.state" in e.columns:
        for s in e["observation.state"].tolist():
            try:
                vec = list(s)
            except Exception:
                vec = []
            try:
                speed.append(float(vec[0]))
            except Exception:
                speed.append(0.0)
            for i, n in enumerate(names):
                try:
                    state_cols[n].append(float(vec[i]))
                except Exception:
                    state_cols[n].append(None)
    age = []
    if "action_age" in e.columns:
        for a in e["action_age"].tolist():
            try:
                age.append(float(a[0]))
            except Exception:
                age.append(None)
    return {
        "frame_index": [int(x) for x in e["frame_index"].tolist()],
        "action": action,
        "speed": speed,
        "action_age": age,
        "state_names": names,
        "state": state_cols,
    }


# ── mount onto the dashboard app ─────────────────────────────────────────────
def mount(app: FastAPI) -> None:
    @app.get("/api/replay/datasets")
    async def replay_datasets():
        return JSONResponse(_list_datasets())

    @app.get("/api/replay/{ds_id}/episodes")
    async def replay_episodes(ds_id: str):
        info = json.loads((_dataset_dir(ds_id) / "meta" / "info.json").read_text())
        fps = float(info.get("fps", 4))
        eps = _episodes_meta(ds_id)
        return JSONResponse({"dataset": ds_id, "fps": fps, "episodes": eps})

    @app.get("/api/replay/{ds_id}/episode/{episode_index}")
    async def replay_episode(ds_id: str, episode_index: int):
        info = json.loads((_dataset_dir(ds_id) / "meta" / "info.json").read_text())
        fps = float(info.get("fps", 4))
        eps = {e["episode_index"]: e for e in _episodes_meta(ds_id)}
        if episode_index not in eps:
            raise HTTPException(404, f"episode {episode_index} not in {ds_id}")
        meta = eps[episode_index]
        return JSONResponse({
            "dataset": ds_id,
            "fps": fps,
            "episode": meta,
            "series": _series(ds_id, episode_index),
            "reasoning": _reasoning_for_episode(ds_id, episode_index, fps),
        })

    @app.get("/api/replay/{ds_id}/memory/search")
    async def memory_search(ds_id: str, q: str, k: int = 12,
                            modality: str = None, episode: int = None,
                            objects: str = None):
        """Semantic memory search → hits carry frame_index for scrubber seek."""
        try:
            from tools.memory_index import get_index
            idx = get_index(_dataset_dir(ds_id))
            obj_list = [o.strip() for o in objects.split(",")] if objects else None
            ep = int(episode) if episode is not None else None
            hits = idx.search(q, k=int(k), modality=modality or None,
                              episode=ep, objects=obj_list)
            # attach absolute video time so the UI can seek directly
            eps = {e["episode_index"]: e for e in _episodes_meta(ds_id)}
            for h in hits:
                e = eps.get(h["episode"])
                if e and h.get("t_in_episode") is not None and h["t_in_episode"] >= 0:
                    h["video_abs_t"] = e["video_from"] + h["t_in_episode"]
            return JSONResponse({"query": q, "dataset": ds_id, "hits": hits})
        except Exception as e:
            return JSONResponse({"error": str(e), "hits": []}, status_code=500)

    @app.get("/api/replay/{ds_id}/memory/stats")
    async def memory_stats(ds_id: str):
        try:
            from tools.memory_index import get_index
            return JSONResponse(get_index(_dataset_dir(ds_id)).stats())
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/replay/{ds_id}/video/{view}")
    async def replay_video(ds_id: str, view: str):
        if view not in ("front", "rear"):
            raise HTTPException(400, "view must be front|rear")
        path = _video_path(ds_id, view)
        # FileResponse supports HTTP range requests → browser <video> can seek.
        return FileResponse(str(path), media_type="video/mp4")
