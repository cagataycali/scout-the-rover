---
title: Voice
description: "A bidirectional voice persona over the rover's own mic and speaker — providers, echo cancellation, the briefing channel, and the bugs that had to die."
---

# Voice — talking to a rover through the rover

`voice_agent.py` builds a Strands **`BidiAgent`** (bidirectional streaming speech) with the full rover toolbelt and one of
two audio backends:

```
--audio laptop   your mic  ──► BidiAudioIO  ──► BidiAgent ──► your speakers
--audio rover    rover mic ──► RoverAudioIO ──► BidiAgent ──► rover speaker      (SDK /rover-mic, /rover-speaker over WebRTC)
```

The `voice` compose service runs the **rover** backend: people talk *to the robot*, and it answers *from the robot*. The cockpit's
🎙 button is a third path — the phone's mic ↔ `/ws/voice` ↔ the phone's speaker — for driving from a noisy room.

## Providers

| `VOICE_PROVIDER` | model / voice knobs | needs |
|---|---|---|
| `openai` (default) | `VOICE_MODEL=gpt-realtime-2`, `VOICE_NAME=marin` (or `shimmer`, `coral`, `sage`…) | `OPENAI_API_KEY` |
| `nova_sonic` | `--voice matthew` … | AWS credential chain, `AWS_REGION` |
| `gemini` | `--voice Kore` … | `GOOGLE_API_KEY` / `GEMINI_API_KEY` |

```bash
python voice_agent.py                                  # laptop mic ↔ speakers
python voice_agent.py --audio rover                    # rover mic ↔ rover speaker
python voice_agent.py --provider nova_sonic --voice matthew
```

## Philosophy: move, don't talk

The persona prompt makes the wheels the primary reply. *"Come here"* → approach-then-look. *"Are you awake?"* → a wiggle.
`rover_speak` (the onboard TTS) is for the rare sentence that is worth saying; the realtime voice itself is what you hear when
it does talk. It also answers *"what do you see"* honestly: `rover_see` returns a real frame the model looks at.

## Echo cancellation and the half-duplex gate

The rover's mic hears the rover's speaker. `RoverAudioIO` shares one **pywebrtc `AudioProcessor`** between the mic input and
the speaker output with a far-end reference buffer (~3 s), at `ROVER_AEC_RATE` (48 kHz) with a round-trip hint
`ROVER_AEC_DELAY_MS` (150). On top of AEC a **speak gate** mutes the mic while the model is emitting audio and for
`ROVER_GATE_TAIL_S` (0.6 s) afterwards to cover WebRTC + playout latency — so the robot never interrupts itself.

## The briefing channel — other personas whisper to the voice

Text personas cannot speak. Instead `voice_say` (the [voice bridge](reference/tools/voice_bridge.md)) drops a row in a SQLite
queue that `_BriefingInput` inside the voice agent turns into a `[BRIEFING] …` text turn:

- **debounced** — a burst of rows within `SCOUT_BRIEFING_DEBOUNCE_S` coalesces into one briefing;
- **response-gated** — never injected while a response is in flight (tracked via `_voice_patch`), max hold `SCOUT_BRIEFING_MAX_HOLD_S`;
- **muted sources** — `SCOUT_BRIEFING_MUTE_SOURCES` (default `thinker`) are dropped unless tagged URGENT, so the slow thinker
  cannot make the robot chatter every minute;
- stale briefings queued while the voice was down are skipped at startup.

The prompt tells the model to *respond* to a briefing naturally, never to read it aloud.

## Bugs that had to die

Three failures, all found in the field, all fixed in code rather than by restarting things:

1. **Images in tool results crashed OpenAI Realtime.** `rover_see` returns image blocks; the Realtime API rejects anything but
   text in a `function_call_output`. `_voice_patch.py` overrides `_send_tool_result` to send the text parts as the function
   output and re-inject the images as `input_image` items, then commits once. (Upstream only fixed *direct* image input.)
2. **The voice stole the mic from everyone else.** The SDK's `/rover-mic` buffer empties on read, so the recorder, listener and
   voice agent each got a third of the audio. The [MediaHub](perception.md) is the single drainer now; consumers subscribe.
3. **A revoked API key turned into a camera storm.** When the provider returned 401 the voice container crash-looped every few
   seconds, and every restart re-grabbed media. Fatal provider errors (`invalid_api_key`, 401, quota, unknown model) now back
   off exponentially with an actionable log line instead of hammering the stack. Rotate the key → it recovers by itself.

## Operating it

```bash
scout-compose --profile voice up -d voice     # or the 🎙 pill in the cockpit (host supervisor)
scout-compose logs -f voice                   # look for "voice up (provider=… model=… voice=…)"
scout-compose rm -sf voice                    # stop and release the rover's mic/speaker
```

Only **one** voice persona may run at a time — it owns the rover's audio path. The listener (`listener_loop.py`, Whisper wake-word)
is the lightweight alternative when you only want a trigger phrase, not a conversation.
