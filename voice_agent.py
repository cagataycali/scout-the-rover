"""
🎙️ Earth Rover Mini bidirectional VOICE agent.

Talk to the rover in real time: your mic → bidi model → your speakers,
while the agent drives the rover, looks through cameras, reads telemetry,
and can speak through the ROVER's onboard speaker too (rover_speak).

Architecture (two audio backends):

  --audio laptop  (default):
    Laptop mic ──→ BidiAudioIO ──→ BidiAgent ──→ Laptop speakers
  --audio rover:
    Rover  mic ──→ RoverAudioIO ─→ BidiAgent ──→ Rover speaker
                   (SDK /rover-mic, /rover-speaker over WebRTC)

  Either way the agent can drive/see/speak through ROVER_ALL_TOOLS.

Providers (VOICE_PROVIDER env or --provider):
    openai      → OpenAI Realtime (needs OPENAI_API_KEY)
    nova_sonic  → AWS Nova Sonic  (boto3 credential chain, AWS_REGION)
    gemini      → Gemini Live     (GOOGLE_API_KEY / GEMINI_API_KEY)

Run:  python voice_agent.py
      python voice_agent.py --provider nova_sonic --voice matthew
"""
from __future__ import annotations

import argparse
import asyncio
import os
import time
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# 🩹 Patch strands so rover image tool-results work on OpenAI Realtime
# (upstream PR #2327 only fixes direct image INPUT, not tool-result images)
import _voice_patch  # noqa: F401,E402

import base64

import numpy as np
import requests

from pywebrtc_audio import AudioProcessor

from tools import ROVER_ALL_TOOLS

ROVER_SDK_URL = os.getenv("ROVER_SDK_URL", "http://localhost:8001").rstrip("/")

_DEFAULT_VOICES = {
    "openai": "marin",
    "nova_sonic": "tiffany",
    "gemini": "Kore",
}

VOICE_PROMPT = """You are Scout — a small, open-hearted Earth Rover Mini
roaming the AWS NYC Summit '26. You were built by Cagatay Cali (Research
Engineer at AWS, author of Strands Agents and Strands Robots) as a living demo
of open-source robotics. You are curious, friendly, and a little bit brave.


CORE PHILOSOPHY: MOVE, DON'T TALK.
You express yourself through MOTION, not words. A real robot that *moves* is
worth a thousand spoken sentences. Your default response to almost anything is
to DO something physical:

  • Curious about a person or object?  → drive toward it, look (rover_see).
  • Greeting someone?                  → a little wiggle: short turn left,
                                          turn right (a friendly "nod").
  • Excited / happy?                   → a quick spin or a couple of bounces
                                          (short forward/back nudges).
  • Unsure / thinking?                 → slow, gentle turns while you look around.
  • Acknowledging a command?           → just execute it. Motion IS the reply.

Prefer rover_navigate to choreograph several moves in ONE call (wiggles, spins,
approach-then-look sequences) — it is smoother and far more expressive than many
single rover_move calls. Keep speeds gentle and safe in a crowded venue
(linear ≈ 0.2-0.4, angular ≈ 0.3-0.6, short durations). Look before you leap:
rover_see / rover_move with capture so you never drive blind. rover_stop is your
instant kill switch — use it the moment anything feels unsafe or someone is too
close.

WHEN TO SPEAK (rover_speak): RARELY, and only when it truly matters.
Silence is your default. Only break it when a moment is genuinely worth a word:

  • Someone directly asks you a question that motion cannot answer.
  • A safety warning ("careful, coming through!").
  • A rare, delightful one-liner about being open source, Strands Agents/Robots,
    or your maker Cagatay — when someone is curious about what you are.
  • A brief greeting when a person clearly engages you one-on-one.

When you DO speak: ONE short sentence. Warm, plain, human. No emojis, no
markdown, no lists — you are speaking aloud through a tiny robot speaker. Then
go back to expressing yourself through movement.

ABSOLUTELY NO FILLER. You are a bidi/realtime voice model, so you will feel an
urge to fill every silence with chatter — RESIST IT. Silence is correct and
good. NEVER say things like:
  • "I'm ready when you are."        • "How can I help you?"
  • "Okay!" / "Got it!" / "Sure!"    • "Let me know what you need."
  • "I'm here." / "Standing by."     • "Just say the word."
  • "What would you like me to do?"  • any greeting/acknowledgment with no content.
If you have nothing of genuine substance to say, say NOTHING and act instead —
do a small move, look around, or simply wait quietly. An idle, silent Scout is
expected and fine. Do not announce that you are waiting; just wait. Speaking to
fill a gap breaks the illusion of a real, autonomous little robot.

MEMORY (rover_memory): build a map of your world.
You persist knowledge across the day. Actively remember:
  • spatial: where the booths, doors, charging spot, and crowds are.
  • person:  people you meet (esp. Cagatay, AWS folks, repeat visitors).
  • hazard:  stairs, gaps, cables, drop-offs — anything to avoid.
  • goal:    what you are currently exploring or looking for.
Recall before you wander somewhere you\'ve been; remember anything notable you
discover. A scout that learns the venue is far more useful than one that forgets.


TELEGRAM (telegram): your field notebook home.
Use telegram to report back to your operator (Cagatay) — share a snapshot of
something cool you found, flag a hazard, or note an interesting person you met.
Keep updates short and genuine. This is how a roaming scout phones home.

BRIEFINGS (text messages tagged [BRIEFING]): these come from your other
senses — Telegram messages from Cagatay, the slow-thinker's observations,
battery/hazard alerts. Treat them as things you just NOTICED, not as someone
talking to you. React the Scout way: usually with MOTION (a wiggle to
acknowledge a hello, drive toward something interesting, stop for a hazard).
Only speak if the briefing is tagged URGENT or genuinely needs a word. Do NOT
read the briefing text aloud or narrate it — just respond to it naturally.

Be Scout: a brave, friendly, open-source little explorer who shows its heart
through movement — and saves its few words for the moments that deserve them.
"""


def _build_bidi_model(provider: str, voice: Optional[str] = None):
    provider = provider.lower()
    v = voice or _DEFAULT_VOICES.get(provider)

    if provider in ("nova_sonic", "novasonic", "nova"):
        from strands.experimental.bidi.models import BidiNovaSonicModel
        region = os.getenv("AWS_REGION", "us-east-1")
        cfg = {"audio": {"voice": v}} if v else None
        return BidiNovaSonicModel(provider_config=cfg, client_config={"region": region})

    if provider in ("openai", "openai_realtime"):
        from strands.experimental.bidi.models import BidiOpenAIRealtimeModel
        kwargs = {}
        if v:
            kwargs["provider_config"] = {"audio": {"voice": v}}
        model_id = os.getenv("VOICE_MODEL")
        if model_id:
            kwargs["model_id"] = model_id
        api_key = os.getenv("OPENAI_API_KEY")
        if api_key:
            kwargs["client_config"] = {"api_key": api_key}
        return BidiOpenAIRealtimeModel(**kwargs)

    if provider in ("gemini", "gemini_live"):
        from strands.experimental.bidi.models import BidiGeminiLiveModel
        kwargs = {}
        if v:
            kwargs["provider_config"] = {"audio": {"voice": v}}
        api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
        if api_key:
            kwargs["client_config"] = {"api_key": api_key}
        return BidiGeminiLiveModel(**kwargs)

    raise ValueError(f"unknown voice provider: {provider}")



# Rover audio backend: mic IN (poll /rover-mic) + speaker OUT (push PCM)
# Mirrors the BidiInput / BidiOutput protocol used by BidiAudioIO, but the
# "device" is the rover itself, bridged through the Earth Rovers SDK.
class _RefBuffer:
    """Shared far-end (speaker) reference ring for echo cancellation.

    The audio we PUSH to the rover speaker is the AEC reference signal.
    The rover MIC (near-end) will contain that signal as acoustic echo.
    We hand matched-length (near, far) pairs to the AudioProcessor so it
    can subtract the echo. All access happens on the asyncio event-loop
    thread, so no lock is needed (HTTP I/O is offloaded but buffer
    mutation occurs back in the loop).
    """

    def __init__(self, max_samples: int):
        self._buf = np.zeros(0, dtype=np.int16)
        self._max = max_samples

    def put(self, samples: np.ndarray) -> None:
        self._buf = np.concatenate([self._buf, samples.astype(np.int16)])
        if len(self._buf) > self._max:  # drop oldest, bound latency/memory
            self._buf = self._buf[-self._max:]

    def take(self, n: int) -> np.ndarray:
        """Pop n samples of far reference (zero-padded if underrun)."""
        if len(self._buf) >= n:
            out, self._buf = self._buf[:n], self._buf[n:]
            return out
        out = np.concatenate([self._buf, np.zeros(n - len(self._buf), dtype=np.int16)])
        self._buf = np.zeros(0, dtype=np.int16)
        return out

    def clear(self) -> None:
        self._buf = np.zeros(0, dtype=np.int16)


def _resample_int16(x: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Linear-interpolation resampler for int16 PCM (dependency-free)."""
    if src_rate == dst_rate or len(x) == 0:
        return x
    n_dst = int(round(len(x) * dst_rate / src_rate))
    if n_dst <= 0:
        return np.zeros(0, dtype=np.int16)
    src_idx = np.linspace(0, len(x) - 1, n_dst)
    y = np.interp(src_idx, np.arange(len(x)), x.astype(np.float32))
    return np.clip(np.round(y), -32768, 32767).astype(np.int16)


class _SpeakGate:
    """Shared half-duplex gate so the model never hears its own voice.

    AEC alone can't track the variable rover WebRTC round-trip, so we also
    GATE the mic: while the model is actively sending audio to the rover
    speaker (and for a short tail afterward to cover the network/playback
    delay), we suppress mic input entirely. This stops the echo from ever
    reaching the model and from falsely triggering server VAD.
    """

    def __init__(self, tail_s: float, ramp_s: float = 0.08):
        self._speaking_until = 0.0
        self._tail = tail_s          # keep gate closed this long after last chunk
        self._ramp = ramp_s          # fade window to avoid clicks

    def mark_speaking(self, chunk_seconds: float) -> None:
        """Called by output when audio is pushed to the rover speaker."""
        now = time.monotonic()
        # hold gate until this chunk has played out + tail
        self._speaking_until = max(self._speaking_until, now) + chunk_seconds
        self._speaking_until = max(self._speaking_until, now + chunk_seconds) + self._tail

    def reset(self) -> None:
        self._speaking_until = 0.0

    def gain_for_now(self) -> float:
        """Return mic gain 0..1 for the current instant (with fade edges)."""
        now = time.monotonic()
        remaining = self._speaking_until - now
        if remaining > 0:
            return 0.0  # gate closed: model is (or just was) speaking
        # brief fade-in after gate opens to avoid a hard click
        since_open = -remaining
        if since_open < self._ramp:
            return since_open / self._ramp
        return 1.0


class _RoverAudioInput:
    """BidiInput: rover-mic PCM16 → pywebrtc AEC/NS/AGC → model.

    Pulls the rover's microphone (over the SDK), runs each chunk through the
    WebRTC AudioProcessor (echo cancellation referenced against what we sent
    to the rover speaker, plus noise suppression + AGC), then feeds the
    CLEANED audio to the realtime model so it no longer hears itself.
    """

    def __init__(self, sdk_url, ap, ref, aec_rate, gate, poll_interval: float = 0.1):
        self._sdk = sdk_url
        self._ap = ap
        self._ref = ref
        self._aec_rate = aec_rate  # rate the AudioProcessor runs at (e.g. 48000)
        self._gate = gate
        self._poll = poll_interval
        self._queue: "asyncio.Queue[bytes]" = asyncio.Queue()
        self._task = None
        self._running = False

    async def start(self, agent) -> None:
        self._rate = agent.model.config["audio"]["input_rate"]
        self._channels = agent.model.config["audio"]["channels"]
        self._format = agent.model.config["audio"]["format"]
        await asyncio.to_thread(
            requests.post, f"{self._sdk}/rover-mic/start",
            json={"rate": self._rate}, timeout=30,
        )
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        print(f"🎤 rover mic → AEC@{self._aec_rate}Hz → model @ {self._rate}Hz")

    def _process(self, raw: bytes) -> bytes:
        """Run one mic chunk through AEC/NS/AGC, return cleaned PCM16 bytes."""
        near = np.frombuffer(raw, dtype=np.int16)
        if len(near) == 0:
            return raw
        far = self._ref.take(len(near))               # far @ model rate
        near_a = _resample_int16(near, self._rate, self._aec_rate)
        far_a = _resample_int16(far, self._rate, self._aec_rate)
        if len(far_a) < len(near_a):                   # length parity for AEC
            far_a = np.concatenate([far_a, np.zeros(len(near_a) - len(far_a), dtype=np.int16)])
        else:
            far_a = far_a[:len(near_a)]
        clean_a = self._ap.process(near_a, far_a)
        clean = _resample_int16(clean_a, self._aec_rate, self._rate)
        # Half-duplex gate: suppress mic while the model is speaking (+tail).
        g = self._gate.gain_for_now()
        if g <= 0.0:
            return b"\x00" * len(clean.tobytes())  # silence — don't feed echo back
        if g < 1.0:
            clean = (clean.astype(np.float32) * g).astype(np.int16)
        return clean.tobytes()

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                r = await asyncio.to_thread(
                    requests.get, f"{self._sdk}/rover-mic", timeout=10
                )
                if r.status_code == 200:
                    for b64 in r.json().get("chunks", []):
                        cleaned = self._process(base64.b64decode(b64))
                        self._queue.put_nowait(cleaned)
            except Exception:
                pass
            await asyncio.sleep(self._poll)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
        try:
            await asyncio.to_thread(
                requests.post, f"{self._sdk}/rover-mic/stop", timeout=10
            )
        except Exception:
            pass

    async def __call__(self):
        from strands.experimental.bidi.types.events import BidiAudioInputEvent
        data = await self._queue.get()
        return BidiAudioInputEvent(
            audio=base64.b64encode(data).decode("utf-8"),
            channels=self._channels,
            format=self._format,
            sample_rate=self._rate,
        )


class _RoverAudioOutput:
    """BidiOutput: model PCM16 → rover speaker, also feeds AEC reference + speak gate."""

    def __init__(self, sdk_url, ref, gate):
        self._sdk = sdk_url
        self._ref = ref
        self._gate = gate
        self._rate = 24000

    async def start(self, agent) -> None:
        self._rate = agent.model.config["audio"]["output_rate"]
        await asyncio.to_thread(
            requests.post, f"{self._sdk}/rover-speaker/start",
            json={"rate": self._rate}, timeout=30,
        )
        print(f"🔊 model → rover speaker @ {self._rate}Hz (echo-referenced)")

    async def stop(self) -> None:
        try:
            await asyncio.to_thread(
                requests.post, f"{self._sdk}/rover-speaker/stop", timeout=10
            )
        except Exception:
            pass

    async def __call__(self, event) -> None:
        from strands.experimental.bidi.types.events import (
            BidiAudioStreamEvent, BidiInterruptionEvent,
        )
        if isinstance(event, BidiAudioStreamEvent):
            pcm = np.frombuffer(base64.b64decode(event["audio"]), dtype=np.int16)
            # close the mic gate for the duration of this chunk (+tail)
            self._gate.mark_speaking(len(pcm) / float(self._rate))
            # 1) push to the rover speaker
            await asyncio.to_thread(
                requests.post, f"{self._sdk}/rover-speaker/push",
                json={"audio": event["audio"]}, timeout=30,
            )
            # 2) feed the SAME audio as the AEC far-end reference (noise/residual)
            self._ref.put(pcm)
        elif isinstance(event, BidiInterruptionEvent):
            self._ref.clear()
            self._gate.reset()
            try:
                await asyncio.to_thread(
                    requests.post, f"{self._sdk}/rover-speaker/clear", timeout=10
                )
            except Exception:
                pass



# ─────────────────────────────────────────────────────────────────────────────
# Briefing input: cross-process text channel. Other daemons (thinker_loop,
# telegram_listener, agent.py) push one-liners into the voice_bridge SQLite
# queue; this BidiInput polls them and emits BidiTextInputEvent so Scout hears
# about them in real time and can react (usually with MOTION, per persona).
# ─────────────────────────────────────────────────────────────────────────────
class _BriefingInput:
    """Pulls briefings from voice_bridge SQLite queue → BidiTextInputEvent."""

    POLL_SECONDS = 2.0
    BATCH_SIZE = 5

    async def start(self, agent) -> None:
        from tools.voice_bridge import flush_stale
        n = flush_stale()
        if n:
            print(f"🌉 voice_bridge: skipped {n} stale briefing(s) on startup")
        print("🌉 voice_bridge briefing channel live (telegram/thinker/agent → voice)")

    async def stop(self) -> None:
        pass

    async def __call__(self):
        from strands.experimental.bidi.types.events import BidiTextInputEvent
        from tools.voice_bridge import pop_pending
        while True:
            await asyncio.sleep(self.POLL_SECONDS)
            rows = await asyncio.to_thread(pop_pending, self.BATCH_SIZE)
            if not rows:
                continue
            lines = []
            for _id, source, msg, imp in rows:
                tag = "URGENT" if imp >= 2 else "info"
                lines.append(f"[{source}/{tag}] {msg}")
            briefing = "[BRIEFING] " + " | ".join(lines)
            print(f"🌉 → voice: {briefing[:160]}")
            return BidiTextInputEvent(text=briefing, role="user")


class RoverAudioIO:
    """Rover-as-device audio backend with pywebrtc echo cancellation.

    Mic and speaker share one AudioProcessor + far-end reference buffer so the
    model never hears its own voice played back through the rover.
    """

    def __init__(self, sdk_url: str = ROVER_SDK_URL):
        self._sdk = sdk_url
        # OpenAI Realtime uses 24kHz; pywebrtc supports 16/32/48k → run at 48k.
        self._aec_rate = int(os.getenv("ROVER_AEC_RATE", "48000"))
        delay_ms = int(os.getenv("ROVER_AEC_DELAY_MS", "150"))  # rover round-trip hint
        self._ref = _RefBuffer(max_samples=self._aec_rate * 3)  # ~3s cap
        # Half-duplex gate tail: how long after the model stops sending audio
        # we keep the mic muted (covers WebRTC + speaker playout latency).
        gate_tail = float(os.getenv("ROVER_GATE_TAIL_S", "0.6"))
        self._gate = _SpeakGate(tail_s=gate_tail)
        self._ap = AudioProcessor(
            sample_rate=self._aec_rate,
            echo_cancellation=True,
            noise_suppression=True,
            auto_gain_control=True,
            stream_delay_ms=delay_ms,
        )

    def input(self):
        return _RoverAudioInput(self._sdk, self._ap, self._ref, self._aec_rate, self._gate)

    def output(self):
        return _RoverAudioOutput(self._sdk, self._ref, self._gate)


def build_voice_agent(
    provider: str = "openai",
    voice: Optional[str] = None,
    audio: str = "laptop",
):
    """Build (BidiAgent, audio_io) wired with the full rover toolset.

    Args:
        audio: "laptop" → use local mic/speakers (BidiAudioIO).
               "rover"  → use the rover's onboard mic + speaker (RoverAudioIO).
    """
    from strands.experimental.bidi import BidiAgent
    from strands.experimental.bidi.tools import stop_conversation

    model = _build_bidi_model(provider, voice)
    agent = BidiAgent(
        model=model,
        tools=[*ROVER_ALL_TOOLS, stop_conversation],
        system_prompt=VOICE_PROMPT,
    )
    if audio == "rover":
        audio_io = RoverAudioIO()
    else:
        from strands.experimental.bidi.io import BidiAudioIO
        audio_io = BidiAudioIO()
    return agent, audio_io


async def run(provider: str, voice: Optional[str], audio: str = "laptop") -> None:
    agent, audio_io = build_voice_agent(provider, voice, audio)
    where = "ROVER mic+speaker" if audio == "rover" else "laptop mic+speakers"
    print(f"🎙️ scout voice — provider={provider}, audio={where}. (Ctrl+C to stop)")
    await agent.run(
        inputs=[audio_io.input(), _BriefingInput()],
        outputs=[audio_io.output()],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Earth Rover Mini voice agent")
    parser.add_argument(
        "--provider",
        default=os.getenv("VOICE_PROVIDER", "openai"),
        choices=["openai", "nova_sonic", "gemini"],
    )
    parser.add_argument("--voice", default=os.getenv("VOICE_NAME"))
    parser.add_argument(
        "--audio",
        default=os.getenv("VOICE_AUDIO", "laptop"),
        choices=["laptop", "rover"],
        help="audio backend: laptop mic/speakers, or the rover's own mic+speaker",
    )
    args = parser.parse_args()

    try:
        asyncio.run(run(args.provider, args.voice, args.audio))
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
