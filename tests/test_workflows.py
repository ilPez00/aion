"""test_workflows.py — unit tests for the Workflow Pulse data layer.

Covers every collector source and every render helper. Pure data, no UI, no I/O.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.workflows import (
    WorkflowRow, WorkflowAgent,
    collect_workflows, stage_glyph, stage_theme_key,
    _task_stage, _swarm_stage, _pipeline_for_swarm, _aggregate_swarm_stage,
    pipeline_strip, mission_strip, workflow_pulse,
    swarm_dag, board_glance, desktop_missions,
    rank_done, _normalize_swarm,
)


def _task_factory(task_id="t1", label="test", harness="demo",
                  state="running", progress=0.5,
                  paused=False, created=None):
    """Build a minimal task-like object."""
    _label = label
    _harness = harness
    _progress = progress
    _paused = paused
    _created = created or time.time()
    _state = state
    class FakeTask:
        id = task_id
        label = _label
        harness = _harness
        progress = _progress
        paused = _paused
        created = _created
        class State:
            value = _state
        state = State()
    return FakeTask()


def _dict_task(task_id="t1", label="test", harness="demo",
               state="running", progress=0.5, paused=False):
    return {
        "id": task_id, "label": label, "harness": harness,
        "state": state, "progress": progress, "paused": paused,
        "created": time.time(),
    }


# ── stage helpers ─────────────────────────────────────────────────────────

def test_task_stage():
    assert _task_stage("pending") == "plan"
    assert _task_stage("running") == "act"
    assert _task_stage("done") == "done"
    assert _task_stage("failed") == "failed"
    assert _task_stage("cancelled") == "done"
    assert _task_stage("interrupted") == "blocked"
    assert _task_stage("unknown") == "plan"


def test_swarm_stage():
    assert _swarm_stage("idle") == "plan"
    assert _swarm_stage("planning") == "plan"
    assert _swarm_stage("working") == "act"
    assert _swarm_stage("waiting") == "wait"
    assert _swarm_stage("blocked") == "blocked"
    assert _swarm_stage("done") == "done"
    assert _swarm_stage("failed") == "failed"
    assert _swarm_stage("unknown") == "plan"


def test_stage_glyph():
    assert stage_glyph("plan") == "◇"
    assert stage_glyph("act") == "●"
    assert stage_glyph("failed") == "✗"
    assert stage_glyph("unknown") == "·"


def test_stage_theme_key():
    assert stage_theme_key("plan") == "dim"
    assert stage_theme_key("act") == "warn"
    assert stage_theme_key("blocked") == "err"
    assert stage_theme_key("done") == "ok"


# ── pipeline helpers ──────────────────────────────────────────────────────

def test_pipeline_for_swarm_empty():
    assert _pipeline_for_swarm([]) == ["plan"]


def test_pipeline_for_swarm_all_done():
    agents = [{"status": "done"}, {"status": "done"}]
    pipe = _pipeline_for_swarm(agents)
    assert "done" in pipe


def test_pipeline_for_swarm_mixed():
    agents = [{"status": "working"}, {"status": "done"}, {"status": "idle"}]
    pipe = _pipeline_for_swarm(agents)
    assert "plan" in pipe
    assert "act" in pipe
    assert "done" in pipe


def test_pipeline_for_swarm_failed():
    agents = [{"status": "working"}, {"status": "failed"}]
    pipe = _pipeline_for_swarm(agents)
    assert "failed" in pipe
    assert "act" in pipe


def test_aggregate_swarm_stage_empty():
    stage, prog, blocked = _aggregate_swarm_stage([])
    assert stage == "plan"
    assert prog == 0.0
    assert blocked is None


def test_aggregate_swarm_stage_worst_wins():
    agents = [
        {"status": "working", "progress": 0.8},
        {"status": "failed", "progress": 0.3},
    ]
    stage, prog, blocked = _aggregate_swarm_stage(agents)
    assert stage == "failed"
    assert 0.5 < prog < 0.6  # mean


def test_aggregate_swarm_stage_blocked_with_deps():
    agents = [
        {"status": "blocked", "deps": ["Agent-A"]},
        {"status": "done"},
    ]
    _, _, blocked = _aggregate_swarm_stage(agents)
    assert blocked == "Agent-A"


# ── normalize swarm ───────────────────────────────────────────────────────

def test_normalize_swarm_empty():
    out = _normalize_swarm(None, None)
    assert out["agents"] == []
    assert out["total"] == 0


def test_normalize_swarm_from_dict():
    out = _normalize_swarm({"agents": [{"name": "A"}], "working": 1,
                             "total": 3}, None)
    assert out["working"] == 1
    assert out["total"] == 3
    assert len(out["agents"]) == 1


def test_normalize_swarm_orchestrator_wins():
    agents_dict = [{"name": "A", "status": "working"}]
    agents_obj = [{"name": "B", "status": "idle"}]
    out = _normalize_swarm({"agents": agents_dict}, agents_obj)
    assert out["agents"] == agents_obj  # fresh list wins


# ── collect_workflows ────────────────────────────────────────────────────

def test_collect_empty():
    rows = collect_workflows()
    assert rows == []


def test_collect_tasks():
    rows = collect_workflows(tasks=[_task_factory()], now=1000.0)
    assert len(rows) == 1
    assert rows[0].kind == "task"
    assert rows[0].stage == "act"


def test_collect_tasks_failed():
    t = _task_factory(state="failed", progress=0.3)
    rows = collect_workflows(tasks=[t], now=1000.0)
    assert len(rows) == 1
    assert rows[0].stage == "failed"


def test_collect_tasks_paused():
    t = _task_factory(state="running", paused=True)
    rows = collect_workflows(tasks=[t], now=1000.0)
    assert len(rows) == 1
    assert rows[0].stage == "wait"


def test_collect_skips_idle_tasks():
    t = _task_factory(state="done")
    rows = collect_workflows(tasks=[t], now=1000.0)
    assert rows == []


def test_collect_dict_tasks():
    t = _dict_task(state="running")
    rows = collect_workflows(tasks=[t], now=1000.0)
    assert len(rows) == 1


def test_collect_swarm_live():
    agents = [{"name": "A1", "status": "working", "progress": 0.5}]
    rows = collect_workflows(swarm_agents=agents, now=1000.0)
    assert len(rows) == 1
    assert rows[0].kind == "swarm"
    assert rows[0].stage == "act"


def test_collect_swarm_all_idle_omitted():
    agents = [{"name": "A1", "status": "idle", "progress": 0.0}]
    rows = collect_workflows(swarm_agents=agents, now=1000.0)
    # fully idle swarm with no plan should be omitted
    assert rows == []


def test_collect_swarm_blocked():
    agents = [
        {"name": "A1", "status": "blocked", "progress": 0.5,
         "deps": ["A2"]},
        {"name": "A2", "status": "waiting", "progress": 0.0},
    ]
    rows = collect_workflows(swarm_agents=agents, now=1000.0)
    assert len(rows) == 1
    assert rows[0].stage == "blocked"
    assert rows[0].blocked_by == "A2"


def test_collect_boards():
    boards = [{"id": "b1", "title": "Research",
               "column_data": {"backlog": [{"title": "read paper"}],
                               "active": [{"title": "implement"}],
                               "done": [{"title": "done"}]}}]
    rows = collect_workflows(boards=boards, now=1000.0)
    assert len(rows) == 1
    assert rows[0].kind == "board"
    assert rows[0].stage == "act"
    assert rows[0].counts["backlog"] == 1
    assert rows[0].counts["active"] == 1


def test_collect_boards_done_only_omitted():
    boards = [{"id": "b1", "title": "Done Board",
               "column_data": {"done": [{"title": "x"}]}}]
    rows = collect_workflows(boards=boards, now=1000.0)
    assert rows == []


def test_collect_hermes():
    agents = [{"id": "s1", "model": "gpt-4", "branch": "main",
               "age_s": 120, "msgs": 5}]
    rows = collect_workflows(hermes_agents=agents, now=1000.0)
    assert len(rows) == 1
    assert rows[0].kind == "hermes"


def test_collect_agent_entities():
    agents = [{"id": "a1", "name": "Alice", "status": "working",
               "task_status": "running", "goal": "research"}]
    rows = collect_workflows(agent_entities=agents, now=1000.0)
    assert len(rows) == 1
    assert rows[0].kind == "agent"
    assert rows[0].stage == "act"


def test_collect_agent_entities_idle_omitted():
    agents = [{"id": "a1", "name": "Bob", "status": "idle",
               "task_status": "idle"}]
    rows = collect_workflows(agent_entities=agents, now=1000.0)
    assert rows == []


def test_collect_priority_order():
    tasks = [
        _task_factory("t1", "A", state="running", progress=0.2),
        _task_factory("t2", "B", state="failed", progress=0.5),
        _task_factory("t3", "C", state="running", progress=0.8),
    ]
    rows = collect_workflows(tasks=tasks, now=1000.0)
    assert len(rows) == 3
    # failed first
    assert rows[0].id == "task:t2"
    # then running by progress desc
    assert rows[1].id == "task:t3"
    assert rows[2].id == "task:t1"


def test_collect_mixed_sources():
    tasks = [_task_factory(state="running")]
    swarm_agents = [{"name": "S1", "status": "working", "progress": 0.3}]
    rows = collect_workflows(tasks=tasks, swarm_agents=swarm_agents, now=1000.0)
    assert len(rows) >= 2


# ── render helpers ───────────────────────────────────────────────────────

THEME = {
    "accent": "#5ad1ff", "ok": "#7CFFB2", "warn": "#FFD479",
    "err": "#FF6B6B", "dim": "#5a6b7b",
}


def test_pipeline_strip_no_crash():
    r = pipeline_strip(["plan", "act", "done"], "act", THEME)
    assert isinstance(r, str)


def test_pipeline_strip_defaults():
    r = pipeline_strip([], "", THEME)
    assert isinstance(r, str)


def test_rank_done():
    assert rank_done("plan", "act") is True
    assert rank_done("act", "plan") is False
    assert rank_done("verify", "done") is True
    assert rank_done("act", "failed") is False


def test_mission_strip_idle():
    r = mission_strip([], THEME)
    assert "idle" in r or "no agentic" in r


def test_mission_strip_with_rows():
    rows = [
        WorkflowRow(id="s1", kind="swarm", title="research",
                     stage="act", progress=0.5),
    ]
    r = mission_strip(rows, THEME)
    assert "research" in r
    assert "MISSION" in r


def test_workflow_pulse_empty():
    r = workflow_pulse([], THEME)
    assert any("no agentic" in line for line in r)
    assert any("WORKFLOWS" in line for line in r)


def test_workflow_pulse_swarm():
    rows = [
        WorkflowRow(id="s1", kind="swarm", title="research me",
                     stage="act", progress=0.6,
                     agents=[WorkflowAgent("A1", "working", 0.6)],
                     counts={"working": 1, "waiting": 0, "done": 0,
                             "failed": 0, "blocked": 0, "total": 1},
                     pipeline=["plan", "act", "done"]),
    ]
    r = workflow_pulse(rows, THEME)
    assert any("swarm" in line for line in r)
    assert any("research" in line for line in r)


def test_workflow_pulse_board():
    rows = [
        WorkflowRow(id="board:b1", kind="board", title="Tasks",
                     stage="act", progress=0.3,
                     counts={"backlog": 2, "active": 1, "done": 3},
                     pipeline=["plan", "act", "done"]),
    ]
    r = workflow_pulse(rows, THEME)
    assert any("B2" in line for line in r)
    assert any("A1" in line for line in r)


def test_workflow_pulse_blocked_anomaly():
    rows = [
        WorkflowRow(id="s1", kind="swarm", title="stuck thing",
                     stage="blocked", progress=0.3,
                     blocked_by="Agent-X",
                     pipeline=["plan", "act", "blocked"]),
    ]
    r = workflow_pulse(rows, THEME)
    assert any("blocked" in line.lower() for line in r)


def test_workflow_pulse_failed_anomaly():
    rows = [
        WorkflowRow(id="t1", kind="task", title="broken build",
                     stage="failed", progress=0.8,
                     pipeline=["plan", "act", "failed"]),
    ]
    r = workflow_pulse(rows, THEME)
    assert any("failed" in line.lower() for line in r)


def test_desktop_missions_no_rows():
    r = desktop_missions([], THEME)
    assert "no agentic" in r


def test_desktop_missions_with_work():
    rows = [
        WorkflowRow(id="s1", kind="swarm", title="build RAG",
                     stage="act", progress=0.7),
    ]
    r = desktop_missions(rows, THEME)
    assert "build RAG" in r
    assert "MISSIONS" in r


def test_desktop_missions_blocked():
    rows = [
        WorkflowRow(id="s1", kind="swarm", title="pipeline",
                     stage="blocked", progress=0.4,
                     blocked_by="dep-A"),
    ]
    r = desktop_missions(rows, THEME)
    assert "dep-A" in r or "blocked" in r


# ── swarm_dag ─────────────────────────────────────────────────────────────

def test_swarm_dag_empty():
    r = swarm_dag([], THEME)
    assert "no agents" in r


def test_swarm_dag_no_deps():
    agents = [{"name": "A1", "status": "working", "progress": 0.5}]
    r = swarm_dag(agents, THEME)
    assert "A1" in r


def test_swarm_dag_with_deps():
    agents = [
        {"name": "A1", "status": "working", "progress": 0.8,
         "deps": ["A2"]},
        {"name": "A2", "status": "done", "progress": 1.0},
    ]
    r = swarm_dag(agents, THEME)
    assert "A1" in r
    assert "A2" in r


# ── board_glance ──────────────────────────────────────────────────────────

def test_board_glance_empty():
    r = board_glance([], THEME)
    assert "No boards" in r


def test_board_glance_one_board():
    boards = [{"title": "Research",
               "column_data": {"backlog": [{"title": "read"}],
                               "active": [],
                               "done": [{"title": "wrote"}]}}]
    r = board_glance(boards, THEME)
    assert "Research" in r
    assert "B1" in r
    assert "D1" in r


def test_board_glance_flat_cards():
    boards = [{"id": "b1", "title": "Dev",
               "cards": [{"title": "fix bug", "column": "backlog"},
                         {"title": "deploy", "column": "active"}]}]
    r = board_glance(boards, THEME)
    assert "Dev" in r


# ── WorkflowRow as_dict ───────────────────────────────────────────────────

def test_workflow_row_as_dict():
    w = WorkflowRow(id="t1", kind="task", title="test", stage="act",
                     progress=0.5, agents=[WorkflowAgent("a1")])
    d = w.as_dict()
    assert d["id"] == "t1"
    assert d["stage"] == "act"
    assert d["progress"] == 0.5
    assert d["agents"][0]["name"] == "a1"


def test_workflow_agent_as_dict():
    a = WorkflowAgent(name="bot", status="working", progress=0.7)
    d = a.as_dict()
    assert d["name"] == "bot"
    assert d["status"] == "working"
    assert d["progress"] == 0.7


def test_workflow_types_summary():
    from aion.workflows import workflow_types_summary
    rows = [
        WorkflowRow(id="t1", kind="task", title="build", stage="act"),
        WorkflowRow(id="t2", kind="task", title="test", stage="act"),
        WorkflowRow(id="s", kind="swarm", title="research", stage="act"),
    ]
    s = workflow_types_summary(rows)
    assert "2task" in s
    assert "1swarm" in s


def test_workflow_types_summary_idle():
    from aion.workflows import workflow_types_summary
    rows = [
        WorkflowRow(id="t1", kind="task", title="done", stage="done"),
    ]
    assert workflow_types_summary(rows) == "idle"


def test_workflow_types_summary_empty():
    from aion.workflows import workflow_types_summary
    assert workflow_types_summary([]) == "idle"


def test_collect_external_agents():
    ext = [{"name": "OpenCode", "pid": 12345, "age_s": 300}]
    rows = collect_workflows(external_agents=ext)
    assert len(rows) == 1
    assert rows[0].kind == "tool"
    assert rows[0].title == "OpenCode"
    assert rows[0].stage == "act"


def test_collect_external_agents_empty():
    rows = collect_workflows(external_agents=[])
    tools = [r for r in rows if r.kind == "tool"]
    assert len(tools) == 0
