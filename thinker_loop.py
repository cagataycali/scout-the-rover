#!/usr/bin/env python3
"""🧠 scout slow-thinker — background reflective loop.

Every THINKER_INTERVAL seconds (default 60s), this loop:
  1. Reads live rover state (battery, GPS, signal, lamp)
  2. Reads recent turn memory + recent telegram (if a default chat is set)
  3. Spawns a fresh Strands agent with the "thinker" persona
  4. Lets the agent decide ONE useful background action:
        - /tmp snapshot via rover_see + send to Telegram
        - tiny exploratory move (rover_navigate with 1 step)
        - rover_memory observation
        - voice line via rover_speak
        - or NOOP if nothing's worth doing

Goals:
  • Keep scout feeling ALIVE — periodic photos to operator, gentle drift
  • Catch slow problems pure event loops miss ("battery falling, head home")
  • Long-horizon spatial summarization → rover_memory

Anti-goals:
  • Don't spam Telegram — at most one outbound per cycle
  • Don't move recklessly — battery>30 + clear path required
  • Don't replace the main agent loop; only act when there's a clear reason

Env knobs:
    THINKER_INTERVAL          seconds between cycles (default 60)
    THINKER_DISABLED          "1" to no-op the loop (debugging)
    THINKER_TELEGRAM_CHAT_ID  if set, thinker can send updates here
    SCOUT_THINKER_DRIVE       "1" to allow tiny moves (default 0 — observe-only)
"""
from __future__ import annotations

import os
import sys
import signal
import time
import traceback
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

# Make sure repo root is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strands import Agent

from agent import BASE_PROMPT, build_turn_input, live_state_block
from tools import ROVER_ALL_TOOLS
from tools.rover_state import rover_state as _rover_state_tool
from tools.telegram import format_history_for_prompt
import memory as _memory
from tools.voice_bridge import voice_say as _voice_say, push as _voice_push
from tools.reasoning_log import ReasoningLoggerHook
from agent import _AutoRecorder  # reuse the same episode-wrap logic


INTERVAL = int(os.getenv("THINKER_INTERVAL", "60"))
DISABLED = os.getenv("THINKER_DISABLED", "").lower() in ("1", "true", "yes")
TG_CHAT_ID = os.getenv("THINKER_TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_DEFAULT_CHAT_ID", "")
ALLOW_DRIVE = os.getenv("SCOUT_THINKER_DRIVE", "1") not in ("0", "false", "no")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _unwrap(env):
    if not isinstance(env, dict):
        return {}
    for c in env.get("content") or []:
        if isinstance(c, dict) and "json" in c:
            return c["json"]
    return {}


# Thinker prompt — built fresh every cycle so it reflects the latest world.
def _thinker_prompt() -> str:
    state = _unwrap(_rover_state_tool())
    battery = state.get("battery", "?")
    signal_level = state.get("signal_level", "?")
    gps_ok = state.get("gps_signal", 0) > 0 and state.get("latitude", 1000) != 1000
    gps_str = (
        f"{state['latitude']:.6f}, {state['longitude']:.6f}"
        if gps_ok else "no fix"
    )
    tg_block = format_history_for_prompt(TG_CHAT_ID, limit=10) if TG_CHAT_ID else ""
    tg_block = tg_block or "(no telegram history)"

    drive_clause = (
        "  • You MUST do exploratory move via rover_navigate with a "
        "with complex ways"
        "the front camera shows a clear path."
        if ALLOW_DRIVE else
        "  • DO NOT move the rover this cycle (drive disabled — observe only)."
    )

    tg_clause = (
        f"  • You MAY send ONE update to Telegram chat `{TG_CHAT_ID}` if you "
        f"have something worth sharing (a cool view, a status change, a "
        f"problem). Use telegram(action='send_photo', file_path='/tmp/"
        f"scout_thinker.jpg', caption='...', chat_id='{TG_CHAT_ID}'). Skip "
        f"if nothing's interesting — silence is fine."
        if TG_CHAT_ID else
        "  • Telegram chat not configured — don't try to send."
    )

    extra = f"""
## 🤖 SCOUT — autonomous room explorer (real action mode)
You are SCOUT, a curious rover with a mission: actively explore the room,
discover what's happening, and KEEP THE OPERATOR (Cagatay) IN THE LOOP via
Telegram. You run every {INTERVAL}s in the BACKGROUND — every cycle is your
chance to do something real.

## Live rover snapshot ({_now()})
- Battery: {battery}%   Signal: {signal_level}/4
- GPS: {gps_str}
- Heading: {state.get('orientation','?')}°  Lamp: {'on' if state.get('lamp') else 'off'}

## Recent telegram (chat={TG_CHAT_ID or "unset"})
{tg_block}

## Your job EVERY CYCLE (be PROACTIVE, not passive):
1. **LOOK** — call rover_see(camera='both', save=True) to get a fresh view
2. **THINK** — what's interesting, new, worth investigating?
3. **ACT** — pick ONE meaningful action:
   • **EXPLORE**: rover_navigate(...) — drive 1-3 steps toward something
     interesting (a doorway, an object, an unexplored area). Use the front
     camera image to plan a safe path.
   • **REMEMBER**: rover_memory(action='remember', ...) — log spatial
     observations, landmarks, people, changes since last cycle.
   • **REPORT**: telegram(action='send_photo', file_path='/tmp/rover_see_*.jpg',
     caption='...', chat_id='{TG_CHAT_ID or "unset"}') — share what you see with Cagatay.
     Send a photo + short caption EVERY 2-3 cycles minimum, or immediately
     if something interesting happens.
   • **SPEAK**: rover_speak(text='...') — rare, only if you want to greet
     someone in the room.

## Active scouting heuristics
- If you've been still for 2+ cycles → MOVE. Drive 1-2 steps to a new vantage.
- If you see a person → telegram a photo with a friendly caption.
- If you see something new (object moved, door opened, light changed) → log
  it to rover_memory AND telegram a photo.
- If you've sent nothing to telegram in last 3 cycles → send a status photo
  with caption like "still scouting — here's what I see".
- If battery > 40% and path looks clear → ALWAYS prefer ACTION over NOOP.

## Hard rules (safety)
- Battery < 25% → STOP moving, telegram a low-battery alert, then NOOP.
- Battery < 15% → only telegram, never move.
- Max 3 navigation steps per cycle.
- If front camera shows obstacle within ~30cm → don't drive forward, turn
  instead (rover_navigate with a turn command).
- Never drive backward blindly (no rear camera awareness mid-cycle).

## Telegram style
- Captions are SHORT (1 line, max 80 chars), playful, scout-personality.
  Examples: "👀 spotted Cagatay at the workbench", "🚪 door's open — going
  in", "🔋 84% — full energy, exploring north corner", "🤖 G1 humanoid is
  back, saying hi".
- Use emojis. Be fun. You're a curious robot, not a status-bot.

## Output format
- Your final text response is logged (not user-shown). Be terse.
- Format: `ACTION: <what you did>  |  WHY: <one-liner reason>`
- Examples:
  - `ACTION: drove fwd 2 steps + tg photo  |  WHY: doorway visible north`
  - `ACTION: tg photo only  |  WHY: G1 in frame, worth sharing`
  - `ACTION: NOOP  |  WHY: battery 18%, holding`

## Anti-NOOP rule
NOOP is ONLY allowed if: battery<25 OR you've already moved+reported in
the last 60s OR there's literally a wall in every direction. Otherwise,
ACT. Cagatay wants to see scout being scout — not a parked sensor.
"""
    return f"{_memory.recall_block()}\n{live_state_block()}\n{BASE_PROMPT}\n{extra}"


def _build_agent() -> Agent:
    return Agent(
        tools=[*ROVER_ALL_TOOLS, _voice_say],
        system_prompt=_thinker_prompt(),
        hooks=[ReasoningLoggerHook(agent_id="thinker")],
    )


# Wrap thinker action-cycles in episodes (records video+action when it drives;
# auto-drops cycles with no motion so idle reflections don't bloat the dataset).
_thinker_recorder = _AutoRecorder()


def cycle(agent: Agent) -> None:
    """One reflection cycle — refresh prompt, clear messages, run."""
    t0 = time.time()
    print(f"[{_now()}] 🧠 thinker cycle start", flush=True)

    # Clear conversation history each cycle — prompt re-injects context
    try:
        agent.messages.clear()
    except Exception:
        pass

    # Refresh system prompt so live state is current
    try:
        agent.system_prompt = _thinker_prompt()
    except Exception as e:
        print(f"[{_now()}] ⚠️  prompt refresh failed: {e}", flush=True)

    user_turn = (
        "Run one background reflection cycle. Decide what's worth doing "
        "(if anything) per your persona rules and execute. Be terse."
    )

    _thinker_recorder.begin_turn("[thinker] autonomous exploration")
    try:
        result = agent(build_turn_input(user_turn, camera="front"))
        text = str(result)[:1200]
        print(f"[{_now()}] 🧠 → {text[:240]}", flush=True)
        try:
            _memory.record_turn(f"[thinker] {user_turn}", result)
        except Exception:
            pass
        # Feed the thinker's observation into the voice channel as context.
        try:
            _voice_push("thinker", text[:300], importance=1)
        except Exception:
            pass
    except Exception as e:
        print(f"[{_now()}] ❌ cycle error: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
    finally:
        # Close the episode: saves it if the thinker drove, drops it otherwise.
        try:
            _thinker_recorder.end_turn()
        except Exception as _e:
            print(f"[{_now()}] 🎬 thinker auto-record end failed: {_e}", flush=True)

    dur = time.time() - t0
    print(f"[{_now()}] 🧠 cycle done in {dur:.1f}s", flush=True)


def main() -> None:
    print(f"🧠 scout slow-thinker starting (interval={INTERVAL}s, drive={'on' if ALLOW_DRIVE else 'OFF'})")
    if DISABLED:
        print("THINKER_DISABLED=1 — exiting without running")
        return

    stop = {"flag": False}

    def _sig(*_):
        stop["flag"] = True
        print(f"\n[{_now()}] 🧠 stop requested", flush=True)

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    try:
        agent = _build_agent()
        n_tools = len(getattr(agent, "tool_registry", {}).registry) if hasattr(agent, "tool_registry") else len(ROVER_ALL_TOOLS)
        print(f"[{_now()}] 🧠 thinker agent built ({n_tools} tools)", flush=True)
    except Exception as e:
        print(f"❌ failed to build thinker agent: {e}")
        traceback.print_exc()
        sys.exit(1)

    # Initial wait — let SDK + main agent settle
    initial_wait = min(INTERVAL, 15)
    print(f"[{_now()}] 🧠 initial wait {initial_wait}s before first cycle", flush=True)
    for _ in range(initial_wait):
        if stop["flag"]:
            return
        time.sleep(1)

    while not stop["flag"]:
        try:
            cycle(agent)
        except Exception as e:
            print(f"[{_now()}] ❌ outer cycle error: {e}", flush=True)
            traceback.print_exc()
        for _ in range(INTERVAL):
            if stop["flag"]:
                break
            time.sleep(1)

    print(f"[{_now()}] 👋 thinker stopped")


if __name__ == "__main__":
    main()
