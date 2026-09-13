"""Tests for fleetplace.py — cockpit mirror of delegate.sh scoring."""
import json
import os
import socket

import pytest

from aion.fleetplace import (Candidate, delegate_dry_run, load_capability,
                             parse_probe_line, pick, pick_with_reasons,
                             satisfies, score)

DELEGATE = os.path.expanduser("~/dev/randomesh/scripts/fleet/delegate.sh")

CAPS_GPU = {"reachable": True, "cores": 16, "mem_avail_mb": 8735,
            "mem_total_mb": 15000, "disks": {"/": 90000},
            "gpu_vram_mb": 8176, "vulkan": "yes", "kfd": "yes",
            "has_cargo": "yes", "llama_server": "/home/x/llama-server"}
CAPS_PLAIN = {"reachable": True, "cores": 12, "mem_avail_mb": 4168,
              "mem_total_mb": 15000, "disks": {"/": 40000},
              "gpu_vram_mb": 0, "has_cargo": "yes",
              "llama_server": "none"}


def test_parse_probe_line():
    c = parse_probe_line("pansa-ts 12 1.64 73 100 110")
    assert (c.name, c.cores, c.load, c.fre, c.gpu, c.tasks) == \
        ("pansa-ts", 12, 1.64, 73, 100, 110)
    assert c.fre_mb is None  # legacy 6-field probe: no MB figure
    c = parse_probe_line("pansa-ts 12 1.64 73 11530 100 110")
    assert c.fre_mb == 11530
    assert parse_probe_line("h down 0 0 0 0 0 0") is None
    assert parse_probe_line("h down 0 0 0 0 0") is None
    assert parse_probe_line("garbage") is None
    assert parse_probe_line("h a b c d e") is None


def test_score_matches_delegate_formula():
    # 0.6*(73/100) + 0.3*(1-1.64/12) + 0.1*(1-110/200) = 0.438+0.259+0.045
    assert abs(score(Candidate("h", 12, 1.64, 73, fre_mb=11530, gpu=100, tasks=110)) - 0.742) < 1e-3
    # overloaded box clamps idle at 0, never negative
    assert score(Candidate("h", 4, 16.0, 50, gpu=0, tasks=0)) == pytest.approx(0.3 + 0.1)
    # zero cores (garbage probe) scores RAM only, never divides
    assert score(Candidate("h", 0, 0, 80, gpu=0, tasks=0)) == pytest.approx(0.48 + 0.1)


def test_pick_skips_and_tie_order():
    a = Candidate("a", 4, 0.5, 90, gpu=0, tasks=10)
    b = Candidate("b", 4, 0.5, 90, gpu=0, tasks=10)
    assert pick([a, b]).name == "a"  # ties keep the first
    assert pick([a, b], need_gpu=True) is None  # neither has a GPU
    g = Candidate("g", 4, 3.9, 90, gpu=50, tasks=10)
    assert pick([a, g], need_gpu=True).name == "g"
    low = Candidate("low", 16, 0.1, 5, 512, 0, 1)
    assert pick([low, a], min_mem_mb=2048).name == "a"  # 512MB < 2048MB
    assert pick([low], min_mem_mb=2048) is None
    rich = Candidate("rich", 2, 1.9, 90, 8192, 0, 190)
    assert pick([low, rich], min_mem_mb=2048).name == "rich"
    legacy = Candidate("legacy", 16, 0.1, 5, None, 0, 1)
    assert pick([legacy], min_mem_mb=2048).name == "legacy"  # ungateable
    assert pick([]) is None


def test_satisfies_matrix():
    assert satisfies(CAPS_GPU, "gpu")
    assert satisfies(CAPS_GPU, "ram:8000")
    assert not satisfies(CAPS_GPU, "ram:99999")
    assert satisfies(CAPS_GPU, "disk:50000")
    assert not satisfies(CAPS_GPU, "disk:999999")
    assert satisfies(CAPS_GPU, "tool:cargo")
    assert satisfies(CAPS_GPU, "tool:llama")
    assert not satisfies(CAPS_GPU, "tool:definitely-not-here")
    assert not satisfies(CAPS_PLAIN, "gpu")       # dead card: 0 VRAM
    assert satisfies(CAPS_PLAIN, "ram:4000")
    assert not satisfies({}, "tool:cargo")        # no data: fail closed
    assert satisfies({}, "anything-else")         # unknown kinds pass open


def test_pick_with_reasons_and_caps():
    g = Candidate("g", 16, 0.14, 57, fre_mb=8735, gpu=0, tasks=88,
                  caps=CAPS_GPU)
    p = Candidate("p", 12, 1.0, 70, fre_mb=4168, gpu=0, tasks=50,
                  caps=CAPS_PLAIN)
    best, rej = pick_with_reasons([p, g], needs=["tool:cargo"])
    assert best.name == "p" and rej == {}  # both qualify: load decides
    best, rej = pick_with_reasons([p, g], needs=["tool:cargo", "gpu"])
    assert best.name == "g" and rej == {"p": ["gpu"]}
    best, rej = pick_with_reasons(
        [p, g], needs=["tool:definitely-not-here"])
    assert best is None
    assert rej == {"p": ["tool:definitely-not-here"],
                   "g": ["tool:definitely-not-here"]}
    # legacy rows without caps: gpu falls back to the probe flag, tool: fails
    leg = Candidate("leg", 4, 0.5, 90, gpu=50, tasks=10)
    best, rej = pick_with_reasons([leg], needs=["gpu"])
    assert best.name == "leg"
    best, rej = pick_with_reasons([leg], needs=["tool:cargo"])
    assert best is None and rej == {"leg": ["tool:cargo"]}


def test_load_capability(tmp_path):
    p = tmp_path / "cap.json"
    p.write_text(json.dumps({"nodes": {"omo-ts": CAPS_GPU}}))
    assert load_capability(p)["omo-ts"]["has_cargo"] == "yes"
    assert load_capability(tmp_path / "missing.json") == {}
    (tmp_path / "broken.json").write_text("{oops")
    assert load_capability(tmp_path / "broken.json") == {}


def test_delegate_dry_run_parity_with_real_script():
    """Cockpit pick() and the REAL delegate.sh agree on controlled inputs.

    Live (ssh probes) — runs only with FLEET_LIVE=1. The suite contract is
    no network from tests (conftest), so this stays opt-in.
    """
    if not os.path.exists(DELEGATE):
        pytest.skip("delegate.sh not present")
    if os.environ.get("FLEET_LIVE") != "1":
        pytest.skip("needs FLEET_LIVE=1 (ssh probes)")
    me = socket.gethostname() + "-ts"
    chosen = delegate_dry_run(["no-such-host-ts", me], "echo hi")
    assert chosen == me  # unreachable skipped, only reachable chosen
    assert delegate_dry_run(["no-such-host-ts"]) is None  # none reachable
    # the MB gate agrees too: impossible floor gates even the reachable host
    assert delegate_dry_run([me], "echo hi", min_mem_mb=99999999) is None
    # --needs agrees too: unsatisfiable need rejects everywhere, both sides
    assert delegate_dry_run([me], "echo hi",
                            needs=["tool:definitely-not-here"]) is None
    # same inputs through the mirror: down candidate parses to None,
    # so pick() over the survivors must agree with the script
    assert pick([Candidate(me, 12, 1.0, 70, gpu=0, tasks=50)]).name == me
