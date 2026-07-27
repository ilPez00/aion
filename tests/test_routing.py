"""Cross-instance routing — which box gets the work, and why."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from aion.routing import (  # noqa: E402
    MAX_HEARTBEAT_AGE_S, Candidate, candidates_from_fleet, eligible, plan, score,
)


def cand(cid, **kw):
    base = dict(alive=True, local=True, running_count=0, age_s=1.0)
    base.update(kw)
    return Candidate(id=cid, **base)


# ── eligibility is not scoring ───────────────────────────────────────────
def test_a_dead_instance_is_not_eligible():
    ok, why = eligible(cand("a", alive=False))
    assert ok is False and why == "offline"


def test_a_wedged_instance_is_not_eligible():
    """Alive pid, stale heartbeat — it may be stuck, not idle."""
    ok, why = eligible(cand("a", age_s=MAX_HEARTBEAT_AGE_S + 5))
    assert ok is False and "heartbeat" in why


def test_an_instance_without_the_harness_is_not_eligible():
    ok, why = eligible(cand("a", harnesses=["demo"]), harness="factory")
    assert ok is False and "factory" in why


def test_an_instance_with_no_declared_harnesses_is_not_filtered_out():
    """Unknown capability is not the same as known-missing."""
    ok, _ = eligible(cand("a", harnesses=[]), harness="factory")
    assert ok is True


def test_ineligible_candidates_score_negative_infinity():
    assert score(cand("a", alive=False)).score == float("-inf")


def test_an_all_dead_fleet_routes_nowhere():
    """Never quietly pick 'the least dead option'."""
    p = plan([cand("a", alive=False), cand("b", alive=False)])
    assert p.ok is False and "nothing eligible" in p.reason


def test_an_empty_fleet_routes_nowhere():
    p = plan([])
    assert p.ok is False and not p.target


# ── scoring ──────────────────────────────────────────────────────────────
def test_idle_beats_busy():
    p = plan([cand("busy", running_count=3), cand("idle")])
    assert p.target.id == "idle"


def test_less_busy_beats_more_busy():
    p = plan([cand("a", running_count=4), cand("b", running_count=1)])
    assert p.target.id == "b"


def test_the_right_harness_already_active_wins():
    p = plan([cand("a"), cand("b", active_harness="factory")], harness="factory")
    assert p.target.id == "b"


def test_a_loaded_cpu_is_penalised():
    p = plan([cand("hot", cpu=0.95), cand("cool", cpu=0.05)])
    assert p.target.id == "cool"


def test_a_stale_but_live_instance_is_the_last_resort():
    stale = cand("stale", age_s=MAX_HEARTBEAT_AGE_S * 0.75)
    p = plan([stale, cand("fresh")])
    assert p.target.id == "fresh"


def test_a_stale_instance_still_wins_if_it_is_the_only_one():
    p = plan([cand("stale", age_s=MAX_HEARTBEAT_AGE_S * 0.75)])
    assert p.ok and p.target.id == "stale"


def test_busy_local_loses_to_idle_remote():
    """The local bonus must not pin everything to this box forever."""
    p = plan([cand("here", running_count=3, local=True),
              cand("there", running_count=0, local=False)])
    assert p.target.id == "there"


def test_local_wins_all_else_equal():
    p = plan([cand("here", local=True), cand("there", local=False)])
    assert p.target.id == "here"


# ── explainability ───────────────────────────────────────────────────────
def test_the_plan_explains_the_winner():
    p = plan([cand("a", running_count=2), cand("b")])
    assert "b scored" in p.reason and "idle" in p.reason


def test_the_plan_names_the_runner_up():
    p = plan([cand("a"), cand("b", running_count=1)])
    assert "next best" in p.reason


def test_every_candidate_gets_a_verdict():
    p = plan([cand("a"), cand("b", alive=False), cand("c", running_count=9)])
    assert {v.id for v in p.verdicts} == {"a", "b", "c"}


def test_rejections_carry_their_reason():
    p = plan([cand("a"), cand("dead", alive=False)])
    dead = next(v for v in p.verdicts if v.id == "dead")
    assert dead.eligible is False and dead.rejection == "offline"


def test_winning_verdict_shows_its_arithmetic():
    p = plan([cand("a", running_count=2, cpu=0.5)])
    v = next(v for v in p.verdicts if v.id == "a")
    assert any("running" in r for r in v.reasons)
    assert any("cpu" in r for r in v.reasons)


def test_plan_serialises_for_the_api():
    d = plan([cand("a")]).as_dict()
    assert d["ok"] is True and d["target"]["id"] == "a"
    assert isinstance(d["verdicts"], list)


# ── pinning (the drag-and-drop gesture) ──────────────────────────────────
def test_pinning_overrides_the_score():
    p = plan([cand("idle"), cand("busy", running_count=5)], target_id="busy")
    assert p.target.id == "busy" and "pinned" in p.reason


def test_pinning_to_an_unknown_instance_fails_clearly():
    p = plan([cand("a")], target_id="ghost")
    assert p.ok is False and "ghost" in p.reason


def test_pinning_to_a_dead_instance_is_refused_not_obeyed():
    """Dropping work on a box that just died must say so, not fail in transport."""
    p = plan([cand("a"), cand("dead", alive=False)], target_id="dead")
    assert p.ok is False and "cannot take work" in p.reason and "offline" in p.reason


def test_pinning_respects_harness_capability():
    p = plan([cand("a", harnesses=["demo"])], target_id="a", harness="factory")
    assert p.ok is False and "factory" in p.reason


# ── fleet adapter ────────────────────────────────────────────────────────
class FakePeer:
    def __init__(self, pid_, **kw):
        self.id = pid_
        self.port = kw.get("port", 8765)
        self.running_count = kw.get("running_count", 0)
        self.active_harness = kw.get("active_harness", "")
        self.is_self = kw.get("is_self", False)
        self._age = kw.get("age", 1.0)

    def age_s(self):
        return self._age


def test_adapter_maps_fleet_peers():
    got = candidates_from_fleet([FakePeer("main", running_count=2),
                                 FakePeer("hud", is_self=True)])
    assert {c.id for c in got} == {"main", "hud"}
    assert next(c for c in got if c.id == "main").running_count == 2
    assert all(c.alive and c.local for c in got)


def test_adapter_only_attributes_cpu_to_this_process():
    """We can measure our own load; we cannot measure another instance's."""
    got = candidates_from_fleet([FakePeer("me", is_self=True), FakePeer("other")],
                                now_cpu=0.9)
    assert next(c for c in got if c.id == "me").cpu == 0.9
    assert next(c for c in got if c.id == "other").cpu == 0.0


def test_adapter_survives_a_peer_missing_fields():
    class Bare:
        id = "bare"
    got = candidates_from_fleet([Bare()])
    assert got[0].id == "bare" and got[0].port == 8765


def test_end_to_end_pick_across_a_realistic_fleet():
    peers = [FakePeer("main", running_count=3, active_harness="demo"),
             FakePeer("worker", running_count=0, active_harness="factory")]
    p = plan(candidates_from_fleet(peers, harnesses=["demo", "factory"]),
             harness="factory")
    assert p.target.id == "worker"
