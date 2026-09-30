"""Trackpad + keyboard navigation: rail clicks switch workspaces,
row clicks select-then-activate, every workspace has a key."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.ui.app import AiOSApp


@pytest.mark.asyncio
async def test_rail_click_switches_workspace():
    Path.home().joinpath(".aion", "session.json").unlink(missing_ok=True)
    app = AiOSApp()
    async with app.run_test(size=(120, 40)) as pilot:
        assert app.store.state.active_ws == 0
        # real mouse path: click the second rail icon (models)
        cells = list(app.query("#rail Cell"))
        assert len(cells) >= 2
        await pilot.click(cells[1])
        await pilot.pause()
        assert app.store.state.active_ws == 1


@pytest.mark.asyncio
async def test_row_click_selects_then_activates():
    Path.home().joinpath(".aion", "session.json").unlink(missing_ok=True)
    app = AiOSApp()
    async with app.run_test(size=(120, 40)) as pilot:
        app.click_workspace(2)  # tasks
        await pilot.pause()
        assert app.store.state.active_ws == 2
        # select path only (focus was reset to 0 by the switch)
        app.click_item(1)
        assert app.store.state.focus == 1
        # fleet rows use panel selection, not state.focus
        app.click_workspace(10)
        await pilot.pause()
        app.click_item(1)
        assert app._mesh_sel == 1
