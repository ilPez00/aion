"""Tests for the factory loop. Injectable runner — no subprocess, no agent."""
from __future__ import annotations

import pytest

from aion import factory
from aion.factory import (
    STOP_ABORTED, STOP_BUDGET, STOP_DONE, STOP_ERROR, STOP_STALLED,
    FactoryConfig, render_command, run_factory,
    output_novelty, detect_stall,
)


def make_runner(outputs, exit_codes=None):
    """run_cmd returning scripted (exit, output) per call; records commands."""
    calls: list[str] = []
    exit_codes = exit_codes or [0] * len(outputs)

    def run(cmd: str):
        i = len(calls)
        calls.append(cmd)
        out = outputs[i] if i < len(outputs) else outputs[-1]
        code = exit_codes[i] if i < len(exit_codes) else 0
        return code, out

    run.calls = calls  # type: ignore[attr-defined]
    return run


# ── render / injection ────────────────────────────────────────────────────────
def test_render_substitutes_iteration_and_prompt():
    out = render_command("run {p} step {n}", 3, "hello", "", 400)
    assert "step 3" in out
    assert "hello" in out


def test_render_quotes_prompt_against_injection():
    out = render_command("agent {p}", 1, "; rm -rf ~", "", 400)
    # the metacharacters must be inside a single quoted argument
    assert "; rm -rf ~" not in out.replace("'; rm -rf ~'", "")
    assert "'" in out


def test_render_quotes_last_output():
    out = render_command("next {last}", 1, "p", "$(evil)", 400)
    assert "$(evil)" not in out.replace("'$(evil)'", "")


def test_render_truncates_last_output_to_tail():
    out = render_command("{last}", 1, "p", "X" * 1000, tail_chars=10)
    assert out.count("X") == 10


# ── done detection ────────────────────────────────────────────────────────────
def test_detect_done_by_marker():
    cfg = FactoryConfig(command="c", done_marker="DONE")
    assert factory.detect_done(cfg, "work... DONE", 0, None) is True
    assert factory.detect_done(cfg, "still going", 0, None) is False


def test_detect_done_by_check_command():
    cfg = FactoryConfig(command="c", done_command="test -f x")
    assert factory.detect_done(cfg, "", 0, lambda c: 0) is True
    assert factory.detect_done(cfg, "", 0, lambda c: 1) is False


def test_marker_takes_precedence_over_check():
    cfg = FactoryConfig(command="c", done_marker="DONE", done_command="never")
    # marker matches, so the (failing) check is never consulted
    assert factory.detect_done(cfg, "DONE", 0, lambda c: 1) is True


# ── loop ──────────────────────────────────────────────────────────────────────
def test_loop_stops_on_marker():
    run = make_runner(["working", "working", "TASK_COMPLETE"])
    cfg = FactoryConfig(command="agent {p}", max_iters=10, done_marker="TASK_COMPLETE")
    res = run_factory("p", cfg, run)
    assert res.stopped == STOP_DONE
    assert res.count == 3


def test_loop_stops_at_budget():
    run = make_runner(["nope"] * 20)
    cfg = FactoryConfig(command="c", max_iters=4, done_marker="DONE")
    res = run_factory("p", cfg, run)
    assert res.stopped == STOP_BUDGET
    assert res.count == 4


def test_loop_stops_on_error_by_default():
    run = make_runner(["ok", "boom"], exit_codes=[0, 2])
    cfg = FactoryConfig(command="c", max_iters=10)
    res = run_factory("p", cfg, run)
    assert res.stopped == STOP_ERROR
    assert res.count == 2


def test_loop_continues_past_error_when_disabled():
    run = make_runner(["fail", "fail", "DONE"], exit_codes=[1, 1, 0])
    cfg = FactoryConfig(command="c", max_iters=10, done_marker="DONE",
                        stop_on_error=False)
    res = run_factory("p", cfg, run)
    assert res.stopped == STOP_DONE
    assert res.count == 3


def test_loop_feeds_last_output_into_next_command():
    run = make_runner(["first-output", "DONE"])
    cfg = FactoryConfig(command="continue {last}", max_iters=5, done_marker="DONE")
    run_factory("p", cfg, run)
    # second command should contain (a quoted form of) the first output
    assert "first-output" in run.calls[1]


def test_loop_uses_check_command_for_completion():
    run = make_runner(["ran", "ran", "ran"])
    # "done" once we're on the 2nd iteration
    state = {"n": 0}
    def check(cmd):
        state["n"] += 1
        return 0 if state["n"] >= 2 else 1
    cfg = FactoryConfig(command="c", max_iters=10, done_command="check")
    res = run_factory("p", cfg, run, check_cmd=check)
    assert res.stopped == STOP_DONE
    assert res.count == 2


def test_loop_aborts_when_reporter_returns_false():
    run = make_runner(["x"] * 10)
    cfg = FactoryConfig(command="c", max_iters=10)
    res = run_factory("p", cfg, run, report_step=lambda *a: False)
    assert res.stopped == STOP_ABORTED
    assert run.calls == []      # killed before the first run


def test_loop_soft_fails_when_runner_raises():
    def boom(cmd):
        raise RuntimeError("agent crashed")
    cfg = FactoryConfig(command="c", max_iters=5)
    res = run_factory("p", cfg, boom)
    assert res.stopped == STOP_ERROR
    assert res.count == 1


def test_result_as_dict_serialisable():
    import json
    run = make_runner(["DONE"])
    cfg = FactoryConfig(command="c", max_iters=1, done_marker="DONE")
    json.dumps(run_factory("p", cfg, run).as_dict())


# ── novelty / stall detection ─────────────────────────────────────────────────
def test_novelty_first_iteration_is_fully_novel():
    assert output_novelty("", "anything") == 1.0


def test_novelty_identical_output_is_zero():
    assert output_novelty("same tail", "same tail") == 0.0


def test_novelty_partial_change_between_zero_and_one():
    n = output_novelty("hello world foo", "hello world bar")
    assert 0.0 < n < 1.0


def test_detect_stall_disabled_when_window_zero():
    assert detect_stall([0.0, 0.0, 0.0], window=0, threshold=0.1) is False


def test_detect_stall_needs_full_window():
    # only two low-novelty samples but a window of 3 -> not yet stalled
    assert detect_stall([0.0, 0.0], window=3, threshold=0.1) is False


def test_detect_stall_fires_on_repeat_run():
    assert detect_stall([0.5, 0.0, 0.05, 0.0], window=3, threshold=0.1) is True


def test_loop_bails_out_of_a_spinning_agent():
    # agent prints the exact same thing forever; no marker, big budget
    run = make_runner(["stuck output"] * 20)
    cfg = FactoryConfig(command="c", max_iters=20, stall_window=3)
    result = run_factory("p", cfg, run)
    assert result.stopped == STOP_STALLED
    # 1st is novel, then 3 repeats trip the window -> stops at iter 4
    assert result.count == 4


def test_stall_off_by_default_runs_full_budget():
    run = make_runner(["stuck"] * 5)
    cfg = FactoryConfig(command="c", max_iters=5)   # stall_window defaults to 0
    assert run_factory("p", cfg, run).stopped == STOP_BUDGET


def test_coherence_fn_scores_each_iteration():
    run = make_runner(["DONE"])
    cfg = FactoryConfig(command="c", max_iters=1, done_marker="DONE")
    result = run_factory("p", cfg, run, coherence_fn=lambda out: 0.7)
    assert result.iterations[0].coherence == 0.7


def test_reporter_receives_iteration_for_live_hud():
    # the post-run step call carries the Iteration so the harness can stash
    # coherence/novelty on the task for the live HUD glyph
    seen = []

    def report(n, total, exit_code, tail, it=None):
        if it is not None:
            seen.append((it.n, it.novelty, it.coherence))
        return True

    run = make_runner(["DONE"])
    cfg = FactoryConfig(command="c", max_iters=1, done_marker="DONE")
    run_factory("p", cfg, run, report_step=report, coherence_fn=lambda o: 0.4)
    assert seen == [(1, 1.0, 0.4)]     # first iter: fully novel, scored 0.4


def test_coherence_fn_failure_is_swallowed():
    def boom(_out):
        raise RuntimeError("physis down")
    run = make_runner(["DONE"])
    cfg = FactoryConfig(command="c", max_iters=1, done_marker="DONE")
    result = run_factory("p", cfg, run, coherence_fn=boom)
    assert result.iterations[0].coherence == 0.0   # neutral, loop unaffected


# ── coherence as a control input ────────────────────────────────────────────
# Novelty catches a loop repeating itself. It cannot catch one that keeps
# producing fresh output about the wrong thing — drift looks maximally novel
# right up to the budget. The brain already scored every round for the HUD;
# these tests are about the wiring that lets those scores end a run.

def test_detect_incoherence_is_off_when_the_window_is_zero():
    assert factory.detect_incoherence([-1.0, -1.0, -1.0], window=0, floor=-0.2) is False


def test_detect_incoherence_needs_a_full_window():
    assert factory.detect_incoherence([-0.9, -0.9], window=3, floor=-0.2) is False


def test_detect_incoherence_fires_on_sustained_drift():
    assert factory.detect_incoherence([0.8, -0.4, -0.5, -0.9],
                                      window=3, floor=-0.2) is True


def test_one_good_round_inside_the_window_keeps_the_loop_alive():
    assert factory.detect_incoherence([-0.9, 0.5, -0.9], window=3, floor=-0.2) is False


def test_a_dead_brain_never_stops_a_loop():
    """`physis.score_text` returns 0.0 when the brain is down, when classify
    comes back degraded and when the output is empty. Treating that as "bad"
    would mean an unreachable brain kills every run — the exact inversion of
    the fail-soft the client was written for."""
    assert factory.detect_incoherence([0.0, 0.0, 0.0, 0.0],
                                      window=3, floor=0.0) is False


def test_a_decision_is_never_made_on_a_round_nobody_scored():
    # Three bad readings, then a round with no reading at all. The bad news is
    # stale; wait for a score rather than act on the last one that existed.
    assert factory.detect_incoherence([-0.9, -0.9, -0.9, 0.0],
                                      window=3, floor=-0.2) is False


def test_readings_are_counted_even_when_some_rounds_were_not_scored():
    # A brain that answers intermittently still has an opinion, and it is the
    # same opinion three times.
    assert factory.detect_incoherence([-0.9, 0.0, -0.9, 0.0, -0.9],
                                      window=3, floor=-0.2) is True


def test_a_score_exactly_at_the_floor_counts_as_incoherent():
    assert factory.detect_incoherence([-0.2, -0.2, -0.2], window=3, floor=-0.2) is True


def test_the_loop_stops_when_the_brain_stops_recognising_the_work():
    run = make_runner([f"fresh nonsense {i}" for i in range(20)])
    cfg = FactoryConfig(command="c", max_iters=20, coherence_window=3)
    result = run_factory("p", cfg, run, coherence_fn=lambda out: -0.8)
    assert result.stopped == factory.STOP_INCOHERENT
    assert result.count == 3, "stopped as soon as the window was full"


def test_coherence_control_is_off_by_default():
    """An upgrade must not start ending runs that used to finish."""
    run = make_runner([f"fresh nonsense {i}" for i in range(5)])
    cfg = FactoryConfig(command="c", max_iters=5)
    result = run_factory("p", cfg, run, coherence_fn=lambda out: -1.0)
    assert result.stopped == STOP_BUDGET


def test_a_scorer_that_raises_does_not_end_the_run():
    def boom(_out):
        raise RuntimeError("physis down")
    run = make_runner(["out"] * 4)
    cfg = FactoryConfig(command="c", max_iters=4, coherence_window=2,
                        coherence_floor=0.0)
    assert run_factory("p", cfg, run, coherence_fn=boom).stopped == STOP_BUDGET


def test_finishing_the_work_beats_a_bad_score():
    run = make_runner(["ALL DONE"])
    cfg = FactoryConfig(command="c", max_iters=5, done_marker="ALL DONE",
                        coherence_window=1)
    result = run_factory("p", cfg, run, coherence_fn=lambda out: -1.0)
    assert result.stopped == STOP_DONE


def test_a_spinning_loop_is_reported_as_stalled_not_incoherent():
    """Both guards fire on the same round: novelty is measured on the text in
    hand, coherence is a remote model's opinion. The certain one is the better
    thing to have in the log."""
    run = make_runner(["same"] * 10)
    # Both windows are full on iteration 3: novelty is [1.0, 0.0, 0.0] (the
    # first round is novel by definition) and every round scored -0.9.
    cfg = FactoryConfig(command="c", max_iters=10, stall_window=2,
                        coherence_window=3)
    result = run_factory("p", cfg, run, coherence_fn=lambda out: -0.9)
    assert result.stopped == STOP_STALLED
