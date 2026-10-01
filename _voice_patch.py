"""
🩹 Local patch for strands OpenAIRealtimeModel (strands-agents >= 1.57.1).

Why: rover_see / rover_screenshot return inline IMAGE content blocks inside
their *tool result*. OpenAI's Realtime API rejects image blocks in a
`function_call_output` (only text/json allowed), and 1.57.1's
`_send_tool_result` raises ValueError on them — the session dies the first
time Scout looks at something.

This patch overrides `_send_tool_result` to:
  1. Send the text/json parts of the tool result as `function_call_output`.
  2. Re-inject any image blocks as separate `input_image` conversation items
     (the same `data:` URL wire format `_send_image_content` uses).
  3. Hand the continuation to the model's own coalescing `_request_response()`
     (1.57.1 tracks active responses natively in `_SessionState`; the 1.56-era
     "conversation_already_has_active_response" guard this file used to carry
     is gone — it would fight the native state machine).

Plus two tool-call argument hardenings on `_convert_openai_event` (whose 1.57.1
signature is `(self, openai_event, state=None)`; the wrapper passes everything
through):
  • trust the authoritative `arguments` string on
    `response.function_call_arguments.done` over the delta buffer;
  • repair malformed JSON in it with json_repair so the tool call still fires.

Import this module BEFORE building the voice agent:  `import _voice_patch`
(voice_agent.py does, at module level). The model class comes from
tools/_bidi_compat so this works on strands.bidi (main) and
strands.experimental.bidi (1.57.1) alike; where a private this patch relies on
is missing, it logs and leaves the method alone instead of crashing.
"""
from __future__ import annotations

import base64
import json
import logging

from tools import _bidi_compat as _bidi

logger = logging.getLogger(__name__)

_Model = _bidi.model_class("openai")


def _has(*names: str) -> bool:
    missing = [n for n in names if not hasattr(_Model, n)]
    if missing:
        logger.warning("voice patch skipped: %s lacks %s (strands %s)", _Model.__name__, missing, _bidi.BIDI_PACKAGE)
    return not missing


def _split_tool_result(tool_result) -> tuple[str, list, list]:
    """(tool_use_id, text/json blocks, image blocks) for a ToolResultBlock (1.57) or a dict (older)."""
    if isinstance(tool_result, dict):
        tool_use_id = tool_result.get("toolUseId")
        content = tool_result.get("content", []) or []
    else:
        tool_use_id = tool_result.tool_use_id
        content = tool_result.content or []
    text_blocks: list = []
    image_blocks: list = []
    for block in content:
        if "image" in block:
            image_blocks.append(block)
        elif "text" in block or "json" in block:
            text_blocks.append(block)
        else:
            logger.debug("dropping unsupported tool-result block: %s", list(block.keys()))
    return tool_use_id, text_blocks, image_blocks


def _image_item(block: dict) -> dict:
    """The conversation.item.create payload for one image block (OpenAI input_image wire format)."""
    img = block["image"]
    fmt = img.get("format", "jpeg")
    raw = img["source"]["bytes"]
    data_url = f"data:image/{fmt};base64," + base64.b64encode(raw).decode("utf-8")
    return {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_image", "image_url": data_url}],
    }


async def _patched_send_tool_result(self, tool_result) -> None:
    tool_use_id, text_blocks, image_blocks = _split_tool_result(tool_result)

    # 1) text/json → function_call_output (image-free, API-safe)
    result_output = json.dumps(text_blocks) if text_blocks else "[image(s) returned]"
    await self._send_event({
        "type": "conversation.item.create",
        "item": {"type": "function_call_output", "call_id": tool_use_id, "output": result_output},
    })

    # 2) images → separate user input_image items
    for block in image_blocks:
        await self._send_event({"type": "conversation.item.create", "item": _image_item(block)})
        logger.debug("injected rover image into realtime context (%d bytes)", len(block["image"]["source"]["bytes"]))

    # 3) continuation: 1.57.1 bookkeeping (pending tool gone) + coalesced response.create
    state = getattr(self, "_session_state", None)
    pending = getattr(state, "pending_tools", None)
    if pending is not None:
        pending.discard(tool_use_id)
    request = getattr(self, "_request_response", None)
    if request is not None:
        await request()
    else:  # pragma: no cover - a strands without the coalescer
        await self._send_event({"type": "response.create"})


if _has("_send_tool_result", "_send_event"):
    _Model._send_tool_result = _patched_send_tool_result


# Fix: "error parsing function arguments" (json.JSONDecodeError) on tool calls.
#
# The SDK accumulates tool-call `arguments` from streaming
# `response.function_call_arguments.delta` events and json.loads() them on
# `.done`. Under rapid/concurrent calls those deltas can arrive corrupted or
# truncated → "Expecting value: line 1 column N". But OpenAI's `.done` event
# ALREADY includes the complete, authoritative `arguments` string. We wrap
# `_convert_openai_event` to trust that field instead of the delta buffer —
# after repairing it when the model itself emitted broken JSON.
try:
    from json_repair import repair_json as _repair_json
except Exception:  # pragma: no cover
    _repair_json = None


def _authoritative_arguments(openai_event: dict) -> None:
    """Mutate a `.done` event so its arguments are the authoritative (and valid) JSON string."""
    raw = openai_event.get("arguments")
    if not isinstance(raw, str) or not raw:
        return
    try:
        json.loads(raw)  # already valid → leave as-is
        return
    except json.JSONDecodeError as e:
        repaired = None
        if _repair_json is not None:
            try:
                repaired = _repair_json(raw)
                json.loads(repaired)  # validate repair
            except Exception:
                repaired = None
        if repaired is not None:
            logger.warning(
                "call_id=<%s> | repaired malformed tool-call JSON (%s) — tool call PRESERVED",
                openai_event.get("call_id"), e,
            )
            openai_event["arguments"] = repaired
        else:
            logger.error(
                "call_id=<%s> | tool-call JSON unrepairable (%s); raw=%r",
                openai_event.get("call_id"), e, raw[:300],
            )


if _has("_convert_openai_event"):
    _orig_convert = _Model._convert_openai_event

    def _patched_convert_openai_event(self, openai_event, *args, **kwargs):
        if openai_event.get("type") == "response.function_call_arguments.done":
            _authoritative_arguments(openai_event)
            cid = openai_event.get("call_id")
            full_args = openai_event.get("arguments")
            if cid and full_args is not None:
                buf = getattr(self, "_function_call_buffer", None)
                if buf is not None:
                    if cid not in buf:
                        buf[cid] = {"call_id": cid, "name": "", "arguments": ""}
                    # authoritative full args from the done event (not delta-accumulated)
                    buf[cid]["arguments"] = full_args
        return _orig_convert(self, openai_event, *args, **kwargs)

    _Model._convert_openai_event = _patched_convert_openai_event
