"""🧠 Reasoning event logger — the language/CoT sidecar to the LeRobot spine.

Captures, LIVE and per-atomic-block, the reasoning of every Strands agent
driving the rover (main / thinker / telegram / voice) and time-aligns it to
the dense LeRobot video via:

        frame_index = round((wall_ts - episode_start_ts) * fps)

Everything lands in ONE cross-process SQLite DB (WAL mode) so 3+ agents in
separate processes can all append concurrently — same pattern as voice_bridge.

Wire-in is one line per agent:

    from tools.reasoning_log import ReasoningLoggerHook
    agent = Agent(..., hooks=[ReasoningLoggerHook(agent_id="main")])

This is ADDITIVE — it does not touch the dense recorder at all. It only READS
the recorder's current (episode_idx, start_ts, fps) to compute frame bindings.

See RESEARCH.md §3–§5 for the full schema + frame-binding rules.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── DB location ──────────────────────────────────────────────────────────────
# Default: alongside the active dataset's reasoning/ dir. The dataset root is
# the same one the recorder uses (ROVER_DATASET_ROOT / datasets).
_DATASET_ROOT = Path(os.getenv("ROVER_DATASET_ROOT", "datasets")).resolve()
_DB_OVERRIDE = os.getenv("ECOT_EVENTS_DB")  # explicit path wins


def _db_path() -> Path:
    if _DB_OVERRIDE:
        p = Path(_DB_OVERRIDE)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    # Bind to the engine's current dataset root if available, else a shared one.
    root = _DATASET_ROOT
    try:
        from ._recorder_engine import get_engine
        eng = get_engine()
        # Per-agent reasoning DB: lives INSIDE this process's own dataset dir,
        # sibling to data/ + videos/. Each agent dir is thus a self-contained
        # ECoT dataset (reasoning + dense frames + exporter output together).
        # LeRobot parquet is single-writer, so datasets are per-agent anyway;
        # a per-agent reasoning DB is the natural, collision-free match.
        root = eng._dataset_root()  # type: ignore[attr-defined]
    except Exception:
        root = _DATASET_ROOT / "_ecot_shared"
    rdir = root / "reasoning"
    rdir.mkdir(parents=True, exist_ok=True)
    return rdir / "events.sqlite"


_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS reasoning_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    wall_ts         REAL    NOT NULL,
    mono_ts         REAL    NOT NULL,
    episode_index   INTEGER,
    frame_index     INTEGER,
    frame_span_lo   INTEGER,
    frame_span_hi   INTEGER,
    t_in_episode    REAL,
    agent_id        TEXT    NOT NULL,
    session_id      TEXT    NOT NULL,
    turn_index      INTEGER NOT NULL DEFAULT 0,
    seq             INTEGER NOT NULL,
    role            TEXT    NOT NULL,
    type            TEXT    NOT NULL,
    tool_name       TEXT,
    tool_use_id     TEXT,
    text            TEXT,
    tool_input      TEXT,
    image_refs      TEXT,
    meta            TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_re_episode ON reasoning_events(episode_index, frame_index);
CREATE INDEX IF NOT EXISTS idx_re_session ON reasoning_events(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_re_agent   ON reasoning_events(agent_id, wall_ts);
CREATE INDEX IF NOT EXISTS idx_re_tooluse ON reasoning_events(tool_use_id);

CREATE TABLE IF NOT EXISTS reasoning_context (
    session_id      TEXT PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    episode_index   INTEGER,
    model_id        TEXT,
    system_prompt   TEXT,
    tool_specs      TEXT,
    started_wall_ts REAL,
    started_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS episode_anchors (
    episode_index   INTEGER PRIMARY KEY,
    repo_id         TEXT,
    fps             REAL,
    start_wall_ts   REAL,
    stop_wall_ts    REAL,
    task            TEXT
);
"""

_local = threading.local()


def _conn() -> sqlite3.Connection:
    """One connection per thread (sqlite connections aren't thread-safe)."""
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(str(_db_path()), timeout=10)
        c.executescript(_SCHEMA)
        c.commit()
        _local.conn = c
    return c


# ── Frame binding ────────────────────────────────────────────────────────────
def _episode_binding() -> Tuple[Optional[int], Optional[float], float]:
    """Return (episode_index, episode_start_ts, fps) from the live recorder.

    If no episode is active → (None, None, fps_default). Ambient events get
    episode_index=None and are reconciled at export time.
    """
    try:
        from ._recorder_engine import get_engine
        eng = get_engine()
        recording = eng._recording_evt.is_set()  # type: ignore[attr-defined]
        fps = float(getattr(eng, "fps", 4) or 4)
        if recording:
            return (
                int(eng._episode_idx),            # type: ignore[attr-defined]
                float(eng._episode_start_ts),     # type: ignore[attr-defined]
                fps,
            )
        return (None, None, fps)
    except Exception:
        return (None, None, float(os.getenv("ROVER_RECORD_FPS", "4")))


def _frame_for(wall_ts: float) -> Tuple[Optional[int], Optional[int], Optional[float]]:
    """Compute (episode_index, frame_index, t_in_episode) for a wall_ts."""
    ep, start, fps = _episode_binding()
    if ep is None or start is None:
        return (None, None, None)
    t = max(0.0, wall_ts - start)
    return (ep, int(round(t * fps)), t)


def _span_for(t_call: float, duration: float) -> Tuple[Optional[int], Optional[int]]:
    """Frame span [lo, hi] for an action that lasts `duration` seconds."""
    ep, start, fps = _episode_binding()
    if ep is None or start is None:
        return (None, None)
    lo = int(round(max(0.0, t_call - start) * fps))
    hi = int(round(max(0.0, t_call + max(0.0, duration) - start) * fps))
    return (lo, hi)


# ── Image dereferencing: base64 blocks → frame refs ──────────────────────────
def _image_refs_from_content(content: List[dict], frame_index: Optional[int]) -> List[str]:
    """Turn inline image blocks into LeRobot frame references (no base64)."""
    refs: List[str] = []
    fi = frame_index if frame_index is not None else "?"
    for c in content:
        if not isinstance(c, dict):
            continue
        if "image" in c:
            # We can't know front vs rear from the block alone; default front
            # (rover_see/move capture front by default). Rear handled by label.
            refs.append(f"observation.images.front#frame={fi}")
    return refs


# ── Low-level emit ───────────────────────────────────────────────────────────
def emit_event(
    *,
    agent_id: str,
    session_id: str,
    seq: int,
    role: str,
    type: str,
    turn_index: int = 0,
    text: Optional[str] = None,
    tool_name: Optional[str] = None,
    tool_use_id: Optional[str] = None,
    tool_input: Optional[dict] = None,
    image_refs: Optional[List[str]] = None,
    frame_span: Optional[Tuple[Optional[int], Optional[int]]] = None,
    meta: Optional[dict] = None,
    wall_ts: Optional[float] = None,
) -> int:
    """Write ONE reasoning event. Best-effort, never raises into the agent."""
    try:
        wt = wall_ts if wall_ts is not None else time.time()
        ep, fi, t_in = _frame_for(wt)
        span_lo, span_hi = (frame_span or (None, None))
        c = _conn()
        cur = c.execute(
            """INSERT INTO reasoning_events(
                 wall_ts, mono_ts, episode_index, frame_index,
                 frame_span_lo, frame_span_hi, t_in_episode,
                 agent_id, session_id, turn_index, seq, role, type,
                 tool_name, tool_use_id, text, tool_input, image_refs, meta)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                wt, time.monotonic(), ep, fi,
                span_lo, span_hi, t_in,
                agent_id, session_id, turn_index, seq, role, type,
                tool_name, tool_use_id,
                text, json.dumps(tool_input, ensure_ascii=False) if tool_input is not None else None,
                json.dumps(image_refs, ensure_ascii=False) if image_refs else None,
                json.dumps(meta, ensure_ascii=False) if meta else None,
            ),
        )
        c.commit()
        return int(cur.lastrowid)
    except Exception as e:
        print(f"🧠 reasoning_log: emit failed ({e})", flush=True)
        return 0


def record_context(
    *,
    session_id: str,
    agent_id: str,
    system_prompt: str,
    tool_specs: List[dict],
    model_id: str = "",
) -> None:
    """Persist the constant per-turn context (system + tool specs)."""
    try:
        ep, _, _ = _episode_binding()
        c = _conn()
        c.execute(
            """INSERT OR REPLACE INTO reasoning_context(
                 session_id, agent_id, episode_index, model_id,
                 system_prompt, tool_specs, started_wall_ts)
               VALUES(?,?,?,?,?,?,?)""",
            (session_id, agent_id, ep, model_id, system_prompt,
             json.dumps(tool_specs, ensure_ascii=False), time.time()),
        )
        c.commit()
    except Exception as e:
        print(f"🧠 reasoning_log: context failed ({e})", flush=True)


def anchor_episode(
    episode_index: int, repo_id: str, fps: float,
    start_wall_ts: float, stop_wall_ts: Optional[float] = None, task: str = "",
) -> None:
    """Record/refresh an episode's wall-clock anchors (called by recorder)."""
    try:
        c = _conn()
        if stop_wall_ts is None:
            c.execute(
                """INSERT OR REPLACE INTO episode_anchors(
                     episode_index, repo_id, fps, start_wall_ts, task)
                   VALUES(?,?,?,?,?)""",
                (episode_index, repo_id, fps, start_wall_ts, task),
            )
        else:
            c.execute(
                "UPDATE episode_anchors SET stop_wall_ts=? WHERE episode_index=?",
                (stop_wall_ts, episode_index),
            )
        c.commit()
    except Exception as e:
        print(f"🧠 reasoning_log: anchor failed ({e})", flush=True)


# ── Tool-spec extraction ─────────────────────────────────────────────────────
def _extract_tool_specs(agent) -> List[dict]:
    out: List[dict] = []
    reg = getattr(agent, "tool_registry", None)
    if not reg:
        return out
    for name, t in getattr(reg, "registry", {}).items():
        spec = getattr(t, "tool_spec", None)
        if not spec:
            continue
        out.append({
            "name": spec.get("name", name),
            "description": spec.get("description", ""),
            "input_schema": spec.get("inputSchema", {}).get("json", {}),
        })
    return out


# ── The Strands hook ─────────────────────────────────────────────────────────
class ReasoningLoggerHook:
    """Attach to any Strands Agent to log its reasoning, time-aligned to frames.

    Subscribes to:
      • BeforeInvocationEvent → start a session, snapshot system+tool specs
      • MessageAddedEvent     → decompose each message into atomic events
      • Before/AfterToolCallEvent → capture precise tool timing → frame SPANS
      • AfterInvocationEvent  → mark assistant_end

    Motion tools (rover_move / rover_navigate) get a frame SPAN derived from
    the tool's `duration` argument and its measured call window; everything
    else binds to the instantaneous frame.
    """

    # Tools whose effect spans a frame range rather than an instant.
    _SPAN_TOOLS = {"rover_move", "rover_navigate"}

    def __init__(self, agent_id: str = "main"):
        self.agent_id = agent_id
        self._session_id = ""
        self._seq = 0
        self._turn = 0
        # tool_use_id → (call_wall_ts, duration_hint)
        self._tool_calls: Dict[str, Tuple[float, float]] = {}

    # Strands HookProvider protocol
    def register_hooks(self, registry, **kwargs) -> None:
        from strands.hooks import (
            BeforeInvocationEvent, AfterInvocationEvent, MessageAddedEvent,
            BeforeToolCallEvent, AfterToolCallEvent,
        )
        registry.add_callback(BeforeInvocationEvent, self._on_before)
        registry.add_callback(MessageAddedEvent, self._on_message)
        registry.add_callback(BeforeToolCallEvent, self._on_tool_before)
        registry.add_callback(AfterToolCallEvent, self._on_tool_after)
        registry.add_callback(AfterInvocationEvent, self._on_after)

    # ── handlers ──
    def _on_before(self, event) -> None:
        self._session_id = f"{self.agent_id}-{uuid.uuid4().hex[:12]}"
        self._seq = 0
        self._turn += 1
        agent = event.agent
        try:
            record_context(
                session_id=self._session_id,
                agent_id=self.agent_id,
                system_prompt=getattr(agent, "system_prompt", "") or "",
                tool_specs=_extract_tool_specs(agent),
                model_id=str(getattr(getattr(agent, "model", None), "model_id", "")),
            )
        except Exception:
            pass

    def _on_tool_before(self, event) -> None:
        tu = getattr(event, "tool_use", None) or {}
        tuid = tu.get("toolUseId") or tu.get("toolUseId", "")
        name = tu.get("name", "")
        inp = tu.get("input", {}) or {}
        # duration hint for span binding (motion tools carry it)
        dur = 0.0
        try:
            dur = float(inp.get("duration", 0.0))
        except Exception:
            dur = 0.0
        if name in self._SPAN_TOOLS and dur <= 0:
            dur = 1.0  # rover_move default duration
        if tuid:
            self._tool_calls[tuid] = (time.time(), dur)

    def _on_tool_after(self, event) -> None:
        # Timing captured; frame span computed when the tool_result message
        # is decomposed in _on_message. Nothing required here besides keeping
        # the call window available (already stored in _on_tool_before).
        pass

    def _on_message(self, event) -> None:
        msg = event.message
        if not isinstance(msg, dict):
            return
        role = msg.get("role", "user")
        content = msg.get("content", "")
        wt = time.time()

        if not isinstance(content, list):
            self._emit(role=role, type=("reasoning" if role == "assistant" else "user_input"),
                       text=str(content), wall_ts=wt)
            return

        fi_ep, fi, _ = _frame_for(wt)
        for c in content:
            if not isinstance(c, dict):
                continue
            if "text" in c and c["text"]:
                t = "reasoning" if role == "assistant" else "user_input"
                self._emit(role=role, type=t, text=c["text"],
                           image_refs=None, wall_ts=wt)
            elif "image" in c:
                self._emit(role=role, type=("user_input" if role == "user" else "reasoning"),
                           text="[image]",
                           image_refs=_image_refs_from_content([c], fi), wall_ts=wt)
            elif "toolUse" in c:
                tu = c["toolUse"]
                tuid = tu.get("toolUseId", "")
                name = tu.get("name", "")
                inp = tu.get("input", {}) or {}
                span = None
                if tuid in self._tool_calls:
                    t_call, dur = self._tool_calls[tuid]
                    if name in self._SPAN_TOOLS:
                        span = _span_for(t_call, dur)
                self._emit(role="assistant", type="tool_use", text=None,
                           tool_name=name, tool_use_id=tuid, tool_input=inp,
                           frame_span=span, wall_ts=wt)
            elif "toolResult" in c:
                tr = c["toolResult"]
                tuid = tr.get("toolUseId", "")
                txt = "".join(
                    rc.get("text", "") for rc in tr.get("content", [])
                    if isinstance(rc, dict) and "text" in rc
                )
                imgs = _image_refs_from_content(tr.get("content", []), fi)
                self._emit(role="user", type="tool_result", text=txt or None,
                           tool_use_id=tuid, image_refs=imgs or None, wall_ts=wt)

    def _on_after(self, event) -> None:
        result = getattr(event, "result", None)
        txt = ""
        try:
            msg = getattr(result, "message", None)
            if isinstance(msg, dict):
                txt = "".join(c.get("text", "") for c in msg.get("content", [])
                              if isinstance(c, dict) and "text" in c)
        except Exception:
            pass
        self._emit(role="assistant", type="assistant_end", text=txt or None,
                   wall_ts=time.time())
        self._tool_calls.clear()

    # ── emit shim ──
    def _emit(self, **kw) -> None:
        emit_event(
            agent_id=self.agent_id,
            session_id=self._session_id or f"{self.agent_id}-nosess",
            seq=self._seq,
            turn_index=self._turn,
            **kw,
        )
        self._seq += 1


# ── quick CLI: dump recent events ────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    c = _conn()
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    rows = c.execute(
        "SELECT id, agent_id, episode_index, frame_index, type, tool_name, "
        "substr(coalesce(text,tool_input,''),1,60) FROM reasoning_events "
        "ORDER BY id DESC LIMIT ?", (n,)
    ).fetchall()
    print(f"DB: {_db_path()}")
    for r in reversed(rows):
        print(f"  #{r[0]} [{r[1]}] ep={r[2]} f={r[3]} {r[4]:13} {r[5] or '':14} {r[6]!r}")
