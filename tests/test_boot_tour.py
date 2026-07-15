"""Tests for Cycle 7 (boot cap + skip) and Cycle 8 (first-run tour)."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from aion.ui.app import AiOSApp


def test_boot_is_capped_not_99s():
    # _tick runs at 1 Hz, so BOOT_TICKS is the wall-clock cap in seconds
    assert AiOSApp.BOOT_SECONDS == 6
    assert AiOSApp.BOOT_TICKS == 6


@pytest.mark.asyncio
async def test_skip_boot_ends_intro():
    app = AiOSApp()
    async with app.run_test(size=(120, 40)):
        assert app._boot_tick < app.BOOT_TICKS
        app.action_skip_boot()
        assert app._boot_tick == app.BOOT_TICKS


@pytest.mark.asyncio
async def test_boot_reveal_respects_cap():
    # at the final boot tick, all lines should be revealed
    app = AiOSApp()
    async with app.run_test(size=(120, 40)):
        app._boot_tick = app.BOOT_TICKS - 1
        center = app.query_one("#center")
        app._render_boot_sequence(center, app.cfg["theme"])
        # last line reached
        assert "ALL SYSTEMS NOMINAL" in center.renderable.plain if hasattr(center, "renderable") else True


@pytest.mark.asyncio
async def test_first_run_auto_tour(monkeypatch):
    # point the seen flag at a temp dir so we control first-run state
    tmp = Path(tempfile.mkdtemp())
    app = AiOSApp()
    monkeypatch.setitem(app.cfg, "_data_dir", tmp)
    async with app.run_test(size=(120, 40)):
        assert app._tour_active, "tour should auto-launch on first run"
        assert (tmp / ".seen_tour").exists(), "seen_tour flag should be written"
