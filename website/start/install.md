---
title: Install on the brain (Thor)
description: "Clone, build scout:slim, wire the Earth Rovers SDK — on a Jetson AGX Thor or any docker host."
---

# Install on the brain

These steps are what produced the reference deployment on the Jetson AGX Thor. Nothing here is Jetson-specific: any
arm64/amd64 docker host works.

## 1 · Clone

```bash
git clone https://github.com/cagataycali/scout-the-rover.git
cd scout-the-rover
cp .env.example .env            # fill it in — see Configuration
```

The Earth Rovers SDK is **not** vendored (it is `.gitignore`d). The compose `sdk` service and the `make sdk` target both use
our fork, which adds the auto-join, the video watchdog and the fixed Chrome profile directory the containers rely on:

```bash
make sdk                        # git clone cagataycali/earth-rovers-sdk into ./earth-rovers-sdk + venv
$EDITOR earth-rovers-sdk/.env   # SDK_API_TOKEN, BOT_SLUG, CHROME_EXECUTABLE_PATH, SDK_PORT=8002
```

!!! tip "Why 8002?"
    Port 8001 is often taken (docker desktop, other SDKs). Everything in this repo assumes the SDK on **8002** —
    keep `ROVER_SDK_URL` and the SDK's own `SDK_PORT` in agreement.

## 2 · Build the image

```bash
make docker-slim-build          # docker compose -f docker-compose.slim.yml build  → scout:slim
```

What goes into `scout:slim` (`Dockerfile.scout.slim`):

- Debian `python:3.12-slim` (musl/Alpine is avoided on purpose: lerobot/opencv/numpy/cryptography wheels).
- `chromium` for the SDK's headless capture, ALSA/PortAudio for voice, `mkcert` tooling, fonts.
- **CPU-only torch first**, then `requirements.txt`; lerobot only when `INSTALL_LEROBOT=1` (default).
- **node 22.14 + tiny-tech vendored in `/opt/tiny-mcp`** so the fleet bridge needs no runtime npm fetch (off unless `TINY_MCP=1`).
- One `docker/entrypoint.sh` that dispatches on the service name: `sdk dashboard telegram thinker listener media yolo agent voice reasoner warmup all`.

## 3 · Bring the stack up

```bash
make docker-slim-up-all         # --profile all: sdk + dashboard + telegram + thinker (+ media + yolo via the override)
docker compose -f docker-compose.slim.yml -f docker-compose.slim.override.yml --profile all ps
```

The `dashboard` waits for `sdk` to be **healthy** (the SDK health check accepts 200/400 from `/`), then serves
`https://<host>:8080`. First visit → the passkey enrolment screen ([Auth](../guide/auth.md)).

The voice persona is a separate profile because it grabs the rover's mic and speaker:

```bash
docker compose -f docker-compose.slim.yml -f docker-compose.slim.override.yml --profile voice up -d voice
```

## 4 · (Optional) the host-side persona supervisor

The cockpit's 🎭 Personas pills start/stop the `voice`, `thinker` and `telegram` containers **without a docker socket in any
container**. A ~250-line stdlib server on the host listens on a unix socket that is bind-mounted into the dashboard only:

```bash
# deploy/scout-supervisor.service — edit WorkingDirectory/ExecStart paths for your user
mkdir -p ~/.config/systemd/user && cp deploy/scout-supervisor.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now scout-supervisor
echo "SCOUT_SUPERVISOR_TOKEN=$(openssl rand -hex 24)" >> .env        # shared with the dashboard via env_file
```

Details and the threat model: [Personas](../personas.md).

## 5 · (Optional) a public hostname

```bash
cloudflared tunnel login
cloudflared tunnel create scout
cloudflared tunnel route dns scout scout.example.com
```

```yaml
# ~/.cloudflared/config.yml
tunnel: <UUID>
credentials-file: /home/you/.cloudflared/<UUID>.json
ingress:
  - hostname: scout.example.com
    service: https://localhost:8080
    originRequest:
      noTLSVerify: true      # scout's own cert is self-signed; Cloudflare serves real TLS publicly
  - service: http_status:404
```

```bash
sudo cloudflared service install
```

Set `SCOUT_PUBLIC_URL=https://scout.example.com` and `SCOUT_AUTH_RP_ID=scout.example.com` so passkeys bind to the public
name. The tunnel exposes **only** the dashboard; the SDK (:8002) and media hub (:8090) stay on the LAN.

[Next: docker compose in detail →](docker.md){ .md-button .md-button--primary }
