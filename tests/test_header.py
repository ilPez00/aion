"""Tests for header status indicators (talon_hud-style mode + alert)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from aion.ui.app import AiOSApp


def _header_text(app):
    return app.query_one("#header").text


@pytest.mark.asyncio
async def test_header_shows_active_mode():
    app = AiOSApp()
    async with app.run_test(size=(120, 40)):
        app.store.state.active_mode = "focus"
        app._render_header()
        assert "focus" in _header_text(app), _header_text(app)


@pytest.mark.asyncio
async def test_header_shows_alert_when_suggestions_present():
    app = AiOSApp()
    async with app.run_test(size=(120, 40)):
        app.store.state.suggestions = ["⚠ 1 task failed — say 'rerun'"]
        app._render_header()
        assert "⚠" in _header_text(app), _header_text(app)


@pytest.mark.asyncio
async def test_header_quiet_when_no_suggestions():
    app = AiOSApp()
    async with app.run_test(size=(120, 40)):
        app.store.state.suggestions = []
        app._render_header()
        assert "⚠" not in _header_text(app), _header_text(app)
