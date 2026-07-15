"""Tests for the interactive walkthrough (talon_hud-style step-by-step tour)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from aion.ui.app import AiOSApp


@pytest.mark.asyncio
async def test_tour_opens_on_first_step():
    app = AiOSApp()
    async with app.run_test(size=(120, 40)):
        app.action_tour()
        assert app._tour_active
        help_w = app.query_one("#help")
        assert help_w.display
        # first step content rendered (TOUR 1/ prefix)
        assert "TOUR 1/" in str(help_w.content)


@pytest.mark.asyncio
async def test_tour_advance_through_steps():
    app = AiOSApp()
    async with app.run_test(size=(120, 40)):
        app.action_tour()
        steps = len(app.WALKTHROUGH)
        for _ in range(steps):
            app._tour_next()
        # after consuming all steps, tour should close
        assert not app._tour_active
        assert not app.query_one("#help").display


@pytest.mark.asyncio
async def test_tour_escape_closes():
    app = AiOSApp()
    async with app.run_test(size=(120, 40)):
        app.action_tour()
        app._tour_close()
        assert not app._tour_active
        assert not app.query_one("#help").display
