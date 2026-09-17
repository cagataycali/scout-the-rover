"""tools/tiny_mcp — allow-list, self-invoke refusal, depth cap, fail-open. No network, no node."""
import base64
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import tiny_mcp as m  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("TINY_MCP", "TINY_TOKEN", "TINY_TOKEN_FILE", "TINY_MCP_COMMAND", "TINY_MCP_PERSONAS",
              "TINY_SELF_DEVICE_IDS", "TINY_SELF_NAME", "TINY_MCP_TINY_TECH", "TINY_MCP_NODE"):
        monkeypatch.delenv(k, raising=False)
    # fresh bridge per test
    m._bridge = m._Bridge()
    yield


def _jwt(exp: int) -> str:
    def b64(o):
        return base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")
    return f"{b64({'alg': 'HS256'})}.{b64({'sub': 'u', 'exp': exp})}.sig"


# ── allow-list ──
def test_allowlist_keeps_only_curated():
    offered = ["use_device", "mesh_peers", "mesh_send", "tiny_recall", "tiny_learn", "tiny_whoami",
               "tiny_send_message", "tiny_events", "tiny_pay_quote", "tiny_pay_confirm", "tiny_unlearn",
               "tiny_schedule", "tiny_wallet", "use_npm", "use_pypi", "use_openapi", "use_memory",
               "use_image", "use_integrations", "tiny_chat", "tiny_delete", "my_weather", "mesh_broadcast"]
    assert m.filter_tool_names(offered) == ["use_device", "mesh_peers", "mesh_send", "tiny_recall",
                                            "tiny_learn", "tiny_whoami", "tiny_send_message", "tiny_events"]


def test_rejected_prefixes_win_even_if_allowed(monkeypatch):
    monkeypatch.setattr(m, "ALLOWED_TOOLS", m.ALLOWED_TOOLS + ("tiny_pay_quote", "use_iphone"))
    assert not m.is_allowed("tiny_pay_quote")
    assert not m.is_allowed("use_iphone")


# ── self-invoke refusal + depth cap ──
def test_self_invoke_refused(monkeypatch):
    monkeypatch.setenv("TINY_SELF_DEVICE_IDS", "a6d198f8-59f1-4952-a225-388ccaa22f29,tiny-the-reachy")
    assert m.guard_use_device("invoke", "a6d198f8-59f1-4952-a225-388ccaa22f29", "look up") is not None
    assert m.guard_use_device("invoke", "A6D198F8", "look up") is not None          # short prefix, any case
    assert m.guard_use_device("invoke", "tiny-the-reachy", "look up") is not None   # by name
    assert m.guard_use_device("invoke", "df7dd835-8114-4517-b694-f390b50a0d92", "say hi") is None
    assert m.guard_use_device("list", None, None) is None
    assert m.guard_use_device("result", None, None) is None


def test_short_ids_do_not_match_by_accident(monkeypatch):
    monkeypatch.setenv("TINY_SELF_DEVICE_IDS", "a6d198f8-59f1-4952-a225-388ccaa22f29")
    assert not m.is_self("a6d")          # < 8 chars never matches
    assert not m.is_self("")
    assert not m.is_self(None)


def test_depth_cap_refuses_relaying_a_fleet_prompt(monkeypatch):
    monkeypatch.setenv("TINY_SELF_NAME", "reachy")
    marked = m.mark_prompt("say hi")
    assert marked.startswith("[fleet depth=1 from reachy] ")
    assert m.looks_like_fleet_turn(marked)
    assert m.guard_use_device("invoke", "df7dd835-8114-4517-b694-f390b50a0d92", marked) is not None
    assert m.guard_use_device("invoke", "df7dd835-8114-4517-b694-f390b50a0d92", "   ") is not None


def test_fleet_turn_gets_no_tools_even_when_enabled(monkeypatch):
    monkeypatch.setenv("TINY_MCP", "1")
    monkeypatch.setenv("TINY_TOKEN", _jwt(int(time.time()) + 3600))
    assert m.get_tools("dashboard", fleet=True) == []
    assert m.prompt_block("dashboard", fleet=True) == ""


# ── fail-open ──
def test_disabled_by_default():
    assert not m.enabled()
    assert m.get_tools("telegram") == []
    assert m.prompt_block("telegram") == ""


def test_autonomous_personas_not_in_default(monkeypatch):
    monkeypatch.setenv("TINY_MCP", "1")
    assert not m.persona_enabled("voice") and not m.persona_enabled("thinker")   # opt-in only
    assert m.persona_enabled("telegram") and m.persona_enabled("dashboard")
    monkeypatch.setenv("TINY_MCP_PERSONAS", "voice,thinker")
    assert m.persona_enabled("voice") and m.persona_enabled("thinker") and not m.persona_enabled("telegram")


def test_missing_token_fails_open(monkeypatch, caplog):
    monkeypatch.setenv("TINY_MCP", "1")
    with caplog.at_level("WARNING", logger="tiny_mcp"):
        assert m.get_tools("telegram") == []
        assert m.get_tools("telegram") == []
    assert sum("WITHOUT fleet tools" in r.message for r in caplog.records) == 1  # warned once


def test_expired_token_is_ignored(monkeypatch, caplog):
    monkeypatch.setenv("TINY_TOKEN", _jwt(int(time.time()) - 10))
    with caplog.at_level("WARNING", logger="tiny_mcp"):
        assert m.load_token() is None
    assert any("expired" in r.message for r in caplog.records)


def test_token_file_wins_and_is_not_logged(tmp_path, monkeypatch, caplog):
    f = tmp_path / "tok"
    f.write_text(_jwt(int(time.time()) + 3600) + "\n")
    monkeypatch.setenv("TINY_TOKEN_FILE", str(f))
    monkeypatch.setenv("TINY_TOKEN", "env-token-should-lose")
    tok = m.load_token()
    assert tok == f.read_text().strip()
    assert tok not in caplog.text
    assert tok not in json.dumps(m.status())  # status() never carries the secret
    assert m.status()["token"] is True


def test_missing_server_binary_fails_open(monkeypatch, caplog):
    monkeypatch.setenv("TINY_MCP", "1")
    monkeypatch.setenv("TINY_TOKEN", _jwt(int(time.time()) + 3600))
    monkeypatch.setenv("TINY_MCP_COMMAND", "/nonexistent/tiny-tech serve")
    monkeypatch.setattr(m, "resolve_command", lambda: None)
    with caplog.at_level("WARNING", logger="tiny_mcp"):
        assert m.get_tools("telegram") == []
    assert any("no node/tiny-tech" in r.message for r in caplog.records)
    assert m.status()["running"] is False and m.status()["failed"] is True


def test_server_start_failure_fails_open(monkeypatch, caplog):
    monkeypatch.setenv("TINY_MCP", "1")
    monkeypatch.setenv("TINY_TOKEN", _jwt(int(time.time()) + 3600))
    monkeypatch.setenv("TINY_MCP_COMMAND", "/bin/false serve")
    monkeypatch.setenv("TINY_MCP_STARTUP_TIMEOUT", "5")
    with caplog.at_level("WARNING", logger="tiny_mcp"):
        assert m.get_tools("telegram") == []
    assert any("failed to start" in r.message or "missing" in r.message for r in caplog.records)


def test_server_env_never_leaks_process_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    env = m.server_env("tok")
    assert "OPENAI_API_KEY" not in env
    assert env["TINY_TOKEN"] == "tok" and env["TINY_MESH"] == "false" and env["TINY_NO_BROWSER"] == "1"
