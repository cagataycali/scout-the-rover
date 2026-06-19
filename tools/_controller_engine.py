"""🎮 PS4 (DualShock 4) controller — direct teleop background thread.

Lets the operator drive the rover with a PS4 controller while the agent
is doing its own thing. Updates ACTION_STATE so the recorder knows the
human action and tags the source as "controller".

Default mapping (Sony DualShock 4):
    Left stick Y      → linear   (forward / reverse)
    Right stick X     → angular  (turn left / right)
    L2 (trigger)      → soft brake (scales linear down)
    R2 (trigger)      → boost (allows full +1.0 linear)
    Square            → lamp toggle
    Circle            → emergency stop (zeroes everything)
    Triangle          → start recording episode (calls callback if set)
    Cross (X)         → stop recording episode (calls callback if set)

Sends `/control` at ~10 Hz only when the action changes meaningfully
(deadzone) or on a heartbeat — the rover firmware needs continuous
commands. When the controller is idle (sticks centered), it stops
sending and any prior agent-issued action will time out naturally.

Joystick is opened lazily via SDL2 in pygame; works on macOS & Linux.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Dict, Optional

from ._recorder_engine import ACTION_STATE
from ._rover_common import sdk_post

logger = logging.getLogger(__name__)


import contextlib
import sys
import warnings
from io import TextIOWrapper

# Hide pygame's startup banner ("pygame 2.6.1 (SDL ...) Hello from the pygame
# community"). Set BEFORE pygame is ever imported.
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "hide")
# pygame ships pkg_resources usage that emits a DeprecationWarning at import.
warnings.filterwarnings("ignore", message=".*pkg_resources is deprecated.*")

# Path used to spool macOS objc/SDL2 dylib-duplicate warnings produced by
# pygame + cv2 both shipping their own libSDL2. Set ROVER_CONTROLLER_DEBUG=1
# to print them to the live stderr instead of swallowing.
_SDL_LOG_PATH = os.environ.get("ROVER_CONTROLLER_LOG", "/tmp/rover_controller_sdl.log")
_DEBUG_STDERR = os.environ.get("ROVER_CONTROLLER_DEBUG", "0") in ("1", "true", "True")


@contextlib.contextmanager
def _silence_stderr():
    """Redirect the OS-level stderr (fd 2) to a file, so dylib loader spam
    written by libc / objc-runtime / SDL2 doesn't clobber the REPL prompt.

    Python's `sys.stderr` redirection is NOT enough here: the macOS objc
    runtime writes to fd 2 directly via `fprintf(stderr, ...)`. We dup-replace
    the underlying file descriptor.
    """
    if _DEBUG_STDERR:
        yield
        return
    saved_fd = -1
    try:
        sink = open(_SDL_LOG_PATH, "a", buffering=1)
    except Exception:
        # Can't open the spool; degrade gracefully — don't silence anything.
        yield
        return
    try:
        sys.stderr.flush()
        saved_fd = os.dup(2)
        os.dup2(sink.fileno(), 2)
        yield
    finally:
        if saved_fd >= 0:
            try:
                sys.stderr.flush()
                os.dup2(saved_fd, 2)
                os.close(saved_fd)
            except Exception:
                pass
        try:
            sink.close()
        except Exception:
            pass

DEADZONE = float(os.getenv("ROVER_CONTROLLER_DEADZONE", "0.08"))
CONTROL_HZ = float(os.getenv("ROVER_CONTROLLER_HZ", "10"))
LINEAR_LIMIT = float(os.getenv("ROVER_CONTROLLER_LINEAR_MAX", "0.7"))
ANGULAR_LIMIT = float(os.getenv("ROVER_CONTROLLER_ANGULAR_MAX", "0.9"))


def _apply_deadzone(v: float, dz: float = DEADZONE) -> float:
    if abs(v) < dz:
        return 0.0
    # rescale outside-deadzone region to [0, 1]
    sign = 1.0 if v > 0 else -1.0
    return sign * (abs(v) - dz) / (1.0 - dz)


class ControllerEngine:
    """Background pygame joystick poller with on-state-change /control calls."""

    def __init__(
        self,
        on_record_start: Optional[Callable[[str], Dict]] = None,
        on_record_stop: Optional[Callable[[], Dict]] = None,
    ) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._joystick = None
        self._lamp_state = 0
        self._connected = False
        self._error: Optional[str] = None
        self._last_axes: Dict[str, float] = {"linear": 0.0, "angular": 0.0}
        self._last_send_ts = 0.0
        self._on_record_start = on_record_start
        self._on_record_stop = on_record_stop
        self._device_name = "unknown"
        self._lock = threading.Lock()

    # ── Status ───────────────────────────────────────────────────────

    def status(self) -> Dict:
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            return {
                "running": running,
                "connected": self._connected,
                "device": self._device_name if self._connected else None,
                "linear": self._last_axes["linear"],
                "angular": self._last_axes["angular"],
                "lamp": self._lamp_state,
                "error": self._error,
                "deadzone": DEADZONE,
                "linear_limit": LINEAR_LIMIT,
                "angular_limit": ANGULAR_LIMIT,
            }

    # ── Lifecycle ────────────────────────────────────────────────────

    def start(self) -> Dict:
        if self._thread is not None and self._thread.is_alive():
            return {"ok": True, "already_running": True, **self.status()}
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="ps4-ctl")
        self._thread.start()
        # wait briefly for joystick init / failure
        for _ in range(20):
            if self._connected or self._error:
                break
            time.sleep(0.05)
        return {"ok": self._connected, **self.status()}

    def stop(self) -> Dict:
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=2)
        # ensure rover is stopped on disconnect
        try:
            sdk_post("/control", json={"command": {"linear": 0.0, "angular": 0.0}}, timeout=2)
            ACTION_STATE.set(0.0, 0.0, source="idle")
        except Exception:
            pass
        return {"ok": True, "running": False}

    # ── Internal ─────────────────────────────────────────────────────

    def _init_pygame(self) -> bool:
        # Redirect macOS dylib spam (cv2 and pygame both ship libSDL2-2.0.0.dylib;
        # the second one to load makes the runtime print "Class X implemented in
        # both" for every duplicated SDL class — these floods the REPL prompt).
        with _silence_stderr():
            try:
                os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
                # No audio — we don't need pygame.mixer and it can hang on macOS
                os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
                import pygame
                pygame.init()
                pygame.joystick.init()
                count = pygame.joystick.get_count()
                if count == 0:
                    self._error = "no joystick detected (plug in PS4 controller via USB or Bluetooth)"
                    return False
                self._joystick = pygame.joystick.Joystick(0)
                self._joystick.init()
                self._device_name = self._joystick.get_name()
                self._connected = True
                self._error = None
                logger.info(f"controller connected: {self._device_name} "
                            f"({self._joystick.get_numaxes()} axes, {self._joystick.get_numbuttons()} buttons)")
                return True
            except Exception as e:
                self._error = f"pygame init failed: {e}"
                logger.exception("controller init")
                return False

    def _loop(self) -> None:
        if not self._init_pygame():
            return

        # Keep stderr silenced for the lifetime of the poll loop — any later
        # SDL2 class registration (e.g. from cv2 video capture) would otherwise
        # spam "Class X implemented in both" warnings to the user's REPL.
        with _silence_stderr():
            return self._poll_loop()

    def _poll_loop(self) -> None:
        import pygame  # already initialised
        period = 1.0 / max(CONTROL_HZ, 1.0)
        # PS4 axis indices are SDL2-standard:
        AX_LX, AX_LY, AX_RX, AX_RY = 0, 1, 2, 3
        AX_L2, AX_R2 = 4, 5
        # Button indices (SDL2 mapping for DS4):
        # 0=Cross 1=Circle 2=Square 3=Triangle 4=Share 5=PS 6=Options
        # 7=L3 8=R3 9=L1 10=R1 11=Up 12=Down 13=Left 14=Right
        BTN_CROSS, BTN_CIRCLE, BTN_SQUARE, BTN_TRIANGLE = 0, 1, 2, 3

        last_buttons: Dict[int, bool] = {}

        try:
            while not self._stop_evt.is_set():
                pygame.event.pump()
                try:
                    n_axes = self._joystick.get_numaxes()
                    ly = self._joystick.get_axis(AX_LY) if n_axes > AX_LY else 0.0
                    rx = self._joystick.get_axis(AX_RX) if n_axes > AX_RX else 0.0
                    r2 = self._joystick.get_axis(AX_R2) if n_axes > AX_R2 else -1.0
                    l2 = self._joystick.get_axis(AX_L2) if n_axes > AX_L2 else -1.0

                    # Buttons — edge-trigger logic
                    n_btn = self._joystick.get_numbuttons()
                    btns = {i: bool(self._joystick.get_button(i)) for i in range(n_btn)}
                except Exception as e:
                    self._error = f"joystick read: {e}"
                    self._connected = False
                    time.sleep(0.5)
                    continue

                # Edge: pressed-this-tick = currently-down AND was-up
                def pressed(i: int) -> bool:
                    cur = btns.get(i, False)
                    prev = last_buttons.get(i, False)
                    return cur and not prev

                if pressed(BTN_SQUARE):
                    self._lamp_state = 0 if self._lamp_state else 1
                    try:
                        sdk_post("/control", json={"command": {
                            "linear": 0.0, "angular": 0.0, "lamp": self._lamp_state,
                        }}, timeout=2)
                    except Exception as e:
                        logger.debug(f"lamp toggle failed: {e}")

                if pressed(BTN_CIRCLE):
                    # E-stop
                    try:
                        sdk_post("/control", json={"command": {"linear": 0.0, "angular": 0.0}}, timeout=2)
                    except Exception:
                        pass
                    ACTION_STATE.set(0.0, 0.0, source="controller")

                if pressed(BTN_TRIANGLE) and self._on_record_start:
                    try:
                        self._on_record_start("controller-teleop")
                    except Exception as e:
                        logger.warning(f"record-start callback failed: {e}")

                if pressed(BTN_CROSS) and self._on_record_stop:
                    try:
                        self._on_record_stop()
                    except Exception as e:
                        logger.warning(f"record-stop callback failed: {e}")

                last_buttons = btns

                # Axes → action
                # Left stick Y is +1 down, -1 up on most SDL drivers → invert for "forward".
                linear = _apply_deadzone(-ly) * LINEAR_LIMIT
                angular = _apply_deadzone(-rx) * ANGULAR_LIMIT  # invert so +x = right turn → negative angular

                # L2 acts as soft brake (axis goes -1 released → +1 fully pressed)
                brake = max(0.0, (l2 + 1.0) / 2.0)
                if brake > 0.05:
                    linear *= max(0.0, 1.0 - brake)
                # R2 boost → allow up to ±1.0 linear
                boost = max(0.0, (r2 + 1.0) / 2.0)
                if boost > 0.05:
                    linear = max(-1.0, min(1.0, linear * (1.0 + boost * (1.0 / max(LINEAR_LIMIT, 0.1) - 1.0))))

                with self._lock:
                    self._last_axes["linear"] = round(linear, 3)
                    self._last_axes["angular"] = round(angular, 3)

                # Always update ACTION_STATE so the recorder logs it
                if abs(linear) > 1e-3 or abs(angular) > 1e-3:
                    ACTION_STATE.set(linear, angular, lamp=self._lamp_state, source="controller")

                # Send to rover at CONTROL_HZ regardless (firmware needs heartbeat)
                now = time.time()
                if now - self._last_send_ts >= period:
                    try:
                        sdk_post("/control", json={"command": {
                            "linear": linear, "angular": angular, "lamp": self._lamp_state,
                        }}, timeout=2)
                    except Exception as e:
                        logger.debug(f"control send failed: {e}")
                    self._last_send_ts = now

                time.sleep(period)
        finally:
            try:
                import pygame
                pygame.joystick.quit()
                pygame.quit()
            except Exception:
                pass
            self._connected = False


# Singleton

_controller: Optional[ControllerEngine] = None
_controller_lock = threading.Lock()


def get_controller(**kwargs) -> ControllerEngine:
    global _controller
    with _controller_lock:
        if _controller is None:
            _controller = ControllerEngine(**kwargs)
    return _controller


def reset_controller() -> None:
    global _controller
    with _controller_lock:
        if _controller is not None:
            _controller.stop()
            _controller = None
