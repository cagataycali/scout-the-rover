"""tiny.technology fleet bridge — the `tiny-tech` MCP server as Strands tools.

Gives a robot persona the owner's *other* devices (use_device: fomo, q-the-brain,
the Mac, the other robot, the Sticky via the Mac), the LAN mesh (mesh_peers /
mesh_send), shared cross-agent memory (tiny_recall / tiny_learn), DMs
(tiny_send_message) and the activity feed (tiny_events).

Design rules (see docs/MCP.md):
  * OFF by default — ``TINY_MCP=1`` turns it on. Nothing here can crash a persona:
    missing node, missing/expired token, server failing to start → ``get_tools()``
    returns ``[]`` and logs ONE warning.
  * Curated allow-list, never the server's whole surface (wallet, schedule,
    unlearn, shell-ish device tools are excluded).
  * ``use_device`` is wrapped: a robot refuses to invoke ITSELF and every prompt
    it relays carries a ``[fleet depth=1 …]`` marker. A turn that ARRIVED via the
    fleet (``/api/chat`` from the platform's endpoint proxy) gets no MCP tools at
    all → cross-device chains stop at depth 1.
  * One MCP server per process (module singleton), shared by every agent the
    process builds; the server is spawned lazily on first use.

Env (all optional):
  TINY_MCP=1                 enable
  TINY_TOKEN / TINY_TOKEN_FILE   bearer (user CLI JWT); FILE wins if both set
  TINY_MCP_PERSONAS          comma list of personas that get the tools
                             (default: telegram,dashboard,shell — NOT the autonomous
                             thinker and NOT voice; opt them in explicitly)
  TINY_MCP_COMMAND           full command to run the server (space separated);
                             default = autodetect node + installed tiny-tech
  TINY_MCP_TINY_TECH         path to a tiny-tech package dir (…/node_modules/tiny-tech)
  TINY_MCP_NODE              path to the node binary
  TINY_MCP_MESH=1            let the server join the zenoh mesh (default off: CPU)
  TINY_MCP_HOME              TINY_HOME for the server (default ~/.tiny-mcp)
  TINY_SELF_DEVICE_IDS       comma list of this robot's own device ids / names
  TINY_MCP_TIMEOUT           per-tool-call read timeout seconds (default 90)
"""
from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import threading
import time
from datetime import timedelta
from typing import Any, Dict, Iterable, List, Optional

log = logging.getLogger("tiny_mcp")

# ── policy ──────────────────────────────────────────────────────────
ALLOWED_TOOLS: tuple = (
    "use_device", "mesh_peers", "mesh_send",
    "tiny_recall", "tiny_learn", "tiny_whoami",
    "tiny_send_message", "tiny_events",
)
# Belt and braces: even if ALLOWED_TOOLS is widened later, these never pass.
REJECTED_PREFIXES: tuple = (
    "tiny_pay", "tiny_unlearn", "tiny_schedule", "tiny_delete", "tiny_create",
    "tiny_update", "tiny_wallet", "tiny_login", "tiny_remove_tool", "tiny_reload_tools",
    "use_iphone", "use_computer", "use_shell", "use_npm", "use_pypi", "use_openapi",
    "use_apple", "use_adb", "use_flipper", "use_whatsapp", "use_google", "use_telegram",
    "use_spotify", "use_integrations", "use_memory", "use_image", "shell", "bash", "file",
)
DEFAULT_PERSONAS = ("telegram", "dashboard", "shell")
FLEET_MARKER = "[fleet depth=1 from {name}] "
DEFAULT_TINY_TECH_VERSION = "0.13.9"


def is_allowed(name: str) -> bool:
    """Allow-list filter (pure; unit-tested)."""
    if any(name.startswith(p) for p in REJECTED_PREFIXES):
        return False
    return name in ALLOWED_TOOLS


def filter_tool_names(names: Iterable[str]) -> List[str]:
    return [n for n in names if is_allowed(n)]


def enabled() -> bool:
    return os.getenv("TINY_MCP", "0").strip().lower() in ("1", "true", "on", "yes")


def persona_enabled(persona: str) -> bool:
    raw = os.getenv("TINY_MCP_PERSONAS")
    allowed = tuple(p.strip() for p in raw.split(",") if p.strip()) if raw else DEFAULT_PERSONAS
    return persona in allowed


def self_ids() -> List[str]:
    raw = os.getenv("TINY_SELF_DEVICE_IDS", "")
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


def self_name() -> str:
    return os.getenv("TINY_SELF_NAME") or (self_ids()[0] if self_ids() else "robot")


def is_self(device_id: Optional[str]) -> bool:
    """True when `device_id` (uuid or name, any case, prefix ≥ 8 chars ok) is this robot."""
    if not device_id:
        return False
    d = device_id.strip().lower()
    for s in self_ids():
        if d == s or (len(d) >= 8 and (s.startswith(d) or d.startswith(s))):
            return True
    return False


def looks_like_fleet_turn(text: str) -> bool:
    """A prompt that already carries the fleet marker came from another device."""
    return bool(text) and text.lstrip().startswith("[fleet depth=")


# ── credential ──────────────────────────────────────────────────────
def _jwt_exp(token: str) -> Optional[int]:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload)).get("exp") or 0) or None
    except Exception:  # noqa: BLE001
        return None


def load_token() -> Optional[str]:
    """TINY_TOKEN_FILE (mode-600 file) wins over TINY_TOKEN. Never logged."""
    tok: Optional[str] = None
    path = os.getenv("TINY_TOKEN_FILE")
    if path:
        try:
            with open(os.path.expanduser(path), encoding="utf-8") as fh:
                tok = fh.read().strip() or None
        except OSError as e:
            log.warning("tiny_mcp: TINY_TOKEN_FILE unreadable (%s)", e.__class__.__name__)
            tok = None
    if not tok:
        tok = (os.getenv("TINY_TOKEN") or "").strip() or None
    if not tok:
        return None
    exp = _jwt_exp(tok)
    if exp and exp < time.time():
        log.warning("tiny_mcp: token expired %s — fleet tools disabled (see docs/MCP.md)",
                    time.strftime("%Y-%m-%d", time.gmtime(exp)))
        return None
    return tok


def token_days_left() -> Optional[float]:
    tok = load_token()
    exp = _jwt_exp(tok) if tok else None
    return round((exp - time.time()) / 86400, 1) if exp else None


# ── server command ──────────────────────────────────────────────────
def _candidates() -> List[str]:
    home = os.path.expanduser("~")
    return [
        os.getenv("TINY_MCP_TINY_TECH") or "",
        os.path.join(home, ".local/lib/tiny-mcp/node_modules/tiny-tech"),
        "/opt/tiny-mcp/node_modules/tiny-tech",
        "/usr/local/lib/node_modules/tiny-tech",
        "/usr/lib/node_modules/tiny-tech",
    ]


def _node_bin() -> Optional[str]:
    explicit = os.getenv("TINY_MCP_NODE")
    if explicit and os.access(explicit, os.X_OK):
        return explicit
    local = os.path.expanduser("~/.local/node/bin/node")
    if os.access(local, os.X_OK):
        return local
    return shutil.which("node")


def resolve_command() -> Optional[List[str]]:
    """[command, *args] that runs `tiny-tech serve` on stdio, or None if impossible."""
    explicit = os.getenv("TINY_MCP_COMMAND")
    if explicit:
        return explicit.split()
    node = _node_bin()
    if node:
        for d in _candidates():
            cli = os.path.join(d, "dist", "cli.js") if d else ""
            if cli and os.path.isfile(cli):
                return [node, cli, "serve"]
    npx = shutil.which("npx")
    if npx:
        return [npx, "-y", f"tiny-tech@{DEFAULT_TINY_TECH_VERSION}", "serve"]
    return None


def server_env(token: str) -> Dict[str, str]:
    node = _node_bin()
    path = os.environ.get("PATH", "/usr/bin:/bin")
    if node:
        path = os.path.dirname(node) + os.pathsep + path
    env = {
        "PATH": path,
        "HOME": os.path.expanduser("~"),
        "TINY_TOKEN": token,
        "TINY_HOME": os.path.expanduser(os.getenv("TINY_MCP_HOME", "~/.tiny-mcp")),
        "TINY_MESH": "true" if os.getenv("TINY_MCP_MESH", "0") == "1" else "false",
        "TINY_NO_BROWSER": "1",
        "TINY_DISABLE_LOAD_TOOL": "true",
        "NODE_OPTIONS": "--max-old-space-size=256",
    }
    for k in ("LANG", "LC_ALL", "TINY_API_URL"):
        if os.getenv(k):
            env[k] = os.environ[k]
    return env


# ── the singleton bridge ────────────────────────────────────────────
class _Bridge:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.client = None          # strands MCPClient, started
        self.raw_tools: list = []   # allowed MCPAgentTool objects (minus use_device)
        self.failed = False         # fail-open latch: don't retry every turn
        self.failed_at = 0.0
        self.warned = False

    def warn_once(self, msg: str, *a: Any) -> None:
        if not self.warned:
            log.warning("tiny_mcp: " + msg + " — persona runs WITHOUT fleet tools", *a)
            self.warned = True

    def ensure(self) -> bool:
        with self.lock:
            if self.client is not None:
                return True
            if self.failed and time.time() - self.failed_at < 300:  # retry at most every 5 min
                return False
            token = load_token()
            if not token:
                self.warn_once("no valid TINY_TOKEN / TINY_TOKEN_FILE")
                self._fail()
                return False
            cmd = resolve_command()
            if not cmd:
                self.warn_once("no node/tiny-tech found (TINY_MCP_COMMAND / ~/.local/lib/tiny-mcp)")
                self._fail()
                return False
            try:
                from mcp import StdioServerParameters  # noqa: PLC0415
                from mcp.client.stdio import stdio_client  # noqa: PLC0415
                from strands.tools.mcp import MCPClient  # noqa: PLC0415
            except Exception as e:  # noqa: BLE001
                self.warn_once("python `mcp`/strands MCP support missing (%s)", e.__class__.__name__)
                self._fail()
                return False
            env = server_env(token)
            os.makedirs(env["TINY_HOME"], mode=0o700, exist_ok=True)
            t0 = time.time()
            try:
                client = MCPClient(
                    lambda: stdio_client(StdioServerParameters(command=cmd[0], args=cmd[1:], env=env)),
                    startup_timeout=int(os.getenv("TINY_MCP_STARTUP_TIMEOUT", "45")),
                )
                client.start()
                tools = client.list_tools_sync()
            except Exception as e:  # noqa: BLE001
                self.warn_once("server failed to start (%s: %s)", e.__class__.__name__, str(e)[:120])
                self._fail()
                return False
            names = [t.tool_name for t in tools]
            keep = [t for t in tools if is_allowed(t.tool_name) and t.tool_name != "use_device"]
            self.client = client
            self.raw_tools = keep
            self.has_use_device = "use_device" in names
            log.info("tiny_mcp: server up in %.1fs — %d tools offered, %d allowed (%s%s)",
                     time.time() - t0, len(names), len(keep) + int(self.has_use_device),
                     ",".join(t.tool_name for t in keep), ",use_device" if self.has_use_device else "")
            return True

    def _fail(self) -> None:
        self.failed = True
        self.failed_at = time.time()

    def call(self, name: str, args: Dict[str, Any], tool_use_id: str = "tiny_mcp") -> Dict[str, Any]:
        if not self.ensure():
            return {"status": "error", "content": [{"text": "fleet tools unavailable (tiny-tech MCP not running)"}]}
        timeout = timedelta(seconds=float(os.getenv("TINY_MCP_TIMEOUT", "90")))
        r = self.client.call_tool_sync(tool_use_id, name, args, read_timeout_seconds=timeout)
        return {"status": r.get("status", "error"), "content": r.get("content", [])}

    def stop(self) -> None:
        with self.lock:
            c, self.client, self.raw_tools = self.client, None, []
        if c is not None:
            try:
                c.stop(None, None, None)
            except Exception:  # noqa: BLE001
                pass


_bridge = _Bridge()


def status() -> Dict[str, Any]:
    """For dashboards / health: never includes the token."""
    cmd = resolve_command()
    return {
        "enabled": enabled(),
        "running": _bridge.client is not None,
        "failed": _bridge.failed,
        "token": bool(load_token()),
        "token_days_left": token_days_left(),
        "command": (os.path.basename(cmd[0]) + " …") if cmd else None,
        "self_ids": self_ids(),
        "personas": [p for p in DEFAULT_PERSONAS if persona_enabled(p)] + (["voice"] if persona_enabled("voice") else []),
        "tools": ([t.tool_name for t in _bridge.raw_tools] + (["use_device"] if getattr(_bridge, "has_use_device", False) else [])),
    }


# ── the guarded use_device wrapper ──────────────────────────────────
def guard_use_device(action: str, device_id: Optional[str], prompt: Optional[str]) -> Optional[str]:
    """Return a refusal string, or None when the call may proceed (pure; unit-tested)."""
    if action == "invoke":
        if is_self(device_id):
            return ("refused: that device is ME (this robot). Do the task with my own tools "
                    "instead of relaying to myself.")
        if not prompt or not prompt.strip():
            return "refused: invoke needs a prompt"
        if looks_like_fleet_turn(prompt):
            return "refused: chained fleet relay (depth > 1) is not allowed"
    return None


def mark_prompt(prompt: str) -> str:
    return FLEET_MARKER.format(name=self_name()) + prompt


def _make_use_device_tool():
    from strands import tool  # noqa: PLC0415

    @tool
    def use_device(action: str, device_id: Optional[str] = None, prompt: Optional[str] = None,
                   wait: bool = True, envelope_id: Optional[str] = None) -> Dict[str, Any]:
        """Reach the owner's OTHER devices on tiny.technology (fomo the arm, q-the-brain, the Mac,
        the other robot, the Sticky e-ink via the Mac, the iPhone). action='list' shows every device
        with online presence + capabilities — check it before saying a device is unavailable.
        action='invoke' (device_id, prompt) asks that device's own agent to do something and returns
        its answer (~45 s; a slow task returns pending:true + envelope_id, read it back with
        action='result'). You can NOT invoke yourself, and the device you invoke cannot relay further.
        Never pass wait=False unless the user asked for background work.

        Args:
            action: list | invoke | result
            device_id: target device id from list (invoke)
            prompt: what the device should do (invoke)
            wait: wait up to ~45 s for the answer (invoke, default True)
            envelope_id: pending ticket to read (result)
        """
        refusal = guard_use_device(action, device_id, prompt)
        if refusal:
            return {"status": "error", "content": [{"text": refusal}]}
        args: Dict[str, Any] = {"action": action}
        if device_id:
            args["device_id"] = device_id
        if action == "invoke":
            args["prompt"] = mark_prompt(prompt or "")
            args["wait"] = bool(wait)
        if envelope_id:
            args["envelope_id"] = envelope_id
        try:
            return _bridge.call("use_device", args)
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "content": [{"text": f"fleet call failed: {e.__class__.__name__}: {str(e)[:200]}"}]}

    return use_device


# ── public API ──────────────────────────────────────────────────────
def get_tools(persona: str = "shell", *, fleet: bool = False) -> list:
    """Tools to append to a persona's tool list. Empty when disabled / unavailable / depth-capped.

    fleet=True means this turn ARRIVED from another device (platform endpoint chat) →
    no fleet tools (depth cap). Never raises.
    """
    try:
        if fleet or not enabled() or not persona_enabled(persona):
            return []
        if not _bridge.ensure():
            return []
        tools = list(_bridge.raw_tools)
        if getattr(_bridge, "has_use_device", False):
            tools.append(_make_use_device_tool())
        return tools
    except Exception as e:  # noqa: BLE001
        _bridge.warn_once("unexpected error (%s)", e.__class__.__name__)
        return []


def prompt_block(persona: str = "shell", *, fleet: bool = False) -> str:
    """System-prompt addendum describing the fleet tools (empty when they are not mounted)."""
    if fleet or not enabled() or not persona_enabled(persona) or _bridge.client is None:
        return ""
    return (
        "\n\n## Fleet (tiny.technology)\n"
        "You are one device in the owner's fleet. use_device(action='list') shows the others "
        "(fomo the arm, q-the-brain, the Mac, the rover/reachy, phone); use_device(action='invoke', "
        "device_id, prompt) asks one of them to act and returns its answer. Use it when the owner "
        "asks about or for another device; never to do something you can do yourself; never invoke "
        f"yourself ({self_name()}). tiny_recall/tiny_learn are the owner's shared memory (facts, not chatter). "
        "mesh_peers/mesh_send reach agents on this LAN. Say which device answered.\n"
    )


def shutdown() -> None:
    _bridge.stop()
