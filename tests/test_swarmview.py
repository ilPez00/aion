"""The DAG view: does it show running ORDER, and does it say why work stopped.

Every test here is about a question an operator asks out loud. A layering test
that only checks list shapes would pass while the panel still lied about which
step runs next, so the assertions are written against the answer, not the data
structure that carries it.
"""

import pytest

from aion.swarmview import (capacity, explain, frontier, render,
                            render_dead_letters, spend, unresolved_deps, waves)


def step(name, status="idle", deps=None, progress=0.0, **kw):
    d = {"name": name, "id": f"s{name}", "goal": f"do {name}",
         "status": status, "deps": list(deps or []), "progress": progress,
         "attempts": 0, "retry_at": 0.0}
    d.update(kw)
    return d


def names(layers):
    return [[a["name"] for a in layer] for layer in layers]


# ---- waves: reading order is running order ------------------------------

def test_a_chain_becomes_one_step_per_wave():
    dag = [step("c", deps=["b"]), step("b", deps=["a"]), step("a")]
    assert names(waves(dag)) == [["a"], ["b"], ["c"]]


def test_independent_steps_share_a_wave():
    dag = [step("a"), step("b"), step("c")]
    assert names(waves(dag)) == [["a", "b", "c"]]


def test_a_join_waits_for_its_slowest_branch():
    # writer depends on research (wave 1) and edit (wave 2), so it is wave 3 —
    # not wave 2, which is what "max depth of deps" gets wrong if it uses min.
    dag = [step("research"), step("edit", deps=["research"]),
           step("writer", deps=["research", "edit"])]
    assert names(waves(dag)) == [["research"], ["edit"], ["writer"]]


def test_order_inside_a_wave_follows_input_not_status():
    # A view whose rows reorder as steps run is unreadable: the eye tracks
    # position. Same wave, same order as the swarm lists them.
    dag = [step("a"), step("b", status="working"), step("c", status="done")]
    assert names(waves(dag)) == [["a", "b", "c"]]


def test_an_unknown_dependency_does_not_sink_the_step():
    # The typo is reported by unresolved_deps; burying the row at the bottom
    # would disguise a typo as a layout quirk.
    dag = [step("writer", deps=["reserch"]), step("research")]
    assert names(waves(dag)) == [["writer", "research"]]


def test_a_cycle_is_shown_not_dropped():
    dag = [step("a", deps=["b"]), step("b", deps=["a"]), step("c")]
    layers = names(waves(dag))
    assert layers[0] == ["c"]
    assert sorted(layers[-1]) == ["a", "b"]


def test_every_step_appears_exactly_once():
    dag = [step("a"), step("b", deps=["a"]), step("c", deps=["a"]),
           step("d", deps=["b", "c"]), step("e", deps=["zzz"])]
    flat = [n for layer in names(waves(dag)) for n in layer]
    assert sorted(flat) == ["a", "b", "c", "d", "e"]


def test_no_steps_no_waves():
    assert waves([]) == []


# ---- unresolved deps ----------------------------------------------------

def test_a_misspelled_dependency_is_named():
    dag = [step("writer", deps=["reserch"]), step("research")]
    assert unresolved_deps(dag) == {"writer": ["reserch"]}


def test_a_satisfied_dependency_is_not_reported():
    dag = [step("writer", deps=["research"]), step("research")]
    assert unresolved_deps(dag) == {}


# ---- frontier: what is running, next, stuck -----------------------------

def test_a_step_waiting_on_a_running_step_is_next_not_blocked():
    # Conflating "waiting" with "blocked" is what makes a healthy swarm look
    # broken. Only a dead upstream blocks.
    dag = [step("a", status="working"), step("b", deps=["a"])]
    f = frontier(dag)
    assert f["running"] == ["a"]
    assert f["blocked"] == []
    assert f["ready"] == []


def test_a_step_behind_a_failure_is_blocked_and_says_by_what():
    dag = [step("a", status="failed"), step("b", deps=["a"])]
    f = frontier(dag)
    assert f["blocked"] == [{"name": "b", "needs": ["a"]}]


def test_a_step_whose_deps_are_done_is_ready():
    dag = [step("a", status="done"), step("b", deps=["a"])]
    assert frontier(dag)["ready"] == ["b"]


def test_a_cancelled_dependency_still_unblocks():
    # Cancelling a step is a decision that it will not run; the DAG has to move
    # on, or one cancel strands everything downstream forever.
    dag = [step("a", status="cancelled"), step("b", deps=["a"])]
    assert frontier(dag)["ready"] == ["b"]


def test_a_step_inside_its_backoff_is_retrying_not_ready():
    dag = [step("a", retry_at=1000.0, attempts=2)]
    f = frontier(dag, now=940.0)
    assert f["ready"] == []
    assert f["retrying"][0]["name"] == "a"
    assert f["retrying"][0]["in_s"] == pytest.approx(60.0)


def test_the_soonest_retry_is_reported_first():
    dag = [step("slow", retry_at=1100.0), step("soon", retry_at=1010.0)]
    assert [r["name"] for r in frontier(dag, now=1000.0)["retrying"]] == ["soon", "slow"]


# ---- explain: the sentence the panel shows ------------------------------

def test_an_empty_swarm_says_how_to_start_one():
    assert "swarm create" in explain([])


def test_a_cycle_beats_every_other_reason():
    # Steps are running, but the cycle is the thing a human must fix, and the
    # running steps will not fix it.
    dag = [step("a", deps=["b"]), step("b", deps=["a"]),
           step("c", status="working")]
    assert "cycle" in explain(dag)


def test_a_typo_is_named_before_the_running_count():
    dag = [step("writer", deps=["reserch"]), step("c", status="working")]
    out = explain(dag)
    assert "reserch" in out and "not a step" in out


def test_running_steps_are_listed():
    dag = [step("a", status="working"), step("b", status="working")]
    assert explain(dag) == "2 running: a, b"


def test_a_long_running_list_is_truncated_with_a_count():
    dag = [step(f"a{i}", status="working") for i in range(5)]
    assert explain(dag).endswith("+2")


def test_a_stopped_swarm_inside_a_backoff_says_wait_this_long():
    dag = [step("a", retry_at=1030.0, attempts=1)]
    assert explain(dag, now=1000.0) == "a retries in 30s (attempt 2)"


def test_a_swarm_with_ready_work_says_what_to_type():
    dag = [step("a"), step("b")]
    assert explain(dag) == "2 ready — `swarm run` to start"


def test_a_fully_blocked_swarm_names_the_failure_underneath():
    dag = [step("a", status="failed"), step("b", deps=["a"])]
    assert explain(dag) == "blocked: b needs a, which failed"


def test_a_finished_swarm_says_so():
    dag = [step("a", status="done"), step("b", status="done")]
    assert explain(dag) == "all 2 steps done"


def test_a_failure_with_nothing_left_to_run_is_not_reported_as_idle():
    dag = [step("a", status="failed"), step("b", status="done")]
    assert explain(dag) == "1 failed, nothing left to run"


# ---- render -------------------------------------------------------------

def test_the_render_labels_waves_in_running_order():
    dag = [step("a"), step("b", deps=["a"])]
    out = render(dag)
    assert "wave 1" in out and "wave 2" in out
    assert out.index("wave 1") < out.index("wave 2")
    assert out.index("a") < out.index("b")


def test_a_cycle_wave_is_labelled_a_cycle():
    dag = [step("a", deps=["b"]), step("b", deps=["a"])]
    out = render(dag)
    assert "cycle" in out and "wave 2" not in out


def test_a_blocked_row_names_the_upstream_that_failed():
    dag = [step("scan", status="failed"), step("publish", deps=["scan"])]
    assert "needs scan" in render(dag)


def test_a_retrying_row_shows_the_countdown_and_the_attempt():
    dag = [step("a", retry_at=1012.0, attempts=1)]
    assert "retry 12s (2)" in render(dag, now=1000.0)


def test_a_row_with_a_bad_dependency_says_the_name_does_not_exist():
    dag = [step("writer", deps=["reserch"])]
    assert "no step 'reserch'" in render(dag)


def test_a_failed_row_shows_how_many_tries_it_took():
    dag = [step("a", status="failed", attempts=3)]
    assert "failed ×3" in render(dag)


def test_a_remote_step_shows_where_it_runs():
    dag = [step("a", instance="workstation")]
    assert "@workstation" in render(dag)


def test_progress_fills_the_bar():
    assert "██████" in render([step("a", status="working", progress=1.0)])
    assert "░░░░░░" in render([step("a", progress=0.0)])


def test_a_huge_dag_is_truncated_with_a_count_not_silently_cut():
    dag = [step(f"a{i}") for i in range(30)]
    out = render(dag, max_rows=5)
    assert "more" in out
    assert out.count("\n") < 12


def test_an_empty_dag_renders_a_placeholder_not_a_crash():
    assert "no steps" in render([])


def test_a_quiet_row_spends_its_width_on_the_goal():
    # Nothing to warn about, so say what the step is for. This is what lets
    # one block replace the old flat-list-plus-DAG pair.
    assert "draft the post" in render([step("writer", goal="draft the post")])


def test_a_warning_beats_the_goal_for_the_same_space():
    dag = [step("scan", status="failed"), step("writer", goal="draft", deps=["scan"])]
    out = render(dag)
    assert "needs scan" in out and "draft" not in out.split("needs scan")[1]


def test_a_done_step_draws_a_full_bar_whatever_progress_says():
    # set_status(DONE) does not touch progress; an empty bar beside a ✓ reads
    # as "finished but did nothing".
    assert "██████" in render([step("a", status="done", progress=0.0)])


def test_a_cancelled_step_does_not_claim_to_be_complete():
    assert "░░░░░░" in render([step("a", status="cancelled", progress=0.0)])


def test_a_plan_preview_drops_the_empty_progress_bars():
    # Nothing has run yet, so every bar would be identical and empty — noise
    # occupying the width the goal needs.
    out = render([step("a", goal="do the thing")], bars=False)
    assert "░" not in out and "do the thing" in out


# ---- the governor's numbers ---------------------------------------------

def test_an_unmetered_swarm_shows_no_money_line():
    # Most harnesses have no price configured. A "$0.00" that means "unknown"
    # is worse than silence.
    assert spend({"ledger": {"committed": 0.0}, "budget": 0.0}) == ""


def test_spend_reads_as_an_estimate_every_time():
    out = spend({"ledger": {"committed": 0.12}, "budget": 1.0})
    assert out.startswith("~$0.12 of $1.00")
    assert out.endswith("est")


def test_spend_shows_the_share_of_the_budget_used():
    assert "(25%)" in spend({"ledger": {"committed": 0.25}, "budget": 1.0})


def test_money_spent_on_retries_is_called_out():
    # Retries are exactly what a budget exists to bound, so they are not
    # allowed to hide inside the total.
    out = spend({"ledger": {"committed": 0.30, "retried": 0.10}, "budget": 1.0})
    assert "~$0.10 on retries" in out


def test_spend_without_a_budget_still_reports_what_was_spent():
    assert spend({"ledger": {"committed": 0.4}, "budget": 0}) == "~$0.40 est"


def test_capacity_explains_ready_but_not_started():
    assert capacity({"in_flight": 2, "max_parallel": 3}) == "2/3 slots"


def test_capacity_includes_vram_when_it_is_the_limit():
    out = capacity({"in_flight": 1, "max_parallel": 4,
                    "vram_used": 4096, "vram_total": 8192})
    assert out == "1/4 slots · vram 4.0/8.0G"


def test_capacity_is_silent_when_nothing_bounds_it():
    assert capacity({}) == ""


def test_dead_letters_lead_with_what_is_stuck_behind_them():
    # "What failed" is already on the row above. The remediation question is
    # what it is holding up.
    out = "\n".join(render_dead_letters({"dead_letters": [
        {"name": "scan", "kind": "permanent", "attempts": 3,
         "error": "401 unauthorized", "blocks": ["publish", "index"]}]}))
    assert "blocks publish, index" in out
    assert "401 unauthorized" in out


def test_a_dead_letter_blocking_nothing_says_so():
    out = "\n".join(render_dead_letters({"dead_letters": [
        {"name": "scan", "kind": "transient", "blocks": []}]}))
    assert "blocks nothing else" in out


def test_dead_letters_are_capped_with_a_count():
    out = "\n".join(render_dead_letters({"dead_letters": [
        {"name": f"s{i}", "kind": "unknown", "blocks": []} for i in range(6)]}))
    assert "… 3 more" in out


def test_no_dead_letters_no_block():
    assert render_dead_letters({"dead_letters": []}) == []
    assert render_dead_letters({}) == []


def test_a_cheap_swarm_is_not_rounded_down_to_zero():
    # One prompt costs fractions of a cent. Two decimals read "$0.00" for the
    # whole early life of a swarm, which is a cost display that lies.
    assert "~$0.003" in spend({"ledger": {"committed": 0.0032}, "budget": 1.0})


def test_a_step_the_swarm_added_itself_says_so_on_its_row():
    # "I do not remember adding that" versus "the swarm added it, and here is
    # how deep the chain goes" — the row is where that question gets answered.
    out = render([step("audit", goal="audit the auth service", generation=1)])
    assert "+g1" in out and "audit the auth" in out


def test_a_step_a_human_wrote_carries_no_marker():
    assert "+g" not in render([step("scout", goal="find the docs")])
