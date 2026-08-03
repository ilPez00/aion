"""What a DAG does when a step fails.

Before this, one answer: stop. The step went FAILED, every dependent went
blocked, and an unattended swarm sat dead until morning — including when the
cause was a tunnel that dropped for four seconds and came back.

The tests below pin three things that are easy to get subtly wrong and
expensive when you do: which failures are worth retrying, that the attempt
count is bounded across a restart, and that a retried step is charged for both
runs.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aion.swarm import AgentStatus, SwarmAgent, SwarmOrchestrator  # noqa: E402
from aion.swarmbudget import Price  # noqa: E402
from aion.swarmpolicy import (  # noqa: E402
    RetryPolicy, backoff, classify, policy_from_config, should_retry,
)
from aion.swarmrun import SwarmRunner  # noqa: E402

RETRY3 = RetryPolicy(max_attempts=3, base_delay=10.0)


# ── classifying ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("error", [
    "connection reset by peer",
    "HTTP 503 Service Unavailable",
    "rate limit exceeded, try again in 20s",
    "read timed out",
    "workstation stopped answering after 4 attempts",
])
def test_the_world_being_briefly_wrong_is_transient(error):
    assert classify(error) == "transient"


@pytest.mark.parametrize("error", [
    "401 Unauthorized: check your API key",
    "model refused to answer",
    "context length exceeded",
    "no way to reach workstation",
])
def test_a_thing_that_will_not_fix_itself_is_permanent(error):
    assert classify(error) == "permanent"


def test_an_unrecognised_error_says_so_rather_than_guessing():
    assert classify("exit code 1") == "unknown"


def test_an_empty_error_is_unknown_not_transient():
    """A blank message must not be read as encouraging."""
    assert classify("") == "unknown" and classify("   ") == "unknown"


def test_permanent_wins_over_transient_in_one_message():
    """Retrying costs money and a refusal will not change. When a message
    reads both ways, be wrong in the cheap direction."""
    assert classify("connection ok but 403 forbidden") == "permanent"


# ── the decision ─────────────────────────────────────────────────────────────
def test_nothing_is_retried_by_default():
    """An upgrade must not silently start re-running — and re-paying for —
    work nobody asked to be re-run."""
    assert should_retry("timeout", 1, RetryPolicy())[0] is False


def test_a_transient_failure_with_attempts_left_is_retried():
    ok, why = should_retry("connection reset", 1, RETRY3)
    assert ok is True and "attempt 2 of 3" in why


def test_max_attempts_counts_total_runs_not_extra_ones():
    """Counting extras instead is a 50% overspend hiding in an off-by-one."""
    assert should_retry("timeout", 2, RETRY3)[0] is True     # third run
    assert should_retry("timeout", 3, RETRY3)[0] is False    # no fourth


def test_giving_up_says_how_many_attempts_went():
    ok, why = should_retry("timeout", 3, RETRY3)
    assert ok is False and "3 of 3" in why


def test_a_permanent_failure_is_not_retried_even_with_attempts_left():
    ok, why = should_retry("401 unauthorized", 1, RETRY3)
    assert ok is False and "will not fix itself" in why


def test_a_permanent_failure_can_be_retried_when_asked_for():
    assert should_retry("401 unauthorized", 1,
                        RetryPolicy(max_attempts=3, retry_permanent=True))[0] is True


def test_an_unclassifiable_failure_is_retried_by_default():
    """Most CLI harnesses say nothing useful, and most of what they say
    nothing useful about is a flaky process."""
    assert should_retry("exit code 1", 1, RETRY3)[0] is True


def test_an_unclassifiable_failure_can_be_treated_as_final():
    assert should_retry("exit code 1", 1, RetryPolicy(
        max_attempts=3, retry_unknown=False))[0] is False


# ── backoff ──────────────────────────────────────────────────────────────────
def test_the_first_retry_waits_the_base_delay():
    assert backoff(1, RETRY3) == 10.0


def test_each_retry_waits_longer():
    assert backoff(2, RETRY3) == 20.0 and backoff(3, RETRY3) == 40.0


def test_the_wait_is_capped():
    """A long DAG must not back itself off into next week."""
    assert backoff(20, RetryPolicy(max_attempts=99, base_delay=5,
                                   max_delay=300)) == 300.0


def test_the_very_first_run_waits_for_nothing():
    assert backoff(0, RETRY3) == 0.0


# ── config ───────────────────────────────────────────────────────────────────
def test_no_config_means_no_retries():
    assert policy_from_config({}).enabled is False


def test_a_bare_number_is_the_attempt_count():
    """`"swarm_retry": 3` is what somebody writes first."""
    assert policy_from_config({"swarm_retry": 3}).max_attempts == 3


def test_the_full_form_is_read():
    p = policy_from_config({"swarm_retry": {"max_attempts": 5, "base_delay": 2,
                                            "retry_permanent": True}})
    assert (p.max_attempts, p.base_delay, p.retry_permanent) == (5, 2.0, True)


def test_a_typo_does_not_stop_the_cockpit_booting():
    assert policy_from_config({"swarm_retry": {"max_attempts": "three"}}
                              ).enabled is False
    assert policy_from_config({"swarm_retry": "yes"}).enabled is False


def test_zero_attempts_is_still_one_run():
    """A step must run at least once, whatever the config says."""
    assert policy_from_config({"swarm_retry": 0}).max_attempts == 1


# ── the runner ───────────────────────────────────────────────────────────────
class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def build(retry=RETRY3, goals=("a",), budget=0.0, fails=99):
    """A runner whose spawns fail `fails` times, then succeed."""
    orch = SwarmOrchestrator(persist=False)
    for i, goal in enumerate(goals):
        orch.add_agent(f"step{i}", goal, harness="claude")
    runs = []

    def spawn(agent, prompt):
        runs.append(agent.name)
        return "" if len(runs) <= fails else f"t{len(runs)}"

    clock = Clock()
    runner = SwarmRunner(orch, spawn=spawn, harness="claude", retry=retry,
                         clock=clock, max_parallel=10, budget=budget,
                         prices={"claude": Price(3.0, 15.0)})
    return orch, runner, runs, clock


def test_a_failed_step_goes_back_to_idle_not_failed():
    """FAILED blocks every dependent through `dep_state`. A step we intend to
    run again in ten seconds has not blocked anything yet."""
    orch, runner, _, _ = build()
    runner.pump()
    assert orch.agent_by_name("step0").status is AgentStatus.IDLE


def test_a_retry_does_not_start_immediately():
    orch, runner, runs, _ = build()
    runner.pump()
    runner.pump()                        # inside the backoff
    assert runs == ["step0"]


def test_a_retry_starts_once_the_backoff_expires():
    _, runner, runs, clock = build()
    runner.pump()
    clock.advance(11)
    runner.pump()
    assert runs == ["step0", "step0"]


def test_a_step_stops_after_its_last_attempt():
    orch, runner, runs, clock = build()
    for _ in range(6):
        runner.pump()
        clock.advance(100)
    assert len(runs) == 3
    assert orch.agent_by_name("step0").status is AgentStatus.FAILED


def test_a_step_that_succeeds_on_the_second_go_is_done_not_failed():
    orch, runner, runs, clock = build(fails=1)
    runner.pump()
    clock.advance(11)
    runner.pump()
    aid = orch.agent_by_name("step0").id
    runner.finish(aid, "the answer")
    assert orch.agent_by_name("step0").status is AgentStatus.DONE


def test_a_permanent_failure_is_not_retried_by_the_runner():
    orch, runner, _, _ = build()
    runner.pump()
    aid = orch.agent_by_name("step0").id
    runner.fail(aid, "401 unauthorized")
    assert orch.agent_by_name("step0").status is AgentStatus.FAILED


def test_a_step_waiting_out_a_backoff_is_reported_not_hidden():
    """From outside, a step held back by a backoff is otherwise
    indistinguishable from one the scheduler forgot about."""
    _, runner, _, _ = build()
    runner.pump()
    out = runner.pump()
    assert any("retrying in" in d["reason"] for d in out["deferred"])


def test_the_stall_reason_says_it_is_a_retry():
    _, runner, _, _ = build()
    runner.pump()
    runner.pump()
    assert "retrying in" in runner.stalled()


def test_the_stall_reason_is_right_on_the_tick_the_failure_happened():
    """The admission plan was built before the failure that started the
    backoff, so reading the reason off the plan leaves the one tick that most
    needs an answer with nothing to say."""
    _, runner, _, _ = build()
    runner.pump()
    assert "retrying in" in runner.stalled()


def test_nothing_asks_for_a_pump_before_the_backoff_is_up():
    _, runner, _, clock = build()
    runner.pump()
    assert runner.due_for_retry() is False
    clock.advance(11)
    assert runner.due_for_retry() is True


def test_a_dag_with_no_retries_pending_asks_for_no_timer():
    """This is polled on the cockpit's heartbeat; it must be cheap and quiet
    when nothing is waiting."""
    _, runner, _, _ = build(retry=RetryPolicy(), fails=0)
    runner.pump()
    assert runner.due_for_retry() is False


# ── retries and the rest of the DAG ──────────────────────────────────────────
def test_an_independent_branch_keeps_running_while_another_retries():
    """Continue-if-independent: one flaky step must not idle a DAG that has
    unrelated work ready."""
    orch = SwarmOrchestrator(persist=False)
    orch.add_agent("flaky", "a", harness="claude")
    orch.add_agent("other", "b", harness="claude")
    started = []

    def spawn(agent, prompt):
        started.append(agent.name)
        return "" if agent.name == "flaky" else "t1"

    runner = SwarmRunner(orch, spawn=spawn, harness="claude", retry=RETRY3,
                         clock=Clock(), max_parallel=10)
    runner.pump()
    assert orch.agent_by_name("other").status is AgentStatus.WORKING


def test_a_dependent_step_is_not_unblocked_by_a_step_that_is_still_retrying():
    """A retrying step has produced no output. Letting a dependent start would
    feed it an input that does not exist."""
    orch = SwarmOrchestrator(persist=False)
    orch.add_agent("up", "a", harness="claude")
    orch.add_agent("down", "b", harness="claude", deps=["up"])
    runner = SwarmRunner(orch, spawn=lambda a, p: "", harness="claude",
                         retry=RETRY3, clock=Clock(), max_parallel=10)
    runner.pump()
    assert orch.agent_by_name("down").status is AgentStatus.IDLE
    assert orch.dep_state(orch.agent_by_name("down"))[0] == "waiting"


def test_a_dependent_is_blocked_once_the_retries_are_exhausted():
    orch, runner, _, clock = build()
    orch.add_agent("down", "b", harness="claude", deps=["step0"])
    for _ in range(6):
        runner.pump()
        clock.advance(100)
    assert orch.dep_state(orch.agent_by_name("down"))[0] == "blocked"


# ── the count has to survive a restart ───────────────────────────────────────
def test_the_attempt_count_is_checkpointed():
    """A restart that reset the count would turn a bounded retry policy into
    an unbounded one — the exact failure the bound exists to prevent."""
    a = SwarmAgent(id="s1", name="step", goal="g")
    a.attempts = 2
    a.retry_at = 1234.0
    back = SwarmAgent.from_record(a.as_record())
    assert back.attempts == 2 and back.retry_at == 1234.0


def test_an_old_checkpoint_without_the_fields_still_loads():
    back = SwarmAgent.from_record({"id": "s1", "name": "step", "goal": "g"})
    assert back.attempts == 0 and back.retry_at == 0.0


def test_a_human_retry_gives_the_step_its_attempts_back():
    """Otherwise someone presses retry on an exhausted step and watches it
    fail again instantly, or waits four minutes for a backoff they overrode."""
    orch = SwarmOrchestrator(persist=False)
    aid = orch.add_agent("step", "g").id
    orch.agents[aid].attempts = 3
    orch.agents[aid].retry_at = 9e9
    orch.set_status(aid, AgentStatus.FAILED)
    orch.control(aid, "retry")
    assert orch.agents[aid].attempts == 0 and orch.agents[aid].retry_at == 0.0


# ── retries cost money ───────────────────────────────────────────────────────
def test_a_second_attempt_is_charged_on_top_of_the_first():
    """Entries are keyed by agent id, so without banking the first attempt a
    retry is free — precisely backwards, since retries are the thing a budget
    exists to bound."""
    from aion.swarmbudget import Ledger

    led = Ledger(prices={"claude": Price(3.0, 15.0)})
    led.reserve("a1", "claude", "prompt")
    led.settle("a1", "prompt", "an answer")
    first = led.settled()
    led.reserve("a1", "claude", "prompt")
    led.settle("a1", "prompt", "an answer")
    assert led.settled() == pytest.approx(first * 2)


def test_cancelling_mid_retry_does_not_refund_the_runs_that_happened():
    from aion.swarmbudget import Ledger

    led = Ledger(prices={"claude": Price(3.0, 15.0)})
    led.reserve("a1", "claude", "prompt")
    led.settle("a1", "prompt", "an answer")
    led.reserve("a1", "claude", "prompt")
    led.release("a1")
    assert led.committed() > 0


def test_the_budget_bounds_how_many_times_a_step_may_retry():
    """The two features have to compose: a policy of three attempts on a
    budget that affords two must stop at two."""
    orch = SwarmOrchestrator(persist=False)
    orch.add_agent("step0", "x" * 4000, harness="claude")
    runs = []

    def spawn(agent, prompt):
        runs.append(agent.name)
        return f"t{len(runs)}"          # accepted, then failed by hand below

    clock = Clock()
    runner = SwarmRunner(orch, spawn=spawn, harness="claude",
                         retry=RetryPolicy(max_attempts=9, base_delay=1),
                         clock=clock, max_parallel=10,
                         prices={"claude": Price(3.0, 15.0)})
    # A budget of one step plus a little, set off the real estimate rather
    # than a magic number, so the test is about composition and not about
    # what a token happens to cost today.
    runner.budget = runner.ledger.estimate("claude", "x" * 4000) * 1.3
    for _ in range(9):
        runner.pump()
        aid = orch.agent_by_name("step0").id
        if orch.agents[aid].status is AgentStatus.WORKING:
            runner.fail(aid, "connection reset")
        clock.advance(100)
    assert 1 <= len(runs) < 9
    runner.pump()                        # backoff over; the budget is not
    assert "budget" in runner.stalled()


# ── the dead letter queue ────────────────────────────────────────────────────
def test_an_exhausted_step_lands_in_the_dead_letters():
    orch, runner, _, clock = build()
    orch.add_agent("down", "b", harness="claude", deps=["step0"])
    for _ in range(6):
        runner.pump()
        clock.advance(100)
    dead = runner.dead_letters()
    assert len(dead) == 1
    assert dead[0]["name"] == "step0" and dead[0]["attempts"] == 3


def test_a_dead_letter_names_what_it_is_holding_up():
    """The remediation question is never "what failed", it is "what is stuck
    behind it"."""
    orch, runner, _, clock = build()
    orch.add_agent("down", "b", harness="claude", deps=["step0"])
    for _ in range(6):
        runner.pump()
        clock.advance(100)
    assert runner.dead_letters()[0]["blocks"] == ["down"]


def test_a_healthy_dag_has_no_dead_letters():
    _, runner, _, _ = build(fails=0)
    runner.pump()
    assert runner.dead_letters() == []


def test_status_carries_the_dead_letters_and_the_policy():
    _, runner, _, _ = build()
    st = runner.status()
    assert st["max_attempts"] == 3 and st["dead_letters"] == []
