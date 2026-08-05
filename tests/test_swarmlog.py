"""What happened, not just what is.

`swarm.json` is a snapshot: each write replaces the last, so it answers "what
is the state" and can never answer "how long did the scrape take", "did the
writer retry", or "which step added the three I do not remember planning".
State forgets. These tests are about the record that does not.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aion.swarm import AgentStatus, SwarmOrchestrator  # noqa: E402
from aion.swarmlog import (  # noqa: E402
    EventLog, duration_text, render_timeline, timeline)
from aion.swarmpolicy import RetryPolicy  # noqa: E402
from aion.swarmrun import SwarmRunner  # noqa: E402


def ev(kind, step, ts, **kw):
    return {"kind": kind, "step": step, "ts": ts, **kw}


# ── the file ────────────────────────────────────────────────────────────────

def test_events_append_rather_than_replace(tmp_path):
    """The snapshot problem in one line: a record that overwrites is a record
    of the present, which is what we already had."""
    log = EventLog(tmp_path / "e.jsonl")
    log.record("started", "scout")
    log.record("finished", "scout")
    assert [e["kind"] for e in log.read()] == ["started", "finished"]


def test_a_torn_line_costs_only_itself(tmp_path):
    p = tmp_path / "e.jsonl"
    log = EventLog(p)
    log.record("started", "a")
    with p.open("a") as fh:
        fh.write('{"kind": "fin\n')
    log.record("finished", "a")
    assert [e["kind"] for e in log.read()] == ["started", "finished"]


def test_an_unwritable_log_does_not_stop_a_swarm(tmp_path, capsys):
    """A swarm that stops because its log cannot be written is worse than one
    with an incomplete log — but the hole is printed, never silent."""
    log = EventLog(tmp_path / "nope" / "e.jsonl")
    log.path = tmp_path / "missing-dir" / "e.jsonl"
    log.record("started", "a")
    assert "could not record" in capsys.readouterr().out


def test_reading_a_log_that_does_not_exist_is_empty(tmp_path):
    assert EventLog(tmp_path / "none.jsonl").read() == []


def test_an_unserialisable_field_is_reported_not_raised(tmp_path, capsys):
    EventLog(tmp_path / "e.jsonl").record("started", "a", obj=object())
    assert "could not record" in capsys.readouterr().out


# ── folding into a timeline ─────────────────────────────────────────────────

def test_a_step_reports_when_it_ran_and_for_how_long():
    rows = timeline([ev("started", "scout", 100.0), ev("finished", "scout", 130.0)])
    assert rows[0]["seconds"] == 30.0 and rows[0]["outcome"] == "finished"


def test_duration_spans_the_retries_not_just_the_attempt_that_worked():
    """"How long did this step take" means wall clock from first try to done.
    Measuring the successful attempt alone quietly understates it."""
    rows = timeline([
        ev("started", "scrape", 0.0), ev("failed", "scrape", 10.0),
        ev("retry", "scrape", 10.0, wait=5), ev("started", "scrape", 15.0),
        ev("finished", "scrape", 25.0)])
    assert rows[0]["seconds"] == 25.0
    assert rows[0]["attempts"] == 2
    assert rows[0]["outcome"] == "finished"


def test_a_running_step_has_no_duration_invented_for_it():
    """Filling in "now" makes a stalled step look like a slow one."""
    rows = timeline([ev("started", "scout", 100.0)])
    assert rows[0]["seconds"] is None and rows[0]["ended"] is None


def test_a_step_that_ran_again_is_not_still_reported_as_finished():
    rows = timeline([ev("started", "a", 0.0), ev("failed", "a", 5.0),
                     ev("started", "a", 6.0)])
    assert rows[0]["outcome"] == "" and rows[0]["seconds"] is None


def test_rows_are_ordered_by_when_each_step_began():
    rows = timeline([ev("started", "b", 50.0), ev("started", "a", 10.0)])
    assert [r["step"] for r in rows] == ["a", "b"]


def test_giving_up_is_distinguishable_from_plain_failure():
    """A dead-lettered step and one that never had a retry budget are
    different stories about the same red mark."""
    rows = timeline([ev("started", "a", 0.0),
                     ev("gave_up", "a", 9.0, error="401", reason="permanent")])
    assert rows[0]["outcome"] == "gave_up" and rows[0]["error"] == "401"


def test_what_a_step_added_to_the_dag_is_on_its_row():
    rows = timeline([ev("started", "scout", 0.0), ev("finished", "scout", 5.0),
                     ev("expanded", "scout", 6.0, count=3)])
    assert rows[0]["added"] == 3


def test_events_without_a_step_are_ignored():
    assert timeline([{"kind": "started", "ts": 1.0}]) == []


def test_an_empty_log_folds_to_nothing():
    assert timeline([]) == []


# ── rendering ───────────────────────────────────────────────────────────────

def test_the_timeline_renders_durations_in_readable_units():
    rows = timeline([ev("started", "a", 0.0), ev("finished", "a", 240.0)])
    assert "4m" in render_timeline(rows)


def test_a_retried_step_shows_its_try_count():
    rows = timeline([ev("started", "a", 0.0), ev("failed", "a", 1.0),
                     ev("started", "a", 2.0), ev("finished", "a", 3.0)])
    assert "×2" in render_timeline(rows)


def test_an_unfinished_step_reads_as_running_not_as_instant():
    """"0s" would be a lie about a step that has not ended yet."""
    assert duration_text(None) == "running"
    assert duration_text(0.4) == "0s"


def test_nothing_run_says_so_rather_than_rendering_blank():
    assert "nothing has run yet" in render_timeline([])


# ── through the runner ──────────────────────────────────────────────────────

def build(tmp_path, retry=None, fails=0):
    orch = SwarmOrchestrator(persist=False)
    orch.add_agent("scout", "find it")
    runs = []

    def spawn(agent, prompt):
        runs.append(agent.name)
        return "" if len(runs) <= fails else f"t{len(runs)}"

    log = EventLog(tmp_path / "e.jsonl")
    r = SwarmRunner(orch, spawn=spawn, events=log.record, retry=retry,
                    max_parallel=4)
    return orch, r, log


def test_a_run_leaves_a_record_behind(tmp_path):
    orch, r, log = build(tmp_path)
    r.pump()
    r.finish(orch.agent_by_name("scout").id, "found things")
    kinds = [e["kind"] for e in log.read()]
    assert kinds == ["started", "finished"]


def test_the_record_survives_the_runner(tmp_path):
    """The whole point: a new process can still say what the last run did."""
    orch, r, log = build(tmp_path)
    r.pump()
    r.finish(orch.agent_by_name("scout").id, "out")
    del r
    assert timeline(EventLog(tmp_path / "e.jsonl").read())[0]["outcome"] == "finished"


def test_a_retry_is_recorded_with_how_long_it_waits(tmp_path):
    orch, r, log = build(tmp_path, retry=RetryPolicy(max_attempts=3,
                                                     base_delay=10.0), fails=9)
    r.pump()
    retry = [e for e in log.read() if e["kind"] == "retry"]
    assert retry and retry[0]["wait"] >= 9


def test_a_failure_with_no_retry_budget_is_recorded_as_plain_failure(tmp_path):
    orch, r, log = build(tmp_path, retry=RetryPolicy(max_attempts=1), fails=9)
    r.pump()
    assert [e["kind"] for e in log.read()][-1] == "failed"


def test_running_out_of_attempts_is_recorded_as_giving_up(tmp_path):
    """Two red marks, two different stories: a step nobody gave a budget to,
    and a step that spent one. The log has to tell them apart."""
    orch, r, log = build(tmp_path, retry=RetryPolicy(max_attempts=2,
                                                     base_delay=0.0), fails=9)
    r.pump()
    r.pump()
    assert [e["kind"] for e in log.read()][-1] == "gave_up"
    assert timeline(log.read())[0]["attempts"] == 2


def test_a_cancel_is_recorded(tmp_path):
    orch, r, log = build(tmp_path)
    r.pump()
    r.cancel(orch.agent_by_name("scout").id, "stopped by hand")
    assert [e["kind"] for e in log.read()][-1] == "cancelled"


def test_what_the_swarm_added_to_itself_is_recorded(tmp_path):
    from aion.swarmreplan import ReplanPolicy

    orch = SwarmOrchestrator(persist=False)
    orch.add_agent("scout", "find it")
    log = EventLog(tmp_path / "e.jsonl")
    r = SwarmRunner(orch, spawn=lambda a, p: "t1", events=log.record,
                    replan=ReplanPolicy(max_new_steps=2))
    r.apply_expansion(orch.agent_by_name("scout").id,
                      [{"name": "audit", "goal": "audit it"}])
    added = [e for e in log.read() if e["kind"] == "expanded"]
    assert added[0]["count"] == 1 and added[0]["added"] == ["audit"]


def test_the_runner_exposes_the_timeline_it_wrote(tmp_path):
    orch, r, log = build(tmp_path)
    r.pump()
    r.finish(orch.agent_by_name("scout").id, "out")
    assert r.timeline()[0]["step"] == "scout"
    assert r.status()["timeline"][0]["outcome"] == "finished"


def test_a_runner_with_no_log_still_works(tmp_path):
    orch = SwarmOrchestrator(persist=False)
    orch.add_agent("scout", "g")
    r = SwarmRunner(orch, spawn=lambda a, p: "t1")
    r.pump()
    assert r.timeline() == []
    assert orch.agent_by_name("scout").status is AgentStatus.WORKING
