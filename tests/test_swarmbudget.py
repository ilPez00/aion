"""What a DAG is allowed to spend.

`admit()` already refuses work the machine cannot hold — parallelism and VRAM.
Neither is money, and a swarm left running unattended (the entire point of
one) can sit inside both limits and spend all night. The stall guard stops a
DAG that is stuck; this stops one that is working perfectly and is simply too
expensive.

Nothing here is a bill. aion drives external CLIs that report stdout and an
exit code, not token counts, so these are characters over four times a
configured price. That is sound as a governor and unsound as accounting, and
the tests below pin the difference.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aion.swarmbudget import (  # noqa: E402
    Ledger, Price, affordable, estimate_cost, estimate_tokens,
    prices_from_harnesses,
)
from aion.swarmrun import Slot, SwarmRunner, admit  # noqa: E402

# £3/£15 per million — roughly a frontier model, so the numbers below are
# recognisable rather than arbitrary.
CLAUDE = Price(3.0, 15.0)


# ── estimating ───────────────────────────────────────────────────────────────
def test_empty_text_costs_nothing():
    assert estimate_tokens("") == 0


def test_short_text_never_rounds_down_to_free():
    """A one-character prompt is not zero tokens, and a scheduler that thinks
    it is will admit an unbounded number of them."""
    assert estimate_tokens("hi") == 1


def test_tokens_are_characters_over_four():
    assert estimate_tokens("x" * 400) == 100


def test_an_unpriced_harness_costs_nothing():
    """Local models are free at the margin. Pretending otherwise fires the
    budget on a swarm that costs nothing."""
    assert estimate_cost("x" * 4000, Price()) == 0.0


def test_output_is_reserved_generously_not_optimistically():
    """The prompt is known before a step runs; the output is not. Under-
    estimating lets a DAG past the limit and finds out afterwards, which is
    the one failure this module exists to prevent."""
    small_prompt = estimate_cost("hello", CLAUDE)
    assert small_prompt > 0.02, "output reservation is missing or tiny"


# ── the admission question ───────────────────────────────────────────────────
def test_no_budget_admits_everything():
    assert affordable(999.0, 999.0, budget=0) == (True, "")


def test_a_step_within_the_budget_is_admitted():
    assert affordable(1.0, 2.0, budget=10.0)[0] is True


def test_a_step_that_would_cross_the_line_is_refused_with_numbers():
    ok, why = affordable(5.0, 8.0, budget=10.0)
    assert ok is False and "13.00" in why and "10.00" in why


def test_a_step_bigger_than_the_whole_budget_says_never():
    """Same rule as VRAM: silent starvation is the worst failure a scheduler
    has, because the DAG just stops and nothing says why."""
    ok, why = affordable(50.0, 0.0, budget=10.0)
    assert ok is False and "never" in why


def test_exactly_on_the_budget_is_allowed():
    assert affordable(2.0, 8.0, budget=10.0)[0] is True


# ── the ledger ───────────────────────────────────────────────────────────────
def ledger() -> Ledger:
    return Ledger(prices={"claude": CLAUDE})


def test_a_reservation_is_committed_before_anything_finishes():
    led = ledger()
    held = led.reserve("a1", "claude", "do the thing")
    assert held > 0 and led.committed() == pytest.approx(held)
    assert led.settled() == 0.0


def test_settling_replaces_the_hold_rather_than_adding_to_it():
    """Double-counting here would make a DAG appear to cost twice what it
    did, and stop it halfway through."""
    led = ledger()
    led.reserve("a1", "claude", "prompt")
    led.settle("a1", "prompt", "a short answer")
    assert led.outstanding() == 0.0
    assert led.committed() == pytest.approx(led.settled())


def test_a_real_answer_costs_less_than_the_reservation_when_it_is_short():
    led = ledger()
    held = led.reserve("a1", "claude", "prompt")
    led.settle("a1", "prompt", "ok")
    assert led.settled() < held


def test_a_long_answer_costs_more_than_reserved_and_is_recorded_honestly():
    """The hold is an estimate, not a cap. A step that overruns must show up
    as what it really was, or the next admission decision is made on a lie."""
    led = ledger()
    held = led.reserve("a1", "claude", "prompt")
    led.settle("a1", "prompt", "x" * 200_000)
    assert led.settled() > held


def test_a_harness_that_reports_real_usage_overrides_the_estimate():
    led = ledger()
    led.reserve("a1", "claude", "prompt")
    led.settle("a1", input_tokens=1_000_000, output_tokens=0)
    assert led.settled() == pytest.approx(3.0)


def test_releasing_frees_the_budget_for_the_rest_of_the_dag():
    led = ledger()
    led.reserve("a1", "claude", "prompt")
    led.release("a1")
    assert led.committed() == 0.0


def test_settling_something_never_reserved_is_a_no_op():
    assert ledger().settle("ghost", "p", "o") == 0.0


def test_the_ledger_says_it_is_an_estimate():
    """A caller that mistakes this for a bill will show it to a user as one."""
    assert ledger().as_dict()["estimated"] is True


def test_a_wholly_local_swarm_is_not_metered():
    led = Ledger()
    led.reserve("a1", "ollama", "prompt")
    assert led.metered() is False and led.committed() == 0.0


# ── prices out of config ─────────────────────────────────────────────────────
class FakeHarness:
    def __init__(self, extra):
        self.cfg = type("C", (), {"extra": extra})()


def test_prices_are_read_from_the_harness_extra_block():
    prices = prices_from_harnesses({"claude": FakeHarness(
        {"input_per_m": 3.0, "output_per_m": 15.0})})
    assert prices["claude"] == CLAUDE


def test_an_unpriced_harness_is_simply_absent():
    assert prices_from_harnesses({"ollama": FakeHarness({})}) == {}


def test_a_typo_in_a_price_does_not_stop_the_cockpit_booting():
    assert prices_from_harnesses({"x": FakeHarness({"input_per_m": "three"})}) == {}


def test_a_harness_with_no_config_at_all_is_tolerated():
    assert prices_from_harnesses({"x": object()}) == {}


# ── admission, end to end ────────────────────────────────────────────────────
def test_admit_ignores_cost_when_no_budget_is_set():
    plan = admit([Slot("a", "a", cost=100.0)], budget=0)
    assert plan.admit == ["a"]


def test_admit_stops_at_the_budget_and_says_which_step():
    plan = admit([Slot("a", "first", cost=6.0), Slot("b", "second", cost=6.0)],
                 budget=10.0)
    assert plan.admit == ["a"]
    assert plan.deferred[0]["name"] == "second"


def test_one_tick_cannot_blow_the_budget_n_times_over():
    """Without a running total inside the loop, every agent admitted in one
    tick sees the same 'spent so far'."""
    slots = [Slot(str(i), f"s{i}", cost=4.0) for i in range(5)]
    plan = admit(slots, budget=10.0, max_parallel=10)
    assert len(plan.admit) == 2


def test_the_budget_is_checked_after_the_things_we_actually_know():
    """A step should be refused for a certainty (no free slot) before an
    estimate."""
    plan = admit([Slot("a", "a", cost=99.0)], running=5, max_parallel=1,
                 budget=1.0)
    assert "parallel limit" in plan.deferred[0]["reason"]


# ── the runner ───────────────────────────────────────────────────────────────
def build(budget=0.0, goals=("write a report",)):
    from aion.swarm import SwarmOrchestrator
    orch = SwarmOrchestrator(persist=False)
    for i, goal in enumerate(goals):
        orch.add_agent(f"step{i}", goal, harness="claude")
    spawned = []
    runner = SwarmRunner(
        orch, spawn=lambda a, p: (spawned.append(a.name) or f"t{len(spawned)}"),
        harness="claude", budget=budget, prices={"claude": CLAUDE},
        max_parallel=10)
    return orch, runner, spawned


def test_a_swarm_with_no_budget_runs_as_before():
    _, runner, spawned = build(goals=("a", "b", "c"))
    runner.pump()
    assert spawned == ["step0", "step1", "step2"]


def test_a_tiny_budget_stops_the_dag_before_it_starts():
    _, runner, spawned = build(budget=0.0001, goals=("a", "b"))
    out = runner.pump()
    assert spawned == [] and out["deferred"]


def test_a_stopped_dag_says_it_was_the_budget():
    """From every other panel a DAG stopped by its budget looks exactly like a
    healthy idle one, which is the most expensive kind of silence to debug."""
    _, runner, _ = build(budget=0.0001, goals=("a",))
    runner.pump()
    assert "budget" in runner.stalled()


def test_finishing_a_step_settles_its_reservation():
    orch, runner, _ = build(budget=100.0, goals=("a",))
    runner.pump()
    aid = orch.agent_by_name("step0").id
    runner.finish(aid, "the answer")
    assert runner.ledger.outstanding() == 0.0
    assert runner.ledger.settled() > 0


def test_a_step_that_ran_and_failed_still_costs():
    """Otherwise a DAG that fails repeatedly spends without limit."""
    orch, runner, _ = build(budget=100.0, goals=("a",))
    runner.pump()
    aid = orch.agent_by_name("step0").id
    runner.fail(aid, "model refused")
    assert runner.ledger.settled() > 0


def test_a_step_that_never_reached_a_harness_costs_nothing():
    """No task id means no request was made. Charging for it is just wrong."""
    from aion.swarm import SwarmOrchestrator
    orch = SwarmOrchestrator(persist=False)
    orch.add_agent("step0", "a goal", harness="claude")
    runner = SwarmRunner(orch, spawn=lambda a, p: "", harness="claude",
                         budget=100.0, prices={"claude": CLAUDE})
    runner.pump()                       # spawn returns "" -> fail before running
    assert runner.ledger.committed() == 0.0


def test_cancelling_gives_the_budget_back_to_the_rest_of_the_dag():
    orch, runner, _ = build(budget=100.0, goals=("a",))
    runner.pump()
    runner.cancel(orch.agent_by_name("step0").id, "changed my mind")
    assert runner.ledger.committed() == 0.0


def test_status_reports_the_budget_and_the_ledger():
    _, runner, _ = build(budget=25.0, goals=("a",))
    st = runner.status()
    assert st["budget"] == 25.0 and st["ledger"]["estimated"] is True
