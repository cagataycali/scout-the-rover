"""persona_flags — owner-toggled runtime switches shared by every Scout process.

The dashboard writes `.memory/personas.json` (the `.memory` dir is a bind mount
in every compose service); the thinker and the recorder read it on every
cycle / turn. No restarts, no docker socket, no IPC — a file the processes
already have.

    flag("thinker_drive", default=True)   -> bool
    flag("recording", default=True)       -> bool
    set_flag("recording", False)          -> {"recording": False, ...}
    all_flags()                           -> dict

Defaults fall back to the legacy env vars so an untouched deployment behaves
exactly as before:  SCOUT_THINKER_DRIVE (thinker_drive), SCOUT_AUTO_RECORD
(recording).
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

FLAGS_FILE = Path(os.getenv("SCOUT_PERSONA_FLAGS", ".memory/personas.json"))

# flag -> (env fallback, env default)
KNOWN = {
    "thinker_drive": ("SCOUT_THINKER_DRIVE", "1"),
    "recording": ("SCOUT_AUTO_RECORD", "1"),
}
_FALSE = ("0", "false", "no", "off", "")

_lock = threading.Lock()
_cache: Dict[str, Any] = {"mtime": None, "data": {}, "checked": 0.0}
_MIN_INTERVAL = 0.5  # seconds between stat() calls


def _env_default(name: str) -> bool:
    env, dflt = KNOWN.get(name, ("", "1"))
    val = os.getenv(env, dflt) if env else dflt
    return str(val).strip().lower() not in _FALSE


def _read() -> Dict[str, Any]:
    now = time.monotonic()
    with _lock:
        if now - _cache["checked"] < _MIN_INTERVAL:
            return dict(_cache["data"])
        _cache["checked"] = now
        try:
            st = FLAGS_FILE.stat()
        except FileNotFoundError:
            _cache.update(mtime=None, data={})
            return {}
        if st.st_mtime != _cache["mtime"]:
            try:
                _cache["data"] = json.loads(FLAGS_FILE.read_text() or "{}")
            except Exception:
                _cache["data"] = {}
            _cache["mtime"] = st.st_mtime
        return dict(_cache["data"])


def flag(name: str, default: Optional[bool] = None) -> bool:
    """Current value of a flag: file wins, else env fallback, else default/True."""
    data = _read()
    if name in data:
        return bool(data[name])
    if default is not None:
        return default
    return _env_default(name)


def all_flags() -> Dict[str, Any]:
    data = _read()
    out = {k: flag(k) for k in KNOWN}
    for k, v in data.items():
        if k not in out and not k.startswith("_"):
            out[k] = v
    out["_source"] = str(FLAGS_FILE) if FLAGS_FILE.exists() else "env-defaults"
    return out


def set_flag(name: str, value: bool, actor: str = "dashboard") -> Dict[str, Any]:
    """Atomically write one flag (tmp + rename). Returns the full flag dict."""
    FLAGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        try:
            data = json.loads(FLAGS_FILE.read_text() or "{}") if FLAGS_FILE.exists() else {}
        except Exception:
            data = {}
        data[name] = bool(value)
        data["_updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        data["_actor"] = actor[:64]
        tmp = FLAGS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=1))
        os.replace(tmp, FLAGS_FILE)
        _cache.update(mtime=None, checked=0.0)
    return all_flags()
