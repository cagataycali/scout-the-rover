#!/usr/bin/env python3
"""
🗺️ room_map — turn an Apple RoomPlan .usdz scan into a Scout-ready spatial
prompt block. The scan's meshes are SEMANTICALLY NAMED (toilet0, sofa_rect0,
refrigerator0, wall_*, door_*, floor_<Room>_*), so we don't need photos — we
read object labels + bounding boxes straight out of the USDA geometry and emit
a compact, model-friendly map.

Usage (CLI):   python -m tools.room_map [path/to/scan.usdz]
As a tool:     from tools.room_map import build_map_block, room_map
"""
from __future__ import annotations
import os, re, sys, zipfile, tempfile, math
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict

try:
    from strands import tool
except Exception:                      # allow standalone use w/o strands
    def tool(f=None, **k):
        return f if f else (lambda g: g)

DEFAULT_SCAN = os.getenv("SCOUT_ROOM_SCAN",
                         str(Path(__file__).resolve().parent.parent / "cagatay_lab.usdz"))

_OBJ_LABELS = {
    "storage_cabinet": "cabinet",
    "chair_swivel_star_back_arms": "office chair",
    "chair_other_four_back_no": "chair",
    "sofa_rect": "sofa", "refrigerator": "fridge", "oven": "oven",
    "sink": "sink", "toilet": "toilet", "bed": "bed", "table": "table",
    "stove": "stove", "dishwasher": "dishwasher", "washer": "washer",
    "television": "TV", "tv": "TV", "fireplace": "fireplace",
    "bathtub": "bathtub", "stairs": "stairs",
}
_FURN_PREFIX = tuple(_OBJ_LABELS) + ("bed", "chair", "sofa")


def _nice(name: str) -> str:
    base = re.sub(r"\d+$", "", name)
    for pref, lbl in _OBJ_LABELS.items():
        if base.startswith(pref):
            return lbl
    return base.replace("_", " ").strip()


def _read_usda(usdz_or_usda: str) -> str:
    p = Path(usdz_or_usda)
    if p.suffix == ".usda":
        return p.read_text(errors="ignore")
    with zipfile.ZipFile(p) as z:
        name = next(n for n in z.namelist() if n.endswith(".usda"))
        return z.read(name).decode("utf-8", "ignore")


def parse_scan(path: str) -> Dict[str, Any]:
    """Return {objs, floors, plane, up, footprint, label_rooms}."""
    txt = _read_usda(path)
    mesh_re = re.compile(r'def Mesh "([^"]+)"\s*\{(.*?)\n        \}', re.DOTALL)
    pt_re = re.compile(r"point3f\[\] points = \[(.*?)\]", re.DOTALL)
    tup_re = re.compile(r"\(([^)]+)\)")

    objs: Dict[str, dict] = {}
    for m in mesh_re.finditer(txt):
        name, body = m.group(1), m.group(2)
        pm = pt_re.search(body)
        if not pm:
            continue
        xs = ys = zs = None
        X = []; Y = []; Z = []
        for t in tup_re.finditer(pm.group(1)):
            try:
                a, b, c = (float(v) for v in t.group(1).split(",")[:3])
            except Exception:
                continue
            X.append(a); Y.append(b); Z.append(c)
        if not X:
            continue
        objs[name] = dict(
            cx=(min(X)+max(X))/2, cy=(min(Y)+max(Y))/2, cz=(min(Z)+max(Z))/2,
            dx=max(X)-min(X), dy=max(Y)-min(Y), dz=max(Z)-min(Z))

    floors = {k: v for k, v in objs.items() if k.startswith("floor")}
    if floors:
        f = next(iter(floors.values()))
        up = min("xyz", key=lambda a: f["d" + a])   # flattest axis = vertical
    else:
        up = "y"
    plane = [a for a in "xyz" if a != up]

    label_rooms = defaultdict(list)
    for fk in floors:
        parts = fk.split("_")
        label_rooms[parts[1] if len(parts) > 1 else fk].append(fk)

    return dict(objs=objs, floors=floors, plane=plane, up=up,
                label_rooms=label_rooms)


def _planar(o, plane, up):
    return (o["c"+plane[0]], o["c"+plane[1]],
            o["d"+plane[0]], o["d"+plane[1]], o["d"+up])


def build_map_block(path: str = DEFAULT_SCAN) -> str:
    """Build the markdown spatial-map block for the system prompt."""
    if not Path(path).exists():
        return ""
    S = parse_scan(path)
    objs, floors, plane, up, label_rooms = (
        S["objs"], S["floors"], S["plane"], S["up"], S["label_rooms"])

    # overall footprint
    ax = []; az = []
    for k, v in objs.items():
        if k.startswith(("floor", "wall")):
            ax += [v["c"+plane[0]]-v["d"+plane[0]]/2, v["c"+plane[0]]+v["d"+plane[0]]/2]
            az += [v["c"+plane[1]]-v["d"+plane[1]]/2, v["c"+plane[1]]+v["d"+plane[1]]/2]
    fpw = (max(ax)-min(ax)) if ax else 0
    fpd = (max(az)-min(az)) if az else 0

    # assign furniture to nearest floor-room
    def room_of(v):
        best = None; bd = 1e9
        u, w, *_ = _planar(v, plane, up)
        for fk, fv in floors.items():
            fu, fw = fv["c"+plane[0]], fv["c"+plane[1]]
            hu, hw = fv["d"+plane[0]]/2, fv["d"+plane[1]]/2
            if fu-hu <= u <= fu+hu and fw-hw <= w <= fw+hw:
                return fk
            d = math.hypot(u-fu, w-fw)
            if d < bd:
                bd, best = d, fk
        return best

    room_items = defaultdict(list)
    for k, v in objs.items():
        if k.startswith(_FURN_PREFIX):
            room_items[room_of(v)].append((k, v))

    L = []
    L.append("## 🗺️ ROOM MAP (from RoomPlan scan — Scout's prior world model)")
    L.append(f"Frame: floor plane = ({plane[0].upper()},{plane[1].upper()}), "
             f"up = {up.upper()}, units = meters. Origin is the scan origin.")
    L.append(f"Interior footprint ≈ {fpw:.1f} m × {fpd:.1f} m. "
             f"Rooms: {', '.join(sorted(label_rooms))}.")
    n_wall = sum(k.startswith('wall') for k in objs)
    L.append(f"Structure: {n_wall} wall segments, "
             f"{sum(k.startswith('door') for k in objs)//2} doors, "
             f"{sum(k.startswith('window') for k in objs)//2} windows.")
    L.append("")

    for label in sorted(label_rooms):
        fks = label_rooms[label]
        rx = []; rz = []
        for fk in fks:
            fv = floors[fk]
            rx += [fv["c"+plane[0]]-fv["d"+plane[0]]/2, fv["c"+plane[0]]+fv["d"+plane[0]]/2]
            rz += [fv["c"+plane[1]]-fv["d"+plane[1]]/2, fv["c"+plane[1]]+fv["d"+plane[1]]/2]
        cu = (min(rx)+max(rx))/2; cw = (min(rz)+max(rz))/2
        L.append(f"**{label}** (~{max(rx)-min(rx):.1f}×{max(rz)-min(rz):.1f} m, "
                 f"center ({cu:+.1f},{cw:+.1f})):")
        items = []
        for fk in fks:
            items += room_items.get(fk, [])
        if not items:
            L.append("  · (open / no large objects detected)")
        # collapse duplicate labels, keep position list short
        for k, v in sorted(items, key=lambda kv: kv[0]):
            u, w, du, dv, h = _planar(v, plane, up)
            L.append(f"  · {_nice(k):12s} at ({u:+.1f},{w:+.1f}) "
                     f"[{du:.1f}×{dv:.1f}×{h:.1f} m]")
        L.append("")

    # openings (dedupe by rounded position)
    seen = set(); ops = []
    for pref in ("door", "window"):
        for k, v in sorted(objs.items()):
            if k.startswith(pref):
                u, w, du, dv, h = _planar(v, plane, up)
                key = (pref, round(u, 1), round(w, 1))
                if key in seen:
                    continue
                seen.add(key)
                ops.append(f"  · {pref:6s} at ({u:+.1f},{w:+.1f}) span {max(du,dv):.1f} m")
    if ops:
        L.append("**Openings** (use for navigation between rooms):")
        L.extend(ops)
        L.append("")

    L.append("Use this map to localize ('I'm near the X'), plan routes between "
             "rooms via doors, and reason about what should be where. It is a "
             "PRIOR — always confirm against live camera frames before moving.")

    # Append human/visual notes companion file if present (next to the scan
    # or at repo root): .room_notes.md
    for cand in (Path(path).with_suffix("").parent / ".room_notes.md",
                 Path(__file__).resolve().parent.parent / ".room_notes.md"):
        try:
            if cand.exists():
                L.append("")
                L.append(cand.read_text(errors="ignore").strip())
                break
        except Exception:
            pass
    return "\n".join(L)


@tool
def room_map(action: str = "show", scan_path: str = "") -> Dict[str, Any]:
    """🗺️ Scout's spatial prior from a RoomPlan .usdz scan.

    Args:
        action: "show" (return the map block) | "objects" (flat object list) |
                "rooms" (room summary)
        scan_path: optional override path to a .usdz/.usda scan.
    """
    path = scan_path or DEFAULT_SCAN
    if not Path(path).exists():
        return {"status": "error",
                "content": [{"text": f"Scan not found: {path}"}]}
    try:
        if action == "show":
            return {"status": "success",
                    "content": [{"text": build_map_block(path)}]}
        S = parse_scan(path)
        if action == "rooms":
            rooms = {lbl: len(fks) for lbl, fks in S["label_rooms"].items()}
            return {"status": "success", "content": [{"json": rooms}]}
        if action == "objects":
            out = {k: dict(center=[round(v["c"+a], 3) for a in "xyz"],
                           size=[round(v["d"+a], 3) for a in "xyz"])
                   for k, v in S["objs"].items()
                   if not k.startswith(("wall", "joint"))}
            return {"status": "success", "content": [{"json": out}]}
        return {"status": "error", "content": [{"text": f"unknown action {action}"}]}
    except Exception as e:
        return {"status": "error", "content": [{"text": f"room_map error: {e}"}]}


if __name__ == "__main__":
    print(build_map_block(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SCAN))

