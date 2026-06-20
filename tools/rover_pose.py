"""🧭 rover_pose — dead-reckoning localization in RoomPlan coordinates.

Scout has GPS (useless indoors), an IMU orientation (absolute-ish heading),
and commanded velocities. This module fuses them into an estimated pose
{x, y, yaw} in the SAME coordinate frame as the RoomPlan scan (room_map),
so Scout finally knows *where it is* and can say "I'm near the sofa".

Model (v1 — dead reckoning):
    * You SEED the pose once ("I'm placed at the door, facing +X").
    * Every motion segment integrates commanded linear/angular over its
      duration into a (dx, dy, dyaw) delta in room coords.
    * Heading is corrected toward the IMU's absolute `orientation` when a
      fresh telemetry read is available (complementary filter) — this curbs
      yaw drift, the worst enemy of dead reckoning.
    * Upgradeable: swap commanded linear for measured wheel `rpm[4]` odometry
      (see gap-analysis.md) by setting the calibration below — same math.

Frame convention (matches room_map): floor plane = (X, Z), but we expose the
planar axes as (x, y) for sanity. yaw=0 points along +x, increasing yaw turns
LEFT (CCW), matching the agent's +angular=left convention.

This is a PRIOR estimate — it drifts. Always confirm against camera frames
and the room map before committing to a move. Re-seed whenever you get a
confident visual fix ("I can see the fridge dead-ahead → snap pose").
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from strands import tool

# ── Calibration ───────────────────────────────────────────────────────────
# Map the rover's unitless command magnitudes to physical rates. These are
# rough field estimates — tune against a tape-measure drive. Override via env.
#   linear=1.0  → LIN_SPEED_MAX m/s
#   angular=1.0 → ANG_SPEED_MAX rad/s
LIN_SPEED_MAX = float(os.getenv("SCOUT_LIN_SPEED_MAX", "0.35"))   # m/s at full
ANG_SPEED_MAX = float(os.getenv("SCOUT_ANG_SPEED_MAX", "1.20"))   # rad/s at full

# Complementary-filter weight: how strongly the IMU absolute heading pulls the
# integrated yaw back to truth each correction. 0 = ignore IMU, 1 = trust IMU.
IMU_YAW_TRUST = float(os.getenv("SCOUT_IMU_YAW_TRUST", "0.15"))

_POSE_FILE = Path(os.getenv("SCOUT_POSE_FILE",
                            str(Path(__file__).resolve().parent.parent / ".scout_pose.json")))


class _Pose:
    """Thread-safe estimated pose in room coordinates."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.x = 0.0
        self.y = 0.0
        self.yaw = 0.0          # radians, 0 = +x, CCW positive
        self.seeded = False
        self.imu_yaw_at_seed: Optional[float] = None   # IMU orientation when seeded
        self.last_update = time.time()
        self.total_dist = 0.0   # odometer (m), for drift awareness
        self._load()

    # ── persistence ────────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            if _POSE_FILE.exists():
                d = json.loads(_POSE_FILE.read_text())
                self.x = float(d.get("x", 0.0))
                self.y = float(d.get("y", 0.0))
                self.yaw = float(d.get("yaw", 0.0))
                self.seeded = bool(d.get("seeded", False))
                self.imu_yaw_at_seed = d.get("imu_yaw_at_seed")
                self.total_dist = float(d.get("total_dist", 0.0))
        except Exception:
            pass

    def _save(self) -> None:
        try:
            _POSE_FILE.write_text(json.dumps(self.snapshot()))
        except Exception:
            pass

    def snapshot(self) -> Dict[str, Any]:
        return {
            "x": round(self.x, 3), "y": round(self.y, 3),
            "yaw": round(self.yaw, 4), "yaw_deg": round(math.degrees(self.yaw) % 360, 1),
            "seeded": self.seeded,
            "imu_yaw_at_seed": self.imu_yaw_at_seed,
            "total_dist": round(self.total_dist, 2),
            "age_s": round(time.time() - self.last_update, 1),
        }

    # ── seeding ────────────────────────────────────────────────────────
    def seed(self, x: float, y: float, yaw_deg: float,
             imu_orientation: Optional[float] = None) -> None:
        with self.lock:
            self.x, self.y = float(x), float(y)
            self.yaw = math.radians(float(yaw_deg))
            self.seeded = True
            self.imu_yaw_at_seed = imu_orientation
            self.last_update = time.time()
            self._save()

    # ── integration ────────────────────────────────────────────────────
    def integrate(self, linear: float, angular: float, duration: float,
                  imu_orientation: Optional[float] = None) -> None:
        """Advance the pose by one commanded motion segment (dead reckoning).

        Uses a midpoint heading (theta + 0.5*dtheta) so that combined
        translate+turn arcs are integrated more accurately than naive Euler.
        """
        with self.lock:
            v = float(linear) * LIN_SPEED_MAX           # m/s
            w = float(angular) * ANG_SPEED_MAX          # rad/s (CCW+)
            dt = max(0.0, float(duration))
            dtheta = w * dt
            theta_mid = self.yaw + 0.5 * dtheta
            dist = v * dt
            self.x += dist * math.cos(theta_mid)
            self.y += dist * math.sin(theta_mid)
            self.yaw = (self.yaw + dtheta) % (2 * math.pi)
            self.total_dist += abs(dist)

            # IMU heading correction (complementary filter). The IMU gives an
            # ABSOLUTE compass heading; we know the offset between IMU frame and
            # room frame from the seed. Pull integrated yaw toward that truth.
            if (imu_orientation is not None and self.imu_yaw_at_seed is not None):
                # room_yaw_from_imu = seed_yaw + (imu_now - imu_at_seed)
                # (both in their own frames; assume same rotation sense)
                imu_delta = math.radians(imu_orientation - self.imu_yaw_at_seed)
                # yaw at seed was whatever self.yaw was then; reconstruct target
                # as a *correction*, not absolute, to stay robust:
                target = self._yaw_seed + imu_delta if hasattr(self, "_yaw_seed") else None
                if target is not None:
                    err = math.atan2(math.sin(target - self.yaw),
                                     math.cos(target - self.yaw))
                    self.yaw = (self.yaw + IMU_YAW_TRUST * err) % (2 * math.pi)

            self.last_update = time.time()
            self._save()


POSE = _Pose()


# ── Calibration persistence ─────────────────────────────────────────────────
_CAL_FILE = Path(os.getenv("SCOUT_CAL_FILE",
                           str(Path(__file__).resolve().parent.parent / ".scout_calibration.json")))


def _load_calibration() -> None:
    """Load learned LIN/ANG speed constants from disk, if present."""
    global LIN_SPEED_MAX, ANG_SPEED_MAX
    try:
        if _CAL_FILE.exists():
            import json as _json
            d = _json.loads(_CAL_FILE.read_text())
            if "lin_speed_max" in d:
                LIN_SPEED_MAX = float(d["lin_speed_max"])
            if "ang_speed_max" in d:
                ANG_SPEED_MAX = float(d["ang_speed_max"])
    except Exception:
        pass


def _save_calibration() -> None:
    try:
        import json as _json
        _CAL_FILE.write_text(_json.dumps(
            {"lin_speed_max": round(LIN_SPEED_MAX, 4),
             "ang_speed_max": round(ANG_SPEED_MAX, 4)}))
    except Exception:
        pass


# load any previously-learned constants at import
_load_calibration()


def integrate_move(linear: float, angular: float, duration: float,
                   imu_orientation: Optional[float] = None) -> None:
    """Public hook for motion tools to feed executed segments into the pose."""
    if POSE.seeded:
        POSE.integrate(linear, angular, duration, imu_orientation)


def _imu_orientation() -> Optional[float]:
    """Best-effort current IMU heading from the SDK (degrees), or None."""
    try:
        from ._rover_common import sdk_get
        r = sdk_get("/data", timeout=3)
        if r.status_code == 200:
            d = r.json()
            o = d.get("orientation")
            return float(o) if o is not None else None
    except Exception:
        pass
    return None


def _nearest_objects(x: float, y: float, k: int = 3) -> list:
    """Find the k nearest mapped objects to (x,y) for 'I'm near the X'."""
    try:
        from .room_map import parse_scan, DEFAULT_SCAN, _nice, _FURN_PREFIX
        S = parse_scan(DEFAULT_SCAN)
        plane, up = S["plane"], S["up"]
        cands = []
        for name, v in S["objs"].items():
            if not name.startswith(_FURN_PREFIX):
                continue
            ox, oy = v["c" + plane[0]], v["c" + plane[1]]
            d = math.hypot(x - ox, y - oy)
            cands.append((d, _nice(name), ox, oy))
        cands.sort(key=lambda t: t[0])
        return cands[:k]
    except Exception:
        return []


def pose_block() -> str:
    """Markdown block for the system prompt — Scout's current best-guess pose."""
    s = POSE.snapshot()
    if not s["seeded"]:
        return ("## 🧭 ESTIMATED POSE: not seeded.\n"
                "Scout does NOT yet know its position in the room. Drop a seed "
                "with rover_pose(action='seed', x=.., y=.., yaw_deg=..) once you "
                "place it at a known spot on the map.\n")
    near = _nearest_objects(s["x"], s["y"])
    near_txt = ""
    if near:
        near_txt = " | nearest: " + ", ".join(
            f"{nm} ({d:.1f}m)" for d, nm, *_ in near)
    drift = ("⚠️ high-drift (re-confirm visually)" if s["total_dist"] > 5
             else "fresh")
    return (
        "## 🧭 ESTIMATED POSE (dead-reckoning, room coords — a PRIOR, verify visually):\n"
        f"- Position: ({s['x']:+.2f}, {s['y']:+.2f}) m  Heading: {s['yaw_deg']:.0f}°\n"
        f"- Odometer: {s['total_dist']:.1f} m driven since seed ({drift})\n"
        f"- Updated {s['age_s']:.0f}s ago{near_txt}\n"
    )


@tool
def rover_pose(action: str = "get",
               x: float = 0.0, y: float = 0.0, yaw_deg: float = 0.0,
               linear: float = 0.0, angular: float = 0.0,
               duration: float = 1.0, measured: float = None) -> Dict[str, Any]:
    """🧭 Scout's estimated position in the room (dead-reckoning localization).

    Scout maintains a best-guess {x, y, yaw} in the RoomPlan map's coordinate
    frame, updated automatically as it drives. GPS is useless indoors — THIS
    is how Scout knows where it is. It's a PRIOR estimate that drifts; re-seed
    whenever you get a confident visual fix.

    Actions:
        get   → current estimated pose + nearest mapped objects.
        seed  → set/reset pose to a KNOWN location (x, y in meters, yaw_deg
                heading where 0°=+x axis, 90°=+y, CCW positive). Do this when
                you physically place Scout at a recognizable spot, or when a
                camera view lets you confidently match the map.
        reset → forget the pose (mark un-seeded).
        calibrate → tune motion constants from a measured drive. Pass the
                linear/angular command + duration you used, and measured =
                actual meters driven (linear) or degrees turned (angular).
                Back-solves         reset → forget the pose (mark un-seeded). saves LIN/ANG_SPEED_MAX so dead-reckoning is accurate.

    Args:
        action: "get" | "seed" | "reset".
        x: room X coordinate in meters (for seed).
        y: room Y coordinate in meters (for seed).
        yaw_deg: heading in degrees, 0=+x, CCW positive (for seed).

    Returns:
        Dict with status, summary text, and pose JSON.
    """
    action = (action or "get").lower().strip()
    if action == "seed":
        POSE.seed(x, y, yaw_deg, imu_orientation=_imu_orientation())
        POSE._yaw_seed = POSE.yaw  # remember seed yaw for IMU correction baseline
        s = POSE.snapshot()
        return {"status": "success",
                "content": [{"text": f"🧭 Pose seeded at ({s['x']:+.2f},{s['y']:+.2f}) "
                                     f"facing {s['yaw_deg']:.0f}°."},
                            {"json": s}]}
    if action == "reset":
        POSE.seeded = False
        POSE._save()
        return {"status": "success", "content": [{"text": "🧭 Pose reset (un-seeded)."}]}
    if action == "calibrate":
        # Back-solve a speed constant from a known commanded move vs measured result.
        # Usage:
        #   linear calib: rover_pose(action='calibrate', linear=<cmd>, duration=<s>,
        #                            measured=<actual meters driven>)
        #   angular calib: rover_pose(action='calibrate', angular=<cmd>, duration=<s>,
        #                             measured=<actual degrees turned>)
        global LIN_SPEED_MAX, ANG_SPEED_MAX
        if measured is None or measured <= 0:
            return error_result(
                "calibrate needs measured>0: meters driven (for linear) or "
                "degrees turned (for angular). Also pass the linear/angular command "
                "and duration you used.")
        dur = max(0.1, float(duration))
        if angular and not linear:
            # ANG_SPEED_MAX so that |angular|*ANG*dur == radians(measured)
            import math as _m
            rad = _m.radians(float(measured))
            new_ang = rad / (abs(float(angular)) * dur)
            old = ANG_SPEED_MAX
            ANG_SPEED_MAX = new_ang
            _save_calibration()
            return {"status": "success", "content": [{"text":
                f"🎯 Angular calibrated: ANG_SPEED_MAX {old:.3f} → {new_ang:.3f} rad/s "
                f"(turned {measured}° on angular={angular} for {dur}s). Saved."}]}
        else:
            # LIN_SPEED_MAX so that |linear|*LIN*dur == measured meters
            new_lin = float(measured) / (abs(float(linear) or 1.0) * dur)
            old = LIN_SPEED_MAX
            LIN_SPEED_MAX = new_lin
            _save_calibration()
            return {"status": "success", "content": [{"text":
                f"🎯 Linear calibrated: LIN_SPEED_MAX {old:.3f} → {new_lin:.3f} m/s "
                f"(drove {measured}m on linear={linear} for {dur}s). Saved."}]}

    # get
    s = POSE.snapshot()
    near = _nearest_objects(s["x"], s["y"]) if s["seeded"] else []
    txt = pose_block()
    out = dict(s)
    out["nearest"] = [{"name": nm, "dist_m": round(d, 2), "x": round(ox, 2), "y": round(oy, 2)}
                      for d, nm, ox, oy in near]
    return {"status": "success", "content": [{"text": txt}, {"json": out}]}
