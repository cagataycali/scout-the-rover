"""⚡ rover_async — non-blocking motion queue for FLUID movement.

The problem: rover_move / rover_navigate BLOCK the agent for the whole drive
(time.sleep through every segment). The agent freezes, wheels stop between
thoughts → jerky stop-go motion.

The fix: a single background worker thread that continuously consumes a queue
of motion segments and streams /control frames to the rover WITHOUT pausing.
The agent enqueues segments and returns instantly — it can look, think, and
queue the *next* leg while the current one is still executing. Wheels keep
turning across agent turns → smooth, pipelined motion.

Safety:
  * Every executed segment auto-stops at its end (same as sync tools).
  * `queue_motion(... priority='interrupt')` or `rover_async(action='stop')`
    clears the queue and halts immediately (kill switch).
  * The worker feeds every executed segment into the dead-reckoning pose
    estimator (rover_pose), so localization stays live during async drives.
  * Watchdog: if the queue empties, the worker sends a final stop and idles.

This complements (does not replace) rover_move: use rover_move for a single
careful look-then-move step; use the async queue for fluid multi-leg motion
where you want to keep reasoning while moving.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from strands import tool

from ._rover_common import error_result, ok_result, sdk_post, apply_turn_sign
from ._recorder_engine import ACTION_STATE

try:
    from .rover_pose import integrate_move
except Exception:                       # pragma: no cover
    def integrate_move(*a, **k):
        pass

logger = logging.getLogger(__name__)

_FRAME_INTERVAL = float(os.getenv("SCOUT_ASYNC_FRAME_INTERVAL", "0.35"))  # s between /control frames


def _clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(v)))


@dataclass(order=True)
class _Segment:
    # priority: lower number = sooner. 0=urgent, 5=normal.
    priority: int
    seq: int
    linear: float = field(compare=False, default=0.0)
    angular: float = field(compare=False, default=0.0)
    duration: float = field(compare=False, default=1.0)
    label: str = field(compare=False, default="")


class _MotionWorker:
    """Background thread that drains a priority queue of motion segments."""

    def __init__(self) -> None:
        self._q: "queue.PriorityQueue[_Segment]" = queue.PriorityQueue()
        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._lock = threading.Lock()
        self._seq = 0
        self._current: Optional[str] = None
        self._done_labels: List[str] = []

    # ── lifecycle ──────────────────────────────────────────────────────
    def start(self) -> bool:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._running.set()
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="rover-async-motion")
            self._thread.start()
            return True

    def stop(self, clear: bool = True) -> None:
        self._running.clear()
        if clear:
            self._drain_queue()
        self._safe_stop()

    def _drain_queue(self) -> None:
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass

    # ── enqueue ────────────────────────────────────────────────────────
    def enqueue(self, segments: List[Dict[str, Any]], priority: int = 5) -> List[str]:
        labels = []
        for raw in segments:
            with self._lock:
                self._seq += 1
                seq = self._seq
            seg = _Segment(
                priority=int(priority),
                seq=seq,
                linear=_clamp(raw.get("linear", 0.0)),
                angular=_clamp(raw.get("angular", 0.0)),
                duration=max(0.1, min(10.0, float(raw.get("duration", 1.0)))),
                label=str(raw.get("label", f"seg{seq}")),
            )
            self._q.put(seg)
            labels.append(seg.label)
        if not (self._thread and self._thread.is_alive()):
            self.start()
        return labels

    # ── wire I/O ───────────────────────────────────────────────────────
    def _send(self, linear: float, angular: float) -> None:
        resp = sdk_post("/control",
                        json={"command": {"linear": linear,
                                          "angular": apply_turn_sign(angular)}})
        if resp.status_code != 200:
            raise RuntimeError(f"/control HTTP {resp.status_code}")
        ACTION_STATE.set(linear, angular, source="agent")

    def _safe_stop(self) -> None:
        try:
            self._send(0.0, 0.0)
        except Exception:
            pass

    # ── worker loop ────────────────────────────────────────────────────
    def _loop(self) -> None:
        logger.info("rover-async motion worker started")
        idle_since = time.time()
        while self._running.is_set():
            try:
                seg = self._q.get(timeout=0.25)
            except queue.Empty:
                # idle: ensure stopped, keep thread alive briefly for next leg
                if time.time() - idle_since > 0.5:
                    self._safe_stop()
                if time.time() - idle_since > 30:
                    break   # nothing to do for 30s → retire worker
                continue

            self._current = seg.label
            idle_since = time.time()
            deadline = time.time() + seg.duration
            try:
                while time.time() < deadline and self._running.is_set():
                    self._send(seg.linear, seg.angular)
                    remaining = deadline - time.time()
                    time.sleep(min(_FRAME_INTERVAL, max(0.0, remaining)))
                self._send(0.0, 0.0)  # per-seg auto-stop
                try:
                    integrate_move(seg.linear, seg.angular, seg.duration)
                except Exception:
                    pass
                self._done_labels.append(seg.label)
            except Exception as e:
                logger.warning(f"async seg '{seg.label}' failed: {e}")
                self._safe_stop()
            finally:
                self._current = None
                idle_since = time.time()

        self._safe_stop()
        self._running.clear()
        logger.info("rover-async motion worker retired")

    # ── introspection ──────────────────────────────────────────────────
    def status(self) -> Dict[str, Any]:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "queued": self._q.qsize(),
            "current": self._current,
            "recent_done": self._done_labels[-5:],
        }


WORKER = _MotionWorker()


@tool
def rover_async(action: str = "status",
                steps: Optional[List[Dict[str, Any]]] = None,
                priority: str = "normal") -> Dict[str, Any]:
    """⚡ Non-blocking motion queue — keep moving while you think.

    Unlike rover_move (which blocks until the drive finishes), this enqueues
    motion segments to a background worker and returns INSTANTLY. The rover
    keeps driving fluidly across your turns while you look, reason, and queue
    the next leg. Pose is updated automatically as segments execute.

    Actions:
        queue   → add segments to the motion queue (non-blocking). Provide
                  `steps`: [{linear, angular, duration, label}, ...].
        status  → what's queued / currently executing.
        stop    → CLEAR the queue and halt immediately (kill switch).
        start   → ensure the background worker is running.

    Args:
        action: "queue" | "status" | "stop" | "start".
        steps: list of segment dicts (for action='queue').
        priority: "urgent" (0), "normal" (5), or "interrupt" (clears queue
                  then queues these next). For action='queue'.

    Returns:
        Dict with status, summary, and queue JSON.
    """
    action = (action or "status").lower().strip()

    if action == "stop":
        WORKER.stop(clear=True)
        return ok_result("⚡ Async motion stopped, queue cleared.")

    if action == "start":
        started = WORKER.start()
        return ok_result("⚡ Worker " + ("started." if started else "already running."))

    if action == "queue":
        if not steps or not isinstance(steps, list):
            return error_result("action='queue' needs a non-empty 'steps' list.")
        if len(steps) > 32:
            return error_result(f"Too many steps ({len(steps)}); cap at 32.")
        prio_map = {"urgent": 0, "normal": 5, "interrupt": 0}
        if priority == "interrupt":
            WORKER.stop(clear=True)
            WORKER.start()
        labels = WORKER.enqueue(steps, priority=prio_map.get(priority, 5))
        st = WORKER.status()
        return ok_result(
            f"⚡ Queued {len(labels)} segment(s) [{', '.join(labels)}] "
            f"@ {priority}. {st['queued']} now in queue. Returning immediately — "
            f"rover is moving; look/think and queue the next leg.",
            extra_content=[{"json": st}],
        )

    # status
    st = WORKER.status()
    return ok_result(
        f"⚡ Async motion: {'RUNNING' if st['running'] else 'idle'} | "
        f"queued={st['queued']} current={st['current']} "
        f"recent={st['recent_done']}",
        extra_content=[{"json": st}],
    )
