"""SSH peers, end to end through the web layer.

There is no second machine in CI, so the two halves are simulated separately
and honestly:

  - a real `RemoteServer` on loopback stands in for the remote aion,
  - a fake `ssh` binary opens a real TCP forward to it.

Everything between those — validation, the tunnel pool, the status poll, the
routing adapter, /api/peers, /api/agents — is the shipping code. What is NOT
tested here is ssh's own crypto and authentication, which is exactly the part
we deliberately did not write.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture()
def far_end():
    """A real aion RemoteServer on loopback: the machine at the other end."""
    from aion.remotes import RemoteServer

    loop = asyncio.new_event_loop()
    srv = RemoteServer(host="127.0.0.1", port=0, token="")
    srv.on_status = lambda: {
        "running_count": 2, "active_harness": "claude",
        "hostname": "faraway", "harnesses": ["claude", "codex"]}
    ready = threading.Event()

    def run():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(srv.start())
        ready.set()
        loop.run_forever()

    threading.Thread(target=run, daemon=True).start()
    assert ready.wait(5)
    port = srv._server.sockets[0].getsockname()[1]
    yield port, srv
    loop.call_soon_threadsafe(loop.stop)


@pytest.fixture()
def forwarding_ssh(tmp_path):
    """A fake ssh that really forwards: the local end proxies to the remote.

    A stub that only binds the port would pass a liveness check while proving
    nothing about the request actually arriving, so this one moves bytes.

    It half-closes with shutdown() rather than close(). RemoteClient reads
    until EOF, and a close() issued from one thread while its sibling is
    blocked in recv() on the same fd does not emit FIN on Linux — the file
    description outlives the handle until that syscall returns. The response
    body arrives, EOF never does, and the client times out having read a
    perfectly good reply. shutdown() acts on the socket rather than the
    descriptor, so the FIN goes out immediately.
    """
    script = tmp_path / "fwd-ssh"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import socket, sys, threading\n"
        "argv = sys.argv[1:]\n"
        "lhost, lport, rhost, rport = argv[argv.index('-L') + 1].split(':')\n"
        "def pipe(a, b):\n"
        "    try:\n"
        "        while True:\n"
        "            data = a.recv(65536)\n"
        "            if not data: break\n"
        "            b.sendall(data)\n"
        "    except OSError: pass\n"
        "    finally:\n"
        "        try: b.shutdown(socket.SHUT_WR)\n"
        "        except OSError: pass\n"
        "        try: a.shutdown(socket.SHUT_RD)\n"
        "        except OSError: pass\n"
        "srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "srv.bind((lhost, int(lport))); srv.listen(16)\n"
        "while True:\n"
        "    c, _ = srv.accept()\n"
        "    try:\n"
        "        r = socket.create_connection((rhost, int(rport)), timeout=5)\n"
        "    except OSError:\n"
        "        c.close(); continue\n"
        "    r.settimeout(None)\n"
        "    threading.Thread(target=pipe, args=(c, r), daemon=True).start()\n"
        "    threading.Thread(target=pipe, args=(r, c), daemon=True).start()\n"
    )
    script.chmod(0o755)
    return str(script)


@pytest.fixture()
def peered(tmp_path, monkeypatch, far_end, forwarding_ssh):
    """A configured peer whose tunnel really reaches the far end."""
    port, srv = far_end
    home = tmp_path / "aionhome"
    (home / "instances").mkdir(parents=True)
    monkeypatch.setenv("AION_HOME", str(home))

    from aion import fleet, sshlink
    monkeypatch.setattr(fleet, "AION_HOME", home)
    monkeypatch.setattr(sshlink, "AION_HOME", home)
    sshlink.save_peers([sshlink.SSHPeer(
        id="faraway", host="10.0.0.9", user="gio",
        remote_port=port, label="The Pi")])

    import aion_web
    monkeypatch.setattr(aion_web, "_SSH_POOL", sshlink.TunnelPool(ssh_bin=forwarding_ssh))
    monkeypatch.setattr(aion_web, "_SSH_CACHE", {"at": 0.0, "rows": []})
    yield aion_web
    aion_web._SSH_POOL.close_all()


def test_peer_status_arrives_through_the_tunnel(peered):
    """The whole point, in one assertion: aion on another machine answered a
    /status that was routed over the forward, and nothing above the transport
    had to know."""
    rows = peered.ssh_rows(refresh=True)
    assert len(rows) == 1
    row = rows[0]
    assert row["up"] is True
    assert row["status"]["hostname"] == "faraway"
    assert row["status"]["running_count"] == 2
    assert row["local_port"] >= 8900


def test_peers_snapshot_shape(peered):
    snap = peered.peers_snapshot()
    assert snap["count"] == 1 and snap["up"] == 1
    peer = snap["peers"][0]
    assert peer["reachable"] is True
    assert peer["label"] == "The Pi"
    assert peer["target"] == "gio@10.0.0.9"
    assert peer["active_harness"] == "claude"
    assert "status" not in peer          # flattened for the HUD


def test_peer_is_routable_and_scored(peered):
    plan = peered.route_plan()
    ids = [v["id"] for v in plan["verdicts"]]
    assert "faraway" in ids
    verdict = next(v for v in plan["verdicts"] if v["id"] == "faraway")
    assert verdict["eligible"] is True
    assert any("running" in r for r in verdict["reasons"])


def test_pinning_a_peer_resolves_to_the_tunnel_not_a_hostname(peered):
    """Rule 1 survives the feature: the caller names an id, and the host it
    resolves to is always our own loopback."""
    plan = peered.route_plan(target_id="faraway")
    assert plan["ok"] is True
    assert plan["target"]["host"] == "127.0.0.1"
    assert plan["target"]["port"] == peered._SSH_POOL.local_port("faraway")


def test_unknown_peer_id_is_refused(peered):
    plan = peered.route_plan(target_id="not-a-peer")
    assert plan["ok"] is False and "no instance named" in plan["reason"]


def test_dispatch_still_fail_closed_for_peers(peered):
    """No `confirm`, no remote execution — the rule does not weaken because
    the target is now on another machine. If anything it matters more."""
    out = peered.route_dispatch(prompt="do a thing", target_id="faraway")
    assert out["dispatched"] is False
    assert "preview only" in out["reason"]


def test_dispatch_reaches_the_far_end_when_confirmed(peered, far_end):
    _, srv = far_end
    got: list = []
    srv.on_run = lambda p, h: (got.append((p, h)) or {"task_id": "t1"})
    out = peered.route_dispatch(prompt="run me", target_id="faraway", confirm=True)
    assert out["dispatched"] is True, out.get("error")
    assert got == [("run me", "")]


def test_dead_far_end_is_not_reported_alive(peered, far_end):
    """The half-open failure: ssh stays up, aion does not. The tunnel keeps
    accepting connections, so only a real status poll can tell the truth."""
    _, srv = far_end
    asyncio.run(srv.stop())
    rows = peered.ssh_rows(refresh=True)
    assert rows[0]["up"] is True         # ssh is fine
    assert rows[0]["status"] == {}       # aion is not
    plan = peered.route_plan(target_id="faraway")
    assert plan["ok"] is False and "not answering" in plan["reason"]


def test_peers_appear_in_the_agent_graph(peered):
    snap = peered._with_peers(peered.proc_snapshot())
    remote = [i for i in snap["instances"] if i.get("remote")]
    assert len(remote) == 1
    assert remote[0]["id"] == "faraway"
    assert remote[0]["hostname"] == "faraway"
    assert snap["summary"]["remote_instances"] == 1


def test_no_peers_configured_changes_nothing(tmp_path, monkeypatch):
    """The common case: nobody has peers, and nothing about the HUD moves."""
    home = tmp_path / "h"
    (home / "instances").mkdir(parents=True)
    monkeypatch.setenv("AION_HOME", str(home))
    from aion import fleet, sshlink
    monkeypatch.setattr(fleet, "AION_HOME", home)
    monkeypatch.setattr(sshlink, "AION_HOME", home)
    import aion_web
    monkeypatch.setattr(aion_web, "_SSH_CACHE", {"at": 0.0, "rows": []})
    assert aion_web.ssh_rows(refresh=True) == []
    assert aion_web.peers_snapshot() == {"peers": [], "count": 0, "up": 0}
    snap = {"instances": [], "summary": {}}
    assert aion_web._with_peers(snap) == snap


def test_broken_peers_file_does_not_break_local_routing(tmp_path, monkeypatch):
    """A typo in peers.json must not take away the ability to run anything."""
    home = tmp_path / "h"
    (home / "instances").mkdir(parents=True)
    monkeypatch.setenv("AION_HOME", str(home))
    from aion import fleet, sshlink
    monkeypatch.setattr(fleet, "AION_HOME", home)
    monkeypatch.setattr(sshlink, "AION_HOME", home)
    (home / "peers.json").write_text("{ this is not json")
    import aion_web
    monkeypatch.setattr(aion_web, "_SSH_CACHE", {"at": 0.0, "rows": []})
    plan = aion_web.route_plan()
    assert "verdicts" in plan


def test_cache_stops_a_connect_storm(peered):
    """route/plan runs on every drag-hover; each uncached call is a TCP connect
    per peer."""
    peered.ssh_rows(refresh=True)
    first = peered._SSH_CACHE["at"]
    for _ in range(20):
        peered.ssh_rows()
    assert peered._SSH_CACHE["at"] == first


def test_drawing_the_peer_list_does_not_kill_routing(peered):
    """Regression: peers_snapshot() flattened the CACHED rows in place, popping
    "status" out of the objects route_plan() reads. Opening the peer list made
    every peer look dead to the router until the cache expired — and the HUD
    polls the peer list, so it stayed dead."""
    peered.ssh_rows(refresh=True)
    assert peered.peers_snapshot()["up"] == 1
    plan = peered.route_plan(target_id="faraway")
    assert plan["ok"] is True, plan["reason"]


def test_peers_snapshot_is_repeatable(peered):
    peered.ssh_rows(refresh=True)
    assert peered.peers_snapshot() == peered.peers_snapshot()
