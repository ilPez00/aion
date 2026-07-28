"""Cross-instance dispatch over HTTP.

Sending a task to another instance is remote code execution, so these tests
are mostly about what must NOT happen: no dispatch without confirmation, no
target that did not come from real discovery, no success reported for a task
the fleet never acknowledged.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture()
def fleet_home(tmp_path, monkeypatch):
    """A fleet with one live instance whose pid is this process."""
    home = tmp_path / "aionhome"
    inst = home / "instances" / "worker"
    inst.mkdir(parents=True)
    monkeypatch.setenv("AION_HOME", str(home))
    monkeypatch.setenv("AION_FS_ROOT", str(tmp_path))
    monkeypatch.setenv("AION_FS_DIR", str(tmp_path))

    from aion import fleet
    monkeypatch.setattr(fleet, "AION_HOME", home)
    return home, inst


@pytest.fixture()
def remote(fleet_home):
    """A real RemoteServer on a loopback port, recording what it is asked."""
    from aion.remotes import RemoteServer

    home, inst = fleet_home
    received: list[dict] = []
    loop = asyncio.new_event_loop()
    srv = RemoteServer(host="127.0.0.1", port=0, token="")   # auth off for the test
    srv.on_run = lambda prompt, harness: (
        received.append({"prompt": prompt, "harness": harness})
        or {"task_id": "t9999", "accepted": True})
    srv.on_status = lambda: {"running_count": 0}

    # a live GateBook, as the cockpit would hold
    from aion.hitl import GateBook, GateStore
    book = GateBook()
    store = GateStore(inst / "gates.json")

    def answer(gid, approved):
        g = book.resolve(gid, approved)
        store.publish(book)
        return {"ok": g is not None,
                **({"gate": g.as_dict()} if g else {"error": "no such gate"})}

    srv.on_gate = answer
    srv.book, srv.gate_store = book, store

    ready = threading.Event()

    def run():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(srv.start())
        ready.set()
        loop.run_forever()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert ready.wait(5), "remote server did not start"
    port = srv._server.sockets[0].getsockname()[1]

    (inst / "meta.json").write_text(json.dumps({
        "id": "worker", "pid": os.getpid(), "port": port,
        "hostname": "testbox", "active_harness": "demo",
        "running_count": 0, "updated_at": time.time()}))

    yield port, received, srv
    loop.call_soon_threadsafe(loop.stop)


@pytest.fixture()
def server(fleet_home, monkeypatch):
    from http.server import ThreadingHTTPServer
    import aion_web
    # the fleet transport uses a shared token; the test remote has auth off
    monkeypatch.setattr(aion_web, "TOKEN", "")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), aion_web.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return json.loads(r.read())


def post(base, path, payload):
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# ── planning ─────────────────────────────────────────────────────────────
def test_plan_finds_the_live_instance(server, remote):
    p = get(server, "/api/route/plan")
    assert p["ok"] is True and p["target"]["id"] == "worker"
    assert p["reason"]


def test_plan_explains_itself(server, remote):
    p = get(server, "/api/route/plan")
    assert p["verdicts"] and p["verdicts"][0]["reasons"]


def test_plan_dispatches_nothing(server, remote):
    _, received, _srv = remote
    get(server, "/api/route/plan")
    assert received == []


def test_plan_refuses_an_unknown_target(server, remote):
    p = get(server, "/api/route/plan?target=ghost")
    assert p["ok"] is False and "ghost" in p["reason"]


# ── fail-closed dispatch ─────────────────────────────────────────────────
def test_no_dispatch_without_confirm(server, remote):
    """The safety property: a stray POST must not run anything anywhere."""
    port, received, _srv = remote
    code, r = post(server, "/api/route", {"prompt": "rm -rf /"})
    assert code == 200
    assert r["dispatched"] is False
    assert "preview only" in r["reason"]
    assert received == []


def test_confirm_false_is_still_a_preview(server, remote):
    _, received, _srv = remote
    _, r = post(server, "/api/route", {"prompt": "x", "confirm": False})
    assert r["dispatched"] is False and received == []


def test_a_truthy_string_does_not_count_as_confirmation(server, remote):
    """`confirm` must be the boolean true, not any truthy JSON value."""
    _, received, _srv = remote
    _, r = post(server, "/api/route", {"prompt": "x", "confirm": "yes"})
    assert r["dispatched"] is False and received == []


def test_an_empty_prompt_is_refused(server, remote):
    _, r = post(server, "/api/route", {"prompt": "   ", "confirm": True})
    assert r["ok"] is False and r["dispatched"] is False


# ── real dispatch ────────────────────────────────────────────────────────
def test_confirmed_dispatch_reaches_the_other_instance(server, remote):
    port, received, _srv = remote
    code, r = post(server, "/api/route",
                   {"prompt": "build the parser", "harness": "demo", "confirm": True})
    assert code == 200, r
    assert r["dispatched"] is True, r
    assert r["result"]["task_id"] == "t9999"
    assert received == [{"prompt": "build the parser", "harness": "demo"}]


def test_dispatch_can_be_pinned_to_an_instance(server, remote):
    port, received, _srv = remote
    _, r = post(server, "/api/route",
                {"prompt": "x", "target": "worker", "confirm": True})
    assert r["dispatched"] is True and "pinned" in r["reason"]
    assert len(received) == 1


def test_pinning_to_an_unknown_instance_dispatches_nothing(server, remote):
    _, received, _srv = remote
    _, r = post(server, "/api/route",
                {"prompt": "x", "target": "ghost", "confirm": True})
    assert r["dispatched"] is False and received == []


# ── the target may not be a machine ──────────────────────────────────────
def test_a_host_in_the_request_body_is_ignored(server, remote):
    """The caller may name an instance id; it may not name a machine.

    If host/port were honoured, anyone who could reach this HUD could aim
    aion's /run at an arbitrary address.
    """
    port, received, _srv = remote
    _, r = post(server, "/api/route", {
        "prompt": "x", "confirm": True,
        "host": "10.0.0.99", "port": 31337, "target": "worker"})
    assert r["dispatched"] is True
    assert r["target"]["host"] == "127.0.0.1"      # from discovery, not the body
    assert r["target"]["port"] == port


# ── honest failure ───────────────────────────────────────────────────────
def test_an_unreachable_target_is_not_reported_as_success(server, fleet_home):
    """RemoteClient swallows transport errors into None — the route layer must
    not turn that silence into a success the fleet never acknowledged."""
    home, inst = fleet_home
    (inst / "meta.json").write_text(json.dumps({
        "id": "worker", "pid": os.getpid(), "port": 9,   # discard port
        "hostname": "testbox", "running_count": 0, "updated_at": time.time()}))
    _, r = post(server, "/api/route", {"prompt": "x", "confirm": True})
    assert r["dispatched"] is False
    assert "did not accept" in r.get("error", "")


# ── approval gates over HTTP ─────────────────────────────────────────────
def test_pending_gates_are_visible_to_the_web_process(server, remote):
    """A gate nobody sees is a gate that gets denied — wait() is fail-closed."""
    _, _, srv = remote
    gate = srv.book.request("t0001", "run: rm -rf build/", risk="high")
    srv.gate_store.publish(srv.book)

    g = get(server, "/api/gates")["gates"]
    assert [x["id"] for x in g] == [gate.id]
    assert g[0]["risk"] == "high" and g[0]["instance"] == "worker"


def test_approving_over_http_releases_the_real_gate(server, remote):
    _, _, srv = remote
    gate = srv.book.request("t0001", "run: rm -rf build/")
    srv.gate_store.publish(srv.book)

    code, r = post(server, "/api/gate",
                   {"gate_id": gate.id, "approved": True, "instance": "worker"})
    assert code == 200 and r["ok"] is True
    assert gate.resolved is True and gate.approved is True
    assert get(server, "/api/gates")["gates"] == []


def test_rejecting_over_http_denies_the_real_gate(server, remote):
    _, _, srv = remote
    gate = srv.book.request("t0001", "something risky")
    srv.gate_store.publish(srv.book)
    _, r = post(server, "/api/gate",
                {"gate_id": gate.id, "approved": False, "instance": "worker"})
    assert r["ok"] is True
    assert gate.resolved is True and gate.approved is False


def test_a_non_boolean_approval_does_not_approve(server, remote):
    """Only the literal true approves; this releases a privileged action."""
    _, _, srv = remote
    gate = srv.book.request("t0001", "run: rm -rf /")
    srv.gate_store.publish(srv.book)
    post(server, "/api/gate",
         {"gate_id": gate.id, "approved": "yes", "instance": "worker"})
    assert gate.approved is False


def test_answering_an_unknown_gate_reports_failure(server, remote):
    _, r = post(server, "/api/gate", {"gate_id": "g9999", "approved": True})[0:2]
    assert r["ok"] is False and r["error"]


def test_a_gate_on_a_dead_cockpit_cannot_be_answered(server, fleet_home):
    """Honest failure: the gate stays pending, and pending means denied."""
    _, r = post(server, "/api/gate",
                {"gate_id": "g1", "approved": True, "instance": "ghost"})
    assert r["ok"] is False and "not running" in r["error"]


def test_an_empty_gate_id_is_refused(server, remote):
    _, r = post(server, "/api/gate", {"gate_id": "", "approved": True})
    assert r["ok"] is False
