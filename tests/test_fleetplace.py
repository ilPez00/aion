"""Tests for fleetplace.py — cockpit mirror of delegate.sh scoring."""
import os
import socket

import pytest

from aion.fleetplace import (Candidate, delegate_dry_run, parse_probe_line,
                             pick, score)

DELEGATE = os.path.expanduser("~/dev/randomesh/scripts/fleet/delegate.sh")


def test_parse_probe_line():
    c = parse_probe_line("pansa-ts 12 1.64 73 100 110")
    assert (c.name, c.cores, c.load, c.fre, c.gpu, c.tasks) == \
        ("pansa-ts", 12, 1.64, 73, 100, 110)
    assert parse_probe_line("h down 0 0 0 0 0") is None
    assert parse_probe_line("garbage") is None
    assert parse_probe_line("h a b c d e") is None


def test_score_matches_delegate_formula():
    # 0.6*(73/100) + 0.3*(1-1.64/12) + 0.1*(1-110/200) = 0.438+0.259+0.045
    assert abs(score(Candidate("h", 12, 1.64, 73, 100, 110)) - 0.742) < 1e-3
    # overloaded box clamps idle at 0, never negative
    assert score(Candidate("h", 4, 16.0, 50, 0, 0)) == pytest.approx(0.3 + 0.1)
    # zero cores (garbage probe) scores RAM only, never divides
    assert score(Candidate("h", 0, 0, 80, 0, 0)) == pytest.approx(0.48 + 0.1)


def test_pick_skips_and_tie_order():
    a = Candidate("a", 4, 0.5, 90, 0, 10)
    b = Candidate("b", 4, 0.5, 90, 0, 10)
    assert pick([a, b]).name == "a"  # ties keep the first
    assert pick([a, b], need_gpu=True) is None  # neither has a GPU
    g = Candidate("g", 4, 3.9, 90, 50, 10)
    assert pick([a, g], need_gpu=True).name == "g"
    low = Candidate("low", 16, 0.1, 5, 0, 1)
    assert pick([low, a], min_mem_mb=2048).name == "a"  # fre<10 gated
    assert pick([low], min_mem_mb=2048) is None
    assert pick([]) is None


def test_delegate_dry_run_parity_with_real_script():
    """Cockpit pick() and the REAL delegate.sh agree on controlled inputs."""
    if not os.path.exists(DELEGATE):
        pytest.skip("delegate.sh not present")
    me = socket.gethostname() + "-ts"
    chosen = delegate_dry_run(["no-such-host-ts", me], "echo hi")
    assert chosen == me  # unreachable skipped, only reachable chosen
    assert delegate_dry_run(["no-such-host-ts"]) is None  # none reachable
    # same inputs through the mirror: down candidate parses to None,
    # so pick() over the survivors must agree with the script
    assert pick([Candidate(me, 12, 1.0, 70, 0, 50)]).name == me
