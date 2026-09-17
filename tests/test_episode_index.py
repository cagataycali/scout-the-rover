"""Episode-index durability (lerobot ≥0.6 streams parquet; footer only on finalize).

Covers the owner-reported bug "every episode in the replay shows the same video":
  • tools/repair_episode_index.py rebuilds a footerless meta/episodes parquet
  • dashboard_replay surfaces an unfinalized index instead of video_from=0 for all
  • the recorder seals (finalize + lazy resume) after every save_episode so the
    dataset on disk is readable mid-session (real lerobot, skipped if absent)
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ── helpers ──────────────────────────────────────────────────────────────────

def _truncate_footer(path: Path) -> None:
    """Turn a valid parquet file into what a killed ParquetWriter leaves behind:
    row groups on disk, no footer/magic (pyarrow: 'magic bytes not found')."""
    b = path.read_bytes()
    assert b[-4:] == b"PAR1"
    footer_len = int.from_bytes(b[-8:-4], "little")
    path.write_bytes(b[: len(b) - footer_len - 8])
    with pytest.raises(Exception):
        pq.read_metadata(path)


def _episode_row(ei: int, length: int, frame0: int, vfrom: float, vto: float, file_idx: int = 0) -> dict:
    return {
        "episode_index": ei, "tasks": ["drive"], "length": length,
        "data/chunk_index": 0, "data/file_index": file_idx,
        "dataset_from_index": frame0, "dataset_to_index": frame0 + length,
        "videos/observation.images.front/chunk_index": 0,
        "videos/observation.images.front/file_index": file_idx,
        "videos/observation.images.front/from_timestamp": vfrom,
        "videos/observation.images.front/to_timestamp": vto,
        "meta/episodes/chunk_index": 0, "meta/episodes/file_index": file_idx,
    }


def _write_data_parquet(path: Path, episodes: list[tuple[int, int]], fps: float = 10.0) -> None:
    ei_col, fi_col, ts_col, idx_col = [], [], [], []
    g = 0
    for ei, n in episodes:
        for f in range(n):
            ei_col.append(ei); fi_col.append(f); ts_col.append(f / fps); idx_col.append(g); g += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"episode_index": ei_col, "frame_index": fi_col,
                             "timestamp": ts_col, "index": idx_col}), path)


def _make_dataset(root: Path, episodes: list[tuple[int, int]], fps: float = 10.0,
                  footerless_index: bool = True, footerless_data: bool = True) -> Path:
    total_frames = sum(n for _, n in episodes)
    (root / "meta").mkdir(parents=True)
    (root / "meta" / "info.json").write_text(json.dumps({
        "codebase_version": "v3.0", "fps": fps, "total_episodes": len(episodes),
        "total_frames": total_frames, "total_tasks": 1, "splits": {"train": f"0:{len(episodes)}"},
        "features": {"observation.images.front": {"dtype": "video", "shape": [8, 8, 3]},
                     "observation.state": {"dtype": "float32", "shape": [2]}},
    }))
    (root / "meta" / "stats.json").write_text(json.dumps({
        "observation.state": {q: [0.0, 0.0] for q in ("min", "max", "mean", "std", "q01", "q10", "q50", "q90", "q99")}
        | {"count": [total_frames]}}))
    # packed episodes index (as lerobot writes it), then strip the footer
    rows, cursor, t = [], 0, 0.0
    for ei, n in episodes:
        rows.append(_episode_row(ei, n, cursor, t, t + n / fps)); cursor += n; t += n / fps
    ep = root / "meta" / "episodes" / "chunk-000" / "file-000.parquet"
    ep.parent.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist(rows), ep)
    if footerless_index:
        _truncate_footer(ep)
    dp = root / "data" / "chunk-000" / "file-000.parquet"
    _write_data_parquet(dp, episodes, fps)
    if footerless_data:
        _truncate_footer(dp)
    v = root / "videos" / "observation.images.front" / "chunk-000" / "file-000.mp4"
    v.parent.mkdir(parents=True)
    v.write_bytes(b"\x00" * 64)  # placeholder; ffprobe (if present) fails gracefully → explicit/anchor lengths
    return root


# ── 1. repair script on a synthetic footerless index ─────────────────────────

def test_repair_rebuilds_footerless_index(tmp_path):
    from tools import repair_episode_index as R
    root = _make_dataset(tmp_path / "ds", [(0, 10), (1, 12), (2, 8)])
    # orphan staging dir for an episode that was never saved (index == total_episodes)
    orphan = root / "images" / "observation.images.front" / "episode-000003"
    orphan.mkdir(parents=True); (orphan / "frame-000000.png").write_bytes(b"png")

    before = R.unreadable_parquets(root)
    assert [p.name for p in before["episodes"]] == ["file-000.parquet"]
    assert [p.name for p in before["data"]] == ["file-000.parquet"]

    plan = R.repair(root, dry_run=True, lengths=[10, 12, 8], schema_from=None)
    assert plan["action"] == "dry-run" and plan["lost_episodes"] == [0, 1, 2]
    assert (root / "meta" / "episodes" / "chunk-000" / "file-000.parquet").exists()  # untouched

    rep = R.repair(root, dry_run=False, lengths=[10, 12, 8], schema_from=None)
    assert rep["action"] == "repaired" and rep["verify"]["ok"], rep
    t = pq.read_table(root / "meta" / "episodes" / "chunk-000" / "file-000.parquet")
    rows = sorted(t.to_pylist(), key=lambda r: r["episode_index"])
    assert [r["length"] for r in rows] == [10, 12, 8]
    windows = [(r["videos/observation.images.front/from_timestamp"], r["videos/observation.images.front/to_timestamp"]) for r in rows]
    assert windows == [(0.0, 1.0), (1.0, 2.2), (2.2, 3.0)]
    assert len({w[0] for w in windows}) == 3, "windows must be distinct"
    assert [r["dataset_from_index"] for r in rows] == [0, 10, 22]
    # stats columns follow the aggregate schema; count = episode length
    assert rows[1]["stats/observation.state/count"] == [12]
    # backups: broken files + orphan staging moved, nothing deleted
    backup = root / rep["backup"]
    assert (backup / "meta/episodes/chunk-000/file-000.parquet").exists()
    assert (backup / "data/chunk-000/file-000.parquet").exists()
    assert (backup / "images/observation.images.front/episode-000003/frame-000000.png").exists()
    assert not orphan.exists()
    assert rep["data_lost_episodes"] == [0, 1, 2]
    assert json.loads((root / "meta" / "repair_log.json").read_text())[-1]["action"] == "repaired"
    # idempotent
    again = R.repair(root, dry_run=False, lengths=None, schema_from=None)
    assert again["action"].startswith("none")


def test_repair_scan_and_partial_index(tmp_path):
    """Sealed layout: file-000 fine, file-001 (killed mid-save) footerless →
    only episode 1 is rebuilt, episode 0's row and window are kept verbatim."""
    from tools import repair_episode_index as R
    root = _make_dataset(tmp_path / "ds", [(0, 10)], footerless_index=False, footerless_data=False)
    info = json.loads((root / "meta" / "info.json").read_text())
    info["total_episodes"] = 2; info["total_frames"] = 17
    (root / "meta" / "info.json").write_text(json.dumps(info))
    ep1 = root / "meta" / "episodes" / "chunk-000" / "file-001.parquet"
    pq.write_table(pa.Table.from_pylist([_episode_row(1, 7, 10, 0.0, 0.7, file_idx=1)]), ep1)
    _truncate_footer(ep1)
    (root / "videos" / "observation.images.front" / "chunk-000" / "file-001.mp4").write_bytes(b"\x00" * 64)

    found = R.scan(tmp_path)
    assert found[0]["unreadable_episode_files"] == ["meta/episodes/chunk-000/file-001.parquet"]

    rep = R.repair(root, dry_run=False, lengths=[7], schema_from=None)
    assert rep["lost_episodes"] == [1] and rep["verify"]["ok"], rep
    rows = {}
    for f in sorted((root / "meta" / "episodes").glob("*/*.parquet")):
        for r in pq.read_table(f).to_pylist():
            rows[r["episode_index"]] = r
    assert rows[0]["videos/observation.images.front/to_timestamp"] == 1.0
    assert rows[1]["length"] == 7 and rows[1]["dataset_from_index"] == 10
    # one-mp4-per-episode → the rebuilt row points at its own file
    assert rows[1]["videos/observation.images.front/file_index"] == 1
    assert rows[1]["videos/observation.images.front/from_timestamp"] == 0.0


def test_split_lengths_and_parity():
    from tools import repair_episode_index as R
    assert R.split_lengths(475, 4, {0: 82}, {1: 44.06, 2: 32.33, 3: 31.51}) == [82, 160, 118, 115]
    assert sum(R.split_lengths(100, 3, {}, {})) == 100
    # GOP 2: even segment (82) is invisible, odd ones (149, 113) flip the parity
    kf = list(range(0, 82, 2)) + list(range(82, 231, 2)) + list(range(231, 344, 2)) + list(range(344, 475, 2))
    assert R.parity_boundaries(kf, gop=2) == [231, 344]


# ── 2. dashboard: unfinalized index surfaced + fallback windows ───────────────

@pytest.fixture
def replay(tmp_path, monkeypatch):
    monkeypatch.setenv("ROVER_DATASET_ROOT", str(tmp_path))
    import dashboard_replay as dr
    importlib.reload(dr)
    return dr


def test_replay_unfinalized_index_uses_data_parquet_windows(replay, tmp_path):
    root = _make_dataset(tmp_path / "scout__x" / "thinker", [(0, 10), (1, 20), (2, 5)],
                         footerless_index=True, footerless_data=False)
    idx = replay._episode_index("scout__x::thinker")
    assert idx["index_state"] == "unfinalized"
    assert idx["unreadable_files"] == ["meta/episodes/chunk-000/file-000.parquet"]
    assert idx["fallback_source"] == "data-parquet"
    assert "unfinalized" in idx["warning"] and "repair_episode_index" in idx["warning"]
    rows = idx["rows"]
    assert [r["episode_index"] for r in rows] == [0, 1, 2]
    assert all(r["index"] == "unfinalized" and r["approx"] for r in rows)
    assert [(r["video_from"], r["video_to"]) for r in rows] == [(0.0, 1.0), (1.0, 3.0), (3.0, 3.5)]
    assert len({r["video_from"] for r in rows}) == 3, "never the same clip for every episode"
    # dataset listing carries the cheap footer check
    ds = {d["id"]: d for d in replay._list_datasets()}
    assert ds["scout__x::thinker"]["index_state"] == "unfinalized"


def test_replay_unfinalized_index_no_data_falls_back_to_anchors(replay, tmp_path):
    import sqlite3
    root = _make_dataset(tmp_path / "scout__y" / "thinker", [(0, 10), (1, 20)],
                         footerless_index=True, footerless_data=True)
    (root / "reasoning").mkdir()
    c = sqlite3.connect(root / "reasoning" / "events.sqlite")
    c.execute("CREATE TABLE episode_anchors (episode_index INTEGER PRIMARY KEY, repo_id TEXT, fps REAL, "
              "start_wall_ts REAL, stop_wall_ts REAL, task TEXT)")
    c.executemany("INSERT INTO episode_anchors VALUES (?,?,?,?,?,?)",
                  [(0, "r", 10.0, 100.0, 110.0, "explore"), (1, "r", 10.0, 200.0, 230.0, "explore")])
    c.commit(); c.close()
    idx = replay._episode_index("scout__y::thinker")
    assert idx["index_state"] == "unfinalized" and idx["fallback_source"] == "reasoning-anchors"
    rows = idx["rows"]
    assert [r["episode_index"] for r in rows] == [0, 1]
    assert all(r["index"] == "unfinalized" and r["window_known"] is False for r in rows)


def test_replay_finalized_index_exposes_per_episode_video_file(replay, tmp_path):
    root = _make_dataset(tmp_path / "scout__z" / "dash", [(0, 10)], footerless_index=False, footerless_data=False)
    # second (sealed) episode in its own files
    info = json.loads((root / "meta" / "info.json").read_text())
    info["total_episodes"] = 2; info["total_frames"] = 16
    (root / "meta" / "info.json").write_text(json.dumps(info))
    pq.write_table(pa.Table.from_pylist([_episode_row(1, 6, 10, 0.0, 0.6, file_idx=1)]),
                   root / "meta" / "episodes" / "chunk-000" / "file-001.parquet")
    (root / "videos" / "observation.images.front" / "chunk-000" / "file-001.mp4").write_bytes(b"\x00" * 64)
    idx = replay._episode_index("scout__z::dash")
    assert idx["index_state"] == "finalized" and idx["unreadable_files"] == []
    rows = idx["rows"]
    assert rows[0]["video_files"] == {"front": "chunk-000/file-000"}
    assert rows[1]["video_files"] == {"front": "chunk-000/file-001"}
    assert rows[1]["video_from"] == 0.0 and rows[1]["video_to"] == 0.6
    assert replay._video_path("scout__z::dash", "front", "chunk-000/file-001").name == "file-001.mp4"
    assert replay._video_path("scout__z::dash", "front").name == "file-000.mp4"
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        replay._video_path("scout__z::dash", "front", "../../etc/passwd")
    with pytest.raises(HTTPException):
        replay._video_path("scout__z::dash", "front", "chunk-000/file-009")


# ── 3. recorder sealing ──────────────────────────────────────────────────────

def test_recorder_seal_drops_handle_and_counts(monkeypatch):
    monkeypatch.setenv("SCOUT_SEAL_EPISODES", "1")
    from tools import _recorder_engine as re_
    eng = re_.RecorderEngine.__new__(re_.RecorderEngine)
    eng.seal_episodes = True; eng._sealed_episodes = 0; eng._episode_idx = 3; eng._last_error = None

    class FakeDS:
        finalized = 0
        def finalize(self):
            FakeDS.finalized += 1
    eng._dataset = FakeDS()
    assert eng._seal_dataset() is True
    assert eng._dataset is None and eng._sealed_episodes == 1 and FakeDS.finalized == 1
    assert eng._seal_dataset() is True  # idempotent when nothing is open

    class BadDS:
        def finalize(self):
            raise RuntimeError("disk full")
    eng._dataset = BadDS()
    assert eng._seal_dataset() is False
    assert eng._dataset is None and "finalize failed" in eng._last_error


def test_recorder_env_knob(monkeypatch):
    from tools import _recorder_engine as re_
    monkeypatch.setenv("SCOUT_SEAL_EPISODES", "0")
    assert re_.RecorderEngine(repo_id="scout/test-x/unit", capture_audio=False).seal_episodes is False
    monkeypatch.setenv("SCOUT_SEAL_EPISODES", "1")
    assert re_.RecorderEngine(repo_id="scout/test-x/unit", capture_audio=False).seal_episodes is True


def test_unreadable_parquets_helper(tmp_path):
    from tools import _recorder_engine as re_
    root = _make_dataset(tmp_path / "ds", [(0, 4)], footerless_index=True, footerless_data=False)
    bad = re_._unreadable_parquets(root)
    assert [str(p.relative_to(root)) for p in bad] == ["meta/episodes/chunk-000/file-000.parquet"]


def test_mid_session_readability_finalize_then_resume(tmp_path):
    pytest.importorskip("lerobot", reason="mid-session readability needs lerobot (run inside scout:slim)")
    """The seal cycle the recorder now performs after EVERY save_episode:
    finalize() → parquet readable → resume() → next episode → finalize().
    Two episodes recorded without 'stopping the process'; both index rows are
    readable and have distinct windows."""
    import numpy as np
    from lerobot.datasets.lerobot_dataset import LeRobotDataset
    root = tmp_path / "seal"
    features = {"observation.state": {"dtype": "float32", "shape": (2,), "names": ["a", "b"]},
                "action": {"dtype": "float32", "shape": (1,), "names": ["v"]}}
    ds = LeRobotDataset.create(repo_id="scout/seal-test", fps=10, features=features, root=root,
                               robot_type="test", use_videos=False)
    for _ in range(6):
        ds.add_frame({"observation.state": np.zeros(2, np.float32), "action": np.zeros(1, np.float32), "task": "t"})
    ds.save_episode(parallel_encoding=False)
    ep_files = lambda: sorted((root / "meta" / "episodes").glob("*/*.parquet"))
    with pytest.raises(Exception):  # the bug: streaming writer, no footer yet
        pq.read_table(ep_files()[0])
    ds.finalize()                  # ← _seal_dataset
    assert pq.read_table(ep_files()[0]).num_rows == 1
    ds = LeRobotDataset.resume(repo_id="scout/seal-test", root=root)  # ← next start_episode
    assert ds.meta.total_episodes == 1
    for _ in range(9):
        ds.add_frame({"observation.state": np.ones(2, np.float32), "action": np.ones(1, np.float32), "task": "t"})
    ds.save_episode(parallel_encoding=False)
    ds.finalize()
    rows = []
    for f in ep_files():
        rows += pq.read_table(f).to_pylist()
    rows.sort(key=lambda r: r["episode_index"])
    assert [r["episode_index"] for r in rows] == [0, 1]
    assert [(r["dataset_from_index"], r["dataset_to_index"]) for r in rows] == [(0, 6), (6, 15)]
    assert json.loads((root / "meta" / "info.json").read_text())["total_frames"] == 15
    # data files: one per sealed episode, all readable
    assert sum(pq.read_table(f).num_rows for f in sorted((root / "data").glob("*/*.parquet"))) == 15
