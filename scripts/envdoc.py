#!/usr/bin/env python3
"""envdoc — every environment variable the code reads, as a Markdown table.

Walks every *.py in the repo (not venvs/site/node_modules) with `ast`, finds
`os.getenv("NAME", default)`, `os.environ.get("NAME", default)` and
`os.environ["NAME"]`, and emits one row per variable: name · default (from the
call, or the first non-empty default seen) · files that read it · purpose.

Purposes come from `scripts/envdoc_notes.py` (a plain dict) — an unknown
variable still gets a row (purpose "—") so the table can never silently miss
one; tests/test_docs.py asserts website/reference/env.md contains every name.

Usage:  python scripts/envdoc.py            # Markdown to stdout
        python scripts/envdoc.py --json     # machine-readable
Never imports the rover code, so it runs anywhere (mkdocs hook, CI).
"""
from __future__ import annotations

import ast
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKIP_DIRS = {".venv", "venv", ".venv-locate", "site", "node_modules", "__pycache__", ".git", "dist", "earth-rovers-sdk", "g1-work", "neon-the-g1", "navigation-scaling", "tests"}
SKIP_NAMES = {"PATH", "HOME", "USER", "PWD", "SHELL", "TERM"}

sys.path.insert(0, str(ROOT / "scripts"))
try:
    from envdoc_notes import NOTES, GROUPS  # type: ignore
except Exception:  # pragma: no cover
    NOTES, GROUPS = {}, {}


def _lit(node: ast.AST | None) -> str | None:
    """A printable default: literals, simple names, f-strings → source text."""
    if node is None:
        return None
    try:
        v = ast.literal_eval(node)
        return repr(v) if not isinstance(v, str) else (v if v else '""')
    except Exception:
        return ast.unparse(node)


def _scan(path: pathlib.Path) -> list[tuple[str, str | None]]:
    out: list[tuple[str, str | None]] = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return out
    for node in ast.walk(tree):
        name = default = None
        if isinstance(node, ast.Call):
            f = node.func
            attr = getattr(f, "attr", None)
            base = getattr(getattr(f, "value", None), "id", None) or getattr(getattr(getattr(f, "value", None), "value", None), "id", None)
            if attr == "getenv" and base == "os" and node.args:
                name = _str(node.args[0]); default = node.args[1] if len(node.args) > 1 else None
            elif attr == "get" and isinstance(f, ast.Attribute) and isinstance(f.value, ast.Attribute) and f.value.attr == "environ" and node.args:
                name = _str(node.args[0]); default = node.args[1] if len(node.args) > 1 else None
        elif isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr == "environ":
            name = _str(node.slice)
        if name and name not in SKIP_NAMES:
            out.append((name, _lit(default)))
    return out


def _str(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def collect() -> dict[str, dict]:
    vars_: dict[str, dict] = {}
    for p in sorted(ROOT.rglob("*.py")):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        rel = p.relative_to(ROOT).as_posix()
        for name, default in _scan(p):
            row = vars_.setdefault(name, {"default": None, "files": []})
            if rel not in row["files"]:
                row["files"].append(rel)
            if default not in (None, '""', "None") and row["default"] is None:
                row["default"] = default
    for name, row in vars_.items():
        row["purpose"] = NOTES.get(name, "—")
        row["group"] = GROUPS.get(name) or _guess_group(name)
    return dict(sorted(vars_.items()))


def _guess_group(name: str) -> str:
    for prefix, g in (("DASH_", "Dashboard"), ("SCOUT_AUTH", "Auth"), ("SCOUT_PUBLIC_URL", "Dashboard"), ("SCOUT_MDNS", "Dashboard"),
                      ("SCOUT_SUPERVISOR", "Personas"), ("SCOUT_PERSONA", "Personas"), ("SCOUT_THINKER", "Personas"), ("SCOUT_AUTO_RECORD", "Datasets"),
                      ("SCOUT_SEAL", "Datasets"), ("SCOUT_VCODEC", "Datasets"), ("SCOUT_DATASET", "Datasets"), ("SCOUT_ENABLE_COSMOS", "Perception"),
                      ("COSMOS", "Perception"), ("YOLO_", "Perception"), ("DET_", "Perception"), ("MEDIA_HUB", "Perception"), ("LOCATE", "Perception"),
                      ("SDK_", "Rover"), ("ROVER_", "Rover"), ("FRODOBOTS", "Rover"), ("TINY_MCP", "Fleet"), ("TINY_SELF", "Fleet"), ("TINY_TOKEN", "Fleet"),
                      ("TINY_", "Fleet"), ("VOICE_", "Voice"), ("TELEGRAM_", "Telegram"), ("THINKER_", "Thinker"), ("SCOUT_BRIEFING", "Voice"),
                      ("OPENAI", "Models"), ("GEMINI", "Models"), ("GOOGLE", "Models"), ("AWS", "Models"), ("BEDROCK", "Models"), ("MODEL", "Models"), ("SCOUT_MODEL", "Models"),
                      ("SCOUT_", "Scout")):
        if name.startswith(prefix):
            return g
    return "Other"


def markdown(vars_: dict[str, dict]) -> str:
    lines = ["<!-- generated by scripts/envdoc.py at mkdocs build — edit the code or scripts/envdoc_notes.py, not this file -->", ""]
    order = ["Rover", "Models", "Voice", "Telegram", "Thinker", "Personas", "Perception", "Datasets", "Dashboard", "Auth", "Fleet", "Scout", "Other"]
    by_group: dict[str, list[str]] = {}
    for n, r in vars_.items():
        by_group.setdefault(r["group"], []).append(n)
    for g in order + sorted(set(by_group) - set(order)):
        names = by_group.get(g)
        if not names:
            continue
        lines += [f"### {g}", "", "| variable | default | read by | purpose |", "|---|---|---|---|"]
        for n in names:
            r = vars_[n]
            d = r["default"] if r["default"] is not None else "*(unset)*"
            files = ", ".join(f"`{f}`" for f in r["files"])
            lines.append(f"| `{n}` | `{d}` | {files} | {r['purpose']} |".replace("`*(unset)*`", "*(unset)*"))
        lines.append("")
    lines.append(f"_{len(vars_)} variables, scanned from every `os.getenv` / `os.environ` in the repo._")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    data = collect()
    if "--json" in sys.argv:
        print(json.dumps(data, indent=2))
    else:
        sys.stdout.write(markdown(data))
