"""tests/test_life.py — the four-domain life HUD collector.

Pure logic: no network, no clock, no files unless tmp_path provides them.
The praxis transport is a fake callable, mirroring test_praxis.py.
"""
from __future__ import annotations

import json

import pytest

from aion.life import (
    DOMAIN_ORDER,
    LifeConfig,
    collect_life,
    domain_score,
    money_from_text,
)


# ── money parsing ─────────────────────────────────────────────────────────────

def test_money_parses_entries_and_total():
    text = """\
# Money

target_mrr: 2500

- 2026-08-20 | invoice | Generalmeccanica pilot | 1800 | sent
- 2026-08-22 | payment | Generalmeccanica pilot | 1800 | paid
- 2026-08-23 | expense | gym annual | -30 | paid
"""
    m = money_from_text(text)
    assert m["ok"] is True
    assert m["target_mrr"] == 2500
    assert m["paid_total"] == 1770        # +1800 -30
    assert m["open_total"] == 1800        # sent, not paid
    assert len(m["entries"]) == 3


def test_money_empty_file_is_ok_but_zero():
    m = money_from_text("")
    assert m["ok"] is True
    assert m["entries"] == []
    assert m["paid_total"] == 0


def test_money_garbage_lines_do_not_crash():
    m = money_from_text("not a pipe line\n- also bad\n")
    assert m["ok"] is True
    assert m["entries"] == []


# ── config ────────────────────────────────────────────────────────────────────

def test_config_defaults_disabled_praxis():
    cfg = LifeConfig.from_env({})
    assert cfg.money_path  # default path exists
    assert cfg.praxis_enabled is False


def test_config_env_overrides(tmp_path):
    env = {
        "AION_PRAXIS_URL": "http://x",
        "AION_PRAXIS_KEY": "k",
        "AION_PRAXIS_USER": "u1",
        "AION_LIFE_MONEY_FILE": str(tmp_path / "m.md"),
    }
    cfg = LifeConfig.from_env(env)
    assert cfg.praxis_enabled is True
    assert cfg.money_path == str(tmp_path / "m.md")


# ── collect ───────────────────────────────────────────────────────────────────

def _fake_transport(responses):
    def transport(method, path, body=None):
        return responses.get(path, (404, {}))
    return transport


def test_collect_all_domains_present():
    cfg = LifeConfig(money_path="/nonexistent/money.md", health_path="/nonexistent/h.json")
    snap = collect_life(cfg)
    # every domain ALWAYS reports, even when its source is missing
    assert set(DOMAIN_ORDER) <= set(snap["domains"].keys())
    assert snap["domains"]["money"]["ok"] is False      # missing file
    assert snap["domains"]["fitness"]["ok"] is False


def test_collect_money_and_fitness_from_files(tmp_path):
    money = tmp_path / "money.md"
    money.write_text("- 2026-08-20 | payment | pilot | 1500 | paid\n")
    health = tmp_path / "health.json"
    health.write_text(json.dumps({"steps": 8400, "sleep_h": 7.2, "resting_hr": 58}))
    cfg = LifeConfig(money_path=str(money), health_path=str(health))
    snap = collect_life(cfg)
    d = snap["domains"]
    assert d["money"]["ok"] and d["money"]["paid_total"] == 1500
    assert d["fitness"]["ok"] and d["fitness"]["steps"] == 8400
    # social offline -> ok False but never raises
    assert d["social"]["ok"] is False


def test_collect_social_via_transport():
    tree = {"nodes": [
        {"progress": 1.0}, {"progress": 0.5}, {"progress": 0.25}]}
    resp = {("GET", "/api/dashboard/summary"): (
        200, {"checkedIn": True,
              "activeBets": [{"id": 1}],
              "goalTree": tree})}
    calls = []

    def transport(method, path, body=None):
        calls.append((method, path))
        return resp.get((method, path), (404, {}))

    cfg = LifeConfig(praxis_url="http://x", praxis_key="k", praxis_user="u")
    snap = collect_life(cfg, transport=transport)
    soc = snap["domains"]["social"]
    assert soc["ok"] is True
    assert soc["checked_in"] is True
    assert soc["goals_avg_progress"] == pytest.approx((1.0 + 0.5 + 0.25) / 3, abs=1e-2)
    assert soc["active_bets"] == 1


def test_collect_social_soft_fail_on_transport_error():
    def boom(method, path, body=None):
        raise OSError("conn refused")

    cfg = LifeConfig(praxis_url="http://x", praxis_key="k", praxis_user="u")
    snap = collect_life(cfg, transport=boom)
    assert snap["domains"]["social"]["ok"] is False
    assert "conn refused" in snap["domains"]["social"]["reason"]


# ── scoring (drives the flow visualizer) ─────────────────────────────────────

def test_domain_scores_in_order():
    snap = {"domains": {
        "computer": {"ok": True, "cpu_pct": 10, "ram_pct": 20},
        "money": {"ok": True, "paid_total": 500, "target_mrr": 1000},
        "fitness": {"ok": True, "steps": 8000, "step_goal": 8000},
        "social": {"ok": True, "checked_in": True,
                   "goals": 3, "goals_avg_progress": 1.0},
    }}
    scores = domain_score(snap)
    # computer idle=1.0 · fitness at-goal=1.0 · social checkin+goals=1.0 ·
    # money half-of-target=0.5
    assert [s for _, s in scores] == pytest.approx([1.0, 1.0, 1.0, 0.5])


def test_scores_never_exceed_one_or_below_zero():
    snap = {"domains": {
        "money": {"ok": True, "paid_total": 99999, "target_mrr": 10},
        "fitness": {"ok": True, "steps": 0},
        "social": {"ok": False, "reason": "down"},
        "computer": {"ok": False},
    }}
    for name, v in domain_score(snap):
        assert 0.0 <= v <= 1.0
