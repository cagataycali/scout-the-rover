"""🔗 Merge per-agent ECoT datasets → one unified timeline.

Per-agent datasets (main/, thinker/, telegram/, voice/) are self-contained
because LeRobot v3 parquet is single-writer (see RESEARCH.md §4.5). This tool
materializes the "one timeline" view at PREP time:

  1. Dense LeRobot data → official `aggregate_datasets` (concats parquet+video,
     offsets episode/frame indices, recomputes stats). This is the canonical,
     training-ready merged dataset.
  2. Reasoning events.sqlite → merged with the SAME episode-index offsets the
     aggregation applied, so reasoning stays frame-aligned to the unified
     video spine. agent_id is preserved (provenance), plus source_episode.

Usage:
    python -m tools.merge_datasets <parent_dir> [--out <name>] [--agents a,b,c]
    python -m tools.merge_datasets datasets/scout__earth-rover-mini-20260619
      → datasets/scout__earth-rover-mini-20260619__merged/

The merged dir is a normal LeRobot dataset + a unified reasoning/events.sqlite,
so the /replay dashboard and ecot_export work on it unchanged.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def _discover_agents(parent: Path) -> List[Path]:
    """Per-agent dataset dirs = children with meta/info.json."""
    out = []
    for child in sorted(parent.iterdir()):
        if child.is_dir() and (child / "meta" / "info.json").exists():
            out.append(child)
    return out


def _parquet_finalized(ds_dir: Path) -> bool:
    """True if the dataset's data parquet has a valid footer (not being written).

    A live recorder holds file-000.parquet open with no PAR1 footer yet; merging
    such a file raises ArrowInvalid. We check the last 4 bytes == b"PAR1".
    """
    import glob
    pqs = glob.glob(str(ds_dir / "data" / "*" / "*.parquet"))
    pqs += glob.glob(str(ds_dir / "meta" / "episodes" / "*" / "*.parquet"))
    if not pqs:
        return False
    for f in pqs:
        try:
            with open(f, "rb") as fh:
                fh.seek(-4, 2)
                if fh.read(4) != b"PAR1":
                    return False
        except Exception:
            return False
    return True


def _episode_count(ds_dir: Path) -> int:
    try:
        return int(json.loads((ds_dir / "meta" / "info.json").read_text()).get("total_episodes", 0))
    except Exception:
        return 0


def _repo_id(ds_dir: Path) -> str:
    """Best-effort repo_id; aggregate_datasets just needs a unique string."""
    return f"scout/{ds_dir.parent.name}__{ds_dir.name}"


def merge(parent: Path, out_name: Optional[str] = None,
          agents: Optional[List[str]] = None) -> Path:
    parent = Path(parent).resolve()
    agent_dirs = _discover_agents(parent)
    if agents:
        want = set(agents)
        agent_dirs = [d for d in agent_dirs if d.name in want]
    # Exclude any prior merged output
    agent_dirs = [d for d in agent_dirs if not d.name.endswith("merged")]
    if not agent_dirs:
        raise SystemExit(f"no per-agent datasets found under {parent}")

    # Skip datasets that are still being written (live agent → no parquet footer).
    finalized, live = [], []
    for d in agent_dirs:
        (finalized if _parquet_finalized(d) else live).append(d)
    if live:
        print("⚠️  skipping datasets still being recorded (stop the agent first):")
        for d in live:
            print(f"     • {d.name}  (parquet not finalized — agent still running?)")
    agent_dirs = finalized
    if not agent_dirs:
        raise SystemExit(
            "All datasets are mid-recording. Stop the agents (Ctrl+C) so their "
            "parquet footers flush, then re-run `make merge`."
        )

    out_name = out_name or f"{parent.name}__merged"
    aggr_root = parent.parent / out_name
    if aggr_root.exists():
        print(f"⚠️  removing existing {aggr_root}")
        shutil.rmtree(aggr_root)

    print(f"🔗 merging {len(agent_dirs)} agent datasets → {aggr_root.name}")
    for d in agent_dirs:
        print(f"   • {d.name}: {_episode_count(d)} episodes")

    # ── 1. dense data via official aggregator ──
    from lerobot.datasets.aggregate import aggregate_datasets
    repo_ids = [_repo_id(d) for d in agent_dirs]
    roots = [d for d in agent_dirs]
    aggr_repo_id = f"scout/{out_name}"
    aggregate_datasets(
        repo_ids=repo_ids,
        aggr_repo_id=aggr_repo_id,
        roots=roots,
        aggr_root=aggr_root,
    )
    print(f"✅ dense LeRobot data aggregated")

    # ── 2. reasoning merge with matching episode offsets ──
    # aggregate_datasets appends episodes in `agent_dirs` order, so agent k's
    # episodes are offset by the cumulative episode count of agents 0..k-1.
    merged_db = aggr_root / "reasoning" / "events.sqlite"
    merged_db.parent.mkdir(parents=True, exist_ok=True)
    _merge_reasoning(agent_dirs, merged_db)

    print(f"\n✅ merged dataset ready: {aggr_root}")
    print(f"   • LeRobot data (training-ready, unified episode indices)")
    print(f"   • reasoning/events.sqlite (frame-aligned, agent_id preserved)")
    print(f"   Use with /replay or `python -m tools.ecot_export --repo {aggr_root}`")
    return aggr_root


def _merge_reasoning(agent_dirs: List[Path], merged_db: Path) -> None:
    """Concat each agent's events.sqlite into one, offsetting episode_index by
    the cumulative episode count (matching aggregate_datasets' append order).
    Adds agent_id + source_episode provenance columns.
    """
    if merged_db.exists():
        merged_db.unlink()
    out = sqlite3.connect(str(merged_db))
    out.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE reasoning_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wall_ts REAL, mono_ts REAL,
            episode_index INTEGER, frame_index INTEGER,
            frame_span_lo INTEGER, frame_span_hi INTEGER, t_in_episode REAL,
            agent_id TEXT, session_id TEXT, turn_index INTEGER,
            seq INTEGER, role TEXT, type TEXT,
            tool_name TEXT, tool_use_id TEXT,
            text TEXT, tool_input TEXT, image_refs TEXT, meta TEXT,
            source_agent TEXT, source_episode INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE reasoning_context (
            session_id TEXT, agent_id TEXT, episode_index INTEGER,
            model_id TEXT, system_prompt TEXT, tool_specs TEXT,
            started_wall_ts REAL, source_agent TEXT
        );
        CREATE TABLE episode_anchors (
            episode_index INTEGER PRIMARY KEY, repo_id TEXT, fps REAL,
            start_wall_ts REAL, stop_wall_ts REAL, task TEXT, source_agent TEXT
        );
        CREATE INDEX idx_re_episode ON reasoning_events(episode_index, frame_index);
        CREATE INDEX idx_re_agent ON reasoning_events(source_agent, wall_ts);
    """)

    ep_offset = 0
    total_events = 0
    for d in agent_dirs:
        agent = d.name
        n_eps = _episode_count(d)
        src_db = d / "reasoning" / "events.sqlite"
        if src_db.exists():
            src = sqlite3.connect(str(src_db))
            src.row_factory = sqlite3.Row
            # events
            try:
                rows = src.execute("SELECT * FROM reasoning_events").fetchall()
            except Exception:
                rows = []
            for r in rows:
                ei = r["episode_index"]
                new_ei = (ei + ep_offset) if ei is not None else None
                out.execute(
                    """INSERT INTO reasoning_events(
                        wall_ts,mono_ts,episode_index,frame_index,
                        frame_span_lo,frame_span_hi,t_in_episode,
                        agent_id,session_id,turn_index,seq,role,type,
                        tool_name,tool_use_id,text,tool_input,image_refs,meta,
                        source_agent,source_episode)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (r["wall_ts"], r["mono_ts"], new_ei, r["frame_index"],
                     r["frame_span_lo"], r["frame_span_hi"], r["t_in_episode"],
                     r["agent_id"], r["session_id"], r["turn_index"], r["seq"],
                     r["role"], r["type"], r["tool_name"], r["tool_use_id"],
                     r["text"], r["tool_input"], r["image_refs"], r["meta"],
                     agent, ei),
                )
                total_events += 1
            # context
            try:
                for r in src.execute("SELECT * FROM reasoning_context").fetchall():
                    ei = r["episode_index"]
                    out.execute(
                        """INSERT INTO reasoning_context(session_id,agent_id,
                             episode_index,model_id,system_prompt,tool_specs,
                             started_wall_ts,source_agent) VALUES(?,?,?,?,?,?,?,?)""",
                        (r["session_id"], r["agent_id"],
                         (ei + ep_offset) if ei is not None else None,
                         r["model_id"], r["system_prompt"], r["tool_specs"],
                         r["started_wall_ts"], agent))
            except Exception:
                pass
            # anchors
            try:
                for r in src.execute("SELECT * FROM episode_anchors").fetchall():
                    out.execute(
                        """INSERT OR REPLACE INTO episode_anchors(episode_index,
                             repo_id,fps,start_wall_ts,stop_wall_ts,task,source_agent)
                           VALUES(?,?,?,?,?,?,?)""",
                        (r["episode_index"] + ep_offset, r["repo_id"], r["fps"],
                         r["start_wall_ts"], r["stop_wall_ts"], r["task"], agent))
            except Exception:
                pass
            src.close()
        ep_offset += n_eps
    out.commit()
    out.close()
    print(f"✅ reasoning merged: {total_events} events, {ep_offset} episodes "
          f"(episode_index offset per agent; source_agent preserved)")


def main():
    ap = argparse.ArgumentParser(description="Merge per-agent ECoT datasets → one timeline")
    ap.add_argument("parent", help="per-day parent dir (datasets/scout__...-YYYYMMDD)")
    ap.add_argument("--out", default=None, help="output dataset name")
    ap.add_argument("--agents", default=None, help="comma list to include (default: all)")
    a = ap.parse_args()
    agents = [x.strip() for x in a.agents.split(",")] if a.agents else None
    merge(Path(a.parent), a.out, agents)


if __name__ == "__main__":
    main()
