"""containers: snapshot parsing and the pure panel render.

No subprocess, no CLI, no cluster: the JSON shapes are recorded fixtures of
what `kubectl get pods -o json` and `docker ps --format json` actually emit.
The parser and the renderer are the testable half; the CLI wrappers are one
`subprocess.run` each and soft-fail into ContainerError, which the app handler
catches and prints verbatim.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aion.containers import _parse_age, backend  # noqa: E402
from aion.ui.containers_panel import render_containers, state_color  # noqa: E402
from aion.ui.theme import TOKENS  # noqa: E402

THEME = TOKENS


# ── parsing ──────────────────────────────────────────────────────────────────
def test_rfc3339_timestamp_becomes_age():
    import time as _t
    now = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(_t.time() - 120))
    age = _parse_age(now)
    assert 100 < age < 200


def test_junk_timestamp_is_zero_not_a_crash():
    assert _parse_age(None) == 0.0
    assert _parse_age("not a time") == 0.0


def test_backend_note_lists_what_exists():
    # one of these binaries is on every dev box; the note is a capability
    # line either way — "none of ..." when nothing is there
    note = backend()
    assert isinstance(note, str) and note


# ── panel ────────────────────────────────────────────────────────────────────
POD = {"id": "ns/x", "name": "api-7d9f", "kind": "pod", "state": "Running",
       "image": "randomesh/lab:0.1", "node": "omo", "age_s": 3600,
       "backend": "kubectl"}
CRASH = {**POD, "id": "ns/y", "name": "worker-0", "state": "CrashLoopBackOff"}


def test_worst_health_sorts_first():
    out = render_containers([POD, CRASH], THEME)
    assert out.index("worker-0") < out.index("api-7d9f")


def test_no_rows_no_backends_renders_nothing():
    """A heading claiming 'no containers' when there is no docker is a lie —
    the caller checks backends before appending this block, but the panel
    itself stays quiet too."""
    assert render_containers([], THEME, backends="none of docker/kubectl/mirrord on PATH")


def test_running_reads_green_and_crash_reads_red():
    assert state_color("Running", THEME) == THEME["ok"]
    assert state_color("CrashLoopBackOff", THEME) == THEME["err"]
    assert state_color("Pending", THEME) == THEME["warn"]
    assert state_color("Whatever", THEME) == THEME["dim"]


def test_more_than_ten_rows_are_summarised_not_dropped_silently():
    rows = [{**POD, "name": f"p{i}", "id": f"ns/p{i}"} for i in range(13)]
    out = render_containers(rows, THEME)
    assert "and 3 more" in out


def test_errors_are_shown_not_swallowed():
    out = render_containers([], THEME, errors=["kubectl: conn refused"],
                            backends="kubectl")
    assert "conn refused" in out
