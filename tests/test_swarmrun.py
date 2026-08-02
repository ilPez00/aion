"""The swarm executor: admission, prompts, and a DAG that actually finishes.

Before this, nothing in aion set a swarm agent to DONE. `run_ready()` moved
layer one to WORKING and layer two waited forever. So the headline test here is
the dullest-looking one: a three-step chain reaching the end on its own.

Scheduling is pure and tested directly, because every interesting failure —
over-admission, starvation, a silently stuck layer — reproduces without an
event loop.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aion.swarm import AgentStatus, SwarmOrchestrator  # noqa: E402
from aion.swarmrun import (  # noqa: E402
    Admission, Slot, SwarmRunner, admit, prompt_for,
)


# ── admission: pure ──────────────────────────────────────────────────────────
def slots(n, vram=0):
    return [Slot(id=f"a{i}", name=f"a{i}", vram_mb=vram) for i in range(n)]


def test_admits_up_to_the_parallel_limit():
    out = admit(slots(5), max_parallel=2)
    assert out.admit == ["a0", "a1"]
    assert len(out.deferred) == 3


def test_the_limit_counts_work_already_running():
    """Counting only this batch is how a scheduler ends up with `max_parallel`
    NEW agents every tick regardless of what is already in flight."""
    out = admit(slots(5), running=2, max_parallel=3)
    assert out.admit == ["a0"]


def test_a_full_pipeline_admits_nothing():
    out = admit(slots(3), running=3, max_parallel=3)
    assert out.admit == []
    assert all("parallel limit" in d["reason"] for d in out.deferred)


def test_zero_or_negative_parallelism_still_makes_progress():
    """A misconfigured limit must not deadlock the swarm forever."""
    assert admit(slots(2), max_parallel=0).admit == ["a0"]
    assert admit(slots(2), max_parallel=-5).admit == ["a0"]


def test_vram_is_a_budget_not_a_filter():
    out = admit(slots(4, vram=3000), max_parallel=10, vram_total=8000)
    assert out.admit == ["a0", "a1"]           # 6000 fits, 9000 would not
    assert "VRAM budget" in out.deferred[0]["reason"]


def test_vram_accounts_for_what_is_already_loaded():
    out = admit(slots(2, vram=3000), max_parallel=10,
                vram_total=8000, vram_used=6000)
    assert out.admit == []


def test_an_agent_too_big_to_ever_run_is_named():
    """Silent starvation is the worst thing a scheduler can do: the DAG stops
    and nothing anywhere says why."""
    out = admit([Slot(id="big", name="big", vram_mb=24000),
                 Slot(id="small", name="small", vram_mb=1000)],
                max_parallel=4, vram_total=8000)
    assert out.admit == ["small"], "one impossible agent blocked the rest"
    assert "can never be admitted" in out.deferred[0]["reason"]
    assert "24000" in out.deferred[0]["reason"]


def test_no_vram_budget_means_unlimited():
    """Most harnesses are API-backed and declare nothing."""
    out = admit(slots(3, vram=99999), max_parallel=10, vram_total=0)
    assert len(out.admit) == 3


def test_admission_is_deterministic():
    a = admit(slots(6), max_parallel=3)
    b = admit(slots(6), max_parallel=3)
    assert a.admit == b.admit == ["a0", "a1", "a2"]


def test_admission_serialises():
    assert admit(slots(2), max_parallel=1).as_dict()["admit"] == ["a0"]


# ── prompts: pure ────────────────────────────────────────────────────────────
def test_no_dependencies_means_the_goal_unchanged():
    assert prompt_for("do the thing", []) == "do the thing"


def test_upstream_output_reaches_the_prompt():
    """The reason `writer` waits for `scout` is scout's output. A dependency
    that only means "wait for" wastes most of what a DAG is for."""
    out = prompt_for("draft it", [("scout", "found three sources")])
    assert "draft it" in out
    assert "scout" in out and "found three sources" in out


def test_a_silent_dependency_is_still_named():
    """Hiding it makes the downstream agent invent an input it never got."""
    out = prompt_for("draft it", [("scout", "")])
    assert "scout" in out and "produced no output" in out


def test_one_chatty_upstream_cannot_crowd_out_the_others():
    out = prompt_for("go", [("loud", "x" * 50_000), ("quiet", "the key fact")],
                     budget=2000)
    assert "the key fact" in out
    assert "truncated" in out
    assert len(out) < 6000


def test_each_dependency_keeps_its_order():
    out = prompt_for("go", [("first", "A"), ("second", "B")])
    assert out.index("first") < out.index("second")


# ── the runner ───────────────────────────────────────────────────────────────
class FakeSpawn:
    """Stands in for the cockpit: hands back a task id and records the prompt."""

    def __init__(self):
        self.calls = []
        self.n = 0
        self.accept = True

    def __call__(self, agent, prompt):
        self.calls.append({"agent": agent.name, "prompt": prompt,
                           "harness": agent.harness})
        if not self.accept:
            return ""
        self.n += 1
        return f"t{self.n}"


@pytest.fixture()
def chain():
    """scout → writer → editor."""
    o = SwarmOrchestrator()
    o.add_agent("scout", "find sources")
    o.add_agent("writer", "draft it", deps=["scout"])
    o.add_agent("editor", "polish", deps=["writer"])
    return o


@pytest.fixture()
def runner(chain):
    return SwarmRunner(chain, spawn=FakeSpawn(), harness="demo")


def test_pump_starts_only_the_ready_layer(runner, chain):
    out = runner.pump()
    assert out["started"] == ["scout"]
    assert chain.agent_by_name("scout").status is AgentStatus.WORKING
    assert chain.agent_by_name("writer").status is AgentStatus.IDLE


def test_a_finished_task_completes_its_agent(runner, chain):
    runner.pump()
    task = runner.task_of[chain.agent_by_name("scout").id]
    runner.on_task_state(task, "done", output="three sources")
    scout = chain.agent_by_name("scout")
    assert scout.status is AgentStatus.DONE
    assert scout.output == "three sources"
    assert scout.progress == 1.0


def test_the_whole_chain_finishes_on_its_own(runner, chain):
    """The headline. Nothing used to set an agent DONE, so a swarm could never
    get past its first layer no matter how long you waited."""
    runner.pump()
    for _ in range(5):
        live = list(runner.task_of.items())
        if not live:
            break
        for agent_id, task_id in live:
            runner.on_task_state(task_id, "done",
                                 output=f"output of {agent_id}")
    assert [a.status for a in chain.agents.values()] == [AgentStatus.DONE] * 3
    assert runner.stalled() == ""


def test_each_step_receives_the_previous_step_s_output(runner, chain):
    runner.pump()
    runner.on_task_state(runner.task_of[chain.agent_by_name("scout").id],
                         "done", output="SOURCES-FOUND")
    writer_prompt = runner.spawn.calls[-1]["prompt"]
    assert "draft it" in writer_prompt
    assert "SOURCES-FOUND" in writer_prompt


def test_a_failed_task_fails_its_agent_and_blocks_downstream(runner, chain):
    runner.pump()
    runner.on_task_state(runner.task_of[chain.agent_by_name("scout").id],
                         "failed", error="boom")
    scout = chain.agent_by_name("scout")
    assert scout.status is AgentStatus.FAILED and scout.error == "boom"
    assert chain.agent_by_name("writer").status is AgentStatus.IDLE
    assert "scout failed" in runner.stalled()


def test_a_cancelled_task_does_not_satisfy_downstream(runner, chain):
    """Downstream still has no input, so treating cancellation as completion
    would run the next step on nothing."""
    runner.pump()
    runner.on_task_state(runner.task_of[chain.agent_by_name("scout").id],
                         "cancelled")
    assert chain.agent_by_name("scout").status is AgentStatus.CANCELLED
    assert chain.agent_by_name("writer").status is AgentStatus.IDLE


def test_progress_updates_are_not_completions(runner, chain):
    runner.pump()
    task = runner.task_of[chain.agent_by_name("scout").id]
    assert runner.on_task_state(task, "running") is None
    assert chain.agent_by_name("scout").status is AgentStatus.WORKING


def test_a_task_we_do_not_own_is_ignored(runner):
    """The cockpit runs plenty of tasks that are nothing to do with a swarm."""
    assert runner.on_task_state("t999", "done") is None


def test_a_spawn_that_is_refused_fails_the_agent_loudly(chain):
    """Otherwise the agent sits in WORKING forever with no task behind it —
    the exact silent stall this module exists to remove."""
    spawn = FakeSpawn()
    spawn.accept = False
    runner = SwarmRunner(chain, spawn=spawn, harness="demo")
    runner.pump()
    scout = chain.agent_by_name("scout")
    assert scout.status is AgentStatus.FAILED
    assert "did not accept" in scout.error


def test_a_spawn_that_raises_is_caught(chain):
    def boom(agent, prompt):
        raise RuntimeError("no harness loaded")
    runner = SwarmRunner(chain, spawn=boom, harness="demo")
    runner.pump()
    assert chain.agent_by_name("scout").status is AgentStatus.FAILED
    assert "no harness loaded" in chain.agent_by_name("scout").error


def test_pump_is_idempotent(runner, chain):
    runner.pump()
    before = len(runner.spawn.calls)
    runner.pump()
    runner.pump()
    assert len(runner.spawn.calls) == before, "a repeated tick started it twice"


def test_the_parallel_budget_applies_to_a_wide_swarm():
    o = SwarmOrchestrator()
    for i in range(6):
        o.add_agent(f"w{i}", "work")
    runner = SwarmRunner(o, spawn=FakeSpawn(), harness="demo", max_parallel=2)
    out = runner.pump()
    assert len(out["started"]) == 2
    assert len(out["deferred"]) == 4


def test_finishing_one_admits_the_next(runner=None):
    o = SwarmOrchestrator()
    for i in range(3):
        o.add_agent(f"w{i}", "work")
    r = SwarmRunner(o, spawn=FakeSpawn(), harness="demo", max_parallel=1)
    r.pump()
    assert len(r.task_of) == 1
    first_task = next(iter(r.task_of.values()))
    r.on_task_state(first_task, "done", output="ok")
    assert len(r.task_of) == 1, "the freed slot was not refilled"


def test_per_agent_harness_overrides_the_default():
    o = SwarmOrchestrator()
    o.add_checked("researcher", "look it up", harness="research")
    o.add_checked("coder", "write it")
    spawn = FakeSpawn()
    SwarmRunner(o, spawn=spawn, harness="demo").pump()
    used = {c["agent"]: c["harness"] for c in spawn.calls}
    assert used["researcher"] == "research"
    assert used["coder"] == ""      # empty means "the runner's default"


# ── explaining a stuck swarm ─────────────────────────────────────────────────
def test_nothing_to_say_while_work_is_in_flight(runner):
    runner.pump()
    assert runner.stalled() == ""


def test_an_empty_swarm_is_not_stalled():
    assert SwarmRunner(SwarmOrchestrator(), spawn=FakeSpawn()).stalled() == ""


def test_a_blocked_dag_says_which_dependency(runner, chain):
    runner.pump()
    runner.on_task_state(runner.task_of[chain.agent_by_name("scout").id],
                         "failed", error="nope")
    msg = runner.stalled()
    assert "writer" in msg and "scout failed" in msg


def test_a_dependency_cycle_is_reported_as_one():
    """The one shape add_checked cannot refuse at insert time: each name
    exists when it is referenced, but together they close a loop."""
    o = SwarmOrchestrator()
    o.add_agent("a", "x", deps=["b"])
    o.add_agent("b", "y", deps=["a"])
    r = SwarmRunner(o, spawn=FakeSpawn())
    assert r.pump()["started"] == []
    assert "cycle" in r.stalled()


def test_status_explains_a_swarm_that_is_not_moving(runner, chain):
    runner.pump()
    st = runner.status()
    assert st["in_flight"] == 1
    assert st["max_parallel"] >= 1
    assert st["total"] == 3
    assert "running_tasks" in st


# ── work on another machine ──────────────────────────────────────────────────
# A remote task cannot announce itself on this process's bus, so it is polled.
# Polling can fail for reasons that have nothing to do with the work — a laptop
# sleeps, a tunnel drops — and confusing the two either strands a DAG or
# cancels live work.

def watch(**kw):
    from aion.swarmrun import Watch
    base = dict(agent_id="a1", instance="pi5", task_id="t1")
    base.update(kw)
    return Watch(**base)


def test_a_single_unanswered_poll_is_not_a_failure():
    from aion.swarmrun import read_poll
    w = watch()
    assert read_poll(None, w) == ("", "")
    assert w.misses == 1


def test_persistent_silence_eventually_gives_up():
    from aion.swarmrun import MAX_MISSES, read_poll
    w = watch()
    for _ in range(MAX_MISSES - 1):
        assert read_poll(None, w)[0] == ""
    verdict, why = read_poll(None, w)
    assert verdict == "lost" and "stopped answering" in why


def test_one_good_answer_forgives_earlier_misses():
    """A blink of wifi must not accumulate toward a death sentence."""
    from aion.swarmrun import read_poll
    w = watch(misses=3)
    assert read_poll({"state": "running"}, w) == ("running", "")
    assert w.misses == 0


def test_a_peer_that_does_not_know_the_task_is_definite():
    """It answered. The work is not coming back, and that is different from
    not being able to ask."""
    from aion.swarmrun import read_poll
    verdict, why = read_poll({"error": "no such task"}, watch())
    assert verdict == "lost" and "no task" in why


def test_a_state_and_its_output_come_through():
    from aion.swarmrun import read_poll
    assert read_poll({"state": "done", "output": "the result"}, watch()) == (
        "done", "the result")


def test_junk_from_a_peer_counts_as_a_miss_not_a_verdict():
    from aion.swarmrun import read_poll
    w = watch()
    assert read_poll("<html>gateway timeout</html>", w) == ("", "")
    assert w.misses == 1


class FakePeer:
    """An instance that runs a task and can be made to go quiet."""

    def __init__(self, state="running"):
        self.state = state
        self.reachable = True
        self.spawned = []

    def spawn(self, instance, agent, prompt):
        self.spawned.append((instance, agent.name, prompt))
        return f"remote-{len(self.spawned)}"

    def poll(self, instance, task_id):
        if not self.reachable:
            return None
        return {"id": task_id, "state": self.state, "output": "remote output"}


def remote_chain():
    o = SwarmOrchestrator()
    o.add_checked("heavy", "train the thing", instance="workstation")
    o.add_checked("report", "write it up", ["heavy"])
    return o


def test_a_step_pinned_to_an_instance_runs_there():
    o = remote_chain()
    peer = FakePeer()
    r = SwarmRunner(o, spawn=FakeSpawn(), spawn_remote=peer.spawn,
                    poll_remote=peer.poll)
    r.pump()
    assert peer.spawned[0][0] == "workstation"
    assert r.spawn.calls == [], "a remote step was run locally"
    assert r.watches


def test_a_remote_step_advances_the_dag_when_it_finishes():
    """The whole point: a DAG spanning machines still walks itself."""
    o = remote_chain()
    peer = FakePeer()
    r = SwarmRunner(o, spawn=FakeSpawn(), spawn_remote=peer.spawn,
                    poll_remote=peer.poll)
    r.pump()
    peer.state = "done"
    r.poll()
    assert o.agent_by_name("heavy").status is AgentStatus.DONE
    assert o.agent_by_name("report").status is AgentStatus.WORKING
    assert r.spawn.calls, "the local step never started"


def test_the_remote_output_reaches_the_next_step():
    o = remote_chain()
    peer = FakePeer()
    r = SwarmRunner(o, spawn=FakeSpawn(), spawn_remote=peer.spawn,
                    poll_remote=peer.poll)
    r.pump()
    peer.state = "done"
    r.poll()
    assert "remote output" in r.spawn.calls[-1]["prompt"]


def test_a_peer_that_disappears_fails_its_agent_eventually():
    from aion.swarmrun import MAX_MISSES
    o = remote_chain()
    peer = FakePeer()
    r = SwarmRunner(o, spawn=FakeSpawn(), spawn_remote=peer.spawn,
                    poll_remote=peer.poll)
    r.pump()
    peer.reachable = False
    for _ in range(MAX_MISSES):
        r.poll()
    heavy = o.agent_by_name("heavy")
    assert heavy.status is AgentStatus.FAILED
    assert "workstation" in heavy.error


def test_a_brief_outage_does_not_fail_the_agent():
    o = remote_chain()
    peer = FakePeer()
    r = SwarmRunner(o, spawn=FakeSpawn(), spawn_remote=peer.spawn,
                    poll_remote=peer.poll)
    r.pump()
    peer.reachable = False
    r.poll(); r.poll()
    peer.reachable = True
    r.poll()
    assert o.agent_by_name("heavy").status is AgentStatus.WORKING


def test_a_poll_that_raises_is_a_miss_not_a_crash():
    o = remote_chain()

    def boom(instance, task_id):
        raise OSError("connection reset")
    r = SwarmRunner(o, spawn=FakeSpawn(),
                    spawn_remote=FakePeer().spawn, poll_remote=boom)
    r.pump()
    r.poll()
    assert o.agent_by_name("heavy").status is AgentStatus.WORKING


def test_a_pinned_step_with_no_transport_fails_loudly():
    """Rather than silently running somewhere the user did not ask for."""
    o = remote_chain()
    r = SwarmRunner(o, spawn=FakeSpawn())          # no spawn_remote
    r.pump()
    heavy = o.agent_by_name("heavy")
    assert heavy.status is AgentStatus.FAILED
    assert "workstation" in heavy.error


def test_polling_with_nothing_remote_is_free():
    o = SwarmOrchestrator()
    o.add_checked("local", "work")
    r = SwarmRunner(o, spawn=FakeSpawn(), poll_remote=lambda *a: 1 / 0)
    r.pump()
    assert r.poll() == {"polled": 0, "advanced": []}


def test_status_shows_what_is_running_elsewhere():
    o = remote_chain()
    peer = FakePeer()
    r = SwarmRunner(o, spawn=FakeSpawn(), spawn_remote=peer.spawn,
                    poll_remote=peer.poll)
    r.pump()
    remote = r.status()["remote"]
    assert list(remote.values())[0]["instance"] == "workstation"


def test_the_owning_cockpit_does_not_clobber_where_an_agent_runs(tmp_path):
    """Two different meanings of "instance" meet in procgraph: the cockpit
    whose checkpoint this is, and the machine the step runs on. `{**a,
    "instance": dir}` let the directory name silently win."""
    import json
    from aion import procgraph
    inst = tmp_path / "main"
    inst.mkdir()
    (inst / "swarm.json").write_text(json.dumps([
        {"id": "s1", "name": "heavy", "goal": "x", "status": "idle",
         "deps": [], "instance": "workstation"}]))
    row = procgraph.read_swarm(tmp_path)[0]
    assert row["instance"] == "main"          # whose checkpoint
    assert row["runs_on"] == "workstation"    # where it runs
