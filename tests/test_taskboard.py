"""AION-002: TaskBoard engine over golden fixtures + live dirs.

Gate: python -m pytest tests/test_taskboard.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from aion.fleet import read_task_queue  # noqa: E402
from aion.ui.task_board import (  # noqa: E402
    MIN_POLL_S,
    POLL_INTERVAL_S,
    TaskBoard,
    render_task_board,
)

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "tasks")
THEME = {"dim": "#9aabbb", "accent": "#5ad1ff", "ok": "#7CFFB2",
         "warn": "#FFD479", "err": "#FF8A8A", "faint": "#6b7d8d"}


def test_golden_fixtures_group_and_sort():
    rows = read_task_queue(FIX)
    assert len(rows) == 3
    b = TaskBoard.from_dicts(rows)
    assert b.rows[0].task.id == "t2"  # lost first
    assert b.rows[0].task.loud
    by = b.by_node()
    assert set(by) == {"feather", "pansa"}
    assert len(by["feather"]) == 2


def test_running_only_filter():
    b = TaskBoard.from_dicts(read_task_queue(FIX))
    assert len(b.running_only().rows) == 1  # t3 queued
    assert "1 active" in b.summary()
    assert "1 need attention" in b.summary()


def test_age_labels_never_crash():
    b = TaskBoard.from_dicts([{"id": "x", "submitted_at": "garbage"}])
    assert b.rows[0].age == "?"
    b2 = TaskBoard.from_dicts([{"id": "y"}])
    assert b2.rows[0].age == "?"


def test_render_loud_and_grouped():
    b = TaskBoard.from_dicts(read_task_queue(FIX))
    out = render_task_board(b, THEME)
    assert "LOST" in out and "pansa" in out and "feather" in out
    assert render_task_board(TaskBoard(), THEME).count("queue empty") == 1


def test_poll_floor():
    assert POLL_INTERVAL_S >= MIN_POLL_S >= 15.0


def test_live_dirs_if_present():
    from aion.fleet import default_task_state_dir
    d = default_task_state_dir()
    if not d.is_dir():
        pytest.skip("no live task dirs on this host")
    b = TaskBoard.from_dicts(read_task_queue(d))
    assert "tasks" in b.summary()
    assert isinstance(render_task_board(b, THEME), str)


@pytest.mark.slow
def test_queue_roundtrip_if_mesh_present():
    import shutil
    import subprocess
    mesh = shutil.which("mesh") or os.path.expanduser("~/dev/randomesh/bin/mesh")
    import pathlib
    if not pathlib.Path(mesh).exists():
        pytest.skip("randomesh absent")
    probe = subprocess.run([mesh, "task", "submit", "echo board-probe",
                              "--engine", "shell", "--title", "board-probe"],
                             capture_output=True, text=True, timeout=50)
    if probe.returncode != 0:
        pytest.skip(f"mesh queue unusable here: {(probe.stderr or '')[-200:]}")
    r = subprocess.run([mesh, "task", "submit", "echo board-probe",
                        "--engine", "shell", "--title", "board-probe",
                        "--run"],
                       capture_output=True, text=True, timeout=50)
    assert r.returncode == 0, r.stderr[-500:]
