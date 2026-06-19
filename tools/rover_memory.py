"""🧠 SQLite-backed long-term memory for scout.

Where the JSONL turn log is a *passive* short-term tape (last N turns auto-
injected), this tool gives scout an *active* long-term knowledge store.
The model decides what to remember, recall, update, or forget.

Use cases scout will reach for this:
    * Spatial map: "kitchen is to the left of the entrance"
    * Operator preferences: "user wants slow speeds at night"
    * Hazards: "step-down by garage door, lat=X lng=Y"
    * Observations: "cat sleeps on the porch chair around 3pm"
    * Goals/state: "looking for the red ball — last seen near sofa"

Storage:
    .scout_memory/memory.db (SQLite, FTS5 on content if available)

Schema:
    memories(
        id INTEGER PRIMARY KEY,
        category TEXT,        -- 'spatial' | 'preference' | 'hazard' | 'fact' | 'goal' | other
        title TEXT,           -- short label (≤120 chars)
        content TEXT,         -- the memory body
        tags TEXT,            -- comma-separated free-form tags
        importance INTEGER,   -- 1..5, used to bias recall ordering
        meta TEXT,            -- JSON blob (lat/lng/battery/etc — anything contextual)
        created_at TEXT,      -- ISO8601
        updated_at TEXT,      -- ISO8601
        access_count INTEGER  -- bumped on recall (LRU-ish)
    )

Public Strands tool: rover_memory
    actions: remember | recall | search | update | forget | list | stats | recent
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from strands import tool

from ._rover_common import error_result, ok_result

# Storage

MEM_DIR = Path(os.getenv("SCOUT_MEMORY_DIR", ".scout_memory"))
DB_FILE = MEM_DIR / "memory.db"

VALID_CATEGORIES = {
    "spatial",      # places, layouts, landmarks
    "preference",   # what the operator likes / dislikes
    "hazard",       # things to avoid
    "fact",         # general world knowledge
    "goal",         # current/past objectives
    "person",       # people scout has met
    "object",       # things to remember about specific items
    "other",
}

_lock = threading.Lock()
_fts_available: Optional[bool] = None


def _connect() -> sqlite3.Connection:
    MEM_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_FILE), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> bool:
    """Create tables + FTS index if missing. Returns True if FTS5 is usable."""
    global _fts_available
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category    TEXT NOT NULL DEFAULT 'other',
            title       TEXT NOT NULL,
            content     TEXT NOT NULL,
            tags        TEXT DEFAULT '',
            importance  INTEGER NOT NULL DEFAULT 3,
            meta        TEXT DEFAULT '{}',
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            access_count INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_category ON memories(category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_importance ON memories(importance DESC, updated_at DESC)")

    if _fts_available is None:
        try:
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
                USING fts5(title, content, tags, content='memories', content_rowid='id')
            """)
            # Triggers to keep FTS in sync
            conn.executescript("""
                CREATE TRIGGER IF NOT EXISTS mem_ai AFTER INSERT ON memories BEGIN
                    INSERT INTO memories_fts(rowid, title, content, tags)
                    VALUES (new.id, new.title, new.content, new.tags);
                END;
                CREATE TRIGGER IF NOT EXISTS mem_ad AFTER DELETE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, title, content, tags)
                    VALUES('delete', old.id, old.title, old.content, old.tags);
                END;
                CREATE TRIGGER IF NOT EXISTS mem_au AFTER UPDATE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, title, content, tags)
                    VALUES('delete', old.id, old.title, old.content, old.tags);
                    INSERT INTO memories_fts(rowid, title, content, tags)
                    VALUES (new.id, new.title, new.content, new.tags);
                END;
            """)
            _fts_available = True
        except sqlite3.OperationalError:
            _fts_available = False
    conn.commit()
    return _fts_available


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    try:
        d["meta"] = json.loads(d.get("meta") or "{}")
    except Exception:
        d["meta"] = {}
    return d


def _bump_access(conn: sqlite3.Connection, ids: List[int]) -> None:
    if not ids:
        return
    conn.executemany(
        "UPDATE memories SET access_count = access_count + 1 WHERE id = ?",
        [(i,) for i in ids],
    )


# The tool


@tool
def rover_memory(
    action: str = "list",
    title: str = "",
    content: str = "",
    category: Optional[str] = None,
    tags: str = "",
    importance: Optional[int] = None,
    meta: Optional[Dict[str, Any]] = None,
    memory_id: Optional[int] = None,
    query: str = "",
    limit: int = 10,
) -> Dict[str, Any]:
    """Long-term SQLite memory for scout — remember, recall, update, forget.

    Use this to retain knowledge ACROSS sessions: spatial maps of places
    you've explored, operator preferences, hazards to avoid, persistent
    goals, observations about people/objects/pets.

    Actions:
        remember : Store a new memory.
            required: title (short label), content (the memory body)
            optional: category, tags, importance (1-5), meta (dict)
            categories: spatial, preference, hazard, fact, goal, person, object, other

        recall   : Pull memories most relevant to a free-text query.
            required: query
            optional: category (filter), limit (default 10)
            Uses FTS5 if available, falls back to LIKE.

        search   : Same as recall but doesn't bump access_count.

        list     : List memories, newest first.
            optional: category (filter), limit (default 10)

        recent   : Most recently created/updated memories.
            optional: limit (default 10)

        update   : Modify an existing memory.
            required: memory_id
            optional: any of title/content/category/tags/importance/meta

        forget   : Delete a memory by id.
            required: memory_id

        stats    : Return DB stats (count, by category, FTS status).

    Examples:
        rover_memory(action="remember",
                     category="spatial",
                     title="kitchen entrance",
                     content="Through the second door on the left from "
                             "the hallway. Has a step-down of ~5cm.",
                     tags="indoor,kitchen,landmark",
                     importance=4,
                     meta={"approx_lat": 37.78, "approx_lng": -122.41})

        rover_memory(action="recall", query="where is the kitchen?")

        rover_memory(action="forget", memory_id=42)

    Returns:
        {"status": "success", "content": [{"text": "..."}]} on success;
        memory rows are inlined as JSON in the text for the model to read.
    """
    action = (action or "list").strip().lower()

    try:
        with _lock:
            conn = _connect()
            try:
                _ensure_schema(conn)

                if action == "remember":
                    return _do_remember(
                        conn, title, content, category, tags, importance, meta
                    )
                if action == "recall":
                    return _do_recall(conn, query, category, limit, bump=True)
                if action == "search":
                    return _do_recall(conn, query, category, limit, bump=False)
                if action == "list":
                    return _do_list(conn, category, limit)
                if action == "recent":
                    return _do_recent(conn, limit)
                if action == "update":
                    return _do_update(
                        conn, memory_id, title, content, category, tags,
                        importance, meta,
                    )
                if action == "forget":
                    return _do_forget(conn, memory_id)
                if action == "stats":
                    return _do_stats(conn)

                return error_result(
                    f"Unknown action '{action}'. Valid: remember, recall, "
                    f"search, list, recent, update, forget, stats."
                )
            finally:
                conn.close()
    except Exception as e:
        return error_result(f"rover_memory failed: {e}")


# Action handlers


def _do_remember(
    conn: sqlite3.Connection,
    title: str, content: str, category: str,
    tags: str, importance: int, meta: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    title = (title or "").strip()
    content = (content or "").strip()
    if not title:
        return error_result("remember: 'title' is required.")
    if not content:
        return error_result("remember: 'content' is required.")
    cat = (category or "other").strip().lower()
    if cat not in VALID_CATEGORIES:
        return error_result(
            f"Invalid category '{cat}'. Use one of: {sorted(VALID_CATEGORIES)}"
        )
    importance = max(1, min(5, int(importance if importance is not None else 3)))
    tags_str = ",".join(t.strip() for t in (tags or "").split(",") if t.strip())
    meta_json = json.dumps(meta or {}, ensure_ascii=False)
    now = datetime.now().isoformat(timespec="seconds")

    cur = conn.execute(
        """INSERT INTO memories
           (category, title, content, tags, importance, meta, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (cat, title, content, tags_str, importance, meta_json, now, now),
    )
    conn.commit()
    new_id = cur.lastrowid
    return ok_result(
        f"🧠 Remembered #{new_id} [{cat}] {title!r} (importance={importance})."
    )


def _do_recall(
    conn: sqlite3.Connection,
    query: str, category: str, limit: int, bump: bool,
) -> Dict[str, Any]:
    q = (query or "").strip()
    if not q:
        return error_result("recall/search: 'query' is required.")
    cat = (category or "").strip().lower() or None
    limit = max(1, min(50, int(limit or 10)))
    rows: List[sqlite3.Row] = []

    if _fts_available:
        # FTS5 with bm25() ordering
        try:
            sql = (
                "SELECT m.* FROM memories m "
                "JOIN memories_fts f ON f.rowid = m.id "
                "WHERE memories_fts MATCH ?"
            )
            params: List[Any] = [_fts_query(q)]
            if cat:
                sql += " AND m.category = ?"
                params.append(cat)
            sql += " ORDER BY bm25(memories_fts), m.importance DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            rows = []

    if not rows:
        # Fallback: LIKE across title/content/tags
        like = f"%{q}%"
        sql = (
            "SELECT * FROM memories "
            "WHERE (title LIKE ? OR content LIKE ? OR tags LIKE ?)"
        )
        params = [like, like, like]
        if cat:
            sql += " AND category = ?"
            params.append(cat)
        sql += " ORDER BY importance DESC, updated_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()

    if not rows:
        return ok_result(f"🧠 No memories matched query={q!r}"
                          + (f" in category={cat}." if cat else "."))

    if bump:
        _bump_access(conn, [r["id"] for r in rows])
        conn.commit()

    return _format_rows(rows, header=f"🧠 {len(rows)} memory match(es) for {q!r}:")


def _do_list(conn: sqlite3.Connection, category: str, limit: int) -> Dict[str, Any]:
    cat = (category or "").strip().lower() or None
    limit = max(1, min(100, int(limit or 10)))
    if cat:
        rows = conn.execute(
            "SELECT * FROM memories WHERE category = ? "
            "ORDER BY importance DESC, updated_at DESC LIMIT ?",
            (cat, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM memories "
            "ORDER BY importance DESC, updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    if not rows:
        return ok_result("🧠 No memories stored yet.")
    return _format_rows(rows, header=f"🧠 Top {len(rows)} memories"
                                       + (f" in [{cat}]:" if cat else ":"))


def _do_recent(conn: sqlite3.Connection, limit: int) -> Dict[str, Any]:
    limit = max(1, min(100, int(limit or 10)))
    rows = conn.execute(
        "SELECT * FROM memories ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    if not rows:
        return ok_result("🧠 No memories stored yet.")
    return _format_rows(rows, header=f"🧠 {len(rows)} most-recent memories:")


def _do_update(
    conn: sqlite3.Connection, memory_id: Optional[int],
    title: str, content: str, category: str,
    tags: str, importance: int, meta: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    if not memory_id:
        return error_result("update: 'memory_id' is required.")
    row = conn.execute(
        "SELECT * FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()
    if not row:
        return error_result(f"update: no memory with id={memory_id}")

    fields: Dict[str, Any] = {}
    if title:
        fields["title"] = title.strip()
    if content:
        fields["content"] = content.strip()
    if category:
        cat = category.strip().lower()
        if cat not in VALID_CATEGORIES:
            return error_result(
                f"Invalid category '{cat}'. Use: {sorted(VALID_CATEGORIES)}"
            )
        fields["category"] = cat
    if tags:
        fields["tags"] = ",".join(
            t.strip() for t in tags.split(",") if t.strip()
        )
    if importance is not None and 1 <= int(importance) <= 5:
        fields["importance"] = int(importance)
    if meta is not None:
        fields["meta"] = json.dumps(meta, ensure_ascii=False)

    if not fields:
        return error_result("update: nothing to change.")

    fields["updated_at"] = datetime.now().isoformat(timespec="seconds")
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    params = [*fields.values(), memory_id]
    conn.execute(f"UPDATE memories SET {set_clause} WHERE id = ?", params)
    conn.commit()
    return ok_result(f"🧠 Updated memory #{memory_id}: {list(fields)}.")


def _do_forget(conn: sqlite3.Connection, memory_id: Optional[int]) -> Dict[str, Any]:
    if not memory_id:
        return error_result("forget: 'memory_id' is required.")
    cur = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    conn.commit()
    if cur.rowcount == 0:
        return error_result(f"forget: no memory with id={memory_id}")
    return ok_result(f"🧠 Forgot memory #{memory_id}.")


def _do_stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    by_cat = conn.execute(
        "SELECT category, COUNT(*) AS n FROM memories "
        "GROUP BY category ORDER BY n DESC"
    ).fetchall()
    parts = [f"🧠 Memory DB: {DB_FILE}",
             f"   total: {total}",
             f"   FTS5: {'on' if _fts_available else 'off (LIKE fallback)'}"]
    if by_cat:
        parts.append("   by category:")
        for r in by_cat:
            parts.append(f"     - {r['category']}: {r['n']}")
    return ok_result("\n".join(parts))


# Helpers


def _fts_query(q: str) -> str:
    """Sanitize free-text query for FTS5 MATCH.

    Strategy: escape internal double-quotes, wrap each token in quotes,
    join with OR. This avoids FTS syntax errors on user/model input
    containing special characters.
    """
    tokens = [t.strip().replace('"', '""') for t in q.split() if t.strip()]
    if not tokens:
        return q
    return " OR ".join(f'"{t}"' for t in tokens)


def _format_rows(rows: List[sqlite3.Row], header: str) -> Dict[str, Any]:
    lines = [header]
    for r in rows:
        d = _row_to_dict(r)
        meta_str = ""
        if d.get("meta"):
            meta_str = f" meta={json.dumps(d['meta'], ensure_ascii=False)}"
        tags_str = f" tags=[{d['tags']}]" if d.get("tags") else ""
        lines.append(
            f"\n#{d['id']} [{d['category']}] (imp={d['importance']}, "
            f"hits={d['access_count']}, {d['updated_at']}){tags_str}\n"
            f"  {d['title']}\n  {d['content']}{meta_str}"
        )
    return ok_result("\n".join(lines))
