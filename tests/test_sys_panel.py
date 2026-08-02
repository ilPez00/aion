"""The Iron Man HUD panel.

This was 165 lines inside `AiOSApp`, reachable only by constructing a Textual
app, so none of it was tested. What it renders is a table of cases — system,
thermal, health and physis stats each arrive from a different source and each
can be missing, partial or degraded on its own — and the panel redraws on a
timer, so one exception here takes the cockpit down rather than one panel.

So the cases that matter are mostly absences.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aion.ui.sys_panel import render_sys, temp_colour  # noqa: E402

THEME = {"accent": "#7cf", "ok": "#7f7", "warn": "#fc7", "err": "#f77",
         "dim": "#889"}


def full_system() -> dict:
    return {
        "ok": True,
        "cpu": {"total_pct": 42, "cores": 8, "load1": 1.2,
                "per_core_pct": [10, 20, 30, 40, 50, 60, 70, 80]},
        "mem": {"pct": 63, "used": 8 * 1024**3, "total": 16 * 1024**3},
        "disks": [{"mount": "/", "pct": 71, "free": 120 * 1024**3}],
        "net": {"up_bps": 1024, "down_bps": 4096, "conns": 12},
    }


# ── absences ─────────────────────────────────────────────────────────────────
def test_no_stats_at_all_still_renders():
    """Cold start: the tick fires before any collector has run."""
    out = render_sys({}, THEME)
    assert "COMPUTER" in out and "stats unavailable" in out


def test_none_stats_is_not_a_crash():
    assert render_sys(None, THEME)


def test_system_present_but_not_ok_is_treated_as_absent():
    """sysinfo reports ok=False rather than raising when psutil is missing."""
    assert "stats unavailable" in render_sys({"system": {"ok": False}}, THEME)


def test_missing_health_says_where_health_comes_from():
    out = render_sys({"system": full_system()}, THEME)
    assert "REAL LIFE" in out and "google/apple/json" in out


def test_absent_optional_sections_are_omitted_not_empty():
    """A gap is fine; an empty THERMAL box implies a sensor read zero."""
    out = render_sys({"system": full_system()}, THEME)
    assert "THERMAL" not in out and "PHYSIS" not in out


def test_thermal_with_no_readings_is_omitted():
    sysd = full_system() | {"thermal": {"cpu": [], "other": []}}
    assert "THERMAL" not in render_sys({"system": sysd}, THEME)


# ── the parts that are present ───────────────────────────────────────────────
def test_a_healthy_system_renders_every_core_block():
    out = render_sys({"system": full_system()}, THEME)
    for title in ("COMPUTER", "STORAGE", "NETWORK", "SPECTRUM"):
        assert title in out, title


def test_no_disks_means_no_storage_block():
    sysd = full_system() | {"disks": []}
    out = render_sys({"system": sysd}, THEME)
    assert "STORAGE" not in out and "COMPUTER" in out


def test_gpu_reports_utilisation_when_it_has_it():
    sysd = full_system() | {"gpu": {"gpu_util_pct": 80, "gpu_mem_mb": 4000,
                                    "gpu_mem_total_mb": 8000}}
    out = render_sys({"system": sysd}, THEME)
    assert "GPU" in out and "4000/8000 MB" in out


def test_gpu_falls_back_to_loaded_models():
    """An API-backed or ollama box has no utilisation figure, only what it is
    holding."""
    sysd = full_system() | {"gpu": {"gpu_models": 2, "gpu_vram_mb": 6000}}
    out = render_sys({"system": sysd}, THEME)
    assert "2 model(s) loaded" in out


def test_health_renders_latest_and_the_seven_day_average():
    out = render_sys({"health": {"ok": True,
                                 "latest": {"steps": 8000, "heart_rate": 61,
                                            "sleep_hours": 7.5,
                                            "active_calories": 430},
                                 "avg_7d": {"steps": 7200, "sleep_hours": 7.1},
                                 "series": {"steps": [1, 2, 3, 4]}}}, THEME)
    assert "8000" in out and "7200" in out


def test_a_degraded_physis_says_so_rather_than_looking_live():
    out = render_sys({"physis": {"degraded": True, "kind": "hash"}}, THEME)
    assert "DEGRADED" in out and "LIVE" not in out


def test_physis_holarchy_is_summarised_then_truncated():
    nodes = [{"label": f"node-{i}"} for i in range(9)]
    out = render_sys({"physis": {"kind": "st", "graph": {"nodes": nodes,
                                                         "edges": [1, 2]}}}, THEME)
    assert "9 nodes · 2 edges" in out
    assert "node-4" in out and "node-5" not in out


def test_a_graph_that_is_not_a_dict_does_not_crash():
    """`physis` comes off the wire from a separate service."""
    assert render_sys({"physis": {"graph": "unavailable"}}, THEME)


# ── thermal thresholds ───────────────────────────────────────────────────────
@pytest.mark.parametrize("celsius,key", [(30, "ok"), (75, "warn"), (95, "err")])
def test_temperature_colour_has_three_bands_not_two(celsius, key):
    """Warm is information; hot is a reason to stop what you are doing."""
    assert temp_colour(celsius, THEME) == THEME[key]


def test_thermal_shows_the_hottest_first_then_sensors():
    sysd = full_system() | {"thermal": {
        "cpu": [{"label": "core0", "current": 91.0},
                {"label": "core1", "current": 55.0}],
        "other": [{"label": "nvme", "current": 40.0}]}}
    out = render_sys({"system": sysd}, THEME)
    assert "THERMAL" in out and "91.0" in out and "nvme" in out


def test_thermal_caps_the_sensor_list():
    """A laptop can expose a dozen sensors; the panel has a fixed height."""
    sysd = full_system() | {"thermal": {
        "cpu": [{"label": f"core{i}", "current": 50.0 + i} for i in range(9)],
        "other": []}}
    out = render_sys({"system": sysd}, THEME)
    assert "core3" in out and "core4" not in out


# ── the animated parts ───────────────────────────────────────────────────────
def test_the_tick_changes_what_is_drawn():
    """Both the holo gauges and the spectrum animate off it. If the tick were
    ignored the HUD would look frozen while the machine was busy."""
    sysd = {"system": full_system()}
    assert render_sys(sysd, THEME, tick=0) != render_sys(sysd, THEME, tick=7)


def test_running_tasks_drive_the_spectrum_without_overflowing_it():
    out = render_sys({"system": full_system()}, THEME, running=99)
    assert "SPECTRUM" in out
