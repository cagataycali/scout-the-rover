#!/usr/bin/env python3
"""Repair a LeRobot v3 dataset whose episode index was never finalized.

lerobot ≥0.6 streams ``meta/episodes/*/*.parquet`` (and ``data/*/*.parquet``)
through pyarrow ParquetWriters; the footer is only written by
``LeRobotDataset.finalize()``. A recorder process that was killed before that
leaves files pyarrow cannot open ("Parquet magic bytes not found in footer"):
the dataset cannot be ``resume()``d, and the replay UI has no per-episode
``[from_timestamp, to_timestamp]`` window → every episode plays the same clip.

This tool rebuilds ``meta/episodes`` for the episodes whose index rows were
lost, from what DID survive:

* ``meta/info.json`` — total_episodes / total_frames / fps (updated after every
  save_episode, so it is authoritative);
* the mp4(s) — frame counts (ffprobe). Post-sealing layout (one mp4 per
  episode) gives exact lengths; the legacy packed layout (one mp4 for many
  episodes) is split at keyframe-parity changes (encoder GOP g=2 ⇒ an
  odd-length episode flips the I-frame parity — exact) and, for the remaining
  boundaries, proportionally to the wall-clock durations recorded in
  ``reasoning/events.sqlite`` (approximate — rows are tagged ``repaired``);
* ``--lengths a,b,c`` — explicit per-episode frame counts if you know them
  (e.g. from ``auto-record: ep N saved (X frames)`` log lines).

Unreadable files are MOVED (never deleted) to ``<root>/_repair_backup/<ts>/``.
Footerless ``data/*.parquet`` cannot be recovered (schema lives in the footer);
those episodes keep video/audio/reasoning but lose per-frame action/state and
are marked ``data_lost`` in ``meta/repair_log.json``. Orphan staging PNG dirs
(``images/<key>/episode-N`` with N ≥ total_episodes) are moved to the backup
too — lerobot would otherwise sweep them into the next episode's video.

Idempotent: a dataset with no unreadable parquet is left untouched.

Usage:
    python -m tools.repair_episode_index <dataset_root> [--dry-run] [--yes]
        [--lengths 82,149,113,131] [--schema-from other/meta/episodes/…parquet]
    python -m tools.repair_episode_index --scan datasets/      # list broken ones
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pyarrow as pa
import pyarrow.parquet as pq

EPISODES_GLOB = "meta/episodes/*/*.parquet"
DATA_GLOB = "data/*/*.parquet"
STATS_QUANTS = ("min", "max", "mean", "std", "count", "q01", "q10", "q50", "q90", "q99")


# ── probes ──────────────────────────────────────────────────────────────────

def parquet_readable(path: Path) -> bool:
    try:
        pq.read_metadata(path)
        return True
    except Exception:
        return False


def unreadable_parquets(root: Path) -> Dict[str, List[Path]]:
    return {
        "episodes": [p for p in sorted(root.glob(EPISODES_GLOB)) if not parquet_readable(p)],
        "data": [p for p in sorted(root.glob(DATA_GLOB)) if not parquet_readable(p)],
    }


def scan(datasets_dir: Path) -> List[Dict[str, Any]]:
    """Every dataset root (dir with meta/info.json) under datasets_dir with its broken files."""
    out = []
    for info in sorted(datasets_dir.glob("**/meta/info.json")):
        root = info.parent.parent
        bad = unreadable_parquets(root)
        out.append({
            "root": str(root),
            "unreadable_episode_files": [str(p.relative_to(root)) for p in bad["episodes"]],
            "unreadable_data_files": [str(p.relative_to(root)) for p in bad["data"]],
        })
    return out


def ffprobe_frames(path: Path) -> Optional[int]:
    """Frame count of a video (container nb_frames, falling back to counting)."""
    for args in (
        ["-select_streams", "v:0", "-show_entries", "stream=nb_frames", "-of", "csv=p=0"],
        ["-select_streams", "v:0", "-count_frames", "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0"],
    ):
        try:
            out = subprocess.run(["ffprobe", "-v", "error", *args, str(path)],
                                 capture_output=True, text=True, timeout=120).stdout.strip()
            if out and out != "N/A":
                return int(float(out.splitlines()[0]))
        except Exception:
            continue
    return None


def ffprobe_keyframe_indices(path: Path) -> Optional[List[int]]:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "frame=pict_type",
             "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=600).stdout
    except Exception:
        return None
    idx = []
    for i, line in enumerate(l for l in out.splitlines() if l.strip()):
        if line.split(",")[0].strip() == "I":
            idx.append(i)
    return idx


def parity_boundaries(keyframes: List[int], gop: int = 2) -> List[int]:
    """Frame indices where a new independently-encoded segment must start.

    With a fixed GOP every keyframe sits at start+k*gop; a segment whose length
    is not a multiple of gop shifts the phase, so a keyframe gap != gop marks a
    boundary at that keyframe. (Segments with length % gop == 0 are invisible
    to this test — combine with other evidence.)"""
    out = []
    for a, b in zip(keyframes, keyframes[1:]):
        if b - a != gop:
            out.append(b)
    return out


def anchor_durations(root: Path, episodes: List[int]) -> Dict[int, float]:
    db = root / "reasoning" / "events.sqlite"
    if not db.exists():
        return {}
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = c.execute("SELECT episode_index,start_wall_ts,stop_wall_ts FROM episode_anchors").fetchall()
        c.close()
    except Exception:
        return {}
    out = {}
    for ei, s, e in rows:
        if ei in episodes and s and e and e > s:
            out[int(ei)] = float(e - s)
    return out


def wav_durations(root: Path, episodes: List[int]) -> Dict[int, float]:
    import wave
    out = {}
    for ei in episodes:
        p = root / "audio" / f"episode_{ei:06d}.wav"
        if p.exists():
            try:
                with wave.open(str(p)) as w:
                    out[ei] = w.getnframes() / float(w.getframerate())
            except Exception:
                pass
    return out


# ── length inference ─────────────────────────────────────────────────────────

def split_lengths(total: int, n: int, hard: Dict[int, int], weights: Dict[int, float]) -> List[int]:
    """Frame count per episode (n episodes, sum == total). ``hard`` pins exact
    counts by episode position; the rest is split proportionally to ``weights``
    (equal weights when unknown)."""
    free = [i for i in range(n) if i not in hard]
    remaining = total - sum(hard.values())
    if remaining < len(free):
        raise ValueError(f"inconsistent lengths: hard={hard} total={total}")
    lengths = [hard.get(i, 0) for i in range(n)]
    if free:
        w = [max(weights.get(i, 0.0), 0.0) for i in free]
        if sum(w) <= 0:
            w = [1.0] * len(free)
        raw = [remaining * wi / sum(w) for wi in w]
        ints = [max(1, int(r)) for r in raw]
        # distribute the rounding remainder to the largest fractional parts
        for i in sorted(range(len(free)), key=lambda k: raw[k] - int(raw[k]), reverse=True):
            if sum(ints) >= remaining:
                break
            ints[i] += 1
        while sum(ints) > remaining:
            j = max(range(len(free)), key=lambda k: ints[k])
            ints[j] -= 1
        for k, i in enumerate(free):
            lengths[i] = ints[k]
    assert sum(lengths) == total, (lengths, total)
    return lengths


def infer_packed_lengths(total_frames: int, episodes: List[int], root: Path,
                         video_path: Optional[Path], gop: int,
                         explicit: Optional[List[int]]) -> Dict[str, Any]:
    n = len(episodes)
    if explicit is not None:
        if len(explicit) != n or sum(explicit) != total_frames:
            raise SystemExit(f"--lengths must have {n} values summing to {total_frames} (got {explicit})")
        return {"lengths": explicit, "method": "explicit", "exact": True}

    hard: Dict[int, int] = {}
    method = []
    exact = False
    boundaries: List[int] = []
    if video_path is not None:
        kf = ffprobe_keyframe_indices(video_path)
        if kf:
            boundaries = [b for b in parity_boundaries(kf, gop) if 0 < b < total_frames]
            method.append(f"keyframe-parity:{boundaries}")
    # Boundaries tell us where SOME episodes start. If we found exactly n-1
    # boundaries every length is exact.
    if len(boundaries) == n - 1:
        edges = [0, *boundaries, total_frames]
        return {"lengths": [b - a for a, b in zip(edges, edges[1:])], "method": "+".join(method), "exact": True}

    weights = anchor_durations(root, episodes)
    if weights:
        method.append("anchor-durations")
    else:
        weights = wav_durations(root, episodes)
        if weights:
            method.append("wav-durations")
    wpos = {episodes.index(ei): d for ei, d in weights.items()}

    if boundaries:
        # Use the boundaries as constraints: the episodes between two known
        # boundaries share that frame budget proportionally. Assign each
        # boundary to the episode position whose cumulative proportional
        # frame count is closest.
        prop = split_lengths(total_frames, n, {}, wpos)
        cum = [sum(prop[:i + 1]) for i in range(n)]
        segs = [0]
        for b in boundaries:
            pos = min(range(n - 1), key=lambda i: abs(cum[i] - b)) + 1
            if pos > segs[-1]:
                segs.append(pos)
        segs.append(n)
        edges = [0, *boundaries, total_frames][: len(segs)]
        lengths: List[int] = []
        for (p0, p1), (f0, f1) in zip(zip(segs, segs[1:]), zip(edges, edges[1:])):
            sub = list(range(p0, p1))
            lengths += split_lengths(f1 - f0, len(sub), {}, {k: wpos.get(p, 0.0) for k, p in enumerate(sub)})
        return {"lengths": lengths, "method": "+".join(method) + "(approx)", "exact": False}

    return {"lengths": split_lengths(total_frames, n, hard, wpos), "method": "+".join(method) or "equal-split", "exact": False}


# ── row building ─────────────────────────────────────────────────────────────

def _video_file_indices(rel: Path) -> tuple:
    m = re.search(r"chunk-(\d+)/file-(\d+)\.mp4$", rel.as_posix())
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _episodes_file_indices(rel: Path) -> tuple:
    m = re.search(r"chunk-(\d+)/file-(\d+)\.parquet$", rel.as_posix())
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def build_rows(root: Path, info: dict, episodes: List[int], lengths: List[int],
               first_frame: int, video_plan: Dict[str, List[Dict[str, Any]]],
               stats: Optional[dict], ep_chunk: int, ep_file: int,
               data_chunk: int, data_file: int, task: str) -> List[Dict[str, Any]]:
    fps = float(info.get("fps", 10))
    rows = []
    frame_cursor = first_frame
    for pos, (ei, ln) in enumerate(zip(episodes, lengths)):
        row: Dict[str, Any] = {
            "episode_index": ei,
            "tasks": [task],
            "length": ln,
            "data/chunk_index": data_chunk,
            "data/file_index": data_file,
            "dataset_from_index": frame_cursor,
            "dataset_to_index": frame_cursor + ln,
        }
        for key, plan in video_plan.items():
            seg = plan[pos]
            row[f"videos/{key}/chunk_index"] = seg["chunk"]
            row[f"videos/{key}/file_index"] = seg["file"]
            row[f"videos/{key}/from_timestamp"] = seg["from"]
            row[f"videos/{key}/to_timestamp"] = seg["to"]
        if stats:
            for feat, st in stats.items():
                for q in STATS_QUANTS:
                    if q not in st:
                        continue
                    v = st[q]
                    if q == "count":
                        v = [ln]
                    row[f"stats/{feat}/{q}"] = v
        row["meta/episodes/chunk_index"] = ep_chunk
        row["meta/episodes/file_index"] = ep_file
        rows.append(row)
        frame_cursor += ln
    return rows


def write_rows(rows: List[Dict[str, Any]], path: Path, schema_from: Optional[Path]) -> None:
    table = pa.Table.from_pylist(rows)
    if schema_from is not None and parquet_readable(schema_from):
        ref = pq.read_schema(schema_from)
        common = [n for n in ref.names if n in table.column_names]
        missing = [n for n in ref.names if n not in table.column_names]
        table = table.select(common)
        for name in missing:  # null columns keep the nested-dataset schema uniform
            table = table.append_column(name, pa.nulls(table.num_rows, type=ref.field(name).type))
        table = table.select(ref.names).cast(ref)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression="snappy", use_dictionary=True)


# ── main repair ──────────────────────────────────────────────────────────────

def repair(root: Path, *, dry_run: bool, lengths: Optional[List[int]], schema_from: Optional[Path],
           gop: int = 2, task_fallback: str = "repaired") -> Dict[str, Any]:
    root = root.resolve()
    info_path = root / "meta" / "info.json"
    if not info_path.exists():
        raise SystemExit(f"{root}: not a LeRobot dataset (no meta/info.json)")
    info = json.loads(info_path.read_text())
    total_episodes = int(info.get("total_episodes", 0))
    total_frames = int(info.get("total_frames", 0))
    fps = float(info.get("fps", 10))
    video_keys = [k for k, f in info.get("features", {}).items() if f.get("dtype") == "video"]

    bad = unreadable_parquets(root)
    orphans = []
    for key_dir in (root / "images").glob("*") if (root / "images").exists() else []:
        for ep_dir in key_dir.glob("episode-*"):
            m = re.match(r"episode-(\d+)$", ep_dir.name)
            if m and int(m.group(1)) >= total_episodes:
                orphans.append(ep_dir)

    report: Dict[str, Any] = {
        "root": str(root), "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_episodes": total_episodes, "total_frames": total_frames,
        "unreadable_episode_files": [str(p.relative_to(root)) for p in bad["episodes"]],
        "unreadable_data_files": [str(p.relative_to(root)) for p in bad["data"]],
        "orphan_staging_dirs": [str(p.relative_to(root)) for p in orphans],
        "dry_run": dry_run,
    }
    if not bad["episodes"] and not bad["data"] and not orphans:
        report["action"] = "none (dataset readable)"
        return report

    # Which episodes still have index rows?
    known: Dict[int, dict] = {}
    for p in sorted(root.glob(EPISODES_GLOB)):
        if parquet_readable(p):
            for r in pq.read_table(p).to_pylist():
                known[int(r["episode_index"])] = r
    lost = [ei for ei in range(total_episodes) if ei not in known]
    report["lost_episodes"] = lost

    # Task label: reuse anchors' task when present, else the tasks.parquet, else fallback.
    task = task_fallback
    try:
        c = sqlite3.connect(f"file:{root / 'reasoning' / 'events.sqlite'}?mode=ro", uri=True)
        r = c.execute("SELECT task FROM episode_anchors WHERE task IS NOT NULL ORDER BY episode_index LIMIT 1").fetchone()
        c.close()
        if r and r[0]:
            task = str(r[0])
    except Exception:
        tp = root / "meta" / "tasks.parquet"
        if tp.exists() and parquet_readable(tp):
            try:
                t = pq.read_table(tp).to_pylist()
                if t:
                    task = str(t[0].get("task") or t[0].get("__index_level_0__") or task)
            except Exception:
                pass

    new_rows: List[Dict[str, Any]] = []
    plan_report: Dict[str, Any] = {}
    if lost:
        # Frames already accounted for by surviving rows
        first_frame = max((r["dataset_to_index"] for r in known.values()), default=0)
        lost_frames = total_frames - first_frame

        # Video files not referenced by surviving rows, per key
        video_plan: Dict[str, List[Dict[str, Any]]] = {}
        length_info: Optional[Dict[str, Any]] = None
        for key in video_keys:
            used = {(r.get(f"videos/{key}/chunk_index"), r.get(f"videos/{key}/file_index")) for r in known.values()}
            files = []
            for p in sorted((root / "videos" / key).glob("*/*.mp4")):
                ci, fi = _video_file_indices(p.relative_to(root))
                if (ci, fi) not in used:
                    files.append((ci, fi, p))
            if not files:
                continue
            if len(files) == len(lost):
                # sealed layout: one mp4 per lost episode → exact
                segs = []
                lens = []
                for ci, fi, p in files:
                    n = ffprobe_frames(p) or 0
                    lens.append(n)
                    segs.append({"chunk": ci, "file": fi, "from": 0.0, "to": n / fps, "frames": n})
                video_plan[key] = segs
                if length_info is None:
                    if sum(lens) == lost_frames and all(lens):
                        length_info = {"lengths": lens, "method": "one-mp4-per-episode", "exact": True}
                    else:
                        length_info = infer_packed_lengths(lost_frames, lost, root, None, gop, lengths)
            else:
                # packed layout (all lost episodes in the LAST unreferenced mp4)
                ci, fi, p = files[-1]
                if length_info is None:
                    length_info = infer_packed_lengths(lost_frames, lost, root, p, gop, lengths)
                # from_timestamp continues after whatever the surviving rows used in this file
                base = max((r.get(f"videos/{key}/to_timestamp", 0.0) for r in known.values()
                            if (r.get(f"videos/{key}/chunk_index"), r.get(f"videos/{key}/file_index")) == (ci, fi)),
                           default=0.0)
                segs = []
                t = float(base)
                for ln in length_info["lengths"]:
                    segs.append({"chunk": ci, "file": fi, "from": round(t, 6), "to": round(t + ln / fps, 6), "frames": ln})
                    t += ln / fps
                video_plan[key] = segs
        if length_info is None:
            length_info = infer_packed_lengths(lost_frames, lost, root, None, gop, lengths)
        plan_report = {"lengths": length_info["lengths"], "method": length_info["method"], "exact": length_info["exact"],
                       "video_plan": video_plan}

        stats = None
        sp = root / "meta" / "stats.json"
        if sp.exists():
            try:
                stats = json.loads(sp.read_text())
            except Exception:
                stats = None

        # Where the rebuilt rows go: the (single) unreadable episodes file's slot
        if bad["episodes"]:
            ep_chunk, ep_file = _episodes_file_indices(bad["episodes"][0].relative_to(root))
        else:
            ep_chunk, ep_file = 0, len(list(root.glob(EPISODES_GLOB)))
        # data pointer: unreadable data file's slot if any (its rows are gone), else a fresh index
        if bad["data"]:
            data_chunk, data_file = _episodes_file_indices(bad["data"][0].relative_to(root))
        else:
            data_chunk, data_file = 0, len(list(root.glob(DATA_GLOB)))

        new_rows = build_rows(root, info, lost, length_info["lengths"], first_frame, video_plan, stats,
                              ep_chunk, ep_file, data_chunk, data_file, task)
        plan_report["episodes_file"] = f"meta/episodes/chunk-{ep_chunk:03d}/file-{ep_file:03d}.parquet"
    report["plan"] = plan_report
    report["data_lost_episodes"] = lost if bad["data"] else []

    if dry_run:
        report["action"] = "dry-run"
        return report

    # ── mutate: back up, write, verify ──
    backup = root / "_repair_backup" / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    moved = []
    for p in bad["episodes"] + bad["data"] + orphans:
        dst = backup / p.relative_to(root)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(dst))
        moved.append(str(p.relative_to(root)))
    report["backup"] = str(backup.relative_to(root))
    report["moved"] = moved

    if new_rows:
        out = root / plan_report["episodes_file"]
        ref = schema_from
        if ref is None:
            for p in sorted(root.glob(EPISODES_GLOB)):
                if parquet_readable(p):
                    ref = p
                    break
        write_rows(new_rows, out, ref)
        # verify
        t = pq.read_table(out)
        assert t.num_rows == len(new_rows)
        report["written"] = str(out.relative_to(root))
        report["rows"] = [{k: r[k] for k in r if not k.startswith("stats/")} for r in t.to_pylist()]

    # whole-dataset verification
    all_rows = []
    for p in sorted(root.glob(EPISODES_GLOB)):
        all_rows += pq.read_table(p).to_pylist()
    all_rows.sort(key=lambda r: r["episode_index"])
    report["verify"] = {
        "episodes_indexed": [r["episode_index"] for r in all_rows],
        "frames_indexed": sum(int(r["length"]) for r in all_rows),
        "frames_expected": total_frames,
        "ok": [r["episode_index"] for r in all_rows] == list(range(total_episodes))
        and sum(int(r["length"]) for r in all_rows) == total_frames,
    }
    report["action"] = "repaired"
    log = root / "meta" / "repair_log.json"
    hist = []
    if log.exists():
        try:
            hist = json.loads(log.read_text())
        except Exception:
            hist = []
    hist.append({k: v for k, v in report.items() if k != "rows"})
    log.write_text(json.dumps(hist, indent=2))
    return report


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", nargs="?", help="dataset root (dir containing meta/info.json)")
    ap.add_argument("--scan", metavar="DATASETS_DIR", help="list datasets with unreadable parquet and exit")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    ap.add_argument("--lengths", help="comma-separated exact frame counts for the lost episodes")
    ap.add_argument("--schema-from", type=Path, help="readable meta/episodes parquet to copy the column schema from")
    ap.add_argument("--gop", type=int, default=2, help="encoder keyframe interval (lerobot default 2)")
    a = ap.parse_args(argv)

    if a.scan:
        res = scan(Path(a.scan))
        broken = [r for r in res if r["unreadable_episode_files"] or r["unreadable_data_files"]]
        print(json.dumps({"datasets": len(res), "broken": broken}, indent=2))
        return 1 if broken else 0
    if not a.root:
        ap.error("root required (or --scan)")
    lengths = [int(x) for x in a.lengths.split(",")] if a.lengths else None
    root = Path(a.root)
    plan = repair(root, dry_run=True, lengths=lengths, schema_from=a.schema_from, gop=a.gop)
    print(json.dumps(plan, indent=2, default=str))
    if a.dry_run or plan.get("action", "").startswith("none"):
        return 0
    if not a.yes:
        ans = input("apply? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            return 2
    rep = repair(root, dry_run=False, lengths=lengths, schema_from=a.schema_from, gop=a.gop)
    print(json.dumps(rep, indent=2, default=str))
    return 0 if rep.get("verify", {}).get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
