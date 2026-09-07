"""Tests for the RandoMesh node monitor pure logic (no network)."""
from aion.meshmon import _parse_stat, probe_node, snapshot, NODES


def _fake(rc=0, out=""):
    def t(method, target, cmd):
        return rc, out
    return t


BLOCK = (
    " 12:34:56 up 3 days,  4:00,  2 users,  load average: 0.42, 0.30, 0.21"
    "__S__0.42 0.30 0.21 1/234 5678"
    "__S__Mem:  16000000000 8000000000 8000000000  0  0"
    "__S__/dev/sda2  916G 200G 716G 22% /"
)


def test_parse_stat_full():
    s = _parse_stat(BLOCK, "pansa", "storage-node")
    assert s.reachable is False  # pure parser: reachability set by probe_node
    assert s.load1 == 0.42
    assert s.ram_pct == 50
    assert s.disk_pct == 22
    assert "3 days" in s.uptime
    assert s.role == "storage-node"


def test_probe_node_unreachable():
    s = probe_node("air", transport=_fake(1, ""))
    assert s.reachable is False
    assert s.note == "unreachable"


def test_probe_node_with_fake_block():
    s = probe_node("pansa", transport=_fake(0, BLOCK))
    assert s.reachable is True
    assert s.disk_pct == 22


def test_snapshot_counts_and_soft_fails():
    # air down, pansa up; others unreachable fake
    def mixed(method, target, cmd):
        return (0, BLOCK) if target == "pansa-ts" else (1, "")
    snap = snapshot(transport=mixed)
    assert snap["total"] == len(NODES)
    # only pansa reachable in this fake
    assert snap["reachable"] == 1
    names = {n["name"] for n in snap["nodes"]}
    assert names == set(NODES.keys())
    # unreachable nodes still present, marked down (soft-fail, no crash)
    air = [n for n in snap["nodes"] if n["name"] == "air"][0]
    assert air["reachable"] is False


# ── fleet-manager six-section snapshot (facts-backed) ───────────────────────
import json as _json
import os as _os
import time as _time

import aion.meshmon as _mm

FACTS = {
    "generated": 1788784000,
    "nodes": {
        "omo": {"online": True, "facts": {
            "host": {"hostname": "omo", "uptime_s": 100},
            "hw": {"cpu_model": "R9", "cores": 16, "load1": 0.5,
                   "ram_total_mb": 15557520, "ram_avail_mb": 11000000,
                   "gpus": ["RX 6650M"]},
            "disks": [{"mount": "/", "fs": "ext4", "size_gb": 900,
                       "used_gb": 700, "pct": 78}],
            "services": {"user_units": [
                {"unit": "ollama.service", "active": "active", "sub": "running"},
                {"unit": "randomesh-caddy.service", "active": "active", "sub": "running"},
            ], "timers": ["randomesh-meshd.timer"]},
            "ports": [11434, 8081, 8088],
            "programs": {"python3": "3.13.5", "ollama": "0.32.5"},
            "packages": {"manager": "dpkg", "explicit": "4728"},
            "ollama": {"installed": True, "models": [
                {"name": "qwen3.5-9b-uncensored-aggressive-q4_k_m:latest", "size": "5.6"}]},
            "llama_builds": ["/home/gio/dev/scripts/llama-b8831"],
            "configs": {"randomesh_clone": True, "randomesh_dirty": 6},
            "network": {"tailscale": {"up": True, "self_ip": "100.116.39.57",
                                      "peers": [{"name": "pansa", "online": True},
                                                {"name": "air", "online": False}]}},
        }},
        "air": {"online": False, "error": "ssh failed"},
    },
    "agents": ["== omo-ts ==", "  t1  done"],
}

FLEET = {"services": {
    "ollama-omo": {"host": "omo-ts", "probe": "tcp:11434",
                   "unit": "ollama.service", "critical": True, "note": "models"},
    "gateway-caddy": {"host": "omo-ts", "probe": "tcp:8088",
                      "unit": "randomesh-caddy.service"},
    "omniroute": {"host": "air", "probe": "tcp:20128"},
}}


def test_machine_rows():
    rows = _mm._machine_rows(FACTS)
    omo = next(r for r in rows if r["name"] == "omo")
    assert omo["online"] and omo["cores"] == 16 and omo["gpus"] == ["RX 6650M"]
    air = next(r for r in rows if r["name"] == "air")
    assert not air["online"] and air["error"] == "ssh failed"


def test_service_rows_unit_and_tcp(tmp_path, monkeypatch):
    fleet = tmp_path / "fleet.json"
    fleet.write_text(_json.dumps(FLEET))
    monkeypatch.setenv("AION_FLEET_CONFIG", str(fleet))
    rows = {r["name"]: r for r in _mm._service_rows(FACTS)}
    # unit probe: ollama.service is active on omo
    assert rows["ollama-omo"]["state"] == "active"
    assert rows["ollama-omo"]["critical"] is True
    # tcp probe: 8088 in omo's listening ports
    assert rows["gateway-caddy"]["state"] == "active"
    # air offline -> host down, state stays unknown
    assert rows["omniroute"]["state"] == "down"


def test_program_config_network_rows():
    progs = {r["name"]: r for r in _mm._program_rows(FACTS)}
    assert progs["omo"]["packages"]["explicit"] == "4728"
    assert "qwen3.5-9b-uncensored-aggressive-q4_k_m:latest" in progs["omo"]["ollama_models"]
    cfgs = {r["name"]: r for r in _mm._config_rows(FACTS)}
    assert cfgs["omo"]["randomesh_dirty"] == 6
    net = {r["name"]: r for r in _mm._network_rows(FACTS)}
    assert net["omo"]["peers_online"] == ["pansa"]
    assert 8088 in net["omo"]["listening_ports"]
    assert "air" not in progs  # offline nodes contribute nothing


def test_load_facts_fresh_cache(tmp_path, monkeypatch):
    cache = tmp_path / "facts.json"
    cache.write_text(_json.dumps(FACTS))
    monkeypatch.setattr(_mm, "FACTS_CACHE", str(cache))

    def boom(cmd):  # resweep must NOT run when cache is fresh
        raise AssertionError("resweep on fresh cache")

    assert _mm._load_facts(runner=boom)["generated"] == FACTS["generated"]


def test_load_facts_stale_resweeps(tmp_path, monkeypatch):
    cache = tmp_path / "facts.json"
    old = {"generated": 1, "nodes": {}}
    cache.write_text(_json.dumps(old))
    past = _time.time() - 3600
    _os.utime(cache, (past, past))
    monkeypatch.setattr(_mm, "FACTS_CACHE", str(cache))
    calls = []

    def runner(cmd):
        calls.append(cmd)
        return 0, _json.dumps(FACTS)

    got = _mm._load_facts(runner=runner)
    assert len(calls) == 1 and got["generated"] == FACTS["generated"]


def test_load_facts_unavailable_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(_mm, "FACTS_CACHE", str(tmp_path / "absent.json"))
    assert _mm._load_facts(runner=lambda cmd: (1, "no mesh")) is None


def test_snapshot_sections_facts_path(monkeypatch):
    monkeypatch.setattr(_mm, "_load_facts",
                        lambda max_age_s=_mm.FACTS_MAX_AGE_S, runner=None: FACTS)
    snap = _mm.snapshot_sections()
    assert snap["source"] == "facts"
    for section in ("machines", "services", "programs", "configs",
                    "network", "agents"):
        assert section in snap
    assert snap["machines"][0]["name"] == "omo"


def test_snapshot_sections_legacy_fallback(monkeypatch):
    monkeypatch.setattr(_mm, "_load_facts", lambda max_age_s=60, runner=None: None)
    monkeypatch.setattr(_mm, "snapshot", lambda transport=None: {
        "nodes": [{"name": "omo", "reachable": True}], "total": 1,
        "reachable": 1, "storage": {}})
    snap = _mm.snapshot_sections()
    assert snap["source"] == "legacy" and snap["machines"][0]["name"] == "omo"
    assert snap["services"] == []


def test_render_mesh_renders_facts_sections():
    from aion.ui.mesh_panel import render_mesh
    data = {"sections": {
        "source": "facts", "generated": 1788784000,
        "programs": [{"name": "omo", "packages": {"manager": "dpkg", "explicit": 4728},
                      "ollama_models": ["q1", "q2"], "llama_builds": ["/x"]}],
        "configs": [{"name": "omo", "randomesh_clone": True, "aion_clone": True,
                     "fleet_md": True, "randomesh_dirty": 6}],
        "network": [{"name": "omo", "ts_up": True, "self_ip": "100.116.39.57",
                     "peers_online": ["pansa"], "peers_total": 8,
                     "listening_ports": [22, 8081]}],
    }}
    out = render_mesh(data, {"accent": "#5ad1ff", "dim": "#9aabbb",
                             "ok": "#7CFFB2", "warn": "#FFD479"})
    assert "programs" in out and "configs" in out and "network" in out
    assert "dirty" in out and "q1" in out and "100.116.39.57" in out
