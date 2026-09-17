# Fleet bridge — tiny.technology MCP inside Scout

`tools/tiny_mcp.py` mounts the **tiny-tech MCP server** (the same `npx tiny-tech`
that Claude Code / the Mac TUI use) as Strands tools inside Scout's personas (dashboard chat, telegram, thinker, rover-voice).
With it the robot's agent can reach the owner's *other* devices:

| tool | what it gives the robot |
|---|---|
| `use_device` (list / invoke / result) | ask **fomo the arm**, **q-the-brain**, **the Mac**, **tiny the Reachy Mini**, the phone… to do something and get the answer back. The Sticky e-ink is reached by invoking the Mac. |
| `mesh_peers`, `mesh_send` | agents on this LAN (zenoh mesh) — only when `TINY_MCP_MESH=1` |
| `tiny_recall`, `tiny_learn` | the owner's cross-agent memory graph (facts, not chatter) |
| `tiny_whoami`, `tiny_events`, `tiny_send_message` | identity, activity feed, DMs |

Everything else the server offers (wallet, x402 payments, schedules, `tiny_unlearn`,
`use_npm/pypi/openapi/memory/image`, shell-ish device tools, forged `my_*` tools) is
**filtered out** by an allow-list in the module (`ALLOWED_TOOLS` + `REJECTED_PREFIXES`).

## Off by default

Nothing changes until `TINY_MCP=1`. With the flag on, the bridge is still **fail-open**:
no node, no `tiny-tech`, no/expired token, server crash at start → the persona simply
starts without fleet tools and logs one warning (`tiny_mcp: … persona runs WITHOUT fleet
tools`). A persona is never blocked or crashed by this feature.

Per-persona: `TINY_MCP_PERSONAS` (default `telegram,dashboard,shell`). The autonomous
**thinker** and the **voice** BidiAgent are opt-in (`TINY_MCP_PERSONAS=telegram,dashboard,thinker`)
— a 30 s heartbeat that can move other robots is a decision for the owner, and Realtime
voice is latency-critical.

## Security model

* **Credential**: a tiny.technology user CLI JWT (aud `tiny-cli`, 90 days) passed to the
  server as `TINY_TOKEN`. It lives only in `.tiny-mcp.env` (mode 600, gitignored) and reaches
  the four agent containers as env. It is **never** in the repo, never logged, and
  `tiny_mcp.status()` only says `token: true/false` + days left. The server gets a minimal env (`server_env()`) — the persona's own
  API keys are not forwarded.
* **Server isolation**: `TINY_HOME=~/.tiny-mcp` (no shared credentials/device files),
  `TINY_MESH=false` unless opted in, `TINY_DISABLE_LOAD_TOOL=true`, node heap capped.
* **Self-invoke guard**: `TINY_SELF_DEVICE_IDS` (this robot's endpoint device id + name).
  `use_device invoke` on itself is refused before it reaches the network.
* **Depth cap = 1**: every relayed prompt is prefixed `[fleet depth=1 from <name>]`; the
  wrapper refuses to relay a prompt that already carries the marker, and a turn that
  ARRIVES from the fleet (`POST /api/chat`, the platform's endpoint-proxy chat) is built
  with `fleet=True` → **no fleet tools at all**. Robot A → Robot B works; B cannot fan
  out further.
* **One server per process**, spawned lazily at the first agent build and reused
  (module singleton with `add_consumer` semantics), retried at most every 5 min after
  a failure.

## Install (Thor, docker image `scout:slim` — done 2026-09-17)

`Dockerfile.scout.slim` adds, after the pip layers (so they stay cached):

* Node 22.14.0 from the official tarball into `/usr/local` (arm64/amd64, no headers/docs),
* `tiny-tech@0.13.9` vendored under **`/opt/tiny-mcp/node_modules`** (`npm i --omit=dev`, ~113 MB)
  so the MCP server needs **no network fetch at runtime**,
* `pip install mcp` (already pulled by strands, pinned ≥1.10).

`tools/tiny_mcp.py` autodetects `/opt/tiny-mcp/node_modules/tiny-tech/dist/cli.js` + `node` on PATH;
override with `TINY_MCP_COMMAND`. Rebuild: `docker compose -f docker-compose.slim.yml -f docker-compose.slim.override.yml build`
(~2 min with cache), then `--profile all up -d --no-deps dashboard telegram thinker` (never `down`).

Config is **outside git**: `.tiny-mcp.env` (mode 600, gitignored) is an extra `env_file` for the
four **agent** services only (dashboard, telegram, thinker, voice) — `sdk`, `media`, `yolo` never
see the token:

```
TINY_MCP=1|0
TINY_TOKEN=<user CLI JWT>            # the only secret
TINY_SELF_DEVICE_IDS=df7dd835-8114-4517-b694-f390b50a0d92,scout-the-rover
TINY_SELF_NAME=scout-the-rover
TINY_MCP_PERSONAS=telegram,dashboard,shell   # add thinker / voice to opt them in
TINY_MCP_HOME=/tmp/tiny-mcp
TINY_MCP_MESH=0
```

The bridge also adds **`POST /api/chat`** (gated like every route): the tiny.technology
endpoint-proxy target for `use_device invoke scout-the-rover`. It runs ONE fresh agent turn
with `fleet=True` (no fleet tools) and returns `{ok, result, reply, seconds}` within 90 s.

### Getting / renewing the token (owner, browser needed)

tiny.technology has no headless device-code login — the consent click *is* the login.
On the Mac:

```sh
export TINY_HOME=$(mktemp -d); npx tiny-tech@0.13.9 login        # approve in the browser
TOKEN=$(python3 -c "import json,os;print(json.load(open(os.environ['TINY_HOME']+'/credentials.json'))['token'])"); rm -rf "$TINY_HOME"
ssh thor "cd ~/scout-the-rover && sed -i '/^TINY_TOKEN=/d' .tiny-mcp.env && echo TINY_TOKEN=$TOKEN >> .tiny-mcp.env && \
  docker compose -f docker-compose.slim.yml -f docker-compose.slim.override.yml --profile all up -d --no-deps dashboard telegram"
```

The JWT is the owner's full account token (there is no scoped/device Bearer yet) — treat
Thor's disk accordingly; revoke by rotating (`tiny_devices` / `/devices` on the web).

## Disable / roll back

* Soft: `sed -i s/^TINY_MCP=1/TINY_MCP=0/ .tiny-mcp.env` then `up -d --no-deps dashboard telegram thinker`.
* Hard: delete the `TINY_TOKEN=` line (fail-open → tools vanish on next container start).
* Code roll-back: `~/backups/scout-pre-mcp-<ts>.tgz` on Thor holds the five patched files + Dockerfile.

## Verify

* Dashboard chat (`/ws/chat`) *"ask reachy to look up and say hi"* → tool receipts
  `use_device {action:list}` then `use_device {action:invoke, device_id: a6d198f8…}`; Reachy's
  own log shows `[fleet depth=1 from scout-the-rover] …` → `reachy_look` + `voice_say` (proven
  2026-09-17, 15.7 s round trip, Reachy moved and spoke).
* Depth cap: `POST /api/chat` with a fleet prompt asking to use `use_device` answers
  "NO FLEET TOOLS" (proven live).
* `pytest tests/test_tiny_mcp.py` — allow-list, self-invoke refusal, depth cap, fail-open
  (no network, no node needed).
