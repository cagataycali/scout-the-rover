#!/usr/bin/env python3
"""routedoc — the dashboard HTTP/WS API table, generated from the FastAPI sources.

Finds every `@app.get/post/put/delete/websocket("...")` in `dashboard_server.py`,
`dashboard_replay.py` and `personas.py` (all three attach to the same `app`),
the handler's name, its parameters (from the signature + the JSON body keys it
reads), its docstring or the comment above it, and whether the global auth
middleware lets anonymous callers in (`_PUBLIC_EXACT` / `_PUBLIC_PREFIXES` in
`dashboard_server.py`; `/ws/*` sockets check the session themselves and close
with 4401). Emits a Markdown table grouped by area — nothing typed by hand.

Usage:  python scripts/routedoc.py            # Markdown to stdout
        python scripts/routedoc.py --json
Stdlib only; never imports the dashboard.
"""
from __future__ import annotations

import ast
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCES = ["dashboard_server.py", "dashboard_replay.py", "personas.py"]
REPO = "https://github.com/cagataycali/scout-the-rover/blob/main"
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from routedoc_notes import NOTES  # type: ignore
except Exception:  # pragma: no cover
    NOTES = {}

GROUPS = [
    ("Health & telemetry", r"^/api/(health|telemetry|config)$"),
    ("Camera", r"^/api/(frame/.*|screenshot)$"),
    ("Control", r"^/api/(control|lamp|speak)$"),
    ("Agent (Ask / chat)", r"^/api/chat$|^/ws/(chat|voice)$"),
    ("Personas", r"^/api/personas.*"),
    ("Replay (datasets)", r"^/api/replay.*"),
    ("Auth (passkeys)", r"^/auth/.*"),
    ("Shell & trust", r"^/($|replay$|field-card$|trust$|ca$)"),
]


def _public_rules(src: str) -> tuple[set[str], tuple[str, ...]]:
    ex = re.search(r"_PUBLIC_EXACT\s*=\s*(\{[^}]*\})", src)
    pre = re.search(r"_PUBLIC_PREFIXES\s*=\s*(\([^)]*\))", src)
    return (set(ast.literal_eval(ex.group(1))) if ex else set(), tuple(ast.literal_eval(pre.group(1))) if pre else ())


def _body_keys(fn: ast.AST) -> list[str]:
    """Keys the handler reads from the JSON body: body.get("k") / body["k"] / payload.get("k")."""
    keys: list[str] = []
    names = {"body", "payload", "data", "js"}
    for n in ast.walk(fn):
        k = None
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute) and f.attr == "get" and isinstance(f.value, ast.Name) and f.value.id in names and n.args:
                k = n.args[0]
        elif isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) and n.value.id in names:
            k = n.slice
        if isinstance(k, ast.Constant) and isinstance(k.value, str) and k.value not in keys:
            keys.append(k.value)
    return keys


def _params(fn: ast.AST, path: str) -> str:
    out = []
    path_params = set(re.findall(r"\{(\w+)", path))
    for a in fn.args.args + fn.args.kwonlyargs:
        if a.arg in ("req", "request", "ws", "websocket", "self"):
            continue
        ann = ast.unparse(a.annotation) if a.annotation else ""
        if a.arg in path_params:
            continue  # visible in the path already
        out.append(f"`{a.arg}`" + (f": {ann}" if ann else ""))
    keys = _body_keys(fn)
    if keys:
        out.append("JSON {" + ", ".join(keys) + "}")
    return ", ".join(out) or "—"


def _comment_above(lines: list[str], lineno: int) -> str:
    i = lineno - 2
    while i >= 0 and lines[i].strip().startswith("#"):
        i -= 1
    if i + 1 <= lineno - 2:
        txt = " ".join(l.strip().lstrip("#").strip(" ─-") for l in lines[i + 1:lineno - 1])
        return txt.strip()
    return ""


def routes() -> list[dict]:
    server_src = (ROOT / SOURCES[0]).read_text(encoding="utf-8")
    exact, prefixes = _public_rules(server_src)
    found: list[dict] = []
    for fname in SOURCES:
        p = ROOT / fname
        if not p.exists():
            continue
        src = p.read_text(encoding="utf-8")
        lines = src.splitlines()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr in ("get", "post", "put", "delete", "websocket", "api_route") and dec.args:
                    try:
                        path = ast.literal_eval(dec.args[0])
                    except Exception:
                        continue
                    if not isinstance(path, str):
                        continue
                    method = dec.func.attr.upper() if dec.func.attr != "websocket" else "WS"
                    doc = (ast.get_docstring(node) or "").strip().split("\n")[0]
                    if not doc:
                        doc = _comment_above(lines, dec.lineno)
                    if method == "WS":
                        anon = False  # /ws/* check the session themselves → 4401
                    else:
                        anon = path in exact or any(path.startswith(pfx) for pfx in prefixes) or not path.startswith("/api/")
                    doc = NOTES.get(f"{method} {path}", doc)
                    found.append({"method": method, "path": path, "handler": node.name, "params": _params(node, path),
                                  "doc": doc, "public": anon, "line": dec.lineno, "file": fname})
    found.sort(key=lambda r: (r["path"], r["method"]))
    return found


def markdown(rs: list[dict]) -> str:
    lines = ["<!-- generated by scripts/routedoc.py from dashboard_server.py + dashboard_replay.py + personas.py — edit the code, not this table -->", ""]
    seen = set()
    for title, pat in GROUPS:
        rows = [r for r in rs if re.match(pat, r["path"]) and id(r) not in seen]
        if not rows:
            continue
        lines += [f"### {title}", "", "| method | path | params | anonymous? | what |", "|---|---|---|---|---|"]
        for r in rows:
            seen.add(id(r))
            src = f'<a href="{REPO}/{r["file"]}#L{r["line"]}">src</a>'
            lines.append(f"| `{r['method']}` | `{r['path']}` | {r['params']} | {'✅ public' if r['public'] else '🔒 session'} | {r['doc'].replace('|', '\\|') or '—'} · {src} |")
        lines.append("")
    rest = [r for r in rs if id(r) not in seen]
    if rest:
        lines += ["### Other", "", "| method | path | params | anonymous? | what |", "|---|---|---|---|---|"]
        lines += [f"| `{r['method']}` | `{r['path']}` | {r['params']} | {'✅' if r['public'] else '🔒'} | {r['doc'] or '—'} |" for r in rest]
        lines.append("")
    n_pub = sum(1 for r in rs if r["public"])
    lines.append(f"_{len(rs)} routes; {n_pub} answer without a session (health, the passkey ceremony, the page shells and the trust/CA pages), "
                 "every other `/api/*` route returns 401 to anonymous callers and the WebSockets close with 4401._")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    rs = routes()
    if "--json" in sys.argv:
        print(json.dumps(rs, indent=2))
    else:
        sys.stdout.write(markdown(rs))
