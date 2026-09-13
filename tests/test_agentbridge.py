"""Tests for agentbridge.py — bridge protocol, hermetic (tmp bridge home)."""
import json
import os

import pytest

from aion import agentbridge as ab

TARGET = "aion/test"


@pytest.fixture()
def root(tmp_path, monkeypatch):
    home = tmp_path / "abh"
    monkeypatch.setenv("AGENT_BRIDGE_HOME", str(home))
    return home


def test_valid_target():
    assert ab.valid_target("claude-code/default")
    assert ab.valid_target("openclaw/clawdiboi2")
    assert ab.valid_target("codex/release-review")
    for bad in ("", "../x", "/abs", "a//b", "a/", "x" * 257, "has space/x"):
        assert not ab.valid_target(bad), bad


def test_send_read_roundtrip_order(root):
    p1 = ab.send_local(TARGET, "first", from_="t1")
    p2 = ab.send_local(TARGET, "second", from_="t1", type="reply",
                       reply_to="x")
    assert p1.parent == root / "inbox" / TARGET
    rows = ab.read_inbox(TARGET)
    assert [m["content"] for m in rows] == ["first", "second"]
    assert rows[1]["replyTo"] == "x" and rows[1]["fromTarget"] == "aion"
    # schema matches upstream BridgeMessage keys
    assert {"id", "from", "to", "type", "content", "timestamp",
            "replyTo", "ttl", "target"} <= set(rows[0])


def test_send_rejects_bad_target_and_type(root):
    with pytest.raises(ValueError):
        ab.send_local("../evil", "x", from_="t")
    with pytest.raises(ValueError):
        ab.send_local(TARGET, "x", from_="t", type="nuke")


def test_read_skips_junk_and_expired(root):
    d = root / "inbox" / TARGET
    d.mkdir(parents=True)
    (d / "broken.json").write_text("{oops")
    (d / "notjson.txt").write_text("hello")
    (d / "noid.json").write_text(json.dumps({"content": "x"}))
    assert ab.read_inbox(TARGET) == []
    assert ab.read_inbox("missing-target") == []
    p = ab.send_local(TARGET, "old news", from_="t", ttl=1)
    m = json.loads(p.read_text())
    m["timestamp"] = "2001-01-01T00:00:00Z"
    p.write_text(json.dumps(m))
    assert ab.read_inbox(TARGET) == []  # TTL consumed it
    ab.send_local(TARGET, "fresh", from_="t", ttl=0)  # 0 = never expires
    assert len(ab.read_inbox(TARGET)) == 1


def test_ack_moves_and_ledgers(root):
    p = ab.send_local(TARGET, "do the thing", from_="t")
    mid = p.stem
    assert ab.ack(TARGET, mid) is True
    assert ab.read_inbox(TARGET) == []
    assert (root / "inbox" / ".archive" / TARGET / (mid + ".json")).is_file()
    assert mid in (root / "inbox" / ".processed").read_text().split()
    assert ab.read_inbox(TARGET, include_archived=True) != []
    assert ab.ack(TARGET, "no-such-id") is False


def test_learnings_add_search(root):
    e = ab.add_learning("VPN loop", "Symptom: dialog\nFix: wireguard",
                        tags=["macOS", "vpn", "vpn"], harness="cli")
    assert e["tags"] == ["macos", "vpn"] and e["scope"] == "global"
    assert e["v"] == 1 and e["machine"] == ab.local_machine()
    assert {"id", "ts", "machine", "harness", "title", "body", "tags",
            "scope", "v"} <= set(e)  # upstream LearningEntry keys
    ab.add_learning("Other", "unrelated body", tags=["x"])
    assert [h["title"] for h in ab.search_learnings("wireguard")] == ["VPN loop"]
    assert [h["title"] for h in ab.search_learnings(tag="VPN")] == ["VPN loop"]
    assert ab.search_learnings("nope") == []
    with pytest.raises(ValueError):
        ab.add_learning("", "body")


def test_merge_missing_dedupes_by_id(root):
    ab.add_learning("Keep", "body", tags=[])
    mine = list(ab.iter_learnings())
    dup = json.dumps(dict(mine[0]))
    new = json.dumps({"id": "00000000-0000-0000-0000-000000000001",
                      "title": "New", "body": "b"})
    junk = "{broken"
    assert ab.merge_missing(root, [dup, new, junk]) == [new]
    assert ab.merge_missing(root, []) == []


def test_fleet_hosts_from_env(monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_HOSTS", "a-ts b-ts")
    assert ab.fleet_hosts() == ["a-ts", "b-ts"]


def test_sync_with_self_live():
    """Real ssh to this box (FLEET_LIVE=1 only): sync is a no-op union."""
    if os.environ.get("FLEET_LIVE") != "1":
        pytest.skip("needs FLEET_LIVE=1 (ssh)")
    import socket
    me = socket.gethostname() + "-ts"
    res = ab.sync_with(me)
    assert res["host"] == me and res["pulled"] == 0
