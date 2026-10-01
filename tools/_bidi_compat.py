"""One import surface for Strands bidirectional streaming (the voice persona).

strands-agents 1.57.1 (PyPI) ships bidi under ``strands.experimental.bidi``; harness-sdk
main graduated it to ``strands.bidi`` (#4707) and the experimental path is a deprecation
shim there, removed in v1.60.0. Scout must run on BOTH, so every other module imports
bidi names from HERE and nowhere else (tests/test_voice_bidi.py enforces that).

Resolution order: ``strands.bidi`` first, then ``strands.experimental.bidi``.

Leading underscore on purpose: scripts/tooldoc.py documents every ``tools/*.py`` that is
not ``_``-prefixed as a rover tool module, and this is plumbing, not a Scout ability.

What this module gives the rest of the code base:

* ``BIDI_PACKAGE`` / ``IS_STABLE``   which package was found
* ``BidiAgent``                      the agent class
* ``TextBlock`` / ``ImageBlock`` / ``AudioDelta``   what an InputStream returns (1.57+):
                                     AudioDelta(format, source={"bytes"}) replaced
                                     BidiAudioInputEvent, TextBlock replaced BidiTextInputEvent
* ``model_class(provider)``          lazy loader for OpenAIRealtimeModel / BedrockNovaSonicModel /
                                     GoogleGeminiLiveModel (each pulls optional deps)
* ``DEFAULT_MODEL_IDS``              1.57 made ``model_id`` REQUIRED; these are the 1.56 defaults
* ``audio_io_class()``               lazy loader for ``AudioIO`` (needs PyAudio)
* ``EVENTS``                         output-event type strings (``EVENTS.AUDIO_DELTA`` ...)
* ``event_type(event)``              the type string of an output event (None for non-events)
* ``stop_conversation``              Scout's own tool: sets request_state["stop_event_loop"]
                                     (1.57.1 loop) AND calls agent.cancel() when present (main)
"""
from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any

from strands import tool

try:
    from strands.types.content import TextBlock
    from strands.types.media import ImageBlock
except ImportError as _e:  # strands-agents < 1.57: the BidiTextInputEvent era, not supported
    raise ImportError(
        "scout's voice persona needs strands-agents[bidi-all]>=1.57.1,<1.60 "
        "(agent.send(TextBlock|AudioDelta|ImageBlock), OpenAIRealtimeModel(transcription_model_id=...)); "
        "pip install -r requirements-voice.txt"
    ) from _e

_CANDIDATES = ("strands.bidi", "strands.experimental.bidi")


def _resolve_package() -> tuple[Any, str]:
    last: Exception | None = None
    for name in _CANDIDATES:
        try:
            return importlib.import_module(name), name
        except ImportError as e:  # pragma: no cover - depends on the installed strands
            last = e
    raise ImportError(
        "no Strands bidi package found (tried strands.bidi, strands.experimental.bidi); "
        "pip install 'strands-agents[bidi-all]>=1.57.1,<1.60'"
    ) from last


_bidi, BIDI_PACKAGE = _resolve_package()
IS_STABLE = BIDI_PACKAGE == "strands.bidi"


def _sub(name: str):
    return importlib.import_module(f"{BIDI_PACKAGE}.{name}")


BidiAgent = _bidi.BidiAgent
AudioDelta = _sub("types.media").AudioDelta

# ── output event type strings (1.57+) ──────────────────────────────────────
EVENTS = SimpleNamespace(
    CONNECTION_START="bidi_connection_start",
    CONNECTION_RESTART="bidi_connection_restart",
    CONNECTION_WARNING="bidi_connection_warning",
    CONNECTION_STOP="bidi_connection_stop",
    RESPONSE_START="bidi_response_start",
    RESPONSE_STOP="bidi_response_stop",
    AUDIO_START="bidi_audio_start",
    AUDIO_DELTA="bidi_audio_delta",       # was bidi_audio_stream; "audio" = base64 PCM
    AUDIO_STOP="bidi_audio_stop",
    TRANSCRIPT_START="bidi_transcript_start",
    TRANSCRIPT_DELTA="bidi_transcript_delta",
    TRANSCRIPT_STOP="bidi_transcript_stop",
    BARGE_IN="bidi_barge_in",             # was bidi_interruption
    USAGE="bidi_usage",
)


def event_type(event: Any) -> str | None:
    """Type string of an output event (events are dicts keyed by "type"); None otherwise."""
    if not isinstance(event, dict):
        return None
    t = event.get("type")
    return t if isinstance(t, str) else None


# ── lazy loaders (optional deps) ───────────────────────────────────────────
_MODEL_NAMES = {
    "openai": ("models.openai", "OpenAIRealtimeModel"),
    "openai_realtime": ("models.openai", "OpenAIRealtimeModel"),
    "nova_sonic": ("models.bedrock", "BedrockNovaSonicModel"),
    "novasonic": ("models.bedrock", "BedrockNovaSonicModel"),
    "nova": ("models.bedrock", "BedrockNovaSonicModel"),
    "gemini": ("models.google", "GoogleGeminiLiveModel"),
    "gemini_live": ("models.google", "GoogleGeminiLiveModel"),
}

PROVIDERS = ("openai", "nova_sonic", "gemini")

#: 1.57 dropped the per-provider model_id defaults (ModelConfig.model_id is Required).
#: These are the values 1.56 filled in; VOICE_MODEL overrides them in voice_agent.
DEFAULT_MODEL_IDS = {
    "openai": "gpt-realtime",
    "nova_sonic": "amazon.nova-2-sonic-v1:0",
    "gemini": "gemini-2.5-flash-native-audio-preview-09-2025",
}

#: 1.56 hard-wired OpenAI input transcription to this; 1.57 makes it an explicit kw.
DEFAULT_OPENAI_TRANSCRIPTION_MODEL = "gpt-4o-transcribe"


def canonical_provider(provider: str) -> str:
    """'novasonic' -> 'nova_sonic', 'gemini_live' -> 'gemini' ... (ValueError when unknown)."""
    key = provider.lower()
    if key not in _MODEL_NAMES:
        raise ValueError(f"unknown voice provider: {provider}")
    _mod, cls = _MODEL_NAMES[key]
    return {"OpenAIRealtimeModel": "openai",
            "BedrockNovaSonicModel": "nova_sonic",
            "GoogleGeminiLiveModel": "gemini"}[cls]


def model_class(provider: str):
    """The bidi model class for a provider name (raises ValueError for an unknown one)."""
    key = provider.lower()
    if key not in _MODEL_NAMES:
        raise ValueError(f"unknown voice provider: {provider}")
    mod, cls = _MODEL_NAMES[key]
    return getattr(_sub(mod), cls)


def audio_io_class():
    """``AudioIO`` (PyAudio-backed laptop mic + speakers; was BidiAudioIO)."""
    return _sub("io.audio").AudioIO


# ── stop_conversation ──────────────────────────────────────────────────────
# Built with tool(...)(fn), not @tool, so scripts/tooldoc.py (AST scan for @tool) never
# counts it as a rover ability. The stock strands.experimental.bidi.tools.stop_conversation
# is deprecated in 1.57.1 and deleted on main (#4664).
def _stop_conversation(tool_context) -> str:
    """End the voice session.

    Use ONLY when the user says "stop conversation" or clearly asks Scout to end the voice
    session. Do NOT use for "stop", "goodbye", "bye" or other farewells or phrases.
    """
    # 1.57.1: the bidi loop reads invocation_state["request_state"]["stop_event_loop"] after
    # the tool returns. main (#4664): BidiAgent.cancel() sets a thread-safe cancel signal.
    # Do both; each is a no-op where the other API is in force.
    state = getattr(tool_context, "invocation_state", None)
    if isinstance(state, dict):
        state.setdefault("request_state", {})["stop_event_loop"] = True
    agent = getattr(tool_context, "agent", None)
    cancel = getattr(agent, "cancel", None)
    if callable(cancel):
        cancel()
    return "Ending conversation"


stop_conversation = tool(name="stop_conversation", context=True)(_stop_conversation)


__all__ = [
    "AudioDelta", "BIDI_PACKAGE", "BidiAgent", "DEFAULT_MODEL_IDS",
    "DEFAULT_OPENAI_TRANSCRIPTION_MODEL", "EVENTS", "ImageBlock", "IS_STABLE", "PROVIDERS",
    "TextBlock", "audio_io_class", "canonical_provider", "event_type", "model_class",
    "stop_conversation",
]
