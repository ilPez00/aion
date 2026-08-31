"""test_agg_cli.py — headless `aion mesh agg <sub>` CLI dispatch.

Asserts the headless subcommand never launches the TUI (monkeypatches AiOSApp
to assert, same pattern as test_cli.py) and prints the expected shape.

The CLI imports `agg` lazily inside `_mesh_agg_cli`; tests patch
`aion.agg` (the real module) so the patched callables are the ones used.
"""
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.ui import app as appmod
from aion import agg as aggmod  # patch THIS (real module)


def _never_launch(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("CLI launched the cockpit")
    monkeypatch.setattr(appmod, "AiOSApp", boom)


_FAKE_SUMMARY = {
    "db": "/tmp/agg.db",
    "nodes": {"omo": "omo-ts", "pansa": "pansa-ts"},
    "results": {
        "omo": {"node": "omo", "ok": True,
                "ingested": {"total": 385, "memory": 9, "sessions": 279, "docs": 97}},
        "pansa": {"node": "pansa", "ok": True,
                  "ingested": {"total": 252, "memory": 8, "sessions": 114, "docs": 130}},
    },
    "summary": {"items": 1240, "by_kind": {"session": 601, "memory": 38, "doc": 601},
                "by_node": {"omo": 385, "pansa": 252}, "exists": True},
}


def _run_capture(monkeypatch, argv):
    _never_launch(monkeypatch)
    captured = []
    with mock.patch("builtins.print",
                    side_effect=lambda *a, **k: captured.append(" ".join(map(str, a)))):
        try:
            rc = appmod.main(argv)
            assert rc in (None, 0)
        except SystemExit as e:
            assert e.code in (None, 0)
    return captured


def test_agg_collect_prints_per_node(monkeypatch, tmp_path):
    with mock.patch.object(aggmod, "collect_all", return_value=_FAKE_SUMMARY):
        cap = _run_capture(monkeypatch, ["mesh", "agg", "collect"])
    assert any("omo" in c and "385" in c for c in cap)
    assert any("pansa" in c and "252" in c for c in cap)
    assert any("DB total: 1240" in c for c in cap)


def test_agg_status_does_not_launch_tui(monkeypatch, tmp_path):
    with mock.patch.object(aggmod, "status") as st:
        st.return_value = _FAKE_SUMMARY["summary"]
        cap = _run_capture(monkeypatch, ["mesh", "agg", "status"])
    assert any("1240 items" in c for c in cap)
    # status renders `node <nn>` per-line, not `node:nn`
    assert any(c.strip().startswith("omo") and "385" in c for c in cap)


def test_agg_status_missing_db(monkeypatch, tmp_path):
    with mock.patch.object(aggmod, "status") as st:
        st.return_value = {"db": str(tmp_path / "x.db"), "exists": False, "items": 0,
                           "by_kind": {}, "by_node": {}, "last_collect": None}
        cap = _run_capture(monkeypatch, ["mesh", "agg", "status"])
    assert any("empty" in c.lower() for c in cap)


def test_agg_search(monkeypatch, tmp_path):
    fake_hits = [{"node": "omo", "kind": "memory", "source": "hermes",
                  "title": "FLEET", "body": "…[colibri] HIP=ROCm…", "hl": "…[colibri]…"}]
    with mock.patch.object(aggmod, "search", return_value=fake_hits):
        cap = _run_capture(monkeypatch, ["mesh", "agg", "search", "colibri"])
    assert any("1 match" in c for c in cap)
    # node/source/title on one line, highlight on the next
    assert any("omo" in c and "FLEET" in c for c in cap)
    assert any("colibri" in c for c in cap)
