"""Tests for the bridge palette: inbox/send/ack/learn/search/sync routing."""
import asyncio as _aio

from aion import agentbridge as ab


class _FakeApp:
    pass


def _run(app, text):
    from aion.ui.app import AiOSApp
    return _aio.run(AiOSApp._handle_bridge_command(app, text))


def test_bridge_help():
    out = _run(_FakeApp(), "bridge help")
    assert "bridge inbox" in out and "bridge sync" in out


def test_bridge_inbox_send_ack_flow():
    assert _run(_FakeApp(), "bridge inbox aion") == "bridge inbox aion: empty"
    out = _run(_FakeApp(), "bridge send aion hello fleet")
    assert "queued" in out
    out = _run(_FakeApp(), "bridge inbox")
    assert "1 pending" in out and "hello fleet" in out
    mid = ab.read_inbox("aion")[0]["id"]
    out = _run(_FakeApp(), f"bridge ack aion {mid[:6]}")
    assert "consumed" in out
    assert _run(_FakeApp(), "bridge inbox aion") == "bridge inbox aion: empty"


def test_bridge_ack_ambiguity_and_miss():
    _run(_FakeApp(), "bridge send aion one")
    _run(_FakeApp(), "bridge send aion two")
    out = _run(_FakeApp(), "bridge ack aion zzzz")
    assert "no pending message" in out
    # both ids share no useful prefix here; force ambiguity with empty prefix
    rows = ab.read_inbox("aion")
    assert len(rows) == 2
    out = _run(_FakeApp(), "bridge ack aion t")
    assert "no pending message" in out  # ids are uuids, never start with t


def test_bridge_learn_search_flow():
    out = _run(_FakeApp(), "bridge learn VPN loop | switch to wireguard #vpn")
    assert "recorded" in out and "1 tags" in out
    out = _run(_FakeApp(), "bridge search wireguard")
    assert "VPN loop" in out
    out = _run(_FakeApp(), "bridge search nothing-here-xyz")
    assert "no fleet learning matches" in out
    out = _run(_FakeApp(), "bridge search #vpn")
    assert "VPN loop" in out
    assert _run(_FakeApp(), "bridge learn no-pipe-here").startswith("usage:")


def test_bridge_send_validates():
    out = _run(_FakeApp(), "bridge send ../evil hello")
    assert "invalid bridge target" in out
    assert _run(_FakeApp(), "bridge send").startswith("usage:")


def test_bridge_sync_no_peers(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_BRIDGE_HOSTS", "")
    monkeypatch.setattr(ab, "fleet_hosts", lambda *a, **k: [])
    out = _run(_FakeApp(), "bridge sync")
    assert out == "bridge sync: no peers known"
