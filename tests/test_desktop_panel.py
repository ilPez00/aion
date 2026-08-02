"""The landing HUD panel.

First thing anyone sees, redrawn every tick, and until now testable only by
booting a Textual app — one test did exactly that and asserted two words were
present. Every widget section is conditional on data that may not exist yet,
so most of what matters here is what happens when a section has nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aion.ui.desktop_panel import load_colour, render_desktop  # noqa: E402

THEME = {"accent": "#7cf", "ok": "#7f7", "warn": "#fc7", "err": "#f77",
         "dim": "#889"}
WS = ["desktop", "models", "tasks", "runs", "agent", "vault"]


def render(data=None, **kw):
    return render_desktop(data or {}, THEME, workspaces=WS, **kw)


# ── the frame is always there ────────────────────────────────────────────────
def test_an_empty_desktop_still_has_a_status_bar_and_commands():
    """Cold start, nothing scanned. The hint line is the only thing telling a
    new user what to type, so it must survive having no data."""
    out = render()
    assert "STATUS" in out and "COMMANDS" in out and "Ctrl-K" in out


def test_none_data_is_not_a_crash():
    assert render_desktop(None, THEME, workspaces=WS)


def test_every_configured_workspace_appears_in_the_dock():
    out = render()
    for w in WS:
        assert w[:6] in out, w


def test_a_short_workspace_list_does_not_break_the_second_row():
    assert render_desktop({}, THEME, workspaces=["desktop", "tasks"])


def test_an_unknown_workspace_gets_a_placeholder_icon():
    out = render_desktop({}, THEME, workspaces=["quantum"])
    assert "quantum"[:6] in out


# ── widgets appear only when they have something to say ──────────────────────
def test_absent_sections_are_omitted_not_left_empty():
    """An empty PROJECTS heading claims the scan ran and found nothing."""
    out = render()
    for heading in ("PROJECTS", "TODOS", "SESSIONS"):
        assert heading not in out, heading


def test_projects_show_dirty_count_and_branch():
    out = render({"projects": [{"name": "aion", "dirty": 3, "branch": "main"}]})
    assert "aion ~3 @main" in out


def test_a_clean_project_carries_no_badges():
    out = render({"projects": [{"name": "aion", "dirty": 0, "branch": ""}]})
    assert "aion" in out and "~" not in out.split("PROJECTS")[1].split("\n")[1]


def test_a_project_without_a_name_falls_back_to_its_id():
    out = render({"projects": [{"id": "proj-7"}]})
    assert "proj-7" in out


def test_todos_distinguish_done_from_open():
    out = render({"todos": [{"text": "write it up", "done": False},
                            {"text": "ship it", "done": True}]})
    assert "○" in out and "✓" in out


def test_only_live_tasks_reach_the_sessions_widget():
    """`active_tasks` is everything the registry holds; a finished task in the
    live panel is noise at the exact moment attention is scarce."""
    out = render({"active_tasks": [
        {"state": "running", "harness": "demo", "label": "alpha",
         "progress": 0.5},
        {"state": "done", "harness": "demo", "label": "omega",
         "progress": 1.0}]})
    assert "alpha" in out and "omega" not in out


def test_a_paused_task_is_drawn_differently_from_a_running_one():
    live = {"state": "running", "harness": "demo", "label": "x", "progress": 0.5}
    assert "⏸" not in render({"active_tasks": [live]})
    assert "⏸" in render({"active_tasks": [live | {"paused": True}]})


def test_interrupted_work_sits_with_the_live_work():
    """It is the category most likely to need a decision and least likely to
    be gone looking for."""
    out = render({"interrupted_tasks": [{"harness": "claude", "label": "big job"}]})
    assert "SESSIONS" in out and "interrupted" in out and "big job" in out


def test_a_zombie_session_is_marked_as_such():
    out = render({"recent_sessions": [{"status": "zombie", "model": "opus",
                                       "repo": "aion"}]})
    assert "⊘" in out and "zombie" in out


def test_sessions_are_capped_so_the_panel_keeps_its_height():
    out = render({"recent_sessions": [
        {"status": "ended", "model": f"model-{i}", "repo": "r"}
        for i in range(9)]})
    assert "model-3" in out and "model-4" not in out


# ── load colours ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("pct,key", [(10, "ok"), (60, "warn"), (95, "err")])
def test_load_has_three_bands(pct, key):
    assert load_colour(pct, THEME) == THEME[key]


def test_the_status_bar_colours_each_metric_independently():
    """A busy disk on an idle CPU must not paint the whole bar red."""
    out = render({"cpu_pct": 5, "ram_pct": 5, "disk_pct": 95})
    assert THEME["err"] in out and THEME["ok"] in out


# ── missions vs radar ────────────────────────────────────────────────────────
def test_the_radar_is_the_fallback_when_no_workflow_is_live():
    out = render(stats={"system": {"cpu": {"total_pct": 40},
                                   "mem": {"pct": 20}}}, running=2)
    assert "VIZ" in out and "MISSIONS" not in out


def test_a_live_workflow_takes_the_slot_from_the_radar():
    workflows = [{"id": "w1", "name": "build", "steps": [],
                  "state": "running", "progress": 0.4}]
    out = render(workflows=workflows, workflows_live=True)
    assert "MISSIONS" in out and "VIZ" not in out


def test_a_workflow_that_is_not_live_does_not_take_the_slot():
    out = render(workflows=[{"id": "w1", "name": "build", "steps": []}],
                 workflows_live=False)
    assert "VIZ" in out


def test_the_radar_animates_off_the_tick():
    stats = {"system": {"cpu": {"total_pct": 40}, "mem": {"pct": 20}}}
    assert render(stats=stats, tick=0) != render(stats=stats, tick=6)


def test_missing_stats_do_not_stop_the_radar_drawing():
    assert "VIZ" in render(stats={})
