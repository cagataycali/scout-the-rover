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
import re
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
        # count live image episodes (PNG sequences) so datasets the agent is
        # still recording (total_episodes=0 in info.json) aren't shown empty.
        live_eps = 0
        for view in ("front", "rear"):
            base = d / "images" / f"observation.images.{view}"
            if base.is_dir():
                live_eps = max(live_eps, sum(
                    1 for p in base.iterdir()
                    if p.is_dir() and p.name.startswith("episode-")
                ))
        total_eps = meta.get("total_episodes") or 0
        out.append({
            "id": rel,
            "fps": meta.get("fps"),
            "total_episodes": total_eps,
            "live_episodes": live_eps,
            # what the UI should show as the episode count (prefer finalized,
            # else live image episodes).
            "episode_count": total_eps or live_eps,
            "total_frames": meta.get("total_frames"),
            "robot_type": meta.get("robot_type"),
            "has_reasoning": (d / "reasoning" / "events.sqlite").exists(),
            "has_video": bool(glob.glob(str(d / "videos" / "*" / "*" / "*.mp4"))),
            "live": (total_eps == 0 and live_eps > 0),
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
    anchored = set()
    for a in anchors:
        ei = int(a["episode_index"])
        anchored.add(ei)
        dur = ((a["stop_wall_ts"] or a["start_wall_ts"]) - a["start_wall_ts"]) \
              if a["start_wall_ts"] else 0.0
        # Live episodes have NO mp4 — but they DO have per-frame PNGs. Expose
        # the image sequence as the timeline so the UI is fully replayable.
        front = _image_frames(ds_id, "front", ei)
        rear = _image_frames(ds_id, "rear", ei)
        nframes = len(front) or len(rear)
        rdur = _episode_duration_from_reasoning(ds_id, ei) or dur
        # effective fps maps PNG index → wall time (the live recorder rarely
        # hits the nominal fps, so derive it from frames/duration).
        eff_fps = (nframes / rdur) if (nframes and rdur > 0) else None
        rows.append({
            "episode_index": ei,
            "tasks": [a["task"]] if a["task"] else [],
            "length": nframes or None,
            "from_index": (front[0] if front else None),
            "to_index": (front[-1] if front else None),
            "video_from": 0.0,
            "video_to": float(rdur),
            "live": True,
            "mode": "images" if nframes else "none",
            "image_frames": nframes,
            "image_views": [v for v, fr in (("front", front), ("rear", rear)) if fr],
            "eff_fps": eff_fps,
        })
    # Episodes with PNGs but NO anchor row (recorder died before anchoring) —
    # still surface them so nothing recorded is invisible.
    for ei in _image_episode_indices(ds_id):
        if ei in anchored:
            continue
        front = _image_frames(ds_id, "front", ei)
        rear = _image_frames(ds_id, "rear", ei)
        nframes = len(front) or len(rear)
        rdur = _episode_duration_from_reasoning(ds_id, ei)
        eff_fps = (nframes / rdur) if (nframes and rdur > 0) else None
        rows.append({
            "episode_index": ei,
            "tasks": [],
            "length": nframes or None,
            "from_index": (front[0] if front else None),
            "to_index": (front[-1] if front else None),
            "video_from": 0.0,
            "video_to": float(rdur),
            "live": True,
            "mode": "images" if nframes else "none",
            "image_frames": nframes,
            "image_views": [v for v, fr in (("front", front), ("rear", rear)) if fr],
            "eff_fps": eff_fps,
        })
    rows.sort(key=lambda r: r["episode_index"])
    return rows


def _video_path(ds_id: str, view: str) -> Path:
    d = _dataset_dir(ds_id)
    key = f"observation.images.{view}"
    cands = sorted(glob.glob(str(d / "videos" / key / "*" / "*.mp4")))
    if not cands:
        raise HTTPException(404, f"no {view} video in {ds_id}")
    return Path(cands[0])


# ── image-sequence fallback (LIVE / unfinalized datasets) ────────────────────
# A "live" dataset is one the agent is still recording (or stopped without
# finalizing): it has per-frame PNGs under images/<key>/episode-NNNNNN/ and a
# reasoning DB, but NO packed mp4 and NO data/episode parquet yet. The replay
# UI would otherwise show a blank video + dead scrubber. We expose the PNG
# sequence as a first-class timeline so these episodes are fully replayable.
_FRAME_RE = re.compile(r"frame-(\d+)\.png$")


def _image_episode_dir(ds_id: str, view: str, episode_index: int) -> Optional[Path]:
    d = _dataset_dir(ds_id)
    key = f"observation.images.{view}"
    ep = d / "images" / key / f"episode-{episode_index:06d}"
    return ep if ep.is_dir() else None


def _image_frames(ds_id: str, view: str, episode_index: int) -> List[int]:
    """Sorted list of available PNG frame numbers for an episode/view."""
    ep = _image_episode_dir(ds_id, view, episode_index)
    if not ep:
        return []
    nums = []
    for p in ep.iterdir():
        m = _FRAME_RE.search(p.name)
        if m:
            nums.append(int(m.group(1)))
    nums.sort()
    return nums


def _image_episode_indices(ds_id: str) -> List[int]:
    """Episode indices that have a front (or rear) PNG directory on disk."""
    d = _dataset_dir(ds_id)
    seen = set()
    for view in ("front", "rear"):
        base = d / "images" / f"observation.images.{view}"
        if not base.is_dir():
            continue
        for ep in base.iterdir():
            m = re.match(r"episode-(\d+)$", ep.name)
            if m and ep.is_dir():
                seen.add(int(m.group(1)))
    return sorted(seen)


def _has_video(ds_id: str, view: str = "front") -> bool:
    d = _dataset_dir(ds_id)
    key = f"observation.images.{view}"
    return bool(glob.glob(str(d / "videos" / key / "*" / "*.mp4")))


def _episode_duration_from_reasoning(ds_id: str, episode_index: int) -> float:
    """Best-effort wall-clock duration (s) for a live episode from reasoning."""
    db = _dataset_dir(ds_id) / "reasoning" / "events.sqlite"
    if not db.exists():
        return 0.0
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        row = c.execute(
            "SELECT MIN(wall_ts), MAX(wall_ts), MIN(t_in_episode), MAX(t_in_episode) "
            "FROM reasoning_events WHERE episode_index=?",
            (episode_index,),
        ).fetchone()
        c.close()
    except Exception:
        return 0.0
    if not row:
        return 0.0
    tmin, tmax, ti_min, ti_max = row
    if ti_max is not None and ti_min is not None:
        return float(ti_max)  # t_in_episode is already episode-relative
    if tmin is not None and tmax is not None:
        return float(tmax - tmin)
    return 0.0


# ── reasoning events for an episode ──────────────────────────────────────────
def _reasoning_for_episode(ds_id: str, episode_index: int, fps: float) -> List[Dict[str, Any]]:
    db = _dataset_dir(ds_id) / "reasoning" / "events.sqlite"
    if not db.exists():
        return []
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    try:
        rows = c.execute(
            "SELECT seq,wall_ts,frame_index,frame_span_lo,frame_span_hi,t_in_episode,"
            "agent_id,role,type,tool_name,tool_use_id,text,tool_input "
            "FROM reasoning_events WHERE episode_index=? ORDER BY wall_ts,seq",
            (episode_index,),
        ).fetchall()
    except Exception:
        rows = []
    c.close()
    out = []
    for r in rows:
        fi = r["frame_index"]
        # t_in_episode is the AUTHORITATIVE episode-relative time (seconds).
        # Prefer it over frame_index/fps because in LIVE image-sequence mode the
        # PNG frame counter and the reasoning frame_index live on different
        # scales (recorder fps drift) — only wall time is shared. The UI maps
        # video_t → PNG index via the episode's eff_fps.
        ti = r["t_in_episode"]
        video_t = ti if ti is not None else ((fi / fps) if fi is not None else None)
        out.append({
            "seq": r["seq"],
            "frame_index": fi,
            "t_in_episode": ti,
            "video_t": video_t,  # episode-relative SECONDS (canonical time axis)
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

    @app.get("/api/replay/{ds_id}/frames/{view}/{episode_index}")
    async def replay_frames(ds_id: str, view: str, episode_index: int):
        """List available PNG frame indices for a LIVE (image-sequence) episode.

        Returns the sorted frame numbers + the per-frame image URL template so
        the UI can drive an <img> scrubber when there's no packed mp4 yet.
        """
        if view not in ("front", "rear"):
            raise HTTPException(400, "view must be front|rear")
        frames = _image_frames(ds_id, view, episode_index)
        if not frames:
            raise HTTPException(404, f"no {view} image frames for ep {episode_index} in {ds_id}")
        dur = _episode_duration_from_reasoning(ds_id, episode_index)
        return JSONResponse({
            "dataset": ds_id,
            "view": view,
            "episode_index": episode_index,
            "frames": frames,
            "count": len(frames),
            "duration": dur,
            "eff_fps": (len(frames) / dur) if dur > 0 else None,
            "url_template": f"/api/replay/{ds_id}/frame/{view}/{episode_index}/{{frame}}",
        })

    @app.get("/api/replay/{ds_id}/frame/{view}/{episode_index}/{frame}")
    async def replay_frame(ds_id: str, view: str, episode_index: int, frame: int):
        """Serve a single PNG frame for the image-sequence scrubber."""
        if view not in ("front", "rear"):
            raise HTTPException(400, "view must be front|rear")
        ep = _image_episode_dir(ds_id, view, episode_index)
        if not ep:
            raise HTTPException(404, f"no {view} images for ep {episode_index}")
        p = ep / f"frame-{frame:06d}.png"
        if not p.exists():
            # tolerate non-zero-padded or differently padded names
            cands = sorted(ep.glob(f"frame-*{frame}.png"))
            if not cands:
                raise HTTPException(404, f"frame {frame} not found")
            p = cands[0]
        return FileResponse(str(p), media_type="image/png")
