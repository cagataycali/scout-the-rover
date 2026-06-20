"""🎬 Earth Rover dataset recorder — background capture engine.

Polls the SDK at a fixed FPS, collects per-frame:
  - observation.images.front          (JPEG → mp4)
  - observation.images.rear           (JPEG → mp4)  if available
  - observation.state                 (battery, signal, gps, imu, rpms…)
  - action                            (linear, angular, lamp) — last commanded
  - microphone audio (PCM16 mono)     buffered to .wav per episode

Writes to a LeRobotDataset (v3 codebase) under ./datasets/<repo_id>/.
Each episode = one start_recording / stop_recording cycle.

Audio is stored alongside the dataset in `videos/audio/<chunk>/<file>.wav`
because LeRobot v3 doesn't natively model audio yet — but the dataset
itself (frames + state + action) is fully lerobot-compatible so it can
be loaded with `LeRobotDataset(repo_id, root=...)` and trained on.

Reference paper: arXiv:2601.09444v2 — actions are 2D (linear, angular)
normalized to [-1, 1], targets are H=10 action chunks, FPS=4 in deployment.
We default to FPS=10 here for richer training signal.
"""
from __future__ import annotations

import base64
import io
import logging
import os
import threading
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import requests

from ._rover_common import sdk_get, sdk_post

logger = logging.getLogger(__name__)

# Default dataset root — overridable via env
DATASET_ROOT = Path(os.getenv("ROVER_DATASET_ROOT", "datasets")).resolve()


import contextlib
import sys

# Path used to spool macOS objc/SDL2 dylib-duplicate warnings produced when
# cv2 loads after pygame (or vice versa). ROVER_RECORDER_DEBUG=1 to surface.
_RECORDER_SDL_LOG = os.environ.get("ROVER_RECORDER_LOG", "/tmp/rover_recorder_sdl.log")
_RECORDER_DEBUG_STDERR = os.environ.get("ROVER_RECORDER_DEBUG", "0") in ("1", "true", "True")


@contextlib.contextmanager
def _silence_stderr():
    """Dup-replace fd 2 so libc/objc spam writes to a file, not the REPL."""
    if _RECORDER_DEBUG_STDERR:
        yield
        return
    saved_fd = -1
    try:
        sink = open(_RECORDER_SDL_LOG, "a", buffering=1)
    except Exception:
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


# Eagerly import cv2 at module load WITH stderr silenced — this ensures the
# dylib loader spam (which is unavoidable when pygame is also installed)
# happens once, into the spool, instead of scattered across the REPL during
# capture loop frames.
with _silence_stderr():
    try:
        import cv2 as _cv2_eager  # noqa: F401  (preload only)
    except ImportError:
        pass

DEFAULT_FPS = int(os.getenv("ROVER_RECORD_FPS", "10"))  # earthrover_mini_plus convention
DEFAULT_AUDIO_RATE = int(os.getenv("ROVER_AUDIO_RATE", "16000"))

# Target output frame shape — every captured frame is resized to this BEFORE
# being added to the dataset, so changes in the rover's WebRTC video resolution
# (e.g. front 1080p, rear sometimes drops to 540p) do not break the schema.
# Defaults match the paper (224x224 = arXiv:2601.09444 §III).
TARGET_HEIGHT = int(os.getenv("ROVER_RECORD_HEIGHT", "480"))  # earthrover_mini_plus
TARGET_WIDTH = int(os.getenv("ROVER_RECORD_WIDTH", "640"))   # earthrover_mini_plus

# Default-on rear capture; set ROVER_RECORD_REAR=0 to force front-only datasets
# (useful when the rear camera is unreliable on this bot).
# Default to FRONT-ONLY: cuts SDK load by 33% and matches the paper's
# monocular front-camera setup. Set ROVER_RECORD_REAR=1 to also capture rear.
ENABLE_REAR_DEFAULT = os.getenv("ROVER_RECORD_REAR", "1") in ("1", "true", "True")  # earthrover_mini_plus = front+rear

# How often to surface "capture frame error" logs (anti-spam).
ERROR_LOG_PERIOD_S = float(os.getenv("ROVER_RECORD_ERROR_LOG_PERIOD", "5"))


# Helpers


# Frames smaller than this in either dimension are placeholder/no-track
# fallbacks from the SDK (seen as 2×2 black JPEGs when WebRTC hasn't
# connected to the rover). Reject them rather than upscaling 4 black
# pixels to a full 224×224 dark square.
_MIN_FRAME_DIM = int(os.getenv("ROVER_MIN_FRAME_DIM", "64"))
_REJECT_BLACK_FRAMES = os.getenv("ROVER_REJECT_BLACK_FRAMES", "1") not in ("0", "false", "False")
_BLACK_FRAME_STD_THRESHOLD = float(os.getenv("ROVER_BLACK_FRAME_STD", "1.0"))


def _decode_jpeg_to_rgb(b64: str, resize_to: Optional[tuple] = None) -> Optional[np.ndarray]:
    """Decode base64 JPEG → HxWx3 uint8 RGB numpy array. Returns None on failure.

    If `resize_to=(H, W)` is given, the output is resized to that shape with
    cv2.INTER_AREA (a good general-purpose downscaler). This guarantees a
    consistent dataset shape even when the rover's WebRTC track changes
    resolution mid-stream.

    Rejects (returns None) if the SOURCE frame is clearly a placeholder:
      * smaller than ROVER_MIN_FRAME_DIM in either dim (default 64)
      * std-dev across pixels < ROVER_BLACK_FRAME_STD (default 1.0)
        These signal "WebRTC track not connected yet" — the SDK falls back
        to a 2×2 black image until video flows. Dropping these frames stops
        the dataset filling with smeared black squares when the rover is
        online (telemetry works) but its video pipeline is offline.
    """
    try:
        import cv2  # already preloaded at module init
        raw = base64.b64decode(b64)
        arr = np.frombuffer(raw, dtype=np.uint8)
        with _silence_stderr():
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)  # BGR
            if img is None:
                return None
            # Reject placeholder frames BEFORE resize — checking size and
            # variance on the upscaled version would hide the truth.
            if img.shape[0] < _MIN_FRAME_DIM or img.shape[1] < _MIN_FRAME_DIM:
                return None
            if _REJECT_BLACK_FRAMES and float(img.std()) < _BLACK_FRAME_STD_THRESHOLD:
                return None
            if resize_to is not None:
                target_h, target_w = resize_to
                if img.shape[0] != target_h or img.shape[1] != target_w:
                    img = cv2.resize(img, (target_w, target_h), interpolation=cv2.INTER_AREA)
            return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    except Exception as e:
        logger.warning(f"jpeg decode failed: {e}")
        return None


# ── State schema: aligned to the official `earthrover_mini_plus` convention ──
# The FIRST 10 dims EXACTLY match lilkm/earthrover-navigation (dotted names,
# same order) so policies/checkpoints transfer. We then APPEND our richer raw
# telemetry (full IMU + electrical) as extra dims — supersets are LeRobot-safe
# and a model that only wants the standard 10 can slice [:10].
#
# Reference (10):  linear.vel, angular.vel, battery.level, orientation.deg,
#                  gps.latitude, gps.longitude, gps.signal, signal.level,
#                  vibration, lamp.state
# Our extras (+6): voltage, current, imu.accel.x/y/z is partially covered;
#                  we append accel + gyro + voltage/current for completeness.
STATE_NAMES = [
    # --- standard earthrover_mini_plus core (indices 0..9) ---
    "linear.vel", "angular.vel", "battery.level", "orientation.deg",
    "gps.latitude", "gps.longitude", "gps.signal", "signal.level",
    "vibration", "lamp.state",
    # --- scout extras: electrical (10..11) ---
    "voltage", "current",
    # --- IMU: latest sample of the SDK burst arrays (12..17) ---
    "imu.accel.x", "imu.accel.y", "imu.accel.z",
    "imu.gyro.x", "imu.gyro.y", "imu.gyro.z",
    # --- raw magnetometer x/y/z (18..20) -> absolute heading reference ---
    "imu.mag.x", "imu.mag.y", "imu.mag.z",
    # --- measured per-wheel odometry (21..24): fl, fr, rl, rr ---
    "rpm.front_left", "rpm.front_right", "rpm.rear_left", "rpm.rear_right",
    # --- extra electrical/system context (25..26) ---
    "power", "network_state",
]

# action aligns EXACTLY with the reference: 2-dim velocity command.
# (lamp moved into observation.state as `lamp.state` -- it is not an action.)
ACTION_NAMES = ["linear.vel", "angular.vel"]


def _latest_sample(arr):
    """Return the last sample from an SDK burst array, or [] if empty/invalid.

    SDK bursts look like accels=[[x,y,z,ts], ...], rpms=[[fl,fr,rl,rr,ts], ...].
    We take the most recent (last) sample so the 10Hz frame uses freshest data.
    """
    if isinstance(arr, list) and arr:
        last = arr[-1]
        if isinstance(last, (list, tuple)):
            return list(last)
    return []


def _telemetry_to_state_vec(d: Dict[str, Any]) -> np.ndarray:
    """Flatten /data telemetry -> float32 state vector matching STATE_NAMES.

    First 10 dims == official earthrover_mini_plus layout (dotted), then scout's
    extra electrical + IMU + mag + rpm dims. Missing fields -> 0.0.

    The Frodobots SDK exposes IMU/mag/rpm as *burst arrays* (multiple samples
    per poll, each row ending in a unix timestamp), e.g.:
        accels = [[ax, ay, az, ts], ...]   (~5 samples)
        gyros  = [[gx, gy, gz, ts], ...]   (~5 samples)
        mags   = [[mx, my, mz, ts], ...]   (~1 sample)
        rpms   = [[fl, fr, rl, rr, ts], ...] (~5 samples)
    We take the latest sample of each burst for the per-frame state. (Earlier
    versions read flat accel_x/gyro_x keys which the cloud SDK never emits --
    those dims were silently zero. This fixes that.)
    """
    imu = d.get("imu") or d.get("IMU") or {}
    if not isinstance(imu, dict):
        imu = {}

    def g(*keys, src=d, default=0.0):
        """First non-None among keys, checked in src then imu."""
        for k in keys:
            v = src.get(k)
            if v is None and src is d:
                v = imu.get(k)
            if v is not None:
                return v
        return default

    # ACTION_STATE carries the last commanded velocity (what the rover is doing).
    cmd = ACTION_STATE.snapshot()

    # --- burst arrays: take latest sample (strip trailing timestamp) ---
    accel = _latest_sample(d.get("accels"))      # [ax, ay, az, ts]
    gyro = _latest_sample(d.get("gyros"))        # [gx, gy, gz, ts]
    mag = _latest_sample(d.get("mags"))          # [mx, my, mz, ts]
    rpm = _latest_sample(d.get("rpms"))          # [fl, fr, rl, rr, ts]

    def idx(seq, i):
        return seq[i] if isinstance(seq, list) and len(seq) > i else 0.0

    # Fall back to flat keys (some firmware/local builds) before the burst array.
    ax = g("accel_x", default=idx(accel, 0))
    ay = g("accel_y", default=idx(accel, 1))
    az = g("accel_z", default=idx(accel, 2))
    gx = g("gyro_x", default=idx(gyro, 0))
    gy = g("gyro_y", default=idx(gyro, 1))
    gz = g("gyro_z", default=idx(gyro, 2))

    vals = [
        # 0 linear.vel -- prefer measured speed, fall back to commanded linear
        g("speed", default=cmd.get("linear", 0.0)),
        # 1 angular.vel -- SDK rarely reports measured yaw rate; use commanded
        g("angular_velocity", "yaw_rate", default=cmd.get("angular", 0.0)),
        g("battery"),                              # 2 battery.level
        g("orientation"),                          # 3 orientation.deg
        g("latitude"),                             # 4 gps.latitude
        g("longitude"),                            # 5 gps.longitude
        g("gps_signal"),                           # 6 gps.signal
        g("signal_level"),                         # 7 signal.level
        g("vibration"),                            # 8 vibration
        g("lamp"),                                 # 9 lamp.state
        # --- electrical extras ---
        g("voltage"),                              # 10
        g("current"),                              # 11
        # --- IMU latest-sample (12..17) ---
        ax, ay, az,                                # 12-14 accel
        gx, gy, gz,                                # 15-17 gyro
        # --- magnetometer (18..20) ---
        idx(mag, 0), idx(mag, 1), idx(mag, 2),     # 18-20 mag x/y/z
        # --- per-wheel rpm (21..24) ---
        idx(rpm, 0), idx(rpm, 1),                  # 21-22 front l/r
        idx(rpm, 2), idx(rpm, 3),                  # 23-24 rear  l/r
        # --- system context (25..26) ---
        g("power"),                                # 25 power
        g("network_state"),                        # 26 network_state
    ]
    out = np.zeros(len(STATE_NAMES), dtype=np.float32)
    for i, v in enumerate(vals[:len(STATE_NAMES)]):
        try:
            out[i] = float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            out[i] = 0.0
    return out

# Engine


@dataclass
class _ActionState:
    """Last-commanded action — written by motion tools or controller, read by recorder."""

    linear: float = 0.0
    angular: float = 0.0
    lamp: float = 0.0
    source: str = "idle"  # 'agent' | 'controller' | 'idle'
    ts: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def set(self, linear: float, angular: float, lamp: Optional[float] = None,
            source: str = "agent") -> None:
        with self.lock:
            self.linear = float(linear)
            self.angular = float(angular)
            if lamp is not None:
                self.lamp = float(lamp)
            self.source = source
            self.ts = time.time()

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "linear": self.linear,
                "angular": self.angular,
                "lamp": self.lamp,
                "source": self.source,
                "ts": self.ts,
            }


# Singleton — shared between motion tools, controller, recorder
ACTION_STATE = _ActionState()


class RecorderEngine:
    """Background thread that captures rover observations into a LeRobotDataset.

    State machine:
        idle → recording → idle  (start_episode / stop_episode)

    Multiple episodes can be recorded in sequence; the dataset is created on
    the FIRST start_episode and reused for subsequent ones.
    """

    def __init__(
        self,
        repo_id: str = None,
        fps: int = DEFAULT_FPS,
        audio_rate: int = DEFAULT_AUDIO_RATE,
        capture_rear: bool = ENABLE_REAR_DEFAULT,
        capture_audio: bool = True,
        target_shape: tuple = (TARGET_HEIGHT, TARGET_WIDTH),
    ) -> None:
        if repo_id is None:
            # ── SHARED DATASET (Fix A) ──────────────────────────────────────
            # All processes (main / telegram / thinker / voice) MUST append to
            # ONE dataset so episodes + the cross-process events.sqlite stay
            # unified. Priority:
            #   1. ROVER_REPO_ID  — explicit, pin a specific corpus
            #   2. per-DAY name   — auto-rolls daily, but all same-day procs share
            # (was per-SECOND, which fragmented every `make run`/`make thinker`
            #  into its own dataset — see RESEARCH.md §3 multi-agent intent.)
            from datetime import datetime as _dt
            repo_id = os.getenv("ROVER_REPO_ID")
            if not repo_id:
                day = _dt.now().strftime('%Y%m%d')
                # ── Per-agent dataset (concurrency fix) ─────────────────────
                # LeRobot v3 parquet is SINGLE-WRITER: two processes resuming
                # the same dataset dir corrupt each other (footer not yet
                # written → ArrowInvalid). So each process (main/thinker/
                # telegram/voice) owns its OWN LeRobot dataset under a per-day
                # parent. The ECoT reasoning events.sqlite IS shared (WAL,
                # multi-writer) — see reasoning_log._db_path(), which keys on
                # the per-day root, NOT this per-agent repo_id.
                agent = os.getenv("ROVER_AGENT_ID")
                if not agent:
                    # derive from the running script name (agent.py→main, etc.)
                    import sys as _sys
                    prog = Path(_sys.argv[0]).stem if _sys.argv else "main"
                    agent = {"agent": "main", "thinker_loop": "thinker",
                             "telegram_listener": "telegram",
                             "voice_agent": "voice"}.get(prog, prog or "main")
                repo_id = f"scout/earth-rover-mini-{day}/{agent}"
        self.repo_id = repo_id
        self.fps = fps
        self.audio_rate = audio_rate
        self.capture_rear = capture_rear
        self.capture_audio = capture_audio
        self._target_shape: tuple = tuple(target_shape)  # (H, W)

        # Error-log throttling
        self._last_error_log_ts: float = 0.0
        self._error_count_since_log: int = 0

        self._dataset = None  # LeRobotDataset, lazy-init on first record

        # Shared thread pool for parallel SDK calls per frame (data + front +
        # optional rear all in flight at once). Sized to 3 workers — one per
        # endpoint we hit. Reusing the pool avoids thread-creation overhead
        # at every frame.
        from concurrent.futures import ThreadPoolExecutor
        self._capture_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="rover-cap")
        self._has_rear = False  # detected on first frame
        self._image_shape: Optional[tuple] = None  # (H, W, 3)

        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._recording_evt = threading.Event()  # set while episode active

        self._audio_buffer: List[bytes] = []
        self._audio_lock = threading.Lock()
        self._audio_started = False

        self._episode_idx = 0
        self._episode_frames = 0
        self._episode_start_ts = 0.0
        self._current_task: str = "drive"
        self._last_error: Optional[str] = None

        self._state_lock = threading.Lock()  # protects mutable status fields

    # Public status

    def status(self) -> Dict[str, Any]:
        with self._state_lock:
            return {
                "engine_running": self._thread is not None and self._thread.is_alive(),
                "recording": self._recording_evt.is_set(),
                "repo_id": self.repo_id,
                "fps": self.fps,
                "current_episode": self._episode_idx if self._recording_evt.is_set() else None,
                "current_task": self._current_task if self._recording_evt.is_set() else None,
                "current_frames": self._episode_frames,
                "current_duration_s": (
                    round(time.time() - self._episode_start_ts, 1)
                    if self._recording_evt.is_set() else 0.0
                ),
                "total_episodes": self._episode_idx,
                "dataset_root": str(self._dataset_root()),
                "has_rear_camera": self._has_rear,
                "audio_capture": self.capture_audio and self._audio_started,
                "last_error": self._last_error,
            }

    def _dataset_root(self) -> Path:
        # repo_id may be "scout/earth-rover-mini-YYYYMMDD/<agent>".
        # The LeRobot dataset (parquet/videos) is PER-AGENT to stay single-
        # writer safe. We flatten the leading "scout/" namespace but keep the
        # per-day + per-agent nesting:  datasets/scout__...-YYYYMMDD/<agent>/
        rid = self.repo_id
        if "/" in rid:
            head, _, tail = rid.partition("/")  # "scout", "earth-...-DD/<agent>"
            return DATASET_ROOT / (head + "__" + tail.replace("/", "/"))
        return DATASET_ROOT / rid.replace("/", "__")

    # Engine lifecycle

    def ensure_thread(self) -> None:
        """Start the background capture thread if not running."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="rover-recorder")
        self._thread.start()
        logger.info("recorder engine started")

    def shutdown(self) -> None:
        """Stop the capture thread (and any active episode)."""
        if self._recording_evt.is_set():
            self.stop_episode()
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=3)
        if self._dataset is not None:
            try:
                self._dataset.finalize()
            except Exception as e:
                logger.warning(f"dataset finalize failed: {e}")
        try:
            self._capture_pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        logger.info("recorder engine stopped")

    # Episode control

    def start_episode(self, task: str = "drive") -> Dict[str, Any]:
        if self._recording_evt.is_set():
            return {"ok": False, "error": "episode already in progress"}

        # First episode → probe rear cam, init dataset
        if self._dataset is None:
            self._has_rear = self._probe_rear_camera()
            self._image_shape = self._probe_image_shape()
            if self._image_shape is None:
                return {"ok": False, "error": (
                    "could not probe a usable camera frame. SDK is responding "
                    "but the rover's video track looks inactive (placeholder "
                    "frames smaller than {min_dim}px or all-black). "
                    "Try: (1) `make sdk-down && make sdk-up` to reconnect WebRTC, "
                    "(2) move the rover so it wakes up, (3) check the rover is "
                    "online via /data telemetry."
                ).format(min_dim=_MIN_FRAME_DIM)}
            try:
                self._dataset = self._create_or_resume_dataset()
            except Exception as e:
                self._last_error = f"dataset init failed: {e}"
                logger.exception("dataset init")
                return {"ok": False, "error": self._last_error}

        # Start mic capture (best-effort)
        if self.capture_audio:
            self._start_audio()

        with self._state_lock:
            self._current_task = task
            # ── Off-by-one fix: mirror LeRobot's own episode numbering. ──
            # LeRobot assigns the new episode index == meta.total_episodes
            # (episodes saved so far). Stamping that here keeps reasoning_events
            # / episode_anchors perfectly aligned with the video + parquet.
            try:
                self._episode_idx = int(self._dataset.meta.total_episodes)
            except Exception:
                pass  # fall back to whatever _create_or_resume_dataset set
            self._episode_frames = 0
            self._episode_start_ts = time.time()
            self._audio_buffer.clear()
        self.ensure_thread()
        self._recording_evt.set()
        # ── ECoT: publish wall-clock anchor so reasoning_log can bind frames ──
        try:
            from .reasoning_log import anchor_episode as _anchor
            _anchor(self._episode_idx, self.repo_id, float(self.fps),
                    self._episode_start_ts, task=task)
        except Exception as _e:
            logger.debug(f"ecot anchor(start) skipped: {_e}")
        logger.info(f"episode {self._episode_idx} started: task={task!r}")
        return {
            "ok": True,
            "episode_index": self._episode_idx,
            "task": task,
            "fps": self.fps,
            "dataset_root": str(self._dataset_root()),
        }

    def stop_episode(self) -> Dict[str, Any]:
        if not self._recording_evt.is_set():
            return {"ok": False, "error": "no active episode"}
        self._recording_evt.clear()
        # Brief sleep so capture loop notices and stops adding frames
        time.sleep(1.0 / max(self.fps, 1) + 0.1)

        frames = self._episode_frames
        duration = time.time() - self._episode_start_ts

        # Write audio sidecar (if any)
        wav_path = None
        if self.capture_audio:
            try:
                wav_path = self._flush_audio_to_wav()
            except Exception as e:
                logger.warning(f"audio flush failed: {e}")
            try:
                self._stop_audio()
            except Exception as e:
                logger.debug(f"mic stop: {e}")

        if frames < 2:
            try:
                if self._dataset is not None:
                    self._dataset.clear_episode_buffer()
            except Exception:
                pass
            return {
                "ok": False,
                "error": f"episode dropped (only {frames} frame(s) captured)",
                "duration_s": round(duration, 2),
            }

        try:
            with _silence_stderr():
                self._dataset.save_episode(parallel_encoding=False)  # encodes mp4, writes parquet
        except Exception as e:
            self._last_error = f"save_episode failed: {e}"
            logger.exception("save_episode")
            return {"ok": False, "error": self._last_error}

        # ── ECoT: stamp episode stop time for export reconciliation ──
        try:
            from .reasoning_log import anchor_episode as _anchor
            _anchor(self._episode_idx, self.repo_id, float(self.fps),
                    self._episode_start_ts, stop_wall_ts=time.time())
        except Exception as _e:
            logger.debug(f"ecot anchor(stop) skipped: {_e}")

        # ── ECoT: materialize portable flat files (jsonl + ChatML) next to the
        #    dense LeRobot data, so the reasoning dir is self-contained for
        #    distribution — same convention as data/*.parquet. Best-effort. ──
        try:
            import os as _os
            _ecot_ep = self._episode_idx
            _root = self._dataset_root()
            _os.environ.setdefault("ECOT_EVENTS_DB",
                                   str(_root / "reasoning" / "events.sqlite"))
            from . import ecot_export as _ecot
            _ecot.export_episode(_ecot_ep, repo_dir=str(_root), write=True)
        except Exception as _e:
            logger.debug(f"ecot auto-export skipped: {_e}")
        return {
            "ok": True,
            "episode_index": self._episode_idx,
            "frames": frames,
            "duration_s": round(duration, 2),
            "audio_path": str(wav_path) if wav_path else None,
            "dataset_root": str(self._dataset_root()),
        }

    # Dataset init

    def _create_or_resume_dataset(self):
        from lerobot.datasets.lerobot_dataset import LeRobotDataset

        root = self._dataset_root()
        info_json = root / "meta" / "info.json"
        H, W, _ = self._image_shape  # type: ignore[misc]

        features: Dict[str, Dict[str, Any]] = {
            "observation.images.front": {
                "dtype": "video",
                "shape": (H, W, 3),
                "names": ["height", "width", "channels"],
            },
            "observation.state": {
                "dtype": "float32",
                "shape": (len(STATE_NAMES),),
                "names": STATE_NAMES,
            },
            "action": {
                "dtype": "float32",
                "shape": (len(ACTION_NAMES),),
                "names": ACTION_NAMES,
            },
            # ── ECoT: how stale the commanded action was when this frame was
            #    sampled (seconds). 0 ≈ fresh command; large ≈ idle/transition
            #    frame. Lets the trainer discard smeared boundary frames. ──
            "action_age": {
                "dtype": "float32",
                "shape": (1,),
                "names": ["seconds"],
            },
        }
        if self._has_rear:
            features["observation.images.rear"] = {
                "dtype": "video",
                "shape": (H, W, 3),
                "names": ["height", "width", "channels"],
            }

        if info_json.exists():
            # Schema check — if the on-disk dataset was created with a different
            # target shape, refuse to resume (corrupting the dataset is worse
            # than asking the user to nuke it).
            try:
                import json as _json
                disk_info = _json.loads(info_json.read_text())
                disk_features = disk_info.get("features", {})
                disk_front = tuple(disk_features.get("observation.images.front", {}).get("shape", []))
                want = (H, W, 3)
                if disk_front and disk_front != want:
                    raise RuntimeError(
                        f"dataset shape mismatch: on-disk front camera is "
                        f"{disk_front}, want {want}. "
                        f"Either set ROVER_RECORD_HEIGHT/WIDTH to match, or "
                        f"`rm -rf {root}` to start a fresh dataset."
                    )
                # Same check for rear if both sides have it
                if self._has_rear:
                    disk_rear = tuple(disk_features.get("observation.images.rear", {}).get("shape", []))
                    if disk_rear and disk_rear != want:
                        raise RuntimeError(
                            f"dataset shape mismatch: on-disk rear camera is "
                            f"{disk_rear}, want {want}. "
                            f"Either set ROVER_RECORD_HEIGHT/WIDTH to match, "
                            f"set ROVER_RECORD_REAR=0, or "
                            f"`rm -rf {root}` to start a fresh dataset."
                        )
            except RuntimeError:
                raise
            except Exception as e:
                logger.warning(f"could not validate on-disk schema: {e}")

            logger.info(f"resuming dataset at {root}")
            ds = LeRobotDataset.resume(
                repo_id=self.repo_id,
                root=root,
                vcodec="auto",
            )
            self._episode_idx = ds.meta.total_episodes
            return ds

        logger.info(f"creating dataset at {root}")
        return LeRobotDataset.create(
            repo_id=self.repo_id,
            fps=self.fps,
            features=features,
            root=root,
            robot_type="earthrover_mini_plus",
            use_videos=True,
            vcodec="auto",
            # Flush episode metadata on every save_episode (default 10 buffers
            # in memory until shutdown, which means a Ctrl+C'd session loses
            # meta/episodes/ entirely and the dataset can't be reloaded).
            metadata_buffer_size=1,
        )

    # Probes

    def _probe_rear_camera(self) -> bool:
        try:
            r = sdk_get("/v2/rear", timeout=5)
            if r.status_code != 200:
                return False
            d = r.json()
            return bool(d.get("rear_frame"))
        except Exception:
            return False

    def _probe_image_shape(self) -> Optional[tuple]:
        """Verify the SDK is producing decodable frames, then return our TARGET shape.

        The dataset is locked to a fixed (TARGET_HEIGHT, TARGET_WIDTH) regardless of
        what resolution the rover's WebRTC track happens to be at — every frame is
        resized on the way in. So the SDK only needs to be REACHABLE; the actual
        probed resolution is discarded.
        """
        try:
            r = sdk_get("/v2/front", timeout=5)
            if r.status_code != 200:
                return None
            b64 = r.json().get("front_frame")
            if not b64:
                return None
            # Validate decodability
            img = _decode_jpeg_to_rgb(b64, resize_to=self._target_shape)
            if img is None:
                return None
            return img.shape  # (target_h, target_w, 3)
        except Exception:
            return None

    # Audio

    def _start_audio(self) -> None:
        try:
            r = sdk_post("/rover-mic/start", json={"rate": self.audio_rate}, timeout=5)
            if r.status_code == 200:
                self._audio_started = True
            else:
                logger.info(f"rover-mic/start non-200: {r.status_code} {r.text[:200]}")
                self._audio_started = False
        except Exception as e:
            logger.info(f"rover-mic/start failed (audio disabled): {e}")
            self._audio_started = False

    def _stop_audio(self) -> None:
        try:
            sdk_post("/rover-mic/stop", timeout=5)
        finally:
            self._audio_started = False

    def _drain_audio(self) -> None:
        if not self._audio_started:
            return
        try:
            r = sdk_get("/rover-mic", timeout=3)
            if r.status_code != 200:
                return
            data = r.json()
            chunks = data.get("chunks") or []
            if not chunks:
                return
            with self._audio_lock:
                for b64 in chunks:
                    try:
                        self._audio_buffer.append(base64.b64decode(b64))
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"audio drain: {e}")

    def _flush_audio_to_wav(self) -> Optional[Path]:
        with self._audio_lock:
            if not self._audio_buffer:
                return None
            pcm = b"".join(self._audio_buffer)
            self._audio_buffer.clear()

        audio_dir = self._dataset_root() / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        path = audio_dir / f"episode_{self._episode_idx:06d}.wav"
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.audio_rate)
            wf.writeframes(pcm)
        return path

    # Capture loop

    def _loop(self) -> None:
        period = 1.0 / max(self.fps, 1)
        next_tick = time.time()
        audio_drain_period = 0.5
        next_audio = time.time()

        while not self._stop_evt.is_set():
            now = time.time()

            if now >= next_audio:
                self._drain_audio()
                next_audio = now + audio_drain_period

            if not self._recording_evt.is_set():
                time.sleep(0.05)
                next_tick = time.time() + period
                continue

            try:
                self._capture_one_frame()
            except Exception as e:
                # Rate-limit (the inner add_frame already does its own throttle,
                # so this only catches outer SDK / decode failures).
                now2 = time.time()
                self._error_count_since_log += 1
                self._last_error = f"capture: {e}"
                if now2 - self._last_error_log_ts >= ERROR_LOG_PERIOD_S:
                    logger.warning(
                        f"capture frame error ({self._error_count_since_log}x): {e}"
                    )
                    self._last_error_log_ts = now2
                    self._error_count_since_log = 0

            next_tick += period
            sleep_for = next_tick - time.time()
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                # we're behind — reset cadence to avoid burst-catchup
                next_tick = time.time() + period

    def _capture_one_frame(self) -> None:
        # Fire all 3 SDK requests in parallel — was 3x serial roundtrips
        # (~150-450ms total) on the WebRTC-backed Chrome SDK; now parallel
        # bounded by the slowest single response.
        def _fetch_data():
            try:
                return sdk_get("/data", timeout=3).json()
            except Exception:
                return {}

        def _fetch_front():
            try:
                return sdk_get("/v2/front", timeout=3).json().get("front_frame")
            except Exception:
                return None

        def _fetch_rear():
            try:
                return sdk_get("/v2/rear", timeout=3).json().get("rear_frame")
            except Exception:
                return None

        f_data = self._capture_pool.submit(_fetch_data)
        f_front = self._capture_pool.submit(_fetch_front)
        f_rear = self._capture_pool.submit(_fetch_rear) if self._has_rear else None

        d = f_data.result()
        state_vec = _telemetry_to_state_vec(d)

        front_b64 = f_front.result()
        front_img = (
            _decode_jpeg_to_rgb(front_b64, resize_to=self._target_shape)
            if front_b64 else None
        )
        if front_img is None:
            return  # need at least one frame to record

        rear_img = None
        if f_rear is not None:
            rear_b64 = f_rear.result()
            if rear_b64:
                rear_img = _decode_jpeg_to_rgb(rear_b64, resize_to=self._target_shape)

        # Action (last commanded)
        a = ACTION_STATE.snapshot()
        action_vec = np.array([a["linear"], a["angular"]], dtype=np.float32)  # 2-dim (lamp moved to state)
        # ECoT: seconds since the command was issued (0 if never set).
        _a_ts = a.get("ts", 0.0) or 0.0
        action_age = max(0.0, time.time() - _a_ts) if _a_ts else 0.0
        action_age_vec = np.array([action_age], dtype=np.float32)

        frame: Dict[str, Any] = {
            "observation.images.front": front_img,
            "observation.state": state_vec,
            "action": action_vec,
            "action_age": action_age_vec,
            "task": self._current_task,
        }
        if self._has_rear:
            if rear_img is None:
                # Rear fetch failed this tick — fill with zeros to keep schema
                # consistent (LeRobot requires all features every frame).
                h, w = self._target_shape
                rear_img = np.zeros((h, w, 3), dtype=np.uint8)
            frame["observation.images.rear"] = rear_img

        try:
            self._dataset.add_frame(frame)
            self._episode_frames += 1
        except Exception as e:
            # Rate-limit error spam: count silently, surface 1 line every N seconds
            now = time.time()
            self._error_count_since_log += 1
            self._last_error = f"add_frame: {e}"
            if now - self._last_error_log_ts >= ERROR_LOG_PERIOD_S:
                logger.warning(
                    f"add_frame failed ({self._error_count_since_log}x in last "
                    f"{ERROR_LOG_PERIOD_S}s): {e}"
                )
                self._last_error_log_ts = now
                self._error_count_since_log = 0


# Singleton

_engine: Optional[RecorderEngine] = None
_engine_lock = threading.Lock()


def get_engine(**kwargs) -> RecorderEngine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = RecorderEngine(**kwargs)
    return _engine


def reset_engine() -> None:
    """Tear down the current engine (used on hot-reload / tests)."""
    global _engine
    with _engine_lock:
        if _engine is not None:
            _engine.shutdown()
            _engine = None


# Durability — make sure the dataset finalizes on Ctrl+C / SIGTERM so the
# parquet footers are written and the dataset is reloadable.


import atexit
import signal


def _atexit_finalize() -> None:
    """Called on normal interpreter exit. Finalize the dataset so footers
    are written. Idempotent (RecorderEngine.shutdown handles repeats)."""
    global _engine
    if _engine is not None and _engine._dataset is not None:
        try:
            _engine.shutdown()
        except Exception:
            pass


atexit.register(_atexit_finalize)


def _signal_finalize(signum, frame):
    """Catch SIGINT / SIGTERM, finalize the dataset, then re-raise."""
    _atexit_finalize()
    # Restore the default handler and re-raise so the process actually exits
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


# Only install signal handlers in the main thread of the main interpreter.
# (Importing in a worker thread or under a debugger should not steal them.)
try:
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, _signal_finalize)
        signal.signal(signal.SIGTERM, _signal_finalize)
except (ValueError, RuntimeError):
    # ValueError: signal only works in main thread of the main interpreter
    # RuntimeError: similar; just skip — atexit still covers normal exits
    pass
