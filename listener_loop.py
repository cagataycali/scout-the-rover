"""👂 scout listener — voice-activated agent peer (like telegram/thinker/dashboard).

Always-on background loop that:
  1. Drains the ROVER's microphone (SDK /rover-mic, PCM16 over WebRTC).
  2. Runs energy-based VAD to segment speech (numpy-only; works everywhere).
  3. Transcribes each speech segment with Whisper (faster-whisper preferred,
     openai-whisper fallback; degrades to "audio detected" if neither present).
  4. On a meaningful utterance — optionally gated by a wake phrase — triggers
     the full scout agent with the transcript as ONE input (with live camera
     frames + room map + pose injected, same as agent.py turns).

This makes scout respond to the room's voice without anyone touching a
keyboard. It's the missing "listen loop" — a sibling to thinker_loop.py.

Env:
  ROVER_SDK_URL                 SDK base (default http://localhost:8002 via .env)
  LISTENER_DISABLED=1           exit immediately
  LISTENER_WAKE_WORD="hey scout"  only trigger if transcript contains it
                                  (unset/empty = trigger on any speech)
  LISTENER_MIN_CHARS=4          ignore transcripts shorter than this
  LISTENER_ENERGY_THRESHOLD=0.012  RMS (0..1) above which a frame is "speech"
  LISTENER_SILENCE_SEC=0.8      trailing silence that ends a segment
  LISTENER_MAX_SEG_SEC=12       hard cap on a single utterance
  LISTENER_MIN_SEG_SEC=0.6      drop segments shorter than this
  LISTENER_WHISPER_MODEL=base.en  whisper model id
  LISTENER_COOLDOWN_SEC=2       min gap between agent triggers
  LISTENER_ALLOW_DRIVE=1        (handled by agent persona; informational)
"""
from __future__ import annotations

import base64
import os
import signal
import sys
import time
import traceback
from collections import deque
from datetime import datetime
from typing import List, Optional

import numpy as np
import requests

try:
    import webrtcvad  # optional, more accurate than energy VAD
except Exception:
    webrtcvad = None
from dotenv import load_dotenv

load_dotenv()

from agent import build_agent, build_turn_input, _auto_recorder  # reuse the main agent
import memory as _memory
from tools.voice_bridge import voice_say as _voice_say, push as _voice_push

SDK_URL = os.getenv("ROVER_SDK_URL", "http://localhost:8002").rstrip("/")
DISABLED = os.getenv("LISTENER_DISABLED", "0") in ("1", "true", "True")
WAKE_WORD = (os.getenv("LISTENER_WAKE_WORD", "") or "").strip().lower()
MIN_CHARS = int(os.getenv("LISTENER_MIN_CHARS", "4"))
ENERGY_THRESHOLD = float(os.getenv("LISTENER_ENERGY_THRESHOLD", "0.012"))
SILENCE_SEC = float(os.getenv("LISTENER_SILENCE_SEC", "0.8"))
MAX_SEG_SEC = float(os.getenv("LISTENER_MAX_SEG_SEC", "12"))
MIN_SEG_SEC = float(os.getenv("LISTENER_MIN_SEG_SEC", "0.6"))
WHISPER_MODEL = os.getenv("LISTENER_WHISPER_MODEL", "base.en")
COOLDOWN_SEC = float(os.getenv("LISTENER_COOLDOWN_SEC", "2"))
MIC_RATE = int(os.getenv("LISTENER_MIC_RATE", "16000"))

# Whisper hallucinations on silence/noise — drop these.
NOISE_PHRASES = {
    "", ".", "you", "you.", "thank you", "thank you.", "thanks", "thanks.",
    "bye", "bye.", "the end", "the end.", "okay", "okay.", "so", "so.",
    "hmm", "hmm.", "uh", "uh.", "um", "um.", "♪", "[silence]",
}


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


# ── Transcription backend (lazy, graceful) ──────────────────────────────────
class _Transcriber:
    def __init__(self) -> None:
        self.backend = None
        self.model = None
        self._load()

    def _load(self) -> None:
        # Prefer faster-whisper (CTranslate2, fast on CPU/GPU)
        try:
            from faster_whisper import WhisperModel
            device = "cuda" if os.getenv("LISTENER_CUDA", "auto") != "cpu" else "cpu"
            try:
                self.model = WhisperModel(WHISPER_MODEL, device="cuda",
                                          compute_type="float16")
                self.backend = "faster-whisper(cuda)"
            except Exception:
                self.model = WhisperModel(WHISPER_MODEL, device="cpu",
                                          compute_type="int8")
                self.backend = "faster-whisper(cpu)"
            return
        except Exception:
            pass
        # Fallback: openai-whisper
        try:
            import whisper
            self.model = whisper.load_model(WHISPER_MODEL.replace(".en", "")
                                            if "/" not in WHISPER_MODEL else WHISPER_MODEL)
            self.backend = "openai-whisper"
            return
        except Exception:
            pass
        self.backend = None  # no transcription available

    def transcribe(self, audio_f32: np.ndarray, rate: int) -> str:
        if self.model is None:
            return ""
        try:
            if self.backend and self.backend.startswith("faster-whisper"):
                segments, _ = self.model.transcribe(audio_f32, language="en",
                                                     vad_filter=False)
                return " ".join(s.text for s in segments).strip()
            else:  # openai-whisper
                # whisper wants 16k mono float32
                if rate != 16000:
                    audio_f32 = _resample(audio_f32, rate, 16000)
                res = self.model.transcribe(audio_f32, language="en", fp16=False)
                return (res.get("text") or "").strip()
        except Exception as e:
            print(f"[{_now()}] 👂 transcribe error: {e}", flush=True)
            return ""


def _resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst or len(x) == 0:
        return x
    n = int(round(len(x) * dst / src))
    return np.interp(np.linspace(0, len(x), n, endpoint=False),
                     np.arange(len(x)), x).astype(np.float32)


# ── Rover mic draining ──────────────────────────────────────────────────────
def _mic_start() -> bool:
    try:
        r = requests.post(f"{SDK_URL}/rover-mic/start", json={"rate": MIC_RATE},
                          timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"[{_now()}] 👂 mic start failed: {e}", flush=True)
        return False


def _mic_stop() -> None:
    try:
        requests.post(f"{SDK_URL}/rover-mic/stop", timeout=5)
    except Exception:
        pass


def _mic_drain() -> tuple[int, np.ndarray]:
    """Pull buffered PCM16 chunks → (rate, float32 mono in [-1,1])."""
    try:
        r = requests.get(f"{SDK_URL}/rover-mic", timeout=10)
        if r.status_code != 200:
            return MIC_RATE, np.zeros(0, dtype=np.float32)
        j = r.json()
        rate = int(j.get("rate", MIC_RATE))
        chunks = j.get("chunks", []) or []
        if not chunks:
            return rate, np.zeros(0, dtype=np.float32)
        buf = bytearray()
        for c in chunks:
            buf += base64.b64decode(c)
        pcm = np.frombuffer(bytes(buf), dtype=np.int16).astype(np.float32) / 32768.0
        return rate, pcm
    except Exception:
        return MIC_RATE, np.zeros(0, dtype=np.float32)


# ── VAD segmenter ───────────────────────────────────────────────────────────
class _Segmenter:
    """Energy-based VAD: accumulates speech, emits a segment after trailing silence."""

    def __init__(self, rate: int) -> None:
        self.rate = rate
        # webrtcvad needs 8/16/32/48 kHz and 10/20/30ms frames; use 30ms.
        self._vad = None
        if webrtcvad is not None and rate in (8000, 16000, 32000, 48000):
            try:
                self._vad = webrtcvad.Vad(int(os.getenv('LISTENER_VAD_AGGRESSIVENESS', '2')))
            except Exception:
                self._vad = None
        self.in_speech = False
        self.buf: List[np.ndarray] = []
        self.silence_run = 0.0
        self.speech_dur = 0.0

    def feed(self, samples: np.ndarray) -> Optional[np.ndarray]:
        if len(samples) == 0:
            return None
        # frame in ~30ms windows for energy decisions
        win = max(1, int(self.rate * 0.03))
        emitted = None
        for i in range(0, len(samples), win):
            frame = samples[i:i + win]
            if len(frame) == 0:
                continue
            dt = len(frame) / self.rate
            if self._vad is not None and len(frame) == win:
                try:
                    pcm16 = (np.clip(frame, -1, 1) * 32767).astype(np.int16).tobytes()
                    is_voiced = self._vad.is_speech(pcm16, self.rate)
                except Exception:
                    is_voiced = float(np.sqrt(np.mean(frame * frame))) >= ENERGY_THRESHOLD
            else:
                is_voiced = float(np.sqrt(np.mean(frame * frame))) >= ENERGY_THRESHOLD
            if is_voiced:
                self.in_speech = True
                self.buf.append(frame)
                self.speech_dur += dt
                self.silence_run = 0.0
                if self.speech_dur >= MAX_SEG_SEC:
                    emitted = self._emit()
            elif self.in_speech:
                self.buf.append(frame)        # keep trailing silence in the seg
                self.silence_run += dt
                if self.silence_run >= SILENCE_SEC:
                    emitted = self._emit()
        return emitted

    def _emit(self) -> Optional[np.ndarray]:
        if not self.buf:
            self._reset()
            return None
        seg = np.concatenate(self.buf)
        dur = len(seg) / self.rate
        self._reset()
        if dur < MIN_SEG_SEC:
            return None
        return seg

    def _reset(self) -> None:
        self.in_speech = False
        self.buf = []
        self.silence_run = 0.0
        self.speech_dur = 0.0


def _strip_wake(text: str) -> str:
    """If a wake word is set and present, return only the text AFTER it."""
    if not WAKE_WORD:
        return text.strip()
    low = text.lower()
    idx = low.find(WAKE_WORD)
    if idx < 0:
        return ""
    after = text[idx + len(WAKE_WORD):].strip(" ,.:;!?-")
    return after or text.strip()


def _meaningful(text: str) -> bool:
    t = text.strip().lower()
    if len(t) < MIN_CHARS:
        return False
    if t in NOISE_PHRASES:
        return False
    if WAKE_WORD and WAKE_WORD not in t:
        return False
    return True


def main() -> None:
    print(f"👂 scout listener starting (sdk={SDK_URL}, wake={'<'+WAKE_WORD+'>' if WAKE_WORD else 'ANY speech'})")
    if DISABLED:
        print("LISTENER_DISABLED=1 — exiting")
        return

    stop = {"flag": False}

    def _sig(*_):
        stop["flag"] = True
        print(f"\n[{_now()}] 👂 stop requested", flush=True)

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    transcriber = _Transcriber()
    if transcriber.backend:
        print(f"[{_now()}] 👂 transcription: {transcriber.backend} (model={WHISPER_MODEL})", flush=True)
    else:
        print(f"[{_now()}] ⚠️  no whisper backend (pip install faster-whisper) — "
              f"will only DETECT voice, not transcribe", flush=True)

    try:
        agent = build_agent()
        print(f"[{_now()}] 👂 listener agent built", flush=True)
    except Exception as e:
        print(f"❌ failed to build listener agent: {e}")
        traceback.print_exc()
        sys.exit(1)

    # Wait for SDK + rover audio track, then start mic
    for _ in range(15):
        if stop["flag"]:
            return
        if _mic_start():
            print(f"[{_now()}] 👂 rover mic capture started @ {MIC_RATE}Hz", flush=True)
            break
        time.sleep(2)
    else:
        print(f"[{_now()}] ⚠️  could not start rover mic (no audio track?) — retrying in loop", flush=True)

    seg = _Segmenter(MIC_RATE)
    last_trigger = 0.0

    while not stop["flag"]:
        try:
            rate, pcm = _mic_drain()
            if rate != seg.rate:
                seg = _Segmenter(rate)
            utterance = seg.feed(pcm)
            if utterance is not None:
                dur = len(utterance) / rate
                if transcriber.backend:
                    text = transcriber.transcribe(utterance, rate)
                else:
                    text = ""  # detection-only mode
                if text:
                    print(f"[{_now()}] 👂 heard ({dur:.1f}s): {text!r}", flush=True)
                if transcriber.backend and not _meaningful(text):
                    continue
                if (time.time() - last_trigger) < COOLDOWN_SEC:
                    continue
                last_trigger = time.time()
                spoken = _strip_wake(text) if transcriber.backend else text
                _trigger_agent(agent, spoken, dur)
        except Exception as e:
            print(f"[{_now()}] ❌ listen loop error: {e}", flush=True)
            traceback.print_exc()
        time.sleep(0.15)

    _mic_stop()
    print(f"[{_now()}] 👋 listener stopped")


def _trigger_agent(agent, text: str, dur: float) -> None:
    """Fire the full scout agent with the heard utterance as one input."""
    if text:
        user_turn = f'[Heard over rover mic]: "{text}"'
        rec_task = text[:120]
    else:
        # detection-only: tell scout someone spoke nearby
        user_turn = (f"[Heard voice over rover mic ~{dur:.1f}s but no transcription "
                     f"backend]. Someone is speaking near you — look toward the sound "
                     f"and greet them.")
        rec_task = "voice detected (no transcript)"

    print(f"[{_now()}] 👂 → triggering agent: {user_turn[:80]}", flush=True)
    try:
        agent.messages.clear()
    except Exception:
        pass
    try:
        agent.system_prompt = build_agent().system_prompt  # refresh live state/pose/map
    except Exception:
        pass

    _auto_recorder.begin_turn(rec_task)
    result = None
    try:
        result = agent(build_turn_input(user_turn, camera="both"))
        text_out = str(result)[:1000]
        print(f"[{_now()}] 👂 ← {text_out[:240]}", flush=True)
    except Exception as e:
        print(f"[{_now()}] ❌ agent trigger error: {e}", flush=True)
        traceback.print_exc()
    finally:
        try:
            _auto_recorder.end_turn()
        except Exception:
            pass
        try:
            _memory.record_turn(f"[listener] {text or 'voice'}", result)
        except Exception:
            pass
        try:
            _voice_push("listener", (text or "voice detected")[:200], importance=1)
        except Exception:
            pass


if __name__ == "__main__":
    main()
