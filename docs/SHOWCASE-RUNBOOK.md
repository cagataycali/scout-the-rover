# Scout showcase runbook — 2026-09-17

Scout = FrodoBots Earth Rover Mini+ (bot `finn-zest-beam`), brain on the Jetson AGX Thor
(`ssh thor`, 192.168.1.151), dashboard at **https://scout.cagatay.my**.
Stack = `docker compose` **slim** (CPU) image `scout:slim`; tunnel = user unit `scout-tunnel`.

## 0. Night-before checklist (2 min)
```bash
ssh thor ~/.scout-ops/scout-restart.sh     # prints sdk/dash/public codes + battery
```
Expect `200 200 200`, `rover : battery ≥80%`. Put the rover on its charger overnight.
Phone: open https://scout.cagatay.my once → "Unlock with passkey" → Face ID → confirm
the front camera is live. (Passkey "admin passkey" is enrolled; rp_id = scout.cagatay.my.)

## 1. Ten-minute demo script
| min | beat | what to do / say |
|---|---|---|
| 0–1 | **Lock screen** | Open scout.cagatay.my on the phone. "Passwordless — a WebAuthn passkey is the only key; the rover holds the public half." Face ID → dashboard. |
| 1–3 | **Live eyes** | Front + rear camera tiles, telemetry strip (battery, signal, heading). Toggle the lamp. Point out YOLO labels appearing (it sees "suitcase", people, doors). |
| 3–5 | **Drive** | WASD on laptop or joystick on phone. Short moves; forward, turn, back. Explain the 500 ms dead-man + SDK speed presets. |
| 5–7 | **Talk to it** | Chat panel: "what do you see?" → agent grabs a frame, describes the room. "look around" → it turns and sends photos. Persona = curious small robot. |
| 7–8 | **Telegram** | Show the bot: `/state`, `/photo`. Same tools, different surface. |
| 8–9 | **Memory / data** | `/replay` page — recorded episodes (LeRobot v3 + ECoT). "Every drive becomes a dataset." |
| 9–10 | **Autonomy (optional)** | Only if the room is clear: start the thinker `~/.scout-ops/scout-restart.sh --with-thinker` — it observes every 60 s and (with DRIVE=1) makes 1-step exploratory nudges. Stop with `docker stop scout-slim-thinker`. |

Keep the phone in landscape for the joystick. Don't drive faster than the "walk" preset indoors.

## 2. Bring-up on the phone hotspot (venue Wi-Fi dead)
Thor has Wi-Fi (`wlP1p1s0`, currently `Verizon_SG4VBJ`). A `neon_net` fallback profile needs sudo
(not passwordless) — run once, at home, before leaving:
```bash
ssh thor
sudo nmcli con add type wifi ifname wlP1p1s0 con-name neon_net ssid neon_net \
  wifi-sec.key-mgmt wpa-psk wifi-sec.psk cagatay4321 \
  connection.autoconnect yes connection.autoconnect-priority -10
sudo nmcli con mod Verizon_SG4VBJ connection.autoconnect-priority 10
```
At the venue: turn on the iPhone hotspot **neon_net** → Thor joins within ~30 s → cloudflared
reconnects on its own (`Restart=always`) → scout.cagatay.my is back. The rover itself talks to
FrodoBots' cloud over its own LTE/Wi-Fi; the SDK on Thor just needs internet. Reach the Thor
through Tailscale (`tailscale0` is up) if its LAN IP changes.

## 3. One-command restart
```bash
ssh thor ~/.scout-ops/scout-restart.sh            # sdk dashboard media yolo telegram (thinker stays stopped)
ssh thor ~/.scout-ops/scout-restart.sh --with-thinker
```

## 4. When scout.cagatay.my 502s
1. `ssh thor 'curl -sk -o /dev/null -w %{http_code} https://localhost:8080/api/health'`
   - `000` → dashboard down → `~/.scout-ops/scout-restart.sh`.
   - `200` → origin fine → tunnel: `systemctl --user restart scout-tunnel; journalctl --user -u scout-tunnel -n 20`.
2. `docker ps` shows nothing at all → docker restarted without images? `docker images | grep scout:slim`.
   If the image is gone: rebuild in the background (~2 min with cache, ~20 min cold):
   `cd ~/scout-the-rover && nohup docker compose -f docker-compose.slim.yml -f docker-compose.slim.override.yml build > ~/.scout-ops/build.log 2>&1 &`
3. Rover "OFFLINE" while dashboard is 200 → rover battery/LTE. Power-cycle the rover (hold power 3 s),
   wait for the FrodoBots app to show it online, then `/state` in Telegram.
4. `/api/health` says `"sdk": false` → `docker restart scout-slim-sdk` (headless Chromium inside re-joins Agora, ~30 s).

## 5. What's where
- Thor clone `~/scout-the-rover` @ e08fa9b (ahead of Mac 5cb042f by 2 — voice debounce, telegram allowlist logging, replay fallback). `.env` there is the source of truth (backups `.env.bak-*`).
- Passkeys + mkcert TLS: docker volume `scout-the-rover_scout-auth-slim` — **never `docker compose down -v`**.
- Tunnel: `~/.cloudflared/config.yml` (tunnel 37a41da8…, also serves printer.cagatay.my → :8099).
- Autostart: docker.service enabled + containers `restart: unless-stopped`; user linger on so `scout-tunnel` starts at boot. Thinker is `restart=no` on purpose.
- Logs: `docker logs -f scout-slim-dashboard` · `journalctl --user -u scout-tunnel -f`.
