"""
🩹 Local patch for strands BidiOpenAIRealtimeModel.

Why: rover_see / rover_screenshot return inline IMAGE content blocks inside
their *tool result*. OpenAI's Realtime API rejects image blocks in a
`function_call_output` (only text/json allowed) → ValueError crash.

PR strands-agents/harness-sdk#2327 only wires up DIRECT image INPUT
(BidiImageInputEvent) — it does NOT fix the tool-result path that crashes us.

This patch overrides `_send_tool_result` to:
  1. Send the text/json parts of the tool result as `function_call_output`.
  2. Re-inject any image blocks as separate `input_image` conversation items
     (the same `data:` URL wire format the upstream PR uses).
  3. Commit once with a single `response.create`.

Import this module BEFORE building the voice agent:  `import _voice_patch`
"""
from __future__ import annotations

import base64
import json
import logging

from strands.experimental.bidi.models import openai_realtime as _oair

logger = logging.getLogger(__name__)


async def _patched_send_tool_result(self, tool_result) -> None:
    tool_use_id = tool_result.get("toolUseId")

    text_blocks: list = []
    image_blocks: list = []
    for block in tool_result.get("content", []) or []:
        if "image" in block:
            image_blocks.append(block)
        elif "text" in block or "json" in block:
            text_blocks.append(block)
        else:
            logger.debug("dropping unsupported tool-result block: %s", list(block.keys()))

    # 1) text/json → function_call_output (image-free, API-safe)
    result_output = json.dumps(text_blocks) if text_blocks else "[image(s) returned]"
    await self._send_event({
        "type": "conversation.item.create",
        "item": {"type": "function_call_output", "call_id": tool_use_id, "output": result_output},
    })

    # 2) images → separate user input_image items (PR #2327 wire format)
    for block in image_blocks:
        img = block["image"]
        fmt = img.get("format", "jpeg")
        raw = img["source"]["bytes"]
        b64 = base64.b64encode(raw).decode("utf-8")
        data_url = f"data:image/{fmt};base64,{b64}"
        await self._send_event({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_image", "image_url": data_url}],
            },
        })
        logger.debug("injected rover image into realtime context (%s, %d bytes)", fmt, len(raw))

    # 3) commit
    await self._send_event({"type": "response.create"})


_oair.BidiOpenAIRealtimeModel._send_tool_result = _patched_send_tool_result
# logger.info("🩹 patched BidiOpenAIRealtimeModel._send_tool_result for image tool results")
# print("🩹 voice patch applied: rover image tool results now supported on OpenAI Realtime")


# Fix: "error parsing function arguments" (json.JSONDecodeError) on tool calls.
#
# The SDK accumulates tool-call `arguments` from streaming
# `response.function_call_arguments.delta` events and json.loads() them on
# `.done`. Under rapid/concurrent calls those deltas can arrive corrupted or
# truncated → "Expecting value: line 1 column N". But OpenAI's `.done` event
# ALREADY includes the complete, authoritative `arguments` string. We wrap
# `_convert_openai_event` to trust that field instead of the delta buffer.
_orig_convert = _oair.BidiOpenAIRealtimeModel._convert_openai_event


def _patched_convert_openai_event(self, openai_event):
    if openai_event.get("type") == "response.function_call_arguments.done":
        cid = openai_event.get("call_id")
        full_args = openai_event.get("arguments")
        if cid and full_args is not None:
            buf = getattr(self, "_function_call_buffer", None)
            if buf is not None:
                if cid not in buf:
                    buf[cid] = {"call_id": cid, "name": "", "arguments": ""}
                # authoritative full args from the done event (not delta-accumulated)
                buf[cid]["arguments"] = full_args
    return _orig_convert(self, openai_event)


_oair.BidiOpenAIRealtimeModel._convert_openai_event = _patched_convert_openai_event
# logger.info("🩹 patched _convert_openai_event: use authoritative tool-call arguments from .done")
# print("🩹 voice patch applied: tool-call argument parsing hardened")


# Fix: "conversation_already_has_active_response" crash.
#
# Tool results (and parallel rover tool calls) each emit `response.create`.
# If a response is still active, OpenAI rejects the extra create. We:
#   • track active-response state from incoming response.created/done/cancelled
#   • guard outgoing `response.create`: if one is active, DEFER it
#   • when the active response finishes, flush exactly one deferred create
import asyncio as _asyncio

_orig_send_event = _oair.BidiOpenAIRealtimeModel._send_event
_prev_convert = _oair.BidiOpenAIRealtimeModel._convert_openai_event


async def _guarded_send_event(self, event):
    if event.get("type") == "response.create":
        if getattr(self, "_active_response", False):
            self._pending_create = True
            logger.debug("response.create deferred — a response is already active")
            return
        self._active_response = True  # optimistic; confirmed by response.created
    return await _orig_send_event(self, event)


def _lifecycle_convert(self, openai_event):
    t = openai_event.get("type")
    if t == "response.created":
        self._active_response = True
        self._pending_create = False
    elif t in ("response.done", "response.cancelled"):
        self._active_response = False
        if getattr(self, "_pending_create", False):
            self._pending_create = False
            try:
                loop = _asyncio.get_running_loop()
                loop.create_task(self._send_event({"type": "response.create"}))
                logger.debug("flushed deferred response.create after response finished")
            except RuntimeError:
                pass
    elif t == "error":
        code = (openai_event.get("error") or {}).get("code", "")
        # don't get stuck: clear active flag on the specific conflict error
        if code == "conversation_already_has_active_response":
            self._active_response = True  # one really is active; keep deferred
    return _prev_convert(self, openai_event)


_oair.BidiOpenAIRealtimeModel._send_event = _guarded_send_event
_oair.BidiOpenAIRealtimeModel._convert_openai_event = _lifecycle_convert
# logger.info("🩹 patched response lifecycle: guard against double response.create")
# print("🩹 voice patch applied: response.create conflict guard active")


# Fix (round 2): the .done event's OWN `arguments` string is malformed JSON.
#
# The round-1 patch trusts the authoritative `.done` arguments over the
# delta buffer — but observed failures ("Expecting ',' delimiter: line 1
# column 54") prove the model itself emits BROKEN JSON in `.done`. The SDK
# then drops the entire tool call → the tool never fires (rover goes deaf to
# that command). We repair the JSON in-place on the `.done` event BEFORE the
# downstream chain writes it to the buffer + json.loads() it.
try:
    from json_repair import repair_json as _repair_json
except Exception:  # pragma: no cover
    _repair_json = None

_chain_convert = _oair.BidiOpenAIRealtimeModel._convert_openai_event


def _argrepair_convert(self, openai_event):
    if openai_event.get("type") == "response.function_call_arguments.done":
        raw = openai_event.get("arguments")
        if isinstance(raw, str) and raw:
            try:
                json.loads(raw)  # already valid → leave as-is
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
    return _chain_convert(self, openai_event)


_oair.BidiOpenAIRealtimeModel._convert_openai_event = _argrepair_convert
# print("🩹 voice patch applied: malformed tool-call JSON auto-repair active")
