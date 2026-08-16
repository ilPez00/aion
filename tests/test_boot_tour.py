"""Onboarding gate: the tour auto-launches once per version, then stays quiet.

conftest stubs `should_show_onboarding` OFF so unrelated boot tests are not
disturbed. These tests restore the real gate and drive the marker file to
verify: first run shows it, closing records it, later boots stay silent.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from aion.ui.app import AiOSApp
from aion.ui.wizard import (
    ONBOARDING_VERSION, onboarding_marker, record_onboarding, seen_onboarding,
)

from aion.ui import wizard as _wizard
from aion.ui.wizard import should_show_onboarding as wizard_should_show


def _reset_marker():
    onboarding_marker().unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_no_auto_tour_when_marker_present(monkeypatch):
    """Tour does not auto-launch once onboarding is recorded."""
    record_onboarding()
    monkeypatch.setattr(_wizard, "should_show_onboarding", wizard_should_show)
    app = AiOSApp()
    async with app.run_test(size=(120, 40)):
        assert not app._tour_active, "tour should NOT auto-launch (already seen)"


@pytest.mark.asyncio
async def test_auto_tour_on_first_run_only(monkeypatch):
    """No marker (fresh install) auto-launches the tour once; closing records."""
    _reset_marker()
    monkeypatch.setattr(_wizard, "should_show_onboarding", wizard_should_show)
    assert wizard_should_show() and seen_onboarding() == 0
    app = AiOSApp()
    async with app.run_test(size=(120, 40)):
        assert app._tour_active, "first run should auto-show the tour"
        app._tour_close()
        assert seen_onboarding() == ONBOARDING_VERSION
    app2 = AiOSApp()
    async with app2.run_test(size=(120, 40)):
        assert not app2._tour_active, "second boot should stay quiet"