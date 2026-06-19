"""🔁 ECoT exporter — events.sqlite → frame-aligned ChatML training samples.

Reads the reasoning_events DB (written live by reasoning_log.ReasoningLoggerHook)
and materializes the ECoT view described in RESEARCH.md §6/§8:

    • extended _strands_to_openai: structured tool_calls + frame-REFERENCES
      (no base64) + agent_id + per-event _meta(frame_index, t)
    • action_chunks: pulled from the LeRobot parquet via each motion tool's
      frame_span — the bridge between language reasoning and continuous control.

Usage:
    python -m tools.ecot_export <episode_index> [--repo <dataset_dir>]
    python -m tools.ecot_export --list
    python -m tools.ecot_export --ambient        # export the ambient track

Output (per episode):
    datasets/<repo>/reasoning/episode_NNNNNN.jsonl   # one raw event/line
    datasets/<repo>/reasoning/episode_NNNNNN.ecot.json  # the ChatML sample
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

_DATASET_ROOT = Path(os.getenv("ROVER_DATASET_ROOT", "datasets")).resolve()


# ── DB discovery ─────────────────────────────────────────────────────────────
def _find_db(repo_dir: Optional[str]) -> Path:
    if os.getenv("ECOT_EVENTS_DB"):
        return Path(os.getenv("ECOT_EVENTS_DB"))
    if repo_dir:
        return Path(repo_dir) / "reasoning" / "events.sqlite"
    # newest dataset with a reasoning DB
    cands = sorted(
        _DATASET_ROOT.glob("*/reasoning/events.sqlite"),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    if cands:
        return cands[0]
    raise FileNotFoundError(
        f"no events.sqlite found under {_DATASET_ROOT}/*/reasoning/. "
        f"Set ECOT_EVENTS_DB or pass --repo."
    )


def _conn(db: Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(db), timeout=10)
    c.row_factory = sqlite3.Row
    return c


# ── Span derivation (export-time) ────────────────────────────────────────────
# Motion tools (esp. rover_navigate) often have no `duration` arg the live hook
# could use, so frame_span is NULL. We recover the TRUE span from the actual
# elapsed frames: tool_use's frame_index → its matching tool_result's frame.
_MOTION_TOOLS = {"rover_move", "rover_navigate"}


def _derive_spans(rows):
    """Return rows as dicts with frame_span filled for motion tool_use events.

    Uses tool_use_id to pair a tool_use with its tool_result and spans
    [tool_use.frame_index, tool_result.frame_index]. Falls back to the
    existing frame_span if already present.
    """
    drows = [dict(r) for r in rows]
    # map tool_use_id → frame of its tool_result
    result_frame = {
        d["tool_use_id"]: d["frame_index"]
        for d in drows
        if d.get("type") == "tool_result" and d.get("tool_use_id")
    }
    for d in drows:
        if d.get("type") != "tool_use":
            continue
        if d.get("frame_span_lo") is not None:
            continue  # writer already gave us a span
        if (d.get("tool_name") or "") not in _MOTION_TOOLS:
            continue
        lo = d.get("frame_index")
        hi = result_frame.get(d.get("tool_use_id"))
        if lo is not None and hi is not None and hi >= lo:
            d["frame_span_lo"] = lo
            d["frame_span_hi"] = hi
    return drows


# ── Event loading ────────────────────────────────────────────────────────────
def load_events(db: Path, episode_index: Optional[int]) -> List[sqlite3.Row]:
    c = _conn(db)
    if episode_index is None:  # ambient track
        rows = c.execute(
            "SELECT * FROM reasoning_events WHERE episode_index IS NULL "
            "ORDER BY wall_ts, seq"
        ).fetchall()
    else:
        rows = c.execute(
            "SELECT * FROM reasoning_events WHERE episode_index=? "
            "ORDER BY wall_ts, seq", (episode_index,)
        ).fetchall()
    c.close()
    return rows


def load_context(db: Path, episode_index: Optional[int]) -> Dict[str, Any]:
    """Pick the richest context row for this episode (longest system prompt)."""
    c = _conn(db)
    if episode_index is None:
        rows = c.execute(
            "SELECT * FROM reasoning_context WHERE episode_index IS NULL"
        ).fetchall()
    else:
        rows = c.execute(
            "SELECT * FROM reasoning_context WHERE episode_index=?",
            (episode_index,),
        ).fetchall()
    c.close()
    if not rows:
        return {"system_prompt": "", "tool_specs": [], "model_id": ""}
    best = max(rows, key=lambda r: len(r["system_prompt"] or ""))
    return {
        "system_prompt": best["system_prompt"] or "",
        "tool_specs": json.loads(best["tool_specs"] or "[]"),
        "model_id": best["model_id"] or "",
    }


def load_anchor(db: Path, episode_index: int) -> Dict[str, Any]:
    c = _conn(db)
    row = c.execute(
        "SELECT * FROM episode_anchors WHERE episode_index=?", (episode_index,)
    ).fetchone()
    c.close()
    return dict(row) if row else {}


# ── extended Strands→OpenAI (frame-ref aware) ────────────────────────────────
def events_to_openai(rows: Iterable[sqlite3.Row]) -> List[Dict[str, Any]]:
    """Convert reasoning_events rows → OpenAI/ChatML messages.

    Differences from doer's _strands_to_openai (see RESEARCH.md §6):
      • images are frame REFERENCES (no base64), already in row.image_refs;
      • each message carries _meta {agent_id, frame_index, t, frame_span};
      • tool_use ↔ tool_result correlate by tool_use_id → tool_call_id;
      • consecutive assistant tool_use blocks merge into one assistant msg
        with a tool_calls array (native <tool_call> token emission).
    """
    out: List[Dict[str, Any]] = []
    pending_assistant: Optional[Dict[str, Any]] = None

    def _flush():
        nonlocal pending_assistant
        if pending_assistant is not None:
            # drop empty assistant msgs that carry neither text nor tool_calls
            if pending_assistant.get("content") or pending_assistant.get("tool_calls"):
                out.append(pending_assistant)
            pending_assistant = None

    for r in rows:
        typ = r["type"]
        meta = {
            "agent_id": r["agent_id"],
            "frame_index": r["frame_index"],
            "t": r["t_in_episode"],
        }
        if r["frame_span_lo"] is not None:
            meta["frame_span"] = [r["frame_span_lo"], r["frame_span_hi"]]

        if typ in ("user_input", "tool_result"):
            _flush()
            if typ == "user_input":
                content = r["text"] or ""
                refs = json.loads(r["image_refs"] or "[]")
                if refs:
                    content = (content + " " + " ".join(refs)).strip()
                out.append({"role": "user", "content": content, "_meta": meta})
            else:  # tool_result → OpenAI 'tool' role
                content = r["text"] or ""
                refs = json.loads(r["image_refs"] or "[]")
                if refs:
                    content = (content + " " + " ".join(refs)).strip()
                out.append({
                    "role": "tool",
                    "tool_call_id": r["tool_use_id"] or "",
                    "content": content,
                    "_meta": meta,
                })

        elif typ in ("reasoning", "assistant_end"):
            if pending_assistant is None:
                pending_assistant = {"role": "assistant", "content": "", "_meta": meta}
            txt = r["text"] or ""
            if txt:
                if pending_assistant["content"]:
                    pending_assistant["content"] += "\n" + txt
                else:
                    pending_assistant["content"] = txt
            # assistant_end closes the assistant message
            if typ == "assistant_end":
                _flush()

        elif typ == "tool_use":
            if pending_assistant is None:
                pending_assistant = {"role": "assistant", "content": "", "_meta": meta}
            pending_assistant.setdefault("tool_calls", []).append({
                "id": r["tool_use_id"] or "",
                "type": "function",
                "function": {
                    "name": r["tool_name"] or "unknown",
                    "arguments": r["tool_input"] or "{}",
                },
            })
            # carry span meta up to the assistant message for trainer use
            if "frame_span" in meta:
                pending_assistant["_meta"]["frame_span"] = meta["frame_span"]

    _flush()
    return out


# ── action_chunks puller (LeRobot parquet) ───────────────────────────────────
def pull_action_chunks(
    repo_dir: Path, episode_index: int, rows: Iterable[sqlite3.Row]
) -> List[Dict[str, Any]]:
    """For each motion tool_use with a frame_span, pull the matching action
    rows from the LeRobot parquet → the H-step action targets.

    Returns [] gracefully if pandas/parquet unavailable or no spans.
    """
    spans = [
        (r["tool_use_id"], r["frame_span_lo"], r["frame_span_hi"])
        for r in rows
        if r["type"] == "tool_use" and r["frame_span_lo"] is not None
    ]
    if not spans:
        return []
    try:
        import pandas as pd  # noqa
    except Exception:
        return [{"tool_use_id": tu, "frames": [lo, hi], "actions": None,
                 "note": "pandas unavailable"} for tu, lo, hi in spans]

    # Find this episode's parquet file(s). LeRobot v3 path:
    #   data/chunk-000/file-000.parquet  (episode_index column inside)
    parquets = sorted((repo_dir / "data").glob("chunk-*/file-*.parquet"))
    if not parquets:
        return [{"tool_use_id": tu, "frames": [lo, hi], "actions": None,
                 "note": "no parquet"} for tu, lo, hi in spans]

    import pandas as pd
    frames = []
    for pq in parquets:
        try:
            df = pd.read_parquet(pq)
            if "episode_index" in df.columns:
                df = df[df["episode_index"] == episode_index]
            if len(df):
                frames.append(df)
        except Exception:
            continue
    if not frames:
        return [{"tool_use_id": tu, "frames": [lo, hi], "actions": None,
                 "note": "episode not found in parquet"} for tu, lo, hi in spans]
    ep_df = pd.concat(frames).sort_values("frame_index") if "frame_index" in frames[0].columns else pd.concat(frames)

    chunks = []
    for tu, lo, hi in spans:
        try:
            if "frame_index" in ep_df.columns:
                sel = ep_df[(ep_df["frame_index"] >= lo) & (ep_df["frame_index"] <= hi)]
            else:
                sel = ep_df.iloc[lo:hi + 1]
            acts = [list(map(float, a)) for a in sel["action"].tolist()] if "action" in sel.columns else None
            chunks.append({"tool_use_id": tu, "frames": [lo, hi], "actions": acts})
        except Exception as e:
            chunks.append({"tool_use_id": tu, "frames": [lo, hi], "actions": None,
                           "note": f"pull failed: {e}"})
    return chunks


# ── top-level export ─────────────────────────────────────────────────────────
def export_episode(
    episode_index: Optional[int], repo_dir: Optional[str] = None,
    write: bool = True,
) -> Dict[str, Any]:
    db = _find_db(repo_dir)
    rdir = db.parent
    repo_root = rdir.parent  # datasets/<repo>/

    rows = _derive_spans(load_events(db, episode_index))
    ctx = load_context(db, episode_index)
    anchor = load_anchor(db, episode_index) if episode_index is not None else {}
    messages = events_to_openai(rows)
    action_chunks = (
        pull_action_chunks(repo_root, episode_index, rows)
        if episode_index is not None else []
    )

    sample = {
        "episode_index": episode_index,
        "repo_id": anchor.get("repo_id", repo_root.name),
        "fps": anchor.get("fps"),
        "task": anchor.get("task", ""),
        "model_id": ctx["model_id"],
        "system": ctx["system_prompt"],
        "tools": ctx["tool_specs"],
        "messages": messages,
        "action_chunks": action_chunks,
        "n_events": len(rows),
    }

    if write:
        tag = "ambient" if episode_index is None else f"episode_{episode_index:06d}"
        # raw events jsonl
        with (rdir / f"{tag}.jsonl").open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(dict(r), ensure_ascii=False, default=str) + "\n")
        # ChatML sample
        with (rdir / f"{tag}.ecot.json").open("w", encoding="utf-8") as f:
            json.dump(sample, f, ensure_ascii=False, indent=2, default=str)
        print(f"✅ wrote {rdir / (tag + '.ecot.json')}  "
              f"({len(rows)} events, {len(messages)} msgs, "
              f"{len(action_chunks)} action chunks)")
    return sample


def list_episodes(repo_dir: Optional[str] = None) -> None:
    db = _find_db(repo_dir)
    c = _conn(db)
    print(f"DB: {db}\n")
    print("episode_index | events | agents | tasks")
    rows = c.execute(
        "SELECT episode_index, COUNT(*) n, "
        "GROUP_CONCAT(DISTINCT agent_id) agents "
        "FROM reasoning_events GROUP BY episode_index ORDER BY episode_index"
    ).fetchall()
    for r in rows:
        ep = r["episode_index"]
        anchor = load_anchor(db, ep) if ep is not None else {}
        print(f"  {str(ep):>11} | {r['n']:>6} | {r['agents']:<20} | {anchor.get('task','')}")
    c.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="ECoT exporter")
    ap.add_argument("episode", nargs="?", type=int, help="episode index to export")
    ap.add_argument("--repo", default=None, help="dataset dir (datasets/<repo>)")
    ap.add_argument("--list", action="store_true", help="list episodes in DB")
    ap.add_argument("--ambient", action="store_true", help="export ambient (no-episode) track")
    args = ap.parse_args()

    if args.list:
        list_episodes(args.repo)
        return
    if args.ambient:
        export_episode(None, args.repo)
        return
    if args.episode is None:
        ap.error("pass an episode index, or --list / --ambient")
    export_episode(args.episode, args.repo)


if __name__ == "__main__":
    main()
