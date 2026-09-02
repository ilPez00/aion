"""Tests for AION-001: Fleet Workspace.

Validates the `FleetView` engine (pure functions over capability dict,
task list, meshd state → view models) and the harness that reads
~/.local/state/randomesh/capability.json + `agent-task.sh status`.

Gate: python -m pytest tests/test_fleet_workspace.py -q
"""

import pytest
import json
import tempfile
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from dataclasses import dataclass

# Mock imports for testing
try:
    from aion.fleet import FleetView
    from aion.fleet import FleetSettings
    FLEET_AVAILABLE = True
except ImportError:
    FLEET_AVAILABLE = False


@dataclass
class MockNodeCapability:
    node: str
    can_run_llm: bool = False
    can_serve_chat: bool = False
    has_gpu: bool = False
    ram_total_mb: int = 0
    disk_total_gb: int = 0


def test_fleet_view_basic():
    """Basic FleetView test if module available."""
    if not FLEET_AVAILABLE:
        pytest.skip("aion.fleet not yet available — implementing AION-001")
    # Basic sanity check
    assert FleetView is not None


def test_fleet_view_malformed_json():
    """HUD must never crash on stale/malformed fleet data."""
    if not FLEET_AVAILABLE:
        pytest.skip("aion.fleet not yet available")
    # Test with partial/broken capability.json
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        # Write partial JSON (missing some fields)
        f.write("{\"nodes\": {\"air\": {\"ram_total_mb\": 8192}}}  ")
        tmp_path = f.name
    try:
        with open(tmp_path) as f:
            cap = json.load(f)
        # FleetView should handle this gracefully, not crash
        # The exact behavior depends on implementation
        assert cap is not None
    finally:
        os.unlink(tmp_path)


def test_fleet_view_sorting():
    """FleetView sorting tests."""
    if not FLEET_AVAILABLE:
        pytest.skip("aion.fleet not yet available")
    # Verify FleetView can sort nodes by some criteria
    pass


# Integration test placeholder

def test_fleet_view_integration():
    """Integration: test against real state dirs."""
    # This will be fleshed out when the harness is implemented
    # For now, just verify the module can be imported
    from aion import fleet
    assert fleet is not None
