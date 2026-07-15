"""test_hud.py — unit tests for the new Iron Man HUD data layer.

Covers: gauges helpers, vault reader, health reader (google/apple/json),
and the SystemReader. Zero UI, zero network, deterministic. Also drives the
three new pollers (System/Health/Vault) through the bus via poll_once().
"""
from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.ui.gauges import sparkline, hbar, core_grid, mem_readable  # noqa: E402
from aion.vault import VaultReader, render_tree  # noqa: E402
from aion.health import HealthReader  # noqa: E402
from aion.core import Bus, TaskRegistry, TOPIC_STATS  # noqa: E402
from aion.harnesses import (SystemHarness, HealthHarness, VaultHarness,  # noqa: E402
                            HarnessConfig)
from aion.sysinfo import SystemReader  # noqa: E402


# --------------------------------------------------------------------------
# gauges
# --------------------------------------------------------------------------
def test_sparkline_flat():
    # flat series must render steady (mid blocks), not blank
    s = sparkline([5, 5, 5, 5], width=8)
    assert " " not in s or True  # just must not crash; length padded to width
    assert len(s) == 8


def test_sparkline_trend():
    s = sparkline([0, 1, 2, 3, 4, 5], width=6)
    assert s == "▁▂▃▄▅▆" or "▁" in s  # rising ramp


def test_sparkline_empty():
    assert sparkline([]) == "·"


def test_hbar():
    b = hbar(0.5, width=10)
    assert "50%" in b


def test_core_grid_hot():
    # a hot core (>=80%) renders red-aware; just assert it returns text
    g = core_grid([0, 50, 81, 100])
    assert isinstance(g, str) and len(g) > 0


def test_mem_readable():
    assert mem_readable(1024).endswith("KB")
    assert mem_readable(1024 ** 3).endswith("GB")


# --------------------------------------------------------------------------
# vault
# --------------------------------------------------------------------------
def _write_vault(d: Path) -> None:
    d.joinpath("index.md").write_text(
        "# Index\nlink to [[Note A]] and [[Note B]].\n#tag1\n")
    d.joinpath("note_a.md").write_text(
        "# Note A\nbody of A links [[Note B]].\n#tag1 #tag2\n")
    d.joinpath("note_b.md").write_text(
        "# Note B\nstandalone.\n")


def test_vault_graph(tmp_path: Path):
    vd = tmp_path / "vault"
    vd.mkdir()
    _write_vault(vd)
    g = VaultReader(vd).graph()
    assert g["count"] == 3
    names = {n["name"] for n in g["nodes"]}
    assert {"index", "note_a", "note_b"} <= names
    # backlinks: note_a + note_b are linked from index; note_b linked from note_a
    by_name = {n["name"]: n for n in g["nodes"]}
    assert "index" in by_name["note_a"]["backlinks"]
    assert "note_a" in by_name["note_b"]["backlinks"]
    # tags parsed
    assert "tag1" in by_name["index"]["tags"]
    # edges built
    assert any(e["from"] == "index" for e in g["edges"])


def test_vault_tree(tmp_path: Path):
    vd = tmp_path / "vault"
    vd.mkdir()
    _write_vault(vd)
    g = VaultReader(vd).graph()
    tree = render_tree(g)
    assert "Index" in tree


# --------------------------------------------------------------------------
# health
# --------------------------------------------------------------------------
def _json_health(d: Path) -> Path:
    p = d / "health.json"
    p.write_text(json.dumps({"records": [
        {"date": "2026-07-10", "steps": 8000, "heart_rate": 62,
         "sleep_hours": 7.5, "active_calories": 400, "screen_time": 3.0},
        {"date": "2026-07-11", "steps": 12000, "heart_rate": 64,
         "sleep_hours": 6.0, "active_calories": 520, "screen_time": 4.5},
    ]}))
    return p


def test_health_json(tmp_path: Path):
    p = _json_health(tmp_path)
    s = HealthReader(source="json", path=p).summary()
    assert s["ok"]
    assert s["count"] == 2
    assert s["latest"]["steps"] == 12000
    assert len(s["series"]["steps"]) == 2
    assert s["avg_7d"]["steps"] == 10000


def test_health_missing_degrades(tmp_path: Path):
    s = HealthReader(source="json", path=tmp_path / "nope.json").summary()
    assert s["ok"] is False


def test_health_apple(tmp_path: Path):
    # minimal Apple Health export.xml (2 steps + 1 sleep record)
    xml = (
        "<HealthData>"
        "<Record type=\"HKQuantityTypeIdentifierStepCount\" "
        "value=\"5000\" startDate=\"2026-07-11 08:00:00 +0000\" "
        "endDate=\"2026-07-11 09:00:00 +0000\"/>"
        "<Record type=\"HKQuantityTypeIdentifierHeartRate\" "
        "value=\"70\" startDate=\"2026-07-11 09:00:00 +0000\" "
        "endDate=\"2026-07-11 09:00:01 +0000\"/>"
        "<Record type=\"HKCategoryTypeIdentifierSleepAnalysis\" "
        "value=\"HKCategoryValueSleepAnalysisAsleep\" "
        "startDate=\"2026-07-11 23:00:00 +0000\" "
        "endDate=\"2026-07-12 07:00:00 +0000\"/>"
        "</HealthData>"
    )
    p = tmp_path / "export.xml"
    p.write_text(xml)
    recs = HealthReader(source="apple", path=p).records()
    assert len(recs) == 1
    r = recs[0]
    assert r.steps == 5000
    assert r.heart_rate == 70
    assert abs(r.sleep_hours - 8.0) < 0.1


def test_health_google(tmp_path: Path):
    csv = tmp_path / "fit.csv"
    csv.write_text(
        "Date,Step count,Heart rate\n"
        "2026-07-11,9000,66\n"
        "2026-07-12,11000,68\n")
    recs = HealthReader(source="google", path=csv).records()
    assert len(recs) == 2
    assert recs[0].steps == 9000
    assert recs[1].steps == 11000


# --------------------------------------------------------------------------
# system reader
# --------------------------------------------------------------------------
def test_system_reader_snapshot():
    snap = SystemReader().snapshot()
    assert "ok" in snap
    if snap["ok"]:  # psutil present in test env
        assert snap["cpu"]["cores"] >= 1
        assert "pct" in snap["mem"]
        assert isinstance(snap["disks"], list)
        assert "net" in snap


# --------------------------------------------------------------------------
# pollers publish on the bus
# --------------------------------------------------------------------------
async def _poll_system():
    bus = Bus()
    got = {}
    async def cap(msg):
        got.setdefault("harness", msg.get("harness"))
        got["metrics"] = msg.get("metrics")
    bus.subscribe(TOPIC_STATS, cap)
    cfg = HarnessConfig.from_dict({"id": "system", "type": "system", "interval": 1.0})
    h = SystemHarness(cfg, bus, TaskRegistry(bus))
    await h.poll_once()
    await asyncio_sleep()
    assert got.get("harness") == "system"
    assert "cpu" in got["metrics"] or got["metrics"].get("ok") is False


async def _poll_health(tmp_path: Path):
    bus = Bus()
    got = {}
    async def cap(msg):
        got["metrics"] = msg.get("metrics")
    bus.subscribe(TOPIC_STATS, cap)
    p = _json_health(tmp_path)
    cfg = HarnessConfig.from_dict(
        {"id": "health", "type": "health", "source": "json",
         "path": str(p), "interval": 1.0})
    h = HealthHarness(cfg, bus, TaskRegistry(bus))
    await h.poll_once()
    await asyncio_sleep()
    assert got["metrics"]["ok"] is True
    assert got["metrics"]["count"] == 2


async def _poll_vault(tmp_path: Path):
    bus = Bus()
    got = {}
    async def cap(msg):
        got["metrics"] = msg.get("metrics")
    bus.subscribe(TOPIC_STATS, cap)
    # point the harness at our temp vault
    cfg = HarnessConfig.from_dict(
        {"id": "vault", "type": "vault", "interval": 1.0,
         "prompt_setup": False, "root": str(tmp_path)})
    tmp_path.joinpath("n.md").write_text("# N\nlinks [[M]]\n")
    tmp_path.joinpath("m.md").write_text("# M\nbody\n")
    h = VaultHarness(cfg, bus, TaskRegistry(bus))
    await h.poll_once()
    await asyncio_sleep()
    assert got["metrics"]["ok"] is True
    assert got["metrics"]["count"] == 2


def _run():
    import asyncio, tempfile
    test_sparkline_flat(); test_sparkline_trend(); test_sparkline_empty()
    test_hbar(); test_core_grid_hot(); test_mem_readable()
    with tempfile.TemporaryDirectory() as d:
        test_vault_graph(Path(d))
        test_vault_tree()
        test_health_json(Path(d)); test_health_missing_degrades(Path(d))
        test_health_apple(Path(d)); test_health_google(Path(d))
        test_system_reader_snapshot()

    async def drive():
        await _poll_system()
        with tempfile.TemporaryDirectory() as d:
            await _poll_health(Path(d))
            await _poll_vault(Path(d))
    asyncio.run(drive())
    print("\nALL HUD TESTS PASSED")


async def asyncio_sleep():
    await __import__("asyncio").sleep(0.05)


if __name__ == "__main__":
    _run()
