"""Tests for fleet identity, atomic writes, discovery and health."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from aion import fleet


# ── identity & ports ─────────────────────────────────────────────────────────
def test_instance_id_defaults_to_main(monkeypatch):
    monkeypatch.delenv("AION_INSTANCE", raising=False)
    assert fleet.instance_id() == fleet.DEFAULT_INSTANCE


def test_instance_id_sanitises_path_separators(monkeypatch):
    """The id becomes a directory name -- it must not escape ~/.aion."""
    monkeypatch.setenv("AION_INSTANCE", "../../etc/passwd")
    assert "/" not in fleet.instance_id()
    assert ".." not in fleet.instance_id()


def test_default_instance_keeps_legacy_port(monkeypatch):
    monkeypatch.delenv("AION_INSTANCE", raising=False)
    assert fleet.alloc_port() == 8765


def test_alloc_port_is_deterministic_and_in_range():
    assert fleet.alloc_port("hud") == fleet.alloc_port("hud")
    for name in ("hud", "pi5", "laptop-b", "workstation"):
        assert fleet.BASE_PORT <= fleet.alloc_port(name) < fleet.BASE_PORT + 100


# ── atomic write ─────────────────────────────────────────────────────────────
def test_write_json_atomic_roundtrip(tmp_path):
    target = tmp_path / "nested" / "state.json"
    fleet.write_json_atomic(target, {"a": 1})
    assert json.loads(target.read_text()) == {"a": 1}


def test_write_json_atomic_leaves_no_tmp_files(tmp_path):
    d = tmp_path / "only-this"      # tmp_path also holds the isolated HOME
    target = d / "state.json"
    fleet.write_json_atomic(target, {"a": 1})
    assert list(d.iterdir()) == [target]


def test_write_json_atomic_preserves_old_file_on_failure(tmp_path):
    """A failed write must not truncate the previous good state."""
    target = tmp_path / "state.json"
    fleet.write_json_atomic(target, {"good": True})

    class Unserialisable:
        pass

    with pytest.raises(TypeError):
        fleet.write_json_atomic(target, {"bad": Unserialisable()})
    assert json.loads(target.read_text()) == {"good": True}


# ── discovery ────────────────────────────────────────────────────────────────
def _fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet, "AION_HOME", tmp_path / ".aion")
    return tmp_path / ".aion"


def test_discover_local_finds_self(tmp_path, monkeypatch):
    _fake_home(tmp_path, monkeypatch)
    monkeypatch.setenv("AION_INSTANCE", "cockpit")
    hb = fleet.Heartbeat()
    hb.beat(active_harness="demo", running_count=2)

    peers = fleet.discover_local()
    assert len(peers) == 1
    assert peers[0].id == "cockpit"
    assert peers[0].is_self is True
    assert peers[0].running_count == 2
    assert peers[0].age_s() < 5


def test_discover_local_can_exclude_self(tmp_path, monkeypatch):
    _fake_home(tmp_path, monkeypatch)
    monkeypatch.setenv("AION_INSTANCE", "cockpit")
    fleet.Heartbeat().beat()
    assert fleet.discover_local(include_self=False) == []


def test_discover_local_prunes_dead_pid(tmp_path, monkeypatch):
    home = _fake_home(tmp_path, monkeypatch)
    monkeypatch.setenv("AION_INSTANCE", "ghost")
    hb = fleet.Heartbeat()
    hb.beat()
    # rewrite the heartbeat with a pid that cannot exist
    raw = json.loads(hb.path.read_text())
    raw["pid"] = 2 ** 22
    fleet.write_json_atomic(hb.path, raw)

    assert fleet.discover_local() == []
    assert not hb.path.exists(), "stale meta.json should be reaped"


def test_discover_local_survives_corrupt_meta(tmp_path, monkeypatch):
    home = _fake_home(tmp_path, monkeypatch)
    d = home / "instances" / "broken"
    d.mkdir(parents=True)
    (d / "meta.json").write_text("{not json")
    assert fleet.discover_local() == []


def test_heartbeat_clear_is_idempotent(tmp_path, monkeypatch):
    _fake_home(tmp_path, monkeypatch)
    monkeypatch.setenv("AION_INSTANCE", "cockpit")
    hb = fleet.Heartbeat()
    hb.beat()
    hb.clear()
    hb.clear()  # must not raise
    assert not hb.path.exists()


def test_state_roots_are_separate_per_instance(tmp_path, monkeypatch):
    _fake_home(tmp_path, monkeypatch)
    monkeypatch.setenv("AION_INSTANCE", "a")
    root_a = fleet.instance_root()
    monkeypatch.setenv("AION_INSTANCE", "b")
    root_b = fleet.instance_root()
    assert root_a != root_b
    assert fleet.shared_root() == fleet.AION_HOME / "shared"


# ── health ───────────────────────────────────────────────────────────────────
def test_never_contacted_is_unknown_not_offline():
    """RemoteNode.age_s() returns a 9999 sentinel when never seen -- that must
    not read as 'this machine died'."""
    assert fleet.node_health(ever_seen=False, age_s=9999.0) == fleet.HEALTH_UNKNOWN
    assert fleet.node_health(ever_seen=False, age_s=0.0) == fleet.HEALTH_UNKNOWN


@pytest.mark.parametrize("age,expected", [
    (0.0, fleet.HEALTH_LIVE),
    (14.9, fleet.HEALTH_LIVE),
    (15.0, fleet.HEALTH_STALE),
    (29.9, fleet.HEALTH_STALE),
    (30.0, fleet.HEALTH_OFFLINE),
    (600.0, fleet.HEALTH_OFFLINE),
])
def test_local_health_thresholds(age, expected):
    assert fleet.node_health(True, age, local=True) == expected


@pytest.mark.parametrize("age,expected", [
    (0.0, fleet.HEALTH_LIVE),
    (19.9, fleet.HEALTH_LIVE),
    (20.0, fleet.HEALTH_STALE),
    (59.9, fleet.HEALTH_STALE),
    (60.0, fleet.HEALTH_OFFLINE),
])
def test_remote_health_thresholds(age, expected):
    assert fleet.node_health(True, age, local=False) == expected


def test_remote_is_more_patient_than_local():
    """Same silence, different verdict: the network is the unreliable part."""
    assert fleet.node_health(True, 17.0, local=True) == fleet.HEALTH_STALE
    assert fleet.node_health(True, 17.0, local=False) == fleet.HEALTH_LIVE


def test_load_buys_no_grace():
    """A node too busy to answer is exactly the node you want flagged."""
    assert fleet.node_health(True, 45.0, local=True) == fleet.HEALTH_OFFLINE


# ── token & exposure ─────────────────────────────────────────────────────────
def test_token_is_created_once_and_reused():
    first = fleet.load_or_create_token()
    assert len(first) >= 32
    assert fleet.load_or_create_token() == first


def test_token_file_is_not_world_readable():
    """It authorises command execution -- 0600 or it is not a secret."""
    fleet.load_or_create_token()
    mode = fleet.token_path().stat().st_mode & 0o777
    assert mode == 0o600, f"token mode {oct(mode)}"


def test_listen_host_is_loopback_unless_opted_in(monkeypatch):
    monkeypatch.delenv("AION_LISTEN", raising=False)
    assert fleet.listen_host() == "127.0.0.1"
    monkeypatch.setenv("AION_LISTEN", "lan")
    assert fleet.listen_host() == "0.0.0.0"


def test_server_rejects_missing_and_wrong_token():
    from aion.remotes import RemoteServer
    srv = RemoteServer(token="sekrit")
    assert srv._authorised({}) is False
    assert srv._authorised({fleet.TOKEN_HEADER: "nope"}) is False
    assert srv._authorised({fleet.TOKEN_HEADER: "sekrit"}) is True


def test_server_defaults_to_loopback_and_a_real_token(monkeypatch):
    from aion.remotes import RemoteServer
    monkeypatch.delenv("AION_LISTEN", raising=False)
    srv = RemoteServer()
    assert srv.host == "127.0.0.1"
    assert srv.token, "a default-constructed server must not be open"


@pytest.mark.asyncio
async def test_unauthorised_request_gets_401_and_no_handler_runs():
    """The whole point: /run must not execute for an unauthenticated caller."""
    import asyncio
    from aion.remotes import RemoteClient, RemoteNode, RemoteServer

    ran = []
    srv = RemoteServer(host="127.0.0.1", port=18999, token="right-token")
    srv.on_run = lambda p, h: ran.append(p) or {"task_id": "t1"}
    await srv.start()
    try:
        node = RemoteNode(id="t", host="127.0.0.1", port=18999)
        assert await RemoteClient(token="wrong").run_task(node, "rm -rf /") is None
        assert ran == [], "handler ran for an unauthorised caller"

        assert await RemoteClient(token="right-token").run_task(node, "ok") is not None
        assert ran == ["ok"]
    finally:
        await srv.stop()


# ── migration ────────────────────────────────────────────────────────────────
def test_migrate_moves_legacy_files_into_place(tmp_path, monkeypatch):
    home = _fake_home(tmp_path, monkeypatch)
    monkeypatch.delenv("AION_INSTANCE", raising=False)
    home.mkdir(parents=True, exist_ok=True)
    (home / "todos.md").write_text("- [ ] keep me")
    (home / "session.json").write_text("[]")

    moved = fleet.migrate_legacy()

    assert set(moved) == {"todos.md", "session.json"}
    assert (fleet.shared_root() / "todos.md").read_text() == "- [ ] keep me"
    assert (fleet.instance_root("main") / "session.json").exists()
    assert not (home / "todos.md").exists()


def test_migrate_never_clobbers_an_existing_destination(tmp_path, monkeypatch):
    home = _fake_home(tmp_path, monkeypatch)
    home.mkdir(parents=True, exist_ok=True)
    (home / "todos.md").write_text("legacy")
    fleet.shared_root().mkdir(parents=True, exist_ok=True)
    (fleet.shared_root() / "todos.md").write_text("current")

    assert fleet.migrate_legacy() == []
    assert (fleet.shared_root() / "todos.md").read_text() == "current"
    assert (home / "todos.md").exists(), "legacy file kept for manual recovery"


def test_migrate_is_idempotent(tmp_path, monkeypatch):
    home = _fake_home(tmp_path, monkeypatch)
    home.mkdir(parents=True, exist_ok=True)
    (home / "memory.json").write_text("[]")
    assert fleet.migrate_legacy() == ["memory.json"]
    assert fleet.migrate_legacy() == []


def test_stores_land_in_the_new_layout(tmp_path, monkeypatch):
    """Shared data is shared; the task registry is not."""
    _fake_home(tmp_path, monkeypatch)
    monkeypatch.delenv("AION_INSTANCE", raising=False)
    from aion.core import SessionStore
    from aion.memory import MemoryStore
    from aion.todos import default_path

    assert SessionStore().path.parent == fleet.instance_root("main")
    assert MemoryStore().path.parent == fleet.shared_root()
    assert default_path().parent == fleet.shared_root()


# ── panel ────────────────────────────────────────────────────────────────────
THEME = {"accent": "#5ad1ff", "ok": "#7CFFB2", "warn": "#FFD479",
         "err": "#FF6B6B", "dim": "#5a6b7b"}


def _strip(markup: str) -> str:
    """Drop Rich colour tags so assertions read against the visible text."""
    import re
    return re.sub(r"\[/?[^\]]*\]", "", markup)


def _row(**kw):
    from aion.ui.fleet_panel import FleetRow
    base = dict(id="n", addr="127.0.0.1:8765", health=fleet.HEALTH_LIVE)
    base.update(kw)
    return FleetRow(**base)


def test_panel_empty_state_tells_you_what_to_do():
    from aion.ui.fleet_panel import render_fleet
    out = _strip(render_fleet([], THEME))
    assert "AION_INSTANCE=hud" in out


def test_panel_groups_self_local_and_remote():
    from aion.ui.fleet_panel import render_fleet
    rows = [
        _row(id="pi5", local=False),
        _row(id="hud", local=True),
        _row(id="main", local=True, is_self=True),
    ]
    out = _strip(render_fleet(rows, THEME))
    assert out.index("THIS NODE") < out.index("LOCAL") < out.index("REMOTE")
    assert out.index("main") < out.index("hud") < out.index("pi5")


def test_panel_sorts_sick_nodes_above_healthy_ones():
    """The node needing attention should not be buried under healthy ones."""
    from aion.ui.fleet_panel import render_fleet
    rows = [
        _row(id="aaa-healthy", health=fleet.HEALTH_LIVE),
        _row(id="zzz-broken", health=fleet.HEALTH_OFFLINE, age_s=90),
    ]
    out = _strip(render_fleet(rows, THEME))
    assert out.index("zzz-broken") < out.index("aaa-healthy")


def test_panel_never_prints_the_age_sentinel():
    from aion.ui.fleet_panel import render_fleet
    out = _strip(render_fleet(
        [_row(health=fleet.HEALTH_UNKNOWN, age_s=9999.0)], THEME))
    assert "9999" not in out
    assert "never seen" in out


def test_panel_pulse_animates_only_for_busy_nodes():
    from aion.ui.fleet_panel import render_fleet
    busy = [_row(running=2, history=[0.5] * 8)]
    idle = [_row(running=0, history=[0.0] * 8)]
    assert _strip(render_fleet(busy, THEME, tick=0)) != \
           _strip(render_fleet(busy, THEME, tick=1))
    assert _strip(render_fleet(idle, THEME, tick=0)) == \
           _strip(render_fleet(idle, THEME, tick=1))


def test_panel_hides_vitals_for_unreachable_nodes():
    """No fake sparkline for a machine that is not answering."""
    from aion.ui.fleet_panel import render_fleet
    out = _strip(render_fleet(
        [_row(health=fleet.HEALTH_OFFLINE, age_s=120, harness="cyclops")], THEME))
    assert "cyclops" not in out
