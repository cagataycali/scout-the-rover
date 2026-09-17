---
title: Operations runbook
description: "Bring-up order, health checks, restart recipes, the tunnel, watchdogs, and the pre-demo checklist — commands only, no credentials."
---

# Operations runbook

Everything here is a command you can paste; nothing here is a secret. Credentials live in `.env` on the brain and nowhere else.

```bash
alias scout-compose='docker compose -f docker-compose.slim.yml -f docker-compose.slim.override.yml'
```

## Power-on order

1. **Network first.** The rover and the brain must share a network the rover can reach FrodoBots' cloud from (home Wi-Fi, or a
   phone hotspot at a venue). The rover joins on its own; the brain's containers come up with docker.
2. **Rover on.** Its LED breathes while it registers with Agora — give it a minute.
3. **Brain on.** `restart: unless-stopped` brings the whole stack back; `dashboard` waits for the SDK health check.
4. **Check the cameras** — the one thing that can silently stay down after a rover power-cycle (below).

## Health in 20 seconds

```bash
scout-compose ps                                                        # all Up, sdk (healthy)
curl -sk https://localhost:8080/api/health | jq .                       # dashboard alive
curl -s http://localhost:8002/data | jq '{battery, signal_level, lamp}'  # SDK ↔ rover
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8002/v2/front # 200 = frames flowing (404 = no video yet)
curl -s http://localhost:8090/status | jq .                             # hub drain rates
systemctl --user is-active scout-supervisor cloudflared 2>/dev/null     # host processes
```

## Restart recipes

| symptom | do |
|---|---|
| cameras 404 for > 1 min after the rover rebooted | wait for the SDK's video watchdog (rejoin ≤ 30 s after 6 misses); still dark → `scout-compose restart sdk` |
| dashboard 502 on `/api/frame` but `/data` fine | same as above — it proxies the SDK |
| agent answers but never drives | is the 🕹 **thinker drive** flag off? (`cat .memory/personas.json`) — or the turn came via `/api/chat` from a sibling and it's meant to be advisory |
| voice persona silent | `scout-compose logs --tail 50 voice` — a `401 invalid_api_key` line means rotate `OPENAI_API_KEY` in `.env`, then `scout-compose up -d --force-recreate voice` |
| persona pills spin forever | `systemctl --user status scout-supervisor`; the socket `./.supervisor/scout-supervisor.sock` must exist and be mounted |
| passkey login fails on a new device | you are on a raw IP or plain HTTP — use the hostname + HTTPS; check `SCOUT_AUTH_RP_ID` |
| replay shows the same video for every episode | the episode index has no footer — [repair it](../datasets.md#repairing-a-footerless-dataset) |
| after editing `.env` | `scout-compose up -d --force-recreate <service>` (env_file is read at create time) |
| after editing code / Dockerfile | `scout-compose build && scout-compose --profile all up -d` |

## The tunnel

```bash
systemctl --user status cloudflared      # or: sudo systemctl status cloudflared
cloudflared tunnel list
cloudflared tunnel info <name>
curl -s -o /dev/null -w '%{http_code}\n' https://scout.example.com/api/health
```

A **530** from Cloudflare means the origin (the brain) is down or the tunnel process isn't running; a **401** means the tunnel is
fine and the gate is doing its job. Cloudflare caches `/css` and `/js` for hours — bump `?v=` on asset URLs when you ship UI.

## Watchdogs already in place

- **SDK video watchdog** (`VIDEO_WATCHDOG=true` in our SDK fork): polls the browser page every 5 s; after 6 misses it refreshes
  tokens and rejoins the Agora channel, backing off 30 → 300 s. Fixes the "rover rebooted, cameras 404 for hours" failure.
- **Voice provider back-off**: fatal provider errors slow the restart loop instead of hammering the media path.
- **Episode sealing**: every saved episode is finalized immediately, so a SIGKILL only ever loses the in-flight one.
- **docker `restart: unless-stopped`** on every service.

## Before a demo

- [ ] `scout-compose ps` all Up; cameras 200; battery > 50 %.
- [ ] Open the cockpit on the phone you'll demo with; passkey works; joystick moves the rover.
- [ ] Decide the thinker: off for a scripted demo (it emotes/moves every `THINKER_INTERVAL`), on for "look, it's alive".
- [ ] Voice: one persona only; confirm `voice up (provider=… voice=…)` in the logs; say hello once.
- [ ] Recording: leave **rec** on — the demo becomes a dataset.
- [ ] Print the field card (`make field-card`) if others should enrol.

## Stopping cleanly

```bash
scout-compose rm -sf voice                 # free the rover mic first
scout-compose --profile all stop           # keeps containers + volumes
scout-compose --profile all down           # removes containers; passkeys/TLS survive in the volume
```

Power the rover off last — the SDK tolerates a vanished rover better than the rover tolerates a vanished SDK mid-`/control`.
