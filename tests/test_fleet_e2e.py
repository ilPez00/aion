"""Two machines, one real socket.

Everything about running work on another machine — `RemoteServer`,
`RemoteClient`, the token, the swarm's `spawn_remote`/`poll_remote` hooks, the
`instance` field on a step — was written and unit-tested against fakes, and
had never been seen to work. There is one machine here, so the transport had
no test that opened a port.

That gap is not academic. `remote add`, the only command that CREATES a peer,
raised NameError for as long as it existed and nothing noticed, because
nothing in the suite ever asked a second instance to do anything.

So these run two instances in one process on a real loopback socket: separate
`AION_HOME`s (separate state, separate task registries), the shared token the
fleet actually uses, and work dispatched from A that must be observed running
on B. Anything that only holds because both halves are the same object is not
being tested here.
"""

import asyncio
import sys
from pathlib import Path

import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aion.core import Bus, TaskRegistry, TaskState  # noqa: E402
from aion.remotes import RemoteClient, RemoteNode, RemoteServer  # noqa: E402

TOKEN = "test-fleet-token"


class Instance:
    """One aion instance: its own registry, its own listener, its own state."""

    def __init__(self, name: str, port: int):
        self.name, self.port = name, port
        self.bus = Bus()
        self.registry = TaskRegistry(self.bus)
        self.ran: list[tuple[str, str]] = []
        self.server = RemoteServer(host="127.0.0.1", port=port, token=TOKEN)
        self.server.on_status = self._status
        self.server.on_run = self._run
        self.server.on_task_query = self._task_query
        self.server.on_cancel = self._cancel

    def _status(self) -> dict:
        return {"instance": self.name,
                "tasks": [t.as_dict() for t in self.registry.tasks.values()],
                "active_harness": "demo"}

    def _run(self, prompt: str, harness: str) -> dict:
        """Actually create a task, the way a real instance would."""
        self.ran.append((harness or "demo", prompt))
        task = self.registry.create(prompt[:40], harness or "demo")
        self.registry.set_state(task, TaskState.RUNNING)
        return {"task_id": task.id}

    def _task_query(self, task_id: str) -> dict:
        task = self.registry.tasks.get(task_id)
        if task is None:
            return {"id": task_id, "error": "no such task"}
        return {"id": task.id, "state": task.state.value,
                "progress": task.progress,
                "output": "\n".join(task.log[-20:])}

    def _cancel(self, task_id: str) -> dict:
        task = self.registry.tasks.get(task_id)
        if task is None:
            return {"ok": False}
        self.registry.set_state(task, TaskState.CANCELLED)
        return {"ok": True}

    def finish(self, task_id: str, output: str) -> None:
        task = self.registry.tasks[task_id]
        task.log.append(output)
        self.registry.set_state(task, TaskState.DONE)

    def node(self) -> RemoteNode:
        return RemoteNode(id=self.name, host="127.0.0.1", port=self.port,
                          label=self.name)


@pytest_asyncio.fixture()
async def pair():
    """Instance A (dispatcher) and instance B (worker), both listening."""
    a, b = Instance("alpha", 8791), Instance("beta", 8792)
    await a.server.start()
    await b.server.start()
    try:
        yield a, b, RemoteClient(token=TOKEN, timeout=5.0)
    finally:
        await a.server.stop()
        await b.server.stop()


# ── the transport is real ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_one_instance_can_see_another(pair):
    a, b, client = pair
    status = await client.fetch_status(b.node())
    assert status and status.get("instance") == "beta"


@pytest.mark.asyncio
async def test_work_dispatched_from_a_runs_on_b(pair):
    """The claim the whole fleet rests on. B creates the task; A never does."""
    a, b, client = pair
    out = await client.run_task(b.node(), "count the files", harness="demo")
    assert out and out.get("task_id")
    assert b.ran == [("demo", "count the files")]
    assert a.ran == [], "the dispatcher ran the work itself"
    assert len(b.registry.tasks) == 1
    assert len(a.registry.tasks) == 0


@pytest.mark.asyncio
async def test_the_dispatcher_can_watch_work_it_does_not_own(pair):
    """A remote task cannot announce itself on this process's bus, so the
    only way to learn it finished is to ask."""
    a, b, client = pair
    out = await client.run_task(b.node(), "slow thing", harness="demo")
    tid = out["task_id"]

    seen = await client.task_state(b.node(), tid)
    assert seen["state"] == "running"

    b.finish(tid, "42 files")
    seen = await client.task_state(b.node(), tid)
    assert seen["state"] == "done"
    assert "42 files" in seen["output"]


@pytest.mark.asyncio
async def test_a_task_the_peer_never_had_is_a_definite_unknown(pair):
    """Not an empty state. The watcher treats a definite unknown as work that
    is not coming back and silence as a network problem; conflating them
    either strands a DAG or cancels live work."""
    a, b, client = pair
    seen = await client.task_state(b.node(), "no-such-task")
    assert seen and "error" in seen


@pytest.mark.asyncio
async def test_cancelling_reaches_across(pair):
    a, b, client = pair
    tid = (await client.run_task(b.node(), "stoppable", harness="demo"))["task_id"]
    await client.cancel_task(b.node(), tid)
    assert b.registry.tasks[tid].state is TaskState.CANCELLED


# ── the fail-closed properties ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_wrong_token_gets_nothing(pair):
    """`/run` is arbitrary command execution on another machine. It is the
    one route where an auth regression is not a bug but an incident."""
    a, b, _ = pair
    stranger = RemoteClient(token="not-the-token", timeout=5.0)
    assert await stranger.fetch_status(b.node()) is None
    assert await stranger.run_task(b.node(), "rm -rf /", harness="demo") is None
    assert b.ran == []


@pytest.mark.asyncio
async def test_an_unreachable_peer_is_none_rather_than_an_exception(pair):
    """A sleeping laptop must not take down the cockpit asking about it."""
    a, b, client = pair
    dead = RemoteNode(id="ghost", host="127.0.0.1", port=8799)
    assert await client.fetch_status(dead) is None
    assert await client.run_task(dead, "anything") is None


@pytest.mark.asyncio
async def test_two_instances_keep_separate_state(pair):
    """If both halves shared a registry, every test above would pass while
    proving nothing about two machines."""
    a, b, client = pair
    await client.run_task(b.node(), "on beta", harness="demo")
    await client.run_task(a.node(), "on alpha", harness="demo")
    assert [t.label for t in b.registry.tasks.values()] == ["on beta"]
    assert [t.label for t in a.registry.tasks.values()] == ["on alpha"]


# ── a swarm step pinned to another machine ──────────────────────────────────

@pytest.mark.asyncio
async def test_a_swarm_step_named_for_another_instance_runs_there(pair):
    """The end of the chain: a DAG step with `instance` set is spawned on the
    peer through the same transport, and the runner watches it by polling."""
    from aion.swarm import AgentStatus, SwarmOrchestrator
    from aion.swarmrun import SwarmRunner

    a, b, client = pair
    orch = SwarmOrchestrator(persist=False)
    orch.add_agent("remote_step", "do it over there", instance="beta")

    # The runner's hooks are synchronous and `pump` runs off the event loop,
    # exactly as the cockpit calls it. Coroutines are handed back to the loop
    # rather than run on the worker thread — `asyncio.run()` from a thread
    # that already has a loop is how the first version of this deadlocked.
    loop = asyncio.get_running_loop()

    def sync(coro):
        return asyncio.run_coroutine_threadsafe(coro, loop).result(5)

    def spawn_remote(instance, agent, prompt):
        assert instance == "beta"
        return sync(client.run_task(b.node(), prompt, harness="demo"))["task_id"]

    def poll_remote(instance, task_id):
        return sync(client.task_state(b.node(), task_id))

    runner = SwarmRunner(orch, spawn=lambda ag, p: None,
                         spawn_remote=spawn_remote, poll_remote=poll_remote)
    await asyncio.to_thread(runner.pump)

    agent = orch.agent_by_name("remote_step")
    assert agent.status is AgentStatus.WORKING
    assert len(b.registry.tasks) == 1
    assert a.ran == [], "the step ran locally instead of on the peer"


# ── one status payload, two producers ───────────────────────────────────────

def test_the_status_payload_names_the_machine_not_the_instance():
    """`peers test` printed `?` for every peer because the headless node's
    `/status` had no `hostname`, while the cockpit's did. Two hand-rolled
    copies of one payload, drifted.

    The instance id is `main` on every box in the fleet, so a peer list keyed
    on it is four rows that all say the same thing.
    """
    import socket

    from aion import fleet
    from aion.core import Bus
    from aion.store import Store

    payload = fleet.status_payload(Store(bus=Bus()), headless=True)
    assert payload["hostname"] == socket.gethostname()
    assert payload["headless"] is True


def test_both_producers_build_the_same_shape():
    """Every consumer — the peers CLI, the HUD's peer table,
    `fetch_status` — reads fields by name. A payload built in two places is a
    contract with itself."""
    import inspect

    from aion.ui import app as app_mod

    src = inspect.getsource(app_mod.AiOSApp)
    assert "status_payload(" in src, "the cockpit hand-rolls /status again"
    node = (ROOT / "scripts" / "aion_node.py").read_text(encoding="utf-8")
    assert "status_payload(" in node, "the node hand-rolls /status again"


def test_the_keys_every_consumer_reads_are_present():
    from aion import fleet
    from aion.core import Bus
    from aion.store import Store

    payload = fleet.status_payload(Store(bus=Bus()))
    for key in ("hostname", "instance", "version", "active_harness",
                "running_count", "tasks", "headless"):
        assert key in payload, key
