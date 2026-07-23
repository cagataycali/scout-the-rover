#!/usr/bin/env python3
"""MCP server entrypoint for Scout the Rover.

Exposes Scout's Strands @tool functions over the Model Context Protocol,
so Claude Code / Desktop / Cursor can drive the rover. Camera tools return
real inline-image blocks over MCP.

Built on strands-mcp-server (https://github.com/cagataycali/strands-mcp-server).

Usage:
    python mcp_server_entry.py                    # stdio (Claude Code) — default
    python mcp_server_entry.py --http --port 8000 # HTTP, multi-client
    python mcp_server_entry.py --tools rover_see,rover_move   # subset
    python mcp_server_entry.py --skip telegram    # drop a tool

Claude Code (register from a venv with scout's requirements installed):
    claude mcp add scout -- /path/to/venv/bin/python /abs/path/scout-the-rover/mcp_server_entry.py

> Tools connect to the rover lazily. The server starts without hardware;
> individual tool calls fail cleanly if the rover isn't reachable.
"""
from __future__ import annotations

import argparse
import importlib
import logging
import os
import sys

# Ensure repo root is importable when run by path from anywhere
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# MCP stdio servers MUST log to stderr — stdout is the protocol channel.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("scout.mcp")

AGGREGATE = ("tools", "ROVER_ALL_TOOLS")


def collect_tools(skip: set[str], only: set[str] | None) -> list:
    mod_path, attr = AGGREGATE
    try:
        mod = importlib.import_module(mod_path)
    except Exception as e:
        logger.error(f"Cannot import {mod_path}: {type(e).__name__}: {e}")
        return []
    tools = list(getattr(mod, attr, []))
    out = []
    for fn in tools:
        name = getattr(fn, "tool_name", getattr(fn, "__name__", str(fn)))
        if only and name not in only:
            continue
        if name in skip:
            logger.info(f"⏭  tool '{name}' skipped by flag")
            continue
        out.append(fn)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scout the Rover MCP server — drive the rover over MCP",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:")[1] if "Usage:" in (__doc__ or "") else "",
    )
    parser.add_argument("--http", action="store_true",
                        help="Run HTTP transport instead of stdio (default: stdio)")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port (default: 8000)")
    parser.add_argument("--stateless", action="store_true",
                        help="Stateless HTTP mode (multi-node scalable)")
    parser.add_argument("--tools", type=str, default=None,
                        help="Comma-separated tool names to expose (default: all available)")
    parser.add_argument("--skip", type=str, default="",
                        help="Comma-separated tool names to drop")
    parser.add_argument("--agent-invocation", action="store_true",
                        help="Also expose invoke_agent for full conversations (default: off — tools only)")
    parser.add_argument("--debug", action="store_true", help="Debug logging")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        from strands import Agent
        from strands_mcp_server.mcp_server import mcp_server
    except ImportError as e:
        logger.error(
            f"Missing dependency: {e}\n"
            "Install with: pip install strands-mcp-server strands-agents"
        )
        sys.exit(1)

    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    only = {t.strip() for t in args.tools.split(",")} if args.tools else None

    tools = collect_tools(skip, only)
    if not tools:
        logger.error("No tools collected — check --tools/--skip flags and installed deps")
        sys.exit(1)

    logger.info(f"🛞  Scout MCP server: {len(tools)} tools ready")

    agent = Agent(
        name="scout-mcp",
        tools=tools + [mcp_server],  # mcp_server must be registered to invoke it
        load_tools_from_directory=False,
        system_prompt="Scout the Rover tool server: FrodoBots Earth Rover — camera (inline images), motion, navigation, mapping, pose, memory, voice.",
        callback_handler=None,
    )

    transport = "http" if args.http else "stdio"
    logger.info(f"Starting MCP server (transport={transport})")
    # Call the raw tool function directly (NOT agent.tool.mcp_server) —
    # agent.tool.* marks the agent as mid-invocation, and since stdio mode
    # blocks forever, all nested tool calls would then be rejected by the SDK.
    _fn = getattr(mcp_server, "_tool_func", None) or getattr(mcp_server, "original_function", None) or mcp_server
    _fn(
        action="start",
        transport=transport,
        port=args.port,
        stateless=args.stateless,
        expose_agent=args.agent_invocation,
        agent=agent,
    )

    if args.http:
        import time
        logger.info(f"HTTP MCP server live at http://localhost:{args.port}/mcp (Ctrl+C to stop)")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            logger.info("Shutting down")


if __name__ == "__main__":
    main()
