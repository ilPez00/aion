"""Planning a swarm: goal → validated DAG.

Every accepted step becomes a prompt a harness will execute, so this is the
same posture as `voicecmd`: the model proposes, and almost everything it says
is checked before it becomes anything. The tests are mostly about refusal.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aion.swarm import SwarmOrchestrator  # noqa: E402
from aion.swarmplan import (  # noqa: E402
    MAX_STEPS, Plan, Step, apply, propose, topo_order, validate,
)


def steps(*triples):
    return [{"name": n, "goal": g, "deps": list(d)} for n, g, d in triples]


# ── validation ───────────────────────────────────────────────────────────────
def test_a_clean_plan_validates():
    p = validate("write a report", steps(
        ("scout", "find sources", []),
        ("writer", "draft it", ["scout"]),
    ))
    assert p.ok
    assert [s.name for s in p.steps] == ["scout", "writer"]


def test_steps_come_back_in_dependency_order():
    """add_checked refuses a dependency that does not exist yet, so insertion
    order is not cosmetic."""
    p = validate("g", steps(
        ("editor", "polish", ["writer"]),
        ("writer", "draft", ["scout"]),
        ("scout", "find", []),
    ))
    assert [s.name for s in p.steps] == ["scout", "writer", "editor"]


def test_a_cycle_is_refused_at_creation():
    """stalled() can report a cycle, but by then the agents exist and somebody
    has to delete them by hand."""
    p = validate("g", steps(("a", "x", ["b"]), ("b", "y", ["a"])))
    assert not p.ok
    assert any("cycle" in why for why in p.problems)


def test_a_self_dependency_is_refused():
    p = validate("g", steps(("a", "x", ["a"])))
    assert not p.ok
    assert any("itself" in why for why in p.problems)


def test_an_invented_dependency_is_refused():
    """A step depending on a name nobody defined can never start."""
    p = validate("g", steps(("writer", "draft", ["ghost"])))
    assert not p.ok
    assert any("ghost" in why for why in p.problems)


def test_a_dependency_on_an_existing_agent_is_fine():
    p = validate("g", steps(("writer", "draft", ["scout"])),
                 existing_names=["scout"])
    assert p.ok


def test_duplicate_names_are_refused():
    """Names are the dependency key, so two of them make every reference a
    coin flip."""
    p = validate("g", steps(("a", "x", []), ("a", "y", [])))
    assert not p.ok
    assert any("already used" in why for why in p.problems)


def test_a_name_that_collides_with_an_existing_agent_is_refused():
    p = validate("g", steps(("scout", "x", [])), existing_names=["scout"])
    assert not p.ok


def test_step_count_is_capped():
    """A model returning two hundred steps is an amplification bomb, not an
    ambitious plan."""
    many = steps(*[(f"s{i}", "work", []) for i in range(MAX_STEPS + 5)])
    p = validate("g", many)
    assert not p.ok
    assert p.steps == []
    assert str(MAX_STEPS) in " ".join(p.problems)


def test_exactly_the_cap_is_allowed():
    p = validate("g", steps(*[(f"s{i}", "work", []) for i in range(MAX_STEPS)]))
    assert p.ok


@pytest.mark.parametrize("bad", [
    [], "not a list", None, {}, 42,
])
def test_junk_instead_of_steps(bad):
    assert not validate("g", bad).ok


def test_a_non_object_step_is_reported_not_crashed():
    p = validate("g", ["just a string", {"name": "a", "goal": "x"}])
    assert any("not an object" in why for why in p.problems)


def test_missing_name_or_goal_is_refused():
    assert any("no name" in w for w in validate("g", [{"goal": "x"}]).problems)
    assert any("no goal" in w for w in
               validate("g", [{"name": "a"}]).problems)


def test_a_hostile_name_is_refused():
    """Names end up in prompts, logs and the graph. Keep them boring."""
    for name in ("../../etc/passwd", "a\nb", "<script>", ""):
        p = validate("g", [{"name": name, "goal": "x"}])
        assert not p.ok


def test_deps_must_be_a_list():
    p = validate("g", [{"name": "a", "goal": "x", "deps": "scout"}])
    assert any("must be a list" in w for w in p.problems)


def test_an_unknown_harness_falls_back_rather_than_failing_the_plan():
    """Refusing a whole plan over a guessed model name would be the wrong
    trade: the step is still valid work."""
    p = validate("g", [{"name": "a", "goal": "x", "harness": "gpt-9000"}],
                 known_harnesses=["demo", "research"])
    assert p.steps[0].harness == ""
    assert any("gpt-9000" in w for w in p.problems)


def test_a_known_harness_is_kept():
    p = validate("g", [{"name": "a", "goal": "x", "harness": "research"}],
                 known_harnesses=["demo", "research"])
    assert p.ok and p.steps[0].harness == "research"


def test_overlong_text_is_clipped_not_rejected():
    p = validate("g" * 5000, [{"name": "a" * 200, "goal": "x" * 5000}])
    assert len(p.steps[0].name) <= 40
    assert len(p.steps[0].goal) <= 500


def test_every_problem_is_collected():
    """A five-step DAG fixed one error per round trip is miserable, so
    validation reports all of them in one pass."""
    p = validate("g", steps(
        ("a", "", []),                 # no goal
        ("b", "y", ["ghost"]),         # invented dependency
        ("c", "z", ["c"]),             # self-dependency
        ("b", "w", []),                # duplicate name
    ))
    assert not p.ok
    joined = " ".join(p.problems)
    for expected in ("no goal", "ghost", "itself", "already used"):
        assert expected in joined, f"{expected!r} was not reported"


def test_a_rejected_step_does_not_reserve_its_name():
    """The first `a` is discarded for having no goal, so the second is the
    only `a` and is unambiguous. The plan is invalid either way."""
    p = validate("g", steps(("a", "", []), ("a", "z", [])))
    assert not p.ok
    assert [s.name for s in p.steps] == ["a"]


def test_plan_serialises():
    json.dumps(validate("g", steps(("a", "x", []))).as_dict())


# ── topological order ────────────────────────────────────────────────────────
def test_topo_order_is_stable():
    s = [Step("a", "x"), Step("b", "y"), Step("c", "z")]
    assert [x.name for x in topo_order(s)] == ["a", "b", "c"]


def test_topo_order_detects_a_three_node_cycle():
    s = [Step("a", "x", ["c"]), Step("b", "y", ["a"]), Step("c", "z", ["b"])]
    assert topo_order(s) is None


def test_topo_order_ignores_dependencies_outside_the_plan():
    """A dependency on an agent that already exists is satisfied before the
    first insert, so it must not constrain the order."""
    s = [Step("a", "x", ["already-there"])]
    assert [x.name for x in topo_order(s)] == ["a"]


def test_a_diamond_orders_correctly():
    s = [Step("d", "", ["b", "c"]), Step("b", "", ["a"]),
         Step("c", "", ["a"]), Step("a", "")]
    order = [x.name for x in topo_order(s)]
    assert order[0] == "a" and order[-1] == "d"


# ── proposing (model output is untrusted) ────────────────────────────────────
def reply(text):
    return lambda _prompt: text


def test_a_good_reply_becomes_a_plan():
    out = propose("write a report", chat=reply(json.dumps([
        {"name": "scout", "goal": "find sources", "deps": []},
        {"name": "writer", "goal": "draft", "deps": ["scout"]},
    ])))
    assert out.ok and out.source == "llm"
    assert [s.name for s in out.steps] == ["scout", "writer"]


def test_a_fenced_reply_is_accepted():
    """Models wrap JSON in fences more often than not; failing on a formatting
    habit rather than a substantive problem would be a bad trade."""
    out = propose("g", chat=reply(
        '```json\n[{"name":"a","goal":"x","deps":[]}]\n```'))
    assert out.ok


def test_prose_around_the_json_is_tolerated():
    out = propose("g", chat=reply(
        'Sure! Here is the plan:\n[{"name":"a","goal":"x"}]\nHope that helps.'))
    assert out.ok


def test_unparseable_output_is_a_problem_not_an_exception():
    out = propose("g", chat=reply("I would rather not."))
    assert not out.ok and "JSON" in " ".join(out.problems)


def test_a_planner_that_raises_is_caught():
    def boom(_):
        raise RuntimeError("no provider configured")
    out = propose("g", chat=boom)
    assert not out.ok
    assert "planner unavailable" in " ".join(out.problems)


def test_an_empty_reply_is_a_problem():
    assert not propose("g", chat=reply("")).ok
    assert not propose("g", chat=reply("⚠ no provider")).ok


def test_a_cyclic_proposal_is_refused():
    out = propose("g", chat=reply(json.dumps([
        {"name": "a", "goal": "x", "deps": ["b"]},
        {"name": "b", "goal": "y", "deps": ["a"]},
    ])))
    assert not out.ok


def test_an_oversized_proposal_is_refused():
    out = propose("g", chat=reply(json.dumps(
        [{"name": f"s{i}", "goal": "w"} for i in range(60)])))
    assert not out.ok
    assert out.steps == []


def test_no_goal_never_reaches_the_model():
    calls = []
    propose("   ", chat=lambda p: calls.append(p) or "[]")
    assert calls == []


def test_the_prompt_offers_only_installed_harnesses():
    seen = {}
    propose("g", harnesses=["demo", "research"],
            chat=lambda p: seen.setdefault("p", p) or "[]")
    assert "demo" in seen["p"] and "research" in seen["p"]


# ── applying ─────────────────────────────────────────────────────────────────
def test_apply_creates_the_agents_in_order():
    o = SwarmOrchestrator()
    plan = validate("g", steps(
        ("editor", "polish", ["writer"]),
        ("writer", "draft", ["scout"]),
        ("scout", "find", []),
    ))
    out = apply(o, plan)
    assert out["ok"] and out["created"] == ["scout", "writer", "editor"]
    assert o.agent_by_name("editor").dependencies == ["writer"]


def test_apply_does_not_start_anything():
    """Creating work and starting work are separate decisions — a DAG is N
    prompts a harness will run, and reviewing it first is the point."""
    from aion.swarm import AgentStatus
    o = SwarmOrchestrator()
    apply(o, validate("g", steps(("a", "x", []))))
    assert o.agent_by_name("a").status is AgentStatus.IDLE


def test_apply_refuses_an_invalid_plan():
    o = SwarmOrchestrator()
    out = apply(o, validate("g", steps(("a", "x", ["ghost"]))))
    assert out["ok"] is False and out["created"] == []
    assert o.agents == {}


def test_apply_reports_a_partial_failure():
    o = SwarmOrchestrator()
    o.add_checked("scout", "already here")
    plan = Plan(goal="g", steps=[Step("scout", "duplicate"), Step("new", "fine")])
    out = apply(o, plan)
    assert out["ok"] is False
    assert out["created"] == ["new"]
    assert out["failed"][0]["name"] == "scout"


def test_a_planned_dag_is_immediately_runnable():
    """The join between the two halves: what the planner creates is exactly
    what the executor's readiness check understands."""
    o = SwarmOrchestrator()
    apply(o, validate("g", steps(
        ("scout", "find", []), ("writer", "draft", ["scout"]))))
    assert [a.name for a in o.agents_ready()] == ["scout"]
    assert o.dep_state(o.agent_by_name("writer"))[0] == "waiting"


# ── review-then-commit ───────────────────────────────────────────────────────
def test_applying_reviewed_steps_does_not_re_plan():
    """The HUD shows a plan, then sends those exact steps back. Re-running the
    planner on commit would create a DAG nobody read — the model is not
    deterministic, and "review then commit" means nothing if the commit
    re-rolls the dice."""
    from aion.store import Store
    o = SwarmOrchestrator()
    s = Store.__new__(Store)
    s.swarm = o
    s.harnesses = {}

    reviewed = [{"name": "scout", "goal": "find", "deps": []},
                {"name": "writer", "goal": "draft", "deps": ["scout"]}]
    out = s.swarm_command({"action": "plan", "goal": "g",
                           "steps": reviewed, "apply": True})
    assert out["applied"]["created"] == ["scout", "writer"]
    assert [a.name for a in o.agents.values()] == ["scout", "writer"]


def test_reviewed_steps_are_still_validated():
    """They arrive from a browser, so they get the same checks as the model's."""
    from aion.store import Store
    o = SwarmOrchestrator()
    s = Store.__new__(Store)
    s.swarm = o
    s.harnesses = {}
    out = s.swarm_command({"action": "plan", "goal": "g", "apply": True,
                           "steps": [{"name": "a", "goal": "x", "deps": ["b"]},
                                     {"name": "b", "goal": "y", "deps": ["a"]}]})
    assert out["ok"] is False
    assert any("cycle" in p for p in out["problems"])
    assert o.agents == {}


def test_planning_without_apply_creates_nothing():
    """Fail-closed, like routing: propose, review, then commit."""
    from aion.store import Store
    o = SwarmOrchestrator()
    s = Store.__new__(Store)
    s.swarm = o
    s.harnesses = {}
    out = s.swarm_command({"action": "plan", "goal": "g",
                           "steps": [{"name": "a", "goal": "x"}]})
    assert out["ok"] is True
    assert "applied" not in out
    assert o.agents == {}
