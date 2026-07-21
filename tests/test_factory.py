"""Tests for the factory loop. Injectable runner — no subprocess, no agent."""
from __future__ import annotations

import pytest

from aion import factory
from aion.factory import (
    STOP_ABORTED, STOP_BUDGET, STOP_DONE, STOP_ERROR,
    FactoryConfig, render_command, run_factory,
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
