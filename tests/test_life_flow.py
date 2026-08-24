"""tests for the life-flow visualizer additions to visualizers.py."""
from __future__ import annotations

import re

from aion.ui import visualizers as viz
from aion.ui.visualizers import flow_pipeline


ANSI_MARKUP = re.compile(r"\[/?\w+(?:=#[0-9a-fA-F]{6})?\]")


def _strip(markup: str) -> str:
    return ANSI_MARKUP.sub("", markup)


def test_flow_pipeline_renders_one_column_per_domain():
    out = _strip(flow_pipeline(
        [("computer", 0.9), ("fitness", 0.4), ("social", 0.0), ("money", 1.0)],
        tick=0))
    for word in ("COMPUTER", "FITNESS", "SOCIAL", "MONEY"):
        assert word in out
    # zero score shows an explicit gap marker, not silence
    assert "○" in out


def test_flow_pipeline_height_respected():
    raw = flow_pipeline([("a", 0.5), ("b", 0.8)], tick=3, height=4)
    lines = [l for l in raw.splitlines() if l.strip()]
    assert len(lines) == 4


def test_flow_pipeline_empty_is_soft():
    out = _strip(flow_pipeline([], tick=0))
    assert "no data" in out.lower()


def test_flow_pipeline_pure_and_tick_stable_shape():
    a = flow_pipeline([("x", 0.7)], tick=1)
    b = flow_pipeline([("x", 0.7)], tick=2)
    assert len(a.splitlines()) == len(b.splitlines())


def test_flow_pipeline_registered_in_pick_viz():
    out = _strip(viz.pick_viz("flow", {"scores": [("m", 0.5)]}, tick=0))
    assert isinstance(out, str) and out


def test_life_panel_summary_lines():
    from aion.ui.life_panel import life_panel
    snap = {"domains": {
        "money": {"ok": True, "paid_total": 1800, "open_total": 900,
                  "target_mrr": 2500,
                  "entries": [{"note": "pilot", "amount": 1800}]},
        "fitness": {"ok": True, "steps": 5200, "step_goal": 8000},
        "social": {"ok": False, "reason": "praxis not configured"},
        "computer": {"ok": True, "cpu_pct": 12, "ram_pct": 44,
                     "tasks_running": 2},
    }}
    out = _strip(life_panel(snap, theme={"accent": "#00e5ff", "ok": "#22c55e",
                                         "warn": "#f59e0b", "err": "#ef4444",
                                         "dim": "#64748b", "fg": "#e2e8f0"},
                             tick=0))
    assert "€1.800" in out
    assert "praxis not configured" in out
    assert "LIFE FLOW" in out
