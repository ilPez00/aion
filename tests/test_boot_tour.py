"""Boot sequence removed (instant boot), tour is manual (Ctrl-K: tour)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest


@pytest.mark.asyncio
async def test_no_auto_tour():
    """Tour does not auto-launch on boot anymore."""
    from aion.ui.app import AiOSApp
    app = AiOSApp()
    async with app.run_test(size=(120, 40)):
        assert not app._tour_active, "tour should NOT auto-launch"
