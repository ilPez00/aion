"""Tests for the process viewer integrated into System workspace."""
import pytest

from aion.sysinfo import SystemReader


def test_processes_returns_list():
    reader = SystemReader()
    procs = reader.processes(top_n=5)
    assert isinstance(procs, list)
    # on a real system there should be at least one process
    if procs:
        p = procs[0]
        assert "pid" in p
        assert "name" in p
        assert "cpu_pct" in p
        assert "mem_mb" in p
        assert "status" in p


def test_processes_top_n():
    reader = SystemReader()
    procs = reader.processes(top_n=3)
    assert len(procs) <= 3


def test_processes_sorted_by_cpu():
    reader = SystemReader()
    procs = reader.processes(top_n=10)
    if len(procs) > 1:
        for i in range(len(procs) - 1):
            assert procs[i]["cpu_pct"] >= procs[i + 1]["cpu_pct"]


def test_processes_not_empty():
    """On a real Linux/Python system there is always at least ourselves."""
    reader = SystemReader()
    procs = reader.processes(top_n=50)
    # we should at least see this test process or python
    names = [p["name"] for p in procs if p["name"] != "? "]
    assert len(procs) > 0


def test_processes_without_psutil():
    """Simulate psutil being unavailable — graceful degradation."""
    import aion.sysinfo
    saved = aion.sysinfo.psutil
    aion.sysinfo.psutil = None
    try:
        reader = SystemReader()
        procs = reader.processes()
        assert procs == []
    finally:
        aion.sysinfo.psutil = saved
