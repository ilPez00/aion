"""Is a running step still alive?

`stalled()` opened with `if self._in_flight(): return ""`, so the one condition
it would not diagnose was the one that kills a swarm quietly: a step stuck in
WORKING forever. These tests are about telling a step that is working from one
that stopped existing — and about not confusing the two, which is the easier
mistake and the more expensive one.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aion.swarm import AgentStatus, SwarmOrchestrator  # noqa: E402
from aion.swarmlive import (  # noqa: E402
    HeartbeatPolicy, assess, policy_from_config, render_live, sweep)
from aion.swarmpolicy import RetryPolicy  # noqa: E402
from aion.swarmrun import SwarmRunner  # noqa: E402


def step(started=100.0, last_seen=0.0, name="scout", sid="a1"):
    return {"id": sid, "name": name, "started": started, "last_seen": last_seen}


# ── the two silences ────────────────────────────────────────────────────────

def test_a_step_that_never_reported_is_not_evidence_of_anything():
    """Plenty of CLI harnesses block until they exit and report once. A
    watchdog that kills on this kills healthy work — it invents the failure it
    was installed to catch."""
    live = assess(step(started=100.0), now=100_000.0,
                  policy=HeartbeatPolicy(stall_after=60))
    assert live.heard is False
    assert live.state == "working"


def test_a_step_that_reported_and_stopped_is_evidence():
    live = assess(step(started=100.0, last_seen=200.0), now=1000.0,
                  policy=HeartbeatPolicy(stall_after=60))
    assert live.heard is True
    assert live.state == "stalled" and live.ending


def test_a_step_still_reporting_is_working():
    live = assess(step(started=100.0, last_seen=990.0), now=1000.0,
                  policy=HeartbeatPolicy(stall_after=60))
    assert live.state == "working" and not live.ending


def test_going_quiet_is_shown_long_before_anything_is_ended():
    """Two thresholds, because "worth looking at" and "worth killing" are not
    the same number."""
    live = assess(step(started=1.0, last_seen=100.0), now=300.0,
                  policy=HeartbeatPolicy(quiet_after=60, stall_after=6000))
    assert live.state == "quiet" and not live.ending


def test_a_mute_harness_is_only_ever_caught_by_wall_clock():
    """The blunt instrument, and the only one that sees a step which never
    reports. Separate and separately off, because a legitimately long step and
    a wedged one look identical to it."""
    slow = step(started=1.0)
    assert assess(slow, 100_000.0, HeartbeatPolicy(stall_after=60)).ending is False
    assert assess(slow, 100_000.0, HeartbeatPolicy(max_runtime=3600)).state == "overrun"


def test_an_overrun_outranks_a_stall_in_the_reason_given():
    live = assess(step(started=1.0, last_seen=10.0), now=100_000.0,
                  policy=HeartbeatPolicy(stall_after=60, max_runtime=3600))
    assert live.state == "overrun"
    assert "limit" in live.why


def test_a_step_that_has_not_started_reports_no_elapsed_time():
    live = assess(step(started=0.0), now=1000.0)
    assert live.elapsed == 0.0 and live.silent_for == 0.0


def test_the_default_policy_ends_nothing():
    """A run whose behaviour changed because this module was added would be
    the bug."""
    assert HeartbeatPolicy().enabled is False
    forever = step(started=1.0, last_seen=2.0)
    assert assess(forever, now=10_000_000.0).ending is False
    assert sweep([forever], now=10_000_000.0) == []


# ── the sweep ───────────────────────────────────────────────────────────────

def test_the_sweep_names_what_it_would_end_and_why():
    found = sweep([step(started=1.0, last_seen=10.0, name="scrape")],
                  now=100_000.0, policy=HeartbeatPolicy(stall_after=60))
    assert found[0]["name"] == "scrape" and found[0]["state"] == "stalled"
    assert "silent" in found[0]["why"]


def test_the_sweep_reads_objects_and_display_dicts_alike():
    """The runner holds objects and the renderers are handed dicts; forcing
    one caller to convert is how two views assess different things."""
    class Obj:
        id, name, started, last_seen = "a1", "scout", 1.0, 10.0
    assert len(sweep([Obj()], 100_000.0, HeartbeatPolicy(stall_after=60))) == 1


# ── config ──────────────────────────────────────────────────────────────────

def test_a_configured_bound_is_read():
    p = policy_from_config({"swarm_heartbeat": {"stall_after": 300,
                                                "max_runtime": 7200}})
    assert p.stall_after == 300 and p.max_runtime == 7200 and p.enabled


def test_anything_unparseable_ends_nothing():
    """A misread bound here does not degrade a run, it ENDS steps in it."""
    for bad in (None, 5, "yes", {"swarm_heartbeat": True},
                {"swarm_heartbeat": {"stall_after": "soon"}}):
        cfg = bad if isinstance(bad, dict) else {"swarm_heartbeat": bad}
        assert policy_from_config(cfg).enabled is False


def test_a_negative_bound_is_off_rather_than_immediate():
    assert policy_from_config(
        {"swarm_heartbeat": {"stall_after": -1}}).enabled is False


# ── words ───────────────────────────────────────────────────────────────────

def test_a_running_step_says_how_long_it_has_been_running():
    assert render_live(assess(step(started=1.0), now=241.0)) == "4m"


def test_a_quiet_step_says_how_long_it_has_been_quiet():
    live = assess(step(started=1.0, last_seen=61.0), now=601.0,
                  policy=HeartbeatPolicy(quiet_after=60))
    assert render_live(live) == "10m, quiet 9m"


def test_a_step_that_has_not_started_says_nothing():
    assert render_live(assess(step(started=0.0), now=100.0)) == ""


# ── through the runner ──────────────────────────────────────────────────────

def build(heartbeat=None, retry=None, clock=None):
    orch = SwarmOrchestrator(persist=False)
    orch.add_agent("scout", "find it")
    events = []
    r = SwarmRunner(orch, spawn=lambda a, p: "t1", heartbeat=heartbeat,
                    retry=retry, clock=clock,
                    events=lambda kind, step="", **f: events.append((kind, step)))
    return orch, r, events


def test_hearing_from_a_task_stamps_the_step():
    """`on_task_state` returned early on "running", so a swarm knew a step's
    progress was 0.0 right up to the moment it was 1.0."""
    orch, r, _ = build()
    r.pump()
    agent = orch.agent_by_name("scout")
    assert agent.last_seen == 0.0
    r.on_task_state("t1", "running", progress=0.5)
    assert agent.last_seen > 0.0 and agent.progress == 0.5


def test_a_nonsense_progress_value_is_ignored_not_propagated():
    orch, r, _ = build()
    r.pump()
    r.on_task_state("t1", "running", progress="soon")
    assert orch.agent_by_name("scout").progress == 0.0


def test_progress_stays_inside_its_range():
    orch, r, _ = build()
    r.pump()
    r.on_task_state("t1", "running", progress=7.0)
    assert orch.agent_by_name("scout").progress == 1.0


def test_a_wedged_step_is_reaped_and_retried():
    """Failing it rather than cancelling it: nobody decided to stop this step,
    and an unclassifiable failure is one the retry policy runs again."""
    now = [1000.0]
    orch, r, events = build(heartbeat=HeartbeatPolicy(stall_after=60),
                            retry=RetryPolicy(max_attempts=3, base_delay=1.0),
                            clock=lambda: now[0])
    r.pump()
    agent = orch.agent_by_name("scout")
    r.on_task_state("t1", "running", progress=0.3)
    now[0] += 10_000
    r.pump()
    assert ("reaped", "scout") in events
    assert agent.status is AgentStatus.IDLE          # queued for another try
    assert agent.retry_at > now[0]


def test_a_reaped_step_stops_being_watched():
    """The harness may still be alive. A late completion landing on the retry
    would finish it with the dead run's output."""
    now = [1000.0]
    orch, r, _ = build(heartbeat=HeartbeatPolicy(stall_after=60),
                       clock=lambda: now[0])
    r.pump()
    r.on_task_state("t1", "running", progress=0.3)
    now[0] += 10_000
    r.pump()
    assert r.agent_of.get("t1") is None
    assert r.on_task_state("t1", "done", output="late") is None
    assert orch.agent_by_name("scout").output == ""


def test_nothing_is_reaped_without_a_policy():
    now = [1000.0]
    orch, r, events = build(clock=lambda: now[0])
    r.pump()
    r.on_task_state("t1", "running", progress=0.3)
    now[0] += 10_000_000
    r.pump()
    assert orch.agent_by_name("scout").status is AgentStatus.WORKING
    assert not [e for e in events if e[0] == "reaped"]


def test_a_swarm_full_of_wedged_steps_stops_calling_itself_healthy():
    """`stalled()` treated "something is running" as proof nothing is wrong,
    which is precisely wrong when the running thing has stopped answering."""
    now = [1000.0]
    orch, r, _ = build(heartbeat=HeartbeatPolicy(quiet_after=60),
                       clock=lambda: now[0])
    r.pump()
    r.on_task_state("t1", "running", progress=0.3)
    assert r.stalled() == ""
    now[0] += 600
    assert "silent" in r.stalled() and "scout" in r.stalled()


def test_a_harness_that_never_reported_does_not_look_broken():
    now = [1000.0]
    orch, r, _ = build(heartbeat=HeartbeatPolicy(quiet_after=60),
                       clock=lambda: now[0])
    r.pump()
    now[0] += 100_000
    assert r.stalled() == ""


def test_both_surfaces_are_handed_the_same_sentence():
    """The browser prints these verbatim. Two renderers deciding for
    themselves when a step counts as quiet is worse than either verdict."""
    now = [1000.0]
    orch, r, _ = build(heartbeat=HeartbeatPolicy(quiet_after=60),
                       clock=lambda: now[0])
    r.pump()
    r.on_task_state("t1", "running", progress=0.3)
    now[0] += 600
    assert r.live_lines() == ["scout — 10m, quiet 10m"]
    assert r.status()["live"] == r.live_lines()


def test_nothing_running_means_no_live_lines():
    orch, r, _ = build()
    assert r.live_lines() == []


def test_the_running_row_says_how_long_it_has_been_running():
    from aion.swarmview import render

    orch, r, _ = build()
    r.pump()
    rows = [a.as_dict() for a in orch.agents.values()]
    started = orch.agent_by_name("scout").started
    assert "4m" in render(rows, now=started + 240)
