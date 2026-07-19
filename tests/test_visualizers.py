"""test_visualizers.py — unit tests for animated HUD visualizers.

All functions are pure (no UI, no I/O). Tests verify shape, content, and
animation across multiple tick values. No mocking needed.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.ui.visualizers import (
    spectrum_eq,
    holo_gauge,
    pulse_radar,
    neural_graph,
    task_wave,
    matrix_stream,
    pick_viz,
)


# ── spectrum_eq ──────────────────────────────────────────────────────────────
def test_spectrum_eq_renders():
    r = spectrum_eq([0.8, 0.5, 0.2], tick=0, height=4)
    assert isinstance(r, str)
    assert len(r.split("\n")) == 4 + 2  # 4 rows + labels + values
    assert "CPU" not in r  # no labels by default
    assert "%" in r  # value row


def test_spectrum_eq_with_labels():
    r = spectrum_eq([0.8, 0.5, 0.2], tick=0, height=4, labels=["A", "B", "C"])
    assert "A" in r
    assert "B" in r
    assert "80%" in r or " 80%" in r


def test_spectrum_eq_empty():
    r = spectrum_eq([], tick=0)
    assert "no data" in r


def test_spectrum_eq_animation_changes():
    r0 = spectrum_eq([0.5, 0.5], tick=0, height=3)
    r1 = spectrum_eq([0.5, 0.5], tick=5, height=3)
    # scan line moves, so output should differ
    assert r0 != r1


def test_spectrum_eq_clamps_values():
    r = spectrum_eq([-0.1, 1.5, 0.0, 1.0], tick=0, height=3)
    assert isinstance(r, str)
    assert len(r) > 0


# ── holo_gauge ───────────────────────────────────────────────────────────────
def test_holo_gauge_renders():
    r = holo_gauge(0.75, tick=0, label="CPU")
    assert "75%" in r
    assert "CPU" in r
    assert "╭" in r and "╰" in r  # box corners
    lines = r.split("\n")
    assert len(lines) == 3


def test_holo_gauge_at_zero():
    r = holo_gauge(0.0, tick=0, label="IDLE")
    assert "0%" in r
    assert "░" in r  # all empty


def test_holo_gauge_at_full():
    r = holo_gauge(1.0, tick=0, label="MAX")
    assert "100%" in r
    assert "▓" in r  # all filled


def test_holo_gauge_pulse_moves():
    r0 = holo_gauge(0.5, tick=0)
    r1 = holo_gauge(0.5, tick=5)
    # pulse position changes, so output differs
    assert r0 != r1


def test_holo_gauge_width():
    r = holo_gauge(0.5, tick=0, width=10)
    # width=10 should give 10 fillable positions
    lines = r.split("\n")
    assert len(lines) == 3


# ── pulse_radar ──────────────────────────────────────────────────────────────
def test_pulse_radar_renders():
    rings = [
        {"label": "TASKS", "value": 0.7, "items": ["a", "b", "c"]},
        {"label": "SESS", "value": 0.4, "items": ["x"]},
    ]
    r = pulse_radar(rings, tick=0)
    assert "TASKS" in r
    assert "SESS" in r
    assert "70%" in r or " 70%" in r
    assert "40%" in r or " 40%" in r


def test_pulse_radar_empty():
    r = pulse_radar([], tick=0)
    assert "idle" in r


def test_pulse_radar_animation():
    rings = [{"label": "T", "value": 0.5, "items": ["a", "b"]}]
    r0 = pulse_radar(rings, tick=0)
    r1 = pulse_radar(rings, tick=3)
    # pulsing dots should differ
    assert r0 != r1


# ── neural_graph ─────────────────────────────────────────────────────────────
def test_neural_graph_renders():
    nodes = [("demo", 0.9), ("shell", 0.3)]
    edges = [(0, 1)]
    r = neural_graph(nodes, edges, tick=0)
    assert "d" in r and "e" in r and "m" in r and "o" in r
    assert "s" in r and "h" in r and "l" in r
    assert isinstance(r, str)
    assert len(r) > 0


def test_neural_graph_empty():
    r = neural_graph([], [], tick=0)
    assert "no active" in r


def test_neural_graph_animation():
    nodes = [("a", 0.5), ("b", 0.5), ("c", 0.5)]
    edges = [(0, 1), (1, 2)]
    r0 = neural_graph(nodes, edges, tick=0)
    r1 = neural_graph(nodes, edges, tick=10)
    assert r0 != r1


# ── task_wave ────────────────────────────────────────────────────────────────
def test_task_wave_renders():
    history = [0.1, 0.3, 0.8, 0.5, 0.2, 0.7, 0.9, 0.4]
    r = task_wave(history, tick=0, label="TEST")
    assert "TEST" in r
    assert "samples" in r
    assert isinstance(r, str)
    assert len(r.split("\n")) == 5  # 4 wave rows + label


def test_task_wave_empty():
    r = task_wave([], tick=0)
    assert isinstance(r, str)
    assert len(r) > 0


def test_task_wave_flat():
    r = task_wave([0.5, 0.5, 0.5, 0.5], tick=0)
    assert isinstance(r, str)
    assert len(r) > 0


def test_task_wave_animation():
    h = [0.1, 0.5, 0.9]
    r0 = task_wave(h, tick=0)
    r1 = task_wave(h, tick=3)
    # scanning dot moves
    assert r0 != r1


# ── matrix_stream ────────────────────────────────────────────────────────────
def test_matrix_stream_renders():
    columns = [
        {"label": "CPU", "value": 0.8, "stream": list("10101010")},
        {"label": "RAM", "value": 0.5, "stream": list("01010101")},
    ]
    r = matrix_stream(columns, tick=0)
    assert "C" in r and "P" in r and "U" in r
    assert "R" in r and "A" in r and "M" in r
    assert isinstance(r, str)
    assert len(r) > 0


def test_matrix_stream_empty():
    r = matrix_stream([], tick=0)
    assert "idle" in r


def test_matrix_stream_animation():
    cols = [{"label": "X", "value": 0.5, "stream": list("01")}]
    r0 = matrix_stream(cols, tick=0)
    r1 = matrix_stream(cols, tick=5)
    assert r0 != r1


# ── pick_viz ─────────────────────────────────────────────────────────────────
def test_pick_viz_spectrum():
    r = pick_viz("spectrum", {"values": [0.5, 0.5]}, 0)
    assert "%" in r


def test_pick_viz_gauge():
    r = pick_viz("gauge", {"pct": 0.5, "label": "TEST"}, 0)
    assert "TEST" in r
    assert "50%" in r


def test_pick_viz_radar():
    r = pick_viz("radar", {"rings": [{"label": "X", "value": 0.5, "items": []}]}, 0)
    assert "X" in r


def test_pick_viz_graph():
    r = pick_viz("graph", {"nodes": [("a", 0.5)], "edges": []}, 0)
    assert "a" in r or "no active" in r


def test_pick_viz_wave():
    r = pick_viz("wave", {"history": [0.1, 0.5, 0.9]}, 0)
    assert isinstance(r, str)
    assert len(r) > 0


def test_pick_viz_matrix():
    r = pick_viz("matrix", {"columns": [{"label": "X", "value": 0.5, "stream": ["0"]}]}, 0)
    assert "X" in r


def test_pick_viz_unknown():
    r = pick_viz("nope", {}, 0)
    assert "unknown" in r
