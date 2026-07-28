"""Managing agents from the web HUD, end to end.

The HUD never runs a harness. It POSTs to the daemon, the daemon asks a cockpit
over the authenticated transport, and the cockpit decides. These drive that
whole chain with a real RemoteServer standing in for the cockpit, so a change
that breaks the wiring between the three fails here rather than in a browser.

What is being protected:
  * spawning is remote code execution and stays fail-closed on `confirm`
  * the target is resolved from discovery, never from the request body
  * the instance validates the action; the caller does not get to assert it
  * an unreachable cockpit is reported as such, never as success
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
def cockpit(tmp_path, monkeypatch):
    """A live instance answering /task and /run, discoverable as "worker"."""
    from aion.remotes import RemoteServer

    home = tmp_path / "aionhome"
    inst = home / "instances" / "worker"
    inst.mkdir(parents=True)
    monkeypatch.setenv("AION_HOME", str(home))
    from aion import fleet, sshlink
    monkeypatch.setattr(fleet, "AION_HOME", home)
    monkeypatch.setattr(sshlink, "AION_HOME", home)

    seen: list = []
    loop = asyncio.new_event_loop()
    srv = RemoteServer(host="127.0.0.1", port=0, token="")
    srv.on_status = lambda: {"running_count": 1, "active_harness": "demo"}
    srv.on_task = lambda tid, action: (
        seen.append(("task", tid, action))
        or {"ok": action != "pause" or tid == "t1",
            "action": action, "task_id": tid,
            "reason": "" if (action != "pause" or tid == "t1") else "already done"})
    srv.on_run = lambda p, h: (seen.append(("run", p, h))
                               or {"ok": True, "task_id": "t9", "harness": h})

    ready = threading.Event()

    def run():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(srv.start())
        ready.set()
        loop.run_forever()

    threading.Thread(target=run, daemon=True).start()
    assert ready.wait(5)
    port = srv._server.sockets[0].getsockname()[1]
    (inst / "meta.json").write_text(json.dumps({
        "id": "worker", "pid": os.getpid(), "port": port,
        "hostname": "testbox", "active_harness": "demo",
        "running_count": 1, "updated_at": time.time()}))

    import aion_web
    monkeypatch.setattr(aion_web, "_SSH_CACHE", {"at": 0.0, "rows": []})
    yield aion_web, seen
    loop.call_soon_threadsafe(loop.stop)


# ── control ──────────────────────────────────────────────────────────────────
def test_control_reaches_the_cockpit(cockpit):
    web, seen = cockpit
    out = web.agent_control("worker", "t1", "pause")
    assert out["ok"] is True
    assert ("task", "t1", "pause") in seen


def test_control_surfaces_the_cockpits_refusal(cockpit):
    """The instance owns the decision. The HUD reports it rather than
    second-guessing it, so the two can never disagree."""
    web, _ = cockpit
    out = web.agent_control("worker", "t7", "pause")
    assert out["ok"] is False and out["reason"] == "already done"


@pytest.mark.parametrize("action", ["pause", "resume", "cancel", "rerun"])
def test_every_action_is_carried(cockpit, action):
    web, seen = cockpit
    web.agent_control("worker", "t1", action)
    assert ("task", "t1", action) in seen


def test_unknown_action_never_leaves_the_daemon(cockpit):
    """Refused locally, so a typo cannot become a request to a machine."""
    web, seen = cockpit
    out = web.agent_control("worker", "t1", "delete")
    assert out["ok"] is False and "delete" in out["reason"]
    assert seen == []


def test_control_needs_a_task(cockpit):
    web, seen = cockpit
    assert web.agent_control("worker", "", "cancel")["ok"] is False
    assert seen == []


def test_unknown_instance_is_refused(cockpit):
    web, seen = cockpit
    out = web.agent_control("nowhere", "t1", "cancel")
    assert out["ok"] is False and "no instance named" in out["reason"]
    assert seen == []


def test_a_host_in_the_body_is_not_a_target(cockpit):
    """Rule 1: the caller names an id resolved from discovery. There is no
    parameter that lets a LAN-reachable HUD point this at a machine."""
    web, _ = cockpit
    import inspect
    src = inspect.getsource(web.agent_control) + inspect.getsource(web._instance_node)
    assert "body.get(\"host\"" not in src and "params['host']" not in src
    assert web.agent_control("127.0.0.1:8765", "t1", "cancel")["ok"] is False


# ── spawn ────────────────────────────────────────────────────────────────────
def test_spawn_is_fail_closed(cockpit):
    """No confirm, nothing runs. Same rule as routing, for the same reason."""
    web, seen = cockpit
    out = web.agent_spawn("worker", "demo", "build the thing")
    assert out["ok"] is False
    assert "resend with confirm" in out["reason"]
    assert seen == [], "a preview reached the cockpit"


def test_spawn_runs_when_confirmed(cockpit):
    web, seen = cockpit
    out = web.agent_spawn("worker", "demo", "build the thing", confirm=True)
    assert out["ok"] is True
    assert ("run", "build the thing", "demo") in seen


def test_confirm_must_be_the_boolean(cockpit):
    """A truthy string arriving from JSON must not release execution."""
    web, seen = cockpit
    assert web.agent_spawn("worker", "demo", "x", confirm="yes")["ok"] is False
    assert web.agent_spawn("worker", "demo", "x", confirm=1)["ok"] is False
    assert seen == []


def test_spawn_carries_the_named_harness(cockpit):
    """on_run used to drop the harness argument, so a task pinned to a harness
    silently ran on whatever was active."""
    web, seen = cockpit
    web.agent_spawn("worker", "research", "look into X", confirm=True)
    assert ("run", "look into X", "research") in seen


def test_spawn_needs_a_prompt_and_a_harness(cockpit):
    web, seen = cockpit
    assert web.agent_spawn("worker", "demo", "  ", confirm=True)["ok"] is False
    assert web.agent_spawn("worker", "", "do it", confirm=True)["ok"] is False
    assert seen == []


# ── honesty about failure ────────────────────────────────────────────────────
def test_dead_cockpit_is_not_reported_as_success(tmp_path, monkeypatch):
    """RemoteClient turns transport errors into None. Reporting "done" there
    would tell the user their agent is running when nothing received it."""
    home = tmp_path / "h"
    inst = home / "instances" / "ghost"
    inst.mkdir(parents=True)
    monkeypatch.setenv("AION_HOME", str(home))
    from aion import fleet, sshlink
    monkeypatch.setattr(fleet, "AION_HOME", home)
    monkeypatch.setattr(sshlink, "AION_HOME", home)
    # A port with nothing listening on it.
    (inst / "meta.json").write_text(json.dumps({
        "id": "ghost", "pid": os.getpid(), "port": 9, "updated_at": time.time()}))

    import aion_web
    monkeypatch.setattr(aion_web, "_SSH_CACHE", {"at": 0.0, "rows": []})
    out = aion_web.agent_control("ghost", "t1", "cancel")
    assert out["ok"] is False
    assert "did not answer" in out["reason"]
    assert "./aion.sh" in out["reason"], "the message must say how to fix it"
