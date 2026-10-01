"""The voice persona on strands-agents >= 1.57.1 — no network, no audio device.

Thor's rebuilt image pulled 1.57.1 and scout-slim-voice restart-looped on
``OpenAIRealtimeModel.__init__() missing 1 required keyword-only argument:
'transcription_model_id'``. These cells pin the 1.57 shapes every voice
adapter now speaks (AudioDelta / TextBlock in, bidi_audio_delta /
bidi_barge_in out) and keep tools/_bidi_compat the ONE module that imports
a strands bidi path.
"""
from __future__ import annotations

import asyncio
import base64
import importlib
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

pytest.importorskip("strands")
from tools import _bidi_compat as bidi  # noqa: E402


@pytest.fixture
def fake_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-not-a-real-key")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("VOICE_MODEL", raising=False)
    monkeypatch.delenv("VOICE_TRANSCRIPTION_MODEL", raising=False)
    monkeypatch.setenv("AWS_REGION", "eu-west-1")


@pytest.fixture
def va(fake_env):
    """voice_agent imported with the real pywebrtc-audio, or a stub AudioProcessor when the wheel is absent."""
    try:
        import pywebrtc_audio  # noqa: F401
    except ImportError:
        import types
        stub = types.ModuleType("pywebrtc_audio")

        class AudioProcessor:  # the test never processes audio
            def __init__(self, **kw):
                self.kw = kw

            def process(self, near, far):
                return near

        stub.AudioProcessor = AudioProcessor
        sys.modules["pywebrtc_audio"] = stub
    return importlib.import_module("voice_agent")


# ── the compat surface ─────────────────────────────────────────────────────
def test_compat_resolves_a_bidi_package_and_the_agent():
    assert bidi.BIDI_PACKAGE in ("strands.bidi", "strands.experimental.bidi")
    assert bidi.IS_STABLE == (bidi.BIDI_PACKAGE == "strands.bidi")
    assert bidi.BidiAgent is importlib.import_module(bidi.BIDI_PACKAGE).BidiAgent
    for name in ("send", "run", "start", "stop"):
        assert hasattr(bidi.BidiAgent, name)


def test_installed_strands_is_in_the_supported_window():
    import importlib.metadata as md
    from packaging.version import Version
    v = Version(md.version("strands-agents"))
    assert Version("1.57.1") <= v < Version("1.60"), v


def test_send_types_are_the_core_blocks():
    from strands.types.content import TextBlock
    from strands.types.media import ImageBlock
    assert bidi.TextBlock is TextBlock and bidi.ImageBlock is ImageBlock
    d = bidi.AudioDelta(format="pcm", source={"bytes": b"\x00\x00"})
    assert d.to_dict() == {"audio_delta": {"format": "pcm", "source": {"bytes": b"\x00\x00"}}}
    assert bidi.TextBlock("hi").to_dict() == {"text": "hi"}


def test_old_names_are_gone_and_fail_loudly():
    """1.56 input events do not exist in 1.57: an import of them must raise, never alias silently."""
    events = importlib.import_module(f"{bidi.BIDI_PACKAGE}.types.events")
    for old in ("BidiTextInputEvent", "BidiAudioInputEvent", "BidiAudioStreamEvent", "BidiInterruptionEvent"):
        assert not hasattr(events, old), f"{old} still exists: the installed strands is not 1.57"
        with pytest.raises(ImportError):
            exec(f"from {bidi.BIDI_PACKAGE}.types.events import {old}")
    assert not hasattr(bidi, "BidiTextInputEvent")


def test_event_names_exist_in_the_installed_strands():
    ev = importlib.import_module(f"{bidi.BIDI_PACKAGE}.types.events")
    src = Path(ev.__file__).read_text(encoding="utf-8")
    for name, value in vars(bidi.EVENTS).items():
        assert f'"{value}"' in src, f"{name}={value} is not emitted by {ev.__file__}"
    assert bidi.event_type({"type": "bidi_audio_delta"}) == bidi.EVENTS.AUDIO_DELTA
    assert bidi.event_type("not an event") is None
    assert bidi.event_type({"no": "type"}) is None


def test_model_classes_and_defaults():
    assert bidi.model_class("openai").__name__ == "OpenAIRealtimeModel"
    assert bidi.model_class("openai_realtime") is bidi.model_class("openai")
    assert bidi.model_class("nova").__name__ == "BedrockNovaSonicModel"
    assert bidi.model_class("gemini_live").__name__ == "GoogleGeminiLiveModel"
    assert bidi.canonical_provider("novasonic") == "nova_sonic"
    assert bidi.canonical_provider("GEMINI_LIVE") == "gemini"
    assert set(bidi.DEFAULT_MODEL_IDS) == set(bidi.PROVIDERS)
    with pytest.raises(ValueError):
        bidi.model_class("siri")
    with pytest.raises(ValueError):
        bidi.canonical_provider("siri")


class _Ctx:
    def __init__(self, agent=None):
        self.invocation_state = {}
        self.agent = agent


def test_stop_conversation_sets_the_1_57_flag_and_cancels_when_possible():
    class _Agent:
        cancelled = 0

        def cancel(self):
            self.cancelled += 1

    assert bidi.stop_conversation.tool_spec["name"] == "stop_conversation"
    ctx = _Ctx(_Agent())
    assert bidi.stop_conversation._tool_func(ctx) == "Ending conversation"
    assert ctx.invocation_state["request_state"]["stop_event_loop"] is True   # 1.57.1 loop reads this
    assert ctx.agent.cancelled == 1                                           # main (#4664) reads this
    ctx2 = _Ctx(object())                                                     # 1.57.1 BidiAgent has no cancel()
    assert bidi.stop_conversation._tool_func(ctx2) == "Ending conversation"
    assert ctx2.invocation_state["request_state"]["stop_event_loop"] is True


def test_stop_conversation_is_not_a_documented_rover_tool():
    """Built with tool(...)(fn) so scripts/tooldoc.py's @tool AST scan never lists it."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import tooldoc
    inv = tooldoc.inventory()
    names = {t["name"] for m in inv.values() for t in m["tools"]}
    assert "stop_conversation" not in names
    assert "_bidi_compat" not in inv


# ── the model builders ─────────────────────────────────────────────────────
def test_openai_builder_passes_the_required_1_57_keywords(va, monkeypatch):
    m = va._build_bidi_model("openai")
    assert type(m).__name__ == "OpenAIRealtimeModel"
    assert m.get_config()["model_id"] == bidi.DEFAULT_MODEL_IDS["openai"]
    assert m._transcription_model_id == bidi.DEFAULT_OPENAI_TRANSCRIPTION_MODEL
    assert m._voice == va._DEFAULT_VOICES["openai"]
    cfg = m.get_audio_config()
    assert cfg["input"]["format"] == "pcm" and cfg["output"]["sample_rate"] == 24000

    monkeypatch.setenv("VOICE_MODEL", "gpt-realtime-2")
    monkeypatch.setenv("VOICE_TRANSCRIPTION_MODEL", "off")
    m2 = va._build_bidi_model("openai_realtime", voice="shimmer")
    assert m2.get_config()["model_id"] == "gpt-realtime-2"
    assert m2._transcription_model_id is None
    assert m2._voice == "shimmer"


def test_nova_and_gemini_builders_construct(va):
    nova = va._build_bidi_model("nova_sonic", voice="tiffany")
    assert type(nova).__name__ == "BedrockNovaSonicModel"
    assert nova.get_config()["model_id"] == bidi.DEFAULT_MODEL_IDS["nova_sonic"]
    gem = va._build_bidi_model("gemini")
    assert type(gem).__name__ == "GoogleGeminiLiveModel"
    assert gem.get_config()["model_id"] == bidi.DEFAULT_MODEL_IDS["gemini"]
    with pytest.raises(ValueError, match="unknown voice provider"):
        va._build_bidi_model("siri")


# ── the input / output adapters ────────────────────────────────────────────
class _FakeModel:
    def __init__(self, rate=24000):
        self._rate = rate

    def get_audio_config(self):
        return {"input": {"sample_rate": self._rate, "channels": 1, "format": "pcm"},
                "output": {"sample_rate": self._rate, "channels": 1, "format": "pcm"}}


class _FakeAgent:
    def __init__(self, rate=24000):
        self.model = _FakeModel(rate)


class _Posts:
    """requests.post/get stand-in: records every call, answers 200."""
    def __init__(self):
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append(("post", url.split("/", 3)[-1], json))
        return type("R", (), {"status_code": 200})()

    def get(self, url, timeout=None):
        self.calls.append(("get", url.split("/", 3)[-1], None))
        return type("R", (), {"status_code": 204, "json": lambda self: {}})()


def test_rover_audio_input_returns_an_audio_delta(va, monkeypatch):
    posts = _Posts()
    monkeypatch.setattr(va.requests, "post", posts.post)
    monkeypatch.setattr(va.requests, "get", posts.get)
    gate = va._SpeakGate(tail_s=0.1)
    ref = va._RefBuffer(max_samples=48000)
    ap = va.AudioProcessor(sample_rate=48000, echo_cancellation=True, noise_suppression=True,
                           auto_gain_control=True, stream_delay_ms=1)
    inp = va._RoverAudioInput("http://sdk", ap, ref, 48000, gate, poll_interval=0.01)

    async def go():
        await inp.start(_FakeAgent(24000))
        inp._queue.put_nowait(b"\x01\x00" * 480)
        item = await inp()
        await inp.stop()
        return item

    item = asyncio.run(go())
    assert isinstance(item, bidi.AudioDelta)
    assert item.format == "pcm" and item.source["bytes"] == b"\x01\x00" * 480
    assert ("post", "rover-mic/start", {"rate": 24000}) in posts.calls


def test_rover_audio_output_plays_audio_delta_and_flushes_on_barge_in(va, monkeypatch):
    posts = _Posts()
    monkeypatch.setattr(va.requests, "post", posts.post)
    gate = va._SpeakGate(tail_s=0.1)
    ref = va._RefBuffer(max_samples=48000)
    out = va._RoverAudioOutput("http://sdk", ref, gate)
    pcm = b"\x10\x00" * 2400                       # 0.1 s at 24 kHz
    b64 = base64.b64encode(pcm).decode()

    async def go():
        await out.start(_FakeAgent(24000))
        await out({"type": "bidi_audio_delta", "audio": b64, "format": "pcm", "sample_rate": 24000, "channels": 1})
        assert gate.gain_for_now() == 0.0                      # mic closed while speaking
        assert ref.take(2400).tolist() == [16] * 2400          # far-end reference fed
        await out({"type": "bidi_transcript_delta", "delta": "x", "role": "assistant", "content_id": "c"})
        await out({"type": "bidi_barge_in", "reason": "user_speech"})
        await out.stop()

    asyncio.run(go())
    kinds = [c[1] for c in posts.calls]
    assert kinds == ["rover-speaker/start", "rover-speaker/push", "rover-speaker/clear", "rover-speaker/stop"]
    assert posts.calls[1][2] == {"audio": b64}
    assert gate.gain_for_now() == 1.0                          # barge-in reset the gate
    assert ref.take(10).tolist() == [0] * 10                   # ...and cleared the reference


def test_briefing_input_returns_a_text_block_and_gates_on_session_state(va, monkeypatch):
    bi = va._BriefingInput()
    bi.POLL_SECONDS = 0.0
    bi.DEBOUNCE_SECONDS = 0.0
    rows = [[(1, "telegram", "owner says hi", 1)], []]
    monkeypatch.setattr("tools.voice_bridge.pop_pending", lambda n: rows.pop(0) if rows else [])
    monkeypatch.setattr("tools.voice_bridge.flush_stale", lambda: 0)

    class _State:
        active_responses = set()
        response_requested = False

    class _Model:
        _session_state = _State()

    class _Agent:
        model = _Model()

    async def go():
        await bi.start(_Agent())
        assert bi._model_busy() is False
        _State.active_responses.add("resp_1")
        assert bi._model_busy() is True                        # 1.57.1 _SessionState shape
        _State.active_responses.clear()
        _State.response_requested = True
        assert bi._model_busy() is True
        _State.response_requested = False
        return await bi()

    item = asyncio.run(go())
    assert isinstance(item, bidi.TextBlock)
    assert item.text.startswith("[BRIEFING] [telegram/info] owner says hi")


def test_briefing_gate_is_never_busy_without_session_state(va):
    bi = va._BriefingInput()
    bi._model = object()                                       # Nova / Gemini: no _session_state
    assert bi._model_busy() is False


# ── the dashboard /ws/voice adapters ───────────────────────────────────────
def test_dashboard_voice_bridge_speaks_1_57(va):
    """/ws/voice builds its adapters inline; grade the source the way the guard does."""
    src = (ROOT / "dashboard_server.py").read_text(encoding="utf-8")
    ws = src[src.index("async def ws_voice"):src.index("# Static dashboard")]
    assert "bidi.AudioDelta(" in ws
    assert "bidi.EVENTS.AUDIO_DELTA" in ws and "bidi.EVENTS.BARGE_IN" in ws
    assert "BidiAudioInputEvent" not in ws and "BidiAudioStreamEvent" not in ws
    assert '"barge_in"' in ws                                  # the browser is told to flush playback


# ── the voice patch on 1.57.1 internals ────────────────────────────────────
def test_voice_patch_reinjects_images_and_uses_the_native_coalescer(va):
    import _voice_patch as vp
    from strands.types.tools import ToolResultBlock
    Model = bidi.model_class("openai")
    assert Model._send_tool_result is vp._patched_send_tool_result
    assert Model._convert_openai_event.__name__ == "_patched_convert_openai_event"

    sent = []

    class _State:
        pending_tools = {"call_1"}

    class _Self:
        _session_state = _State()
        requested = 0

        async def _send_event(self, ev):
            sent.append(ev)

        async def _request_response(self):
            self.requested += 1

    tr = ToolResultBlock(tool_use_id="call_1", status="success",
                         content=[{"text": "front camera"}, {"image": {"format": "jpeg", "source": {"bytes": b"\xff\xd8"}}}])
    me = _Self()
    asyncio.run(vp._patched_send_tool_result(me, tr))
    assert [e["item"]["type"] for e in sent] == ["function_call_output", "message"]
    assert json.loads(sent[0]["item"]["output"]) == [{"text": "front camera"}]
    assert sent[1]["item"]["content"][0]["image_url"].startswith("data:image/jpeg;base64,")
    assert not any(e.get("type") == "response.create" for e in sent)   # the coalescer owns it
    assert me.requested == 1 and _State.pending_tools == set()


def test_voice_patch_repairs_done_arguments():
    import _voice_patch as vp
    ev = {"type": "response.function_call_arguments.done", "call_id": "c", "arguments": '{"speed": 0.5, "turn": '}
    vp._authoritative_arguments(ev)
    json.loads(ev["arguments"])                                # repaired into valid JSON
    ok = {"type": "response.function_call_arguments.done", "call_id": "c", "arguments": '{"a": 1}'}
    vp._authoritative_arguments(ok)
    assert ok["arguments"] == '{"a": 1}'


# ── the guard ──────────────────────────────────────────────────────────────
_BIDI_IMPORT = re.compile(r"^\s*(from|import)\s+strands\.(experimental\.)?bidi\b")


def test_only_the_compat_module_imports_a_strands_bidi_path():
    offenders = []
    for p in sorted(ROOT.glob("*.py")) + sorted((ROOT / "tools").glob("*.py")) + sorted((ROOT / "scripts").glob("*.py")):
        if p.name == "_bidi_compat.py":
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if _BIDI_IMPORT.match(code):
                offenders.append(f"{p.relative_to(ROOT)}:{i}: {line.strip()[:80]}")
            if re.search(r"\bBidi(Text|Audio|Image)InputEvent\b|\bBidiAudioStreamEvent\b|\bBidiInterruptionEvent\b|\bBidiAudioIO\b", code):
                offenders.append(f"{p.relative_to(ROOT)}:{i}: 1.56 bidi name: {line.strip()[:80]}")
    assert not offenders, offenders


def test_requirements_pin_the_supported_window():
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    line = next(l for l in req.splitlines() if l.startswith("strands-agents"))
    assert ">=1.57.1" in line and "<1.60" in line, line


# ── end to end on the real BidiAgent (fake model, no network) ──────────────
def test_real_bidi_agent_runs_the_rover_adapters_end_to_end(va, monkeypatch):
    """agent.run(inputs=[rover mic, briefing], outputs=[rover speaker]) on the installed BidiAgent:
    the mic's AudioDelta and the briefing's TextBlock reach model.send, and the model's
    bidi_barge_in / bidi_audio_delta reach the rover speaker as clear / push."""
    BidiModel = importlib.import_module(f"{bidi.BIDI_PACKAGE}.models.model").BidiModel
    ev = importlib.import_module(f"{bidi.BIDI_PACKAGE}.types.events")

    class FakeModel(BidiModel):
        model_id = "fake"

        def __init__(self):
            self.q: asyncio.Queue = asyncio.Queue()
            self.got: list = []
            self._connection_id = None

        async def start(self, system_prompt=None, tools=None, messages=None, **kw):
            self._connection_id = "c1"
            await self.q.put(ev.BidiConnectionStartEvent(connection_id="c1", model="fake"))

        async def stop(self):
            pass

        async def send(self, content):
            self.got.append(type(content).__name__)
            if isinstance(content, bidi.TextBlock):
                await self.q.put(ev.BidiBargeInEvent(reason="user_speech"))
            pcm = content.source["bytes"] if isinstance(content, bidi.AudioDelta) else b"\x05\x00" * 240
            await self.q.put(ev.BidiAudioDeltaEvent(audio=base64.b64encode(pcm).decode(), format="pcm",
                                                    sample_rate=24000, channels=1))

        async def receive(self):
            while True:
                yield await self.q.get()

        def get_config(self):
            return {"model_id": "fake"}

        def update_config(self, **cfg):
            pass

        def get_audio_config(self):
            return _FakeModel(24000).get_audio_config()

    posts = _Posts()
    monkeypatch.setattr(va.requests, "post", posts.post)
    monkeypatch.setattr(va.requests, "get", posts.get)
    rows = [[(1, "telegram", "say hi", 2)]]
    monkeypatch.setattr("tools.voice_bridge.pop_pending", lambda n: rows.pop(0) if rows else [])
    monkeypatch.setattr("tools.voice_bridge.flush_stale", lambda: 0)

    async def main():
        model = FakeModel()
        agent = bidi.BidiAgent(model=model, tools=[bidi.stop_conversation], system_prompt="x")
        io = va.RoverAudioIO("http://sdk")
        inp, out, brief = io.input(), io.output(), va._BriefingInput()
        brief.POLL_SECONDS = 0.01
        brief.DEBOUNCE_SECONDS = 0.01
        runner = asyncio.create_task(agent.run(inputs=[inp, brief], outputs=[out]))
        await asyncio.sleep(0.2)
        inp._queue.put_nowait(b"\x01\x00" * 480)
        for _ in range(100):                       # up to 2 s for both items to round-trip
            await asyncio.sleep(0.02)
            if sorted(model.got) == ["AudioDelta", "TextBlock"] and \
               [c[1] for c in posts.calls].count("rover-speaker/push") >= 2:
                break
        runner.cancel()
        try:
            await runner
        except (asyncio.CancelledError, Exception):
            pass
        return model.got

    got = asyncio.run(main())
    assert sorted(got) == ["AudioDelta", "TextBlock"], got
    kinds = [c[1] for c in posts.calls]
    assert "rover-mic/start" in kinds and "rover-speaker/start" in kinds
    assert kinds.count("rover-speaker/push") >= 2 and "rover-speaker/clear" in kinds
