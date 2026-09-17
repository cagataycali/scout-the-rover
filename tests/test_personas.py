"""Personas control plane — allow-list + state parsing, no docker needed."""
import importlib
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "deploy"))


@pytest.fixture()
def flags(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOUT_PERSONA_FLAGS", str(tmp_path / "personas.json"))
    import tools.persona_flags as pf
    importlib.reload(pf)
    return pf


@pytest.fixture()
def personas(flags, monkeypatch, tmp_path):
    monkeypatch.setenv("SCOUT_SUPERVISOR_SOCK", str(tmp_path / "nope.sock"))
    monkeypatch.setenv("SCOUT_SUPERVISOR_TOKEN", "t")
    import personas as p
    importlib.reload(p)
    return p


# ---- flag file -----------------------------------------------------------

def test_flags_default_from_env(flags, monkeypatch):
    monkeypatch.setenv("SCOUT_THINKER_DRIVE", "0")
    monkeypatch.setenv("SCOUT_AUTO_RECORD", "1")
    assert flags.flag("thinker_drive") is False
    assert flags.flag("recording") is True


def test_flag_file_wins_over_env(flags, monkeypatch):
    monkeypatch.setenv("SCOUT_THINKER_DRIVE", "1")
    flags.set_flag("thinker_drive", False, actor="test")
    assert flags.flag("thinker_drive") is False
    data = json.loads(flags.FLAGS_FILE.read_text())
    assert data["thinker_drive"] is False and data["_actor"] == "test"
    flags.set_flag("thinker_drive", True)
    assert flags.flag("thinker_drive") is True


def test_corrupt_flag_file_falls_back(flags):
    flags.FLAGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    flags.FLAGS_FILE.write_text("{not json")
    assert flags.flag("recording") is True  # env default


# ---- thinker hard gate -----------------------------------------------------

def test_thinker_control_gate(flags, monkeypatch):
    import tools._rover_common as rc
    monkeypatch.setenv("SCOUT_PERSONA", "thinker")
    flags.set_flag("thinker_drive", False)
    move = {"command": {"linear": 0.5, "angular": 0.0}}
    stop = {"command": {"linear": 0, "angular": 0}}
    assert rc._thinker_drive_blocked("/control", move) is True
    assert rc._thinker_drive_blocked("/control", stop) is False   # stop frames always pass
    assert rc._thinker_drive_blocked("/data", move) is False
    with pytest.raises(RuntimeError):
        rc.sdk_post("/control", json=move)
    flags.set_flag("thinker_drive", True)
    assert rc._thinker_drive_blocked("/control", move) is False
    monkeypatch.setenv("SCOUT_PERSONA", "dashboard")
    flags.set_flag("thinker_drive", False)
    assert rc._thinker_drive_blocked("/control", move) is False   # only the thinker is gated


# ---- dashboard-side personas -----------------------------------------------

def test_action_parsing(personas):
    for w in ("start", "on", "ON", "1", "enable"):
        assert personas.parse_action(w) == "start"
    for w in ("stop", "off", "0", "disable"):
        assert personas.parse_action(w) == "stop"
    with pytest.raises(ValueError):
        personas.parse_action("restart")
    with pytest.raises(ValueError):
        personas.parse_action("")


def test_state_normalisation(personas):
    assert personas.normalize_state({"state": "running"}) == "running"
    assert personas.normalize_state({"state": "starting"}) == "starting"
    assert personas.normalize_state({"state": "weird"}) == "error"
    assert personas.normalize_state(None) == "error"


def test_unknown_persona_refused(personas):
    with pytest.raises(KeyError):
        personas.toggle("sdk", "stop")          # not in the allow-list
    with pytest.raises(KeyError):
        personas.toggle("../etc", "start")


def test_flag_persona_toggle_without_supervisor(personas):
    r = personas.toggle("recording", "off")
    assert r["ok"] and r["state"] == "stopped" and r["on"] is False
    r = personas.toggle("thinker_drive", "on")
    assert r["state"] == "running"


def test_snapshot_degrades_when_supervisor_down(personas):
    s = personas.snapshot()
    assert s["supervisor"] == "unavailable"
    for name in personas.CONTAINER_PERSONAS:
        assert s["personas"][name]["state"] == "unavailable"
    assert s["personas"]["recording"]["state"] in ("running", "stopped")


def test_container_toggle_needs_supervisor(personas):
    with pytest.raises(personas.SupervisorError):
        personas.toggle("voice", "start")


# ---- host supervisor allow-list --------------------------------------------

def test_supervisor_allowlist(monkeypatch, tmp_path):
    monkeypatch.setenv("SCOUT_SUPERVISOR_TOKEN", "t")
    monkeypatch.setenv("SCOUT_SUPERVISOR_DIR", str(tmp_path))
    import scout_supervisor as sup
    importlib.reload(sup)
    assert set(sup.PERSONAS) == {"voice", "thinker", "telegram"}
    assert set(sup.ACTIONS) == {"start", "stop", "status", "logs"}
    for spec in sup.PERSONAS.values():
        assert spec["container"].startswith("scout-slim-")
        assert spec["stop"] in ("stop", "rm")
    assert sup.PERSONAS["voice"]["stop"] == "rm"          # dead bidi session must not linger
    assert sup.PERSONAS["voice"]["profile"] == "voice"     # only profile that ever starts voice
    assert all(a in sup.COMPOSE for a in ("docker", "compose", "-f"))
    assert "docker-compose.slim.override.yml" in sup.COMPOSE  # same files as the running stack
