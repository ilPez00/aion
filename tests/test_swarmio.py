"""What a step writes, and who else is writing it.

Two steps editing one file is not a merge conflict — there is no merge, no
branch and no lock, just one of them silently losing. These tests are mostly
about the distinction that makes the check useful: ORDERED writers are a
sequence and fine; unordered ones race.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aion.swarmio import (  # noqa: E402
    artifact_note, blocking_writes, conflicts, normalise, writes_of,
)


def step(name, deps=None, writes=None, status="idle"):
    return {"name": name, "deps": list(deps or []),
            "writes": list(writes or []), "status": status}


# ── normalising ─────────────────────────────────────────────────────────────

def test_one_path_spelled_two_ways_is_one_declaration():
    assert normalise("./docs/api.md") == normalise("docs/api.md") == "docs/api.md"


def test_a_traversal_is_collapsed():
    assert normalise("docs/../src/main.py") == "src/main.py"


def test_a_trailing_slash_does_not_make_a_second_directory():
    assert normalise("build/") == "build"


def test_case_is_left_alone():
    """Two of the three target platforms are case-sensitive. Folding here
    invents conflicts that do not exist on the machine that matters."""
    assert normalise("Docs/API.md") != normalise("docs/api.md")


def test_nothing_is_resolved_against_a_filesystem():
    # A path that does not exist normalises exactly like one that does: this
    # runs while a DAG is still being planned.
    assert normalise("/nowhere/at/all.txt") == "/nowhere/at/all.txt"


def test_duplicate_declarations_collapse():
    assert writes_of(step("a", writes=["docs/x.md", "./docs/x.md"])) == ["docs/x.md"]


def test_a_directory_listing_of_a_declaration_is_capped():
    assert len(writes_of(step("a", writes=[f"f{i}" for i in range(50)]))) == 20


# ── conflicts ───────────────────────────────────────────────────────────────

def test_two_unordered_writers_of_one_path_are_a_race():
    dag = [step("a", writes=["docs/api.md"]), step("b", writes=["docs/api.md"])]
    assert conflicts(dag) == [{"path": "docs/api.md", "steps": ["a", "b"]}]


def test_ordered_writers_are_a_sequence_not_a_race():
    """`edit` writing what `draft` wrote is the point of depending on it."""
    dag = [step("draft", writes=["docs/api.md"]),
           step("edit", deps=["draft"], writes=["docs/api.md"])]
    assert conflicts(dag) == []


def test_ordering_through_a_third_step_still_counts():
    # a → b → c. a and c write the same file, but never at the same time.
    dag = [step("a", writes=["out.txt"]), step("b", deps=["a"]),
           step("c", deps=["b"], writes=["out.txt"])]
    assert conflicts(dag) == []


def test_two_branches_of_a_fork_do_race():
    dag = [step("root"),
           step("left", deps=["root"], writes=["out.txt"]),
           step("right", deps=["root"], writes=["out.txt"])]
    assert [c["steps"] for c in conflicts(dag)] == [["left", "right"]]


def test_different_paths_are_not_a_conflict():
    dag = [step("a", writes=["one.md"]), step("b", writes=["two.md"])]
    assert conflicts(dag) == []


def test_three_unordered_writers_report_every_pair():
    dag = [step(n, writes=["x"]) for n in ("a", "b", "c")]
    assert [c["steps"] for c in conflicts(dag)] == [["a", "b"], ["a", "c"], ["b", "c"]]


def test_a_cycle_in_a_hand_edited_checkpoint_does_not_hang_the_check():
    """This function's whole job is safety checking; it must survive bad
    input rather than recursing into a wall."""
    dag = [step("a", deps=["b"], writes=["x"]), step("b", deps=["a"], writes=["x"])]
    assert conflicts(dag) == []          # each is an ancestor of the other


def test_a_swarm_that_declares_nothing_reports_nothing():
    assert conflicts([step("a"), step("b")]) == []


# ── admission ───────────────────────────────────────────────────────────────

def test_a_step_is_held_while_another_writes_the_same_path():
    running = [step("a", writes=["docs/api.md"], status="working")]
    out = blocking_writes(step("b", writes=["docs/api.md"]), running)
    assert out == [{"step": "a", "paths": ["docs/api.md"]}]


def test_a_step_writing_elsewhere_is_not_held():
    running = [step("a", writes=["one.md"], status="working")]
    assert blocking_writes(step("b", writes=["two.md"]), running) == []


def test_a_step_that_declares_nothing_is_never_held():
    running = [step("a", writes=["one.md"], status="working")]
    assert blocking_writes(step("b"), running) == []


def test_a_step_does_not_block_itself():
    a = step("a", writes=["one.md"], status="working")
    assert blocking_writes(a, [a]) == []


# ── the prompt ──────────────────────────────────────────────────────────────

def test_downstream_is_told_where_the_work_landed():
    """Splicing stdout says what the input SAID. An agent asked to polish the
    draft still has to be told the filename, or it writes a second one."""
    note = artifact_note([step("draft", writes=["docs/api.md"])])
    assert "draft wrote: docs/api.md" in note


def test_an_upstream_that_wrote_nothing_adds_no_noise():
    assert artifact_note([step("scout")]) == ""


def test_several_upstreams_are_listed_one_per_line():
    note = artifact_note([step("a", writes=["x"]), step("b", writes=["y"])])
    assert note.count("\n") == 2


# ── through the runner ──────────────────────────────────────────────────────
from aion.swarm import AgentStatus, SwarmOrchestrator  # noqa: E402
from aion.swarmrun import SwarmRunner, prompt_for  # noqa: E402


def test_the_runner_serialises_two_writers_of_one_file():
    orch = SwarmOrchestrator(persist=False)
    orch.add_agent("a", "write it", writes=["docs/api.md"])
    orch.add_agent("b", "write it too", writes=["./docs/api.md"])
    r = SwarmRunner(orch, spawn=lambda ag, p: f"t{ag.name}", max_parallel=4)

    first = r.pump()
    assert len(first["started"]) == 1
    held = {d["name"]: d["reason"] for d in first["deferred"]}
    assert "is writing docs/api.md" in list(held.values())[0]


def test_the_held_writer_runs_once_the_other_finishes():
    orch = SwarmOrchestrator(persist=False)
    orch.add_agent("a", "g", writes=["x"])
    orch.add_agent("b", "g", writes=["x"])
    r = SwarmRunner(orch, spawn=lambda ag, p: f"t{ag.name}", max_parallel=4)
    r.pump()
    running = [a for a in orch.agents.values() if a.status is AgentStatus.WORKING][0]
    r.finish(running.id, "done")
    assert len(r.pump()["started"]) == 1


def test_the_prompt_names_the_file_an_upstream_wrote():
    orch = SwarmOrchestrator(persist=False)
    orch.add_agent("draft", "write the page", writes=["docs/api.md"])
    orch.add_agent("edit", "polish it", deps=["draft"])
    r = SwarmRunner(orch, spawn=lambda ag, p: f"t{ag.name}", max_parallel=4)
    r.pump()
    r.finish(orch.agent_by_name("draft").id, "wrote the page")
    r.pump()
    assert "docs/api.md" in r._prompts[orch.agent_by_name("edit").id]


def test_a_race_is_reported_in_the_runner_status():
    orch = SwarmOrchestrator(persist=False)
    orch.add_agent("a", "g", writes=["x"])
    orch.add_agent("b", "g", writes=["x"])
    r = SwarmRunner(orch, spawn=lambda ag, p: "t1")
    assert r.status()["write_conflicts"][0]["path"] == "x"


def test_writes_survive_a_checkpoint():
    from aion.swarm import SwarmAgent
    orch = SwarmOrchestrator(persist=False)
    a = orch.add_agent("a", "g", writes=["docs/api.md"])
    assert SwarmAgent.from_record(a.as_record()).writes == ["docs/api.md"]


def test_the_prompt_still_works_with_no_artifacts():
    assert prompt_for("do it", []) == "do it"
