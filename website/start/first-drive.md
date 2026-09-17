---
title: First drive
description: "Sixty seconds from a green health check to the rover doing a 360 on your command."
---

# First drive

You have the stack up ([install](install.md)) and a filled-in `.env` ([configuration](config.md)). Now make it move.

## 0 · Is everything green?

```bash
scout-compose ps                                   # sdk (healthy), dashboard, media, yolo, telegram, thinker
curl -sk https://localhost:8080/api/health | jq     # the only /api route that answers without a session
curl -s  http://localhost:8002/data | jq '.battery, .signal_level'   # the SDK sees the rover
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8002/v2/front   # 200 = a camera frame is flowing
```

If `/v2/front` says 404 the SDK's browser has lost the Agora stream — the fork's video watchdog rejoins by itself within
~30 s after a rover power-cycle; see [Troubleshooting](../guide/troubleshooting.md).

## 1 · Enrol your passkey

Open `https://<host>:8080` (or `https://scout.local:8080`, or your tunnel hostname). With no passkeys stored the cockpit shows
the **enrolment** screen: tap *Create passkey* → Face ID / Touch ID → done. From now on the rover is sealed; the next visitor
sees a login screen instead. On iOS, *Share → Add to Home Screen* makes it a full-screen PWA.

!!! warning "Hostname, not IP"
    WebAuthn refuses raw IP addresses as a relying-party id. Use `scout.local` (mDNS is on by default), a `/etc/hosts` entry,
    or the tunnel hostname — and HTTPS, which the dashboard provides with a self-signed cert (`/ca` and `/trust` help you install it).

## 2 · Drive by hand

- **Glass joystick** — drag; the cockpit streams `POST /api/control {linear, angular, duration}` frames while you hold.
- **Keyboard** — ++w++ ++a++ ++s++ ++d++ (combine for arcs), ++space++ stop, ++bracket-left++ / ++bracket-right++ speed,
  ++1++–++5++ presets, ++shift++ turbo. Driving stops when the tab loses focus — no runaway rover.
- 💡 **lamp** toggle, 📷 **snapshot**, tap the small camera tile to **swap** front/rear.

## 3 · Ask the agent

Type in the Ask box: *"do a slow 360 and tell me what you see"*. Tokens and tool calls stream over `/ws/chat` as they happen —
you will see `rover_see` → `rover_navigate` → `rover_see` appear as receipts. The agent's system prompt is rebuilt every turn
with live battery/GPS/IMU, the room map and its estimated pose, so it does not waste tool calls asking where it is.

Or from the REPL on the brain:

```bash
make run           # 🧠 REPL — same agent, same tools
```

## 4 · Talk to it

The voice persona uses the rover's own mic and speaker:

```bash
scout-compose --profile voice up -d voice        # or tap the 🎙 pill in the cockpit
```

Say *"scout, come here"* — the persona's philosophy is **move, don't talk**: a wiggle, a spin or an approach-then-look is the
default reply, `rover_speak` only when a word is worth it. → [Voice](../voice.md)

## 5 · Look at what it recorded

Every persona auto-records each turn that contained at least one action. Open **Replay** (`/replay`) in the cockpit: pick
today's dataset, scrub an episode, read the reasoning trace aligned to the frames. → [Datasets & replay](../datasets.md)

!!! success "That's the loop"
    See → think → drive → remember. Everything after this page is depth: the [cockpit](../dashboard.md),
    [personas](../personas.md), [perception](../perception.md), the [tools](../reference/tools/index.md) and how to keep it
    all running ([operations](../guide/operations.md)).
