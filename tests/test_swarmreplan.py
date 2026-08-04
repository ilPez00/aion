"""A DAG that grows from its own results — and the bounds that make that safe.

The proposal arrives from a model, about the output of another model, while
nobody is watching. So most of this file is about what gets REFUSED: the width
cap, the depth cap, the total ceiling, the name collisions, the dependencies
that resolve to nothing, and the parent edge that stops new work being
schedulable before the result it came from.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aion.swarmreplan import (  # noqa: E402
    Expansion, ReplanPolicy, policy_from_config, propose, validate,
)

ON = ReplanPolicy(max_new_steps=3)


def step(name, goal="do it", deps=None, harness=""):
    d = {"name": name, "goal": goal}
    if deps:
        d["deps"] = list(deps)
    if harness:
        d["harness"] = harness
    return d


def run(raw, *, parent="scout", generation=0, existing=None, policy=ON, **kw):
    return validate(raw, parent=parent, parent_generation=generation,
                    existing=existing if existing is not None else {"scout": "done"},
                    policy=policy, **kw)


# ── off by default ──────────────────────────────────────────────────────────

def test_replanning_is_off_until_someone_turns_it_on():
    """A swarm must not start writing its own work because a version changed."""
    out = run([step("audit")], policy=ReplanPolicy())
    assert out.steps == [] and "off" in out.problems[0]


def test_an_empty_proposal_is_a_normal_answer_not_a_problem():
    # "This result needs no further work" is the common case and must not read
    # as a failure, or every finished step logs an error.
    out = run([])
    assert out.steps == [] and out.problems == []


# ── the parent edge ─────────────────────────────────────────────────────────

def test_every_new_step_waits_for_the_result_it_came_from():
    """It was proposed FROM that output. Without this edge it is schedulable
    immediately, i.e. before the thing that justified it."""
    out = run([step("audit")])
    assert out.steps[0].deps == ["scout"]


def test_the_parent_edge_is_not_duplicated():
    out = run([step("audit", deps=["scout"])])
    assert out.steps[0].deps == ["scout"]


def test_a_sibling_dependency_is_kept_alongside_the_parent():
    out = run([step("audit"), step("report", deps=["audit"])])
    assert out.steps[1].deps == ["audit", "scout"]


# ── width, depth, ceiling ───────────────────────────────────────────────────

def test_more_steps_than_allowed_are_cut_and_the_cut_is_reported():
    out = run([step(f"s{i}") for i in range(6)], policy=ReplanPolicy(max_new_steps=2))
    assert [s.name for s in out.steps] == ["s0", "s1"]
    assert any("not taken" in d for d in out.dropped)


def test_a_step_at_the_depth_limit_may_not_replan_again():
    """Width bounds one round. Depth is what bounds the recursion, and the
    recursion is what runs up a bill overnight."""
    out = run([step("audit")], generation=2,
              policy=ReplanPolicy(max_new_steps=3, max_generations=2))
    assert out.steps == [] and "as deep as replanning goes" in out.problems[0]


def test_a_full_swarm_refuses_to_grow():
    existing = {f"s{i}": "done" for i in range(24)}
    out = run([step("audit")], parent="s0", existing=existing,
              policy=ReplanPolicy(max_new_steps=3, max_total_steps=24))
    assert out.steps == [] and "ceiling" in out.problems[0]


def test_the_ceiling_beats_the_per_step_width():
    """Ten steps each allowed three must not cooperate their way past the
    total: the room left is what is left, not what one step may add."""
    existing = {f"s{i}": "done" for i in range(9)}
    out = run([step("a"), step("b"), step("c")], parent="s0", existing=existing,
              policy=ReplanPolicy(max_new_steps=3, max_total_steps=10))
    assert [s.name for s in out.steps] == ["a"]


# ── nothing invented is accepted quietly ────────────────────────────────────

def test_a_name_that_already_exists_is_dropped_not_renamed():
    """Names are the dependency key. Renaming invents an identity, and every
    dependency written against that name becomes ambiguous."""
    out = run([step("scout"), step("audit")])
    assert [s.name for s in out.steps] == ["audit"]
    assert any("already exists" in d for d in out.dropped)


def test_two_proposed_steps_cannot_share_a_name():
    out = run([step("audit"), step("audit", goal="again")])
    assert len(out.steps) == 1


def test_a_dependency_on_nothing_drops_the_step():
    out = run([step("audit", deps=["ghost"])])
    assert out.steps == []
    assert any("not in this swarm" in d for d in out.dropped)


def test_a_step_without_a_goal_is_dropped():
    out = run([{"name": "audit"}])
    assert out.steps == [] and any("no goal" in d for d in out.dropped)


def test_an_unusable_name_is_dropped():
    out = run([step("../etc/passwd")])
    assert out.steps == [] and any("not a usable name" in d for d in out.dropped)


def test_junk_in_the_list_is_skipped_not_fatal():
    out = run(["a string", 7, step("audit")])
    assert [s.name for s in out.steps] == ["audit"]


def test_a_reply_that_is_not_a_list_is_refused():
    out = run({"name": "audit"})
    assert out.steps == [] and "list of steps" in out.problems[0]


def test_a_hallucinated_harness_falls_back_rather_than_failing_at_run_time():
    out = run([step("audit", harness="gpt-9")], known_harnesses=("claude",))
    assert out.steps[0].harness == ""


def test_a_real_harness_is_kept():
    out = run([step("audit", harness="claude")], known_harnesses=("claude",))
    assert out.steps[0].harness == "claude"


def test_a_cycle_among_the_new_steps_is_refused_whole():
    out = run([step("a", deps=["b"]), step("b", deps=["a"])])
    # `b` is dropped first (its dep does not exist yet), so the survivor is
    # the honest one; what matters is that no cycle is ever created.
    assert all(s.name != "b" for s in out.steps)


def test_a_long_goal_is_truncated_not_rejected():
    out = run([step("audit", goal="x" * 900)])
    assert len(out.steps[0].goal) == 500


# ── proposing ───────────────────────────────────────────────────────────────

def test_propose_returns_the_raw_list_for_validate_to_judge():
    raw = propose("find docs", "found three subsystems",
                  chat=lambda p: '[{"name": "audit", "goal": "audit them"}]')
    assert raw == [{"name": "audit", "goal": "audit them"}]


def test_propose_tolerates_a_fenced_reply():
    raw = propose("g", "out", chat=lambda p: '```json\n[{"name":"a","goal":"b"}]\n```')
    assert raw[0]["name"] == "a"


def test_a_step_with_no_output_is_not_worth_asking_about():
    called = []
    propose("g", "   ", chat=lambda p: called.append(p) or "[]")
    assert called == []


def test_a_dead_planner_changes_nothing():
    def boom(_p):
        raise RuntimeError("no provider")
    assert propose("g", "out", chat=boom) == []


def test_an_unparseable_reply_changes_nothing():
    assert propose("g", "out", chat=lambda p: "sure! here is my plan: do stuff") == []


def test_a_refusal_changes_nothing():
    assert propose("g", "out", chat=lambda p: "⚠ no provider configured") == []


def test_the_prompt_says_that_no_further_work_is_a_valid_answer():
    seen = {}
    propose("find docs", "output", chat=lambda p: seen.setdefault("p", p) or "[]")
    assert "[] if the result needs no further work" in seen["p"]


def test_the_prompt_carries_the_existing_step_names():
    seen = {}
    propose("g", "o", existing_names=["scout", "writer"],
            chat=lambda p: seen.setdefault("p", p) or "[]")
    assert "scout, writer" in seen["p"]


# ── config ──────────────────────────────────────────────────────────────────

def test_no_config_means_no_replanning():
    assert policy_from_config({}).enabled is False


def test_a_bare_number_is_the_width():
    assert policy_from_config({"swarm_replan": 3}).max_new_steps == 3


def test_the_full_dict_is_read():
    p = policy_from_config({"swarm_replan": {
        "max_new_steps": 2, "max_total_steps": 8, "max_generations": 1}})
    assert (p.max_new_steps, p.max_total_steps, p.max_generations) == (2, 8, 1)


def test_a_typo_falls_back_to_off_rather_than_on():
    """The unsafe direction here is a swarm that grows because a key was
    misspelled, so unparseable config means replanning stays off."""
    assert policy_from_config({"swarm_replan": "yes please"}).enabled is False
    assert policy_from_config({"swarm_replan": {"max_new_steps": "three"}}).enabled is False


def test_true_means_a_sane_default_width():
    assert policy_from_config({"swarm_replan": True}).max_new_steps == 3


def test_the_expansion_serialises_for_a_log():
    exp = Expansion(parent="scout", steps=[], problems=["replanning is off"])
    assert exp.as_dict()["ok"] is False


# ── the runner: queueing, applying, provenance ──────────────────────────────
from aion.swarm import AgentStatus, SwarmOrchestrator  # noqa: E402
from aion.swarmrun import SwarmRunner  # noqa: E402


def runner(policy=ON, goals=("find the docs",)):
    orch = SwarmOrchestrator(persist=False)
    for i, goal in enumerate(goals):
        orch.add_agent(f"step{i}", goal)
    r = SwarmRunner(orch, spawn=lambda a, p: f"t{a.name}", replan=policy,
                    max_parallel=10)
    return orch, r


def test_a_finished_step_is_offered_a_proposal():
    orch, r = runner()
    r.pump()
    r.finish(orch.agent_by_name("step0").id, "found three subsystems")
    assert [s["name"] for s in r.take_replans()] == ["step0"]


def test_a_step_that_produced_nothing_is_not_worth_asking_about():
    orch, r = runner()
    r.pump()
    r.finish(orch.agent_by_name("step0").id, "")
    assert r.take_replans() == []


def test_nothing_is_queued_while_replanning_is_off():
    orch, r = runner(policy=ReplanPolicy())
    r.pump()
    r.finish(orch.agent_by_name("step0").id, "lots of output")
    assert r.take_replans() == []


def test_each_finished_step_is_offered_once():
    orch, r = runner()
    r.pump()
    r.finish(orch.agent_by_name("step0").id, "out")
    r.take_replans()
    assert r.take_replans() == [], "a drained queue must not re-offer"


def test_applying_a_proposal_creates_the_steps():
    orch, r = runner()
    r.pump()
    parent = orch.agent_by_name("step0")
    r.finish(parent.id, "out")
    out = r.apply_expansion(parent.id, [step("audit", "audit the subsystems")])
    assert out["created"] == ["audit"]
    assert orch.agent_by_name("audit").dependencies == ["step0"]


def test_a_created_step_remembers_who_asked_for_it():
    """Provenance is the whole defence against a DAG nobody recognises."""
    orch, r = runner()
    r.pump()
    parent = orch.agent_by_name("step0")
    r.finish(parent.id, "out")
    r.apply_expansion(parent.id, [step("audit")])
    new = orch.agent_by_name("audit")
    assert new.generation == 1 and new.parent_id == parent.id


def test_generation_climbs_one_round_at_a_time_and_then_stops():
    orch, r = runner(policy=ReplanPolicy(max_new_steps=2, max_generations=2))
    r.pump()
    first = orch.agent_by_name("step0")
    r.finish(first.id, "out")
    r.apply_expansion(first.id, [step("gen1")])
    g1 = orch.agent_by_name("gen1")
    r.apply_expansion(g1.id, [step("gen2")])
    g2 = orch.agent_by_name("gen2")
    assert g2.generation == 2
    out = r.apply_expansion(g2.id, [step("gen3")])
    assert out["created"] == [] and orch.agent_by_name("gen3") is None


def test_a_refusal_is_logged_on_the_step_that_proposed_it():
    """A swarm that silently declines to grow looks exactly like one whose
    planner said nothing, and those need different responses from a human."""
    orch, r = runner()
    parent = orch.agent_by_name("step0")
    r.apply_expansion(parent.id, [step("audit", deps=["ghost"])])
    assert any("dropped" in line for line in parent.logs)


def test_what_was_added_is_logged_too():
    orch, r = runner()
    parent = orch.agent_by_name("step0")
    r.apply_expansion(parent.id, [step("audit")])
    assert any("added audit" in line for line in parent.logs)


def test_expanding_an_unknown_step_is_refused_not_crashed():
    _, r = runner()
    assert r.apply_expansion("ghost", [step("audit")])["ok"] is False


def test_new_work_is_schedulable_only_after_its_parent():
    orch, r = runner()
    r.pump()                                   # step0 running
    parent = orch.agent_by_name("step0")
    r.finish(parent.id, "out")
    r.apply_expansion(parent.id, [step("audit")])
    assert orch.agent_by_name("audit").status is AgentStatus.IDLE
    assert [a.name for a in orch.agents_ready()] == ["audit"]


def test_generation_survives_a_checkpoint():
    """It is the bound on recursion. A restart that reset it would let a swarm
    at its depth limit start growing again."""
    from aion.swarm import SwarmAgent
    orch, r = runner()
    parent = orch.agent_by_name("step0")
    r.apply_expansion(parent.id, [step("audit")])
    record = orch.agent_by_name("audit").as_record()
    assert SwarmAgent.from_record(record).generation == 1
