"""🧠 Persistent turn memory for scout.

Every user turn is appended as one JSONL line to ./.scout_memory/turns.jsonl
with the user prompt, the agent's final text response, and a small set of
action stats so future turns have context across sessions.

Public surface:
    record_turn(prompt, result, stats=None)  → append one turn
    recall_block(n=8)                         → markdown block of last N turns
                                                 (for system prompt injection)
    iter_turns()                              → generator over all stored turns

Design:
    * Single JSONL file = trivial to inspect, append-only, replicable.
    * No images stored — only TEXT (prompts + agent's final text reply).
      Images bloat disk + tokens; recent live frames already cover vision.
    * Truncates each field to keep replay context cheap.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

MEM_DIR = Path(os.getenv("SCOUT_MEMORY_DIR", ".scout_memory"))
MEM_FILE = MEM_DIR / "turns.jsonl"
MAX_PROMPT_CHARS = int(os.getenv("SCOUT_MEMORY_PROMPT_MAX", "1000"))
MAX_REPLY_CHARS = int(os.getenv("SCOUT_MEMORY_REPLY_MAX", "1500"))
MAX_RECALL_TURNS = int(os.getenv("SCOUT_MEMORY_RECALL_TURNS", "8"))

_lock = threading.Lock()


def _ensure_dir() -> None:
    MEM_DIR.mkdir(parents=True, exist_ok=True)


def _trunc(s: str, n: int) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[: n - 1].rstrip() + "…"


def _extract_text(result: Any) -> str:
    """Pull a plain-text reply out of whatever Strands handed back."""
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    # Strands AgentResult: .message.content is a list of {"text": ...} blocks
    msg = getattr(result, "message", None)
    if msg is not None:
        content = getattr(msg, "content", None) or (msg.get("content") if isinstance(msg, dict) else None)
        if isinstance(content, list):
            parts = [c.get("text") for c in content if isinstance(c, dict) and c.get("text")]
            if parts:
                return "\n".join(parts)
        # Some shapes nest .message.text directly
        text = getattr(msg, "text", None)
        if text:
            return str(text)
    # Last resort: stringify
    return str(result)


def record_turn(
    prompt: str,
    result: Any = None,
    stats: Optional[Dict[str, Any]] = None,
) -> None:
    """Append one turn to the JSONL log. Best-effort; never raises."""
    try:
        _ensure_dir()
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "prompt": _trunc(prompt or "", MAX_PROMPT_CHARS),
            "reply": _trunc(_extract_text(result), MAX_REPLY_CHARS),
        }
        if stats:
            entry["stats"] = stats
        line = json.dumps(entry, ensure_ascii=False)
        with _lock, MEM_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"🧠 memory: record_turn failed ({e})", flush=True)


def iter_turns() -> Iterator[Dict[str, Any]]:
    """Yield turns oldest→newest. Skips malformed lines silently."""
    if not MEM_FILE.exists():
        return
    with MEM_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _tail(n: int) -> List[Dict[str, Any]]:
    if n <= 0 or not MEM_FILE.exists():
        return []
    # Fast path for typical small tails: read all, slice. JSONL files
    # rarely grow huge here; if they do, swap for a reverse-read.
    all_turns = list(iter_turns())
    return all_turns[-n:]


def recall_block(n: Optional[int] = None) -> str:
    """Return a markdown block of the last N turns, ready to drop into a prompt.

    Empty string if memory is empty — caller can inject unconditionally.
    """
    n = MAX_RECALL_TURNS if n is None else n
    turns = _tail(n)
    if not turns:
        return ""
    lines: List[str] = ["## 🧠 RECENT TURNS (persistent memory):"]
    for i, t in enumerate(turns, 1):
        ts = t.get("ts", "?")
        p = t.get("prompt", "").replace("\n", " ").strip()
        r = t.get("reply", "").replace("\n", " ").strip()
        # Compact one-block-per-turn — keeps token cost bounded.
        block = f"\n[{i}] {ts}\n  user: {p}"
        if r:
            block += f"\n  scout: {r}"
        st = t.get("stats")
        if st:
            actions = st.get("actions", "?")
            block += f"\n  (actions: {actions})"
        lines.append(block)
    lines.append(
        "\nUse this history to maintain continuity across turns — remember "
        "what the user asked for, what you observed, and what you already did."
    )
    return "\n".join(lines) + "\n"


def stats() -> Dict[str, Any]:
    """Quick health check for debugging."""
    if not MEM_FILE.exists():
        return {"file": str(MEM_FILE), "turns": 0, "bytes": 0}
    size = MEM_FILE.stat().st_size
    return {
        "file": str(MEM_FILE),
        "turns": sum(1 for _ in iter_turns()),
        "bytes": size,
    }


if __name__ == "__main__":
    # quick CLI: `python memory.py` → print recall block
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "stats":
        print(json.dumps(stats(), indent=2))
    else:
        print(recall_block())
