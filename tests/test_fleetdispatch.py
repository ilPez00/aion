"""Tests for fleetdispatch.py + mesh dispatch palette (hermetic default)."""
import asyncio as _aio
import os

import pytest

from aion.fleetdispatch import (DispatchJob, parse_status_table,
                                submit_preview)

TABLE = """\
ID                       NODE         NAME                     STATE       IDE RC
20260913-082716-55a0     pansa        stage-probe              done        -   0
20260913-090000-0001     omo          cargo-build              running     Y   -
20260913-090001-0002     air          bad-build                lost        -   1
garbage line without columns
"""


def test_parse_status_table():
    jobs = parse_status_table(TABLE)
    assert [(j.id, j.node, j.state, j.rc) for j in jobs] == [
        ("20260913-082716-55a0", "pansa", "done", "0"),
        ("20260913-090000-0001", "omo", "running", "-"),
        ("20260913-090001-0002", "air", "lost", "1")]
    assert jobs[0].terminal is True and jobs[1].terminal is False
    assert jobs[1].idempotent is True and jobs[0].idempotent is False
    assert jobs[2].as_dict()["terminal"] is True
    assert parse_status_table("") == []
    assert DispatchJob(id="x", state="queued").terminal is False


def _fake_dispatch(tmp_path, chosen="picked-ts"):
    p = tmp_path / "dispatch.sh"
    p.write_text("#!/usr/bin/env bash\n"
                 "if [ \"$2\" = \"--dry-run\" ] || [ \"$3\" = \"--dry-run\" ]; then\n"
                 f"  echo '(dry-run) job t0001 -> {chosen}';\n"
                 "  echo '  cmd: x'; exit 0;\n"
                 "fi\n"
                 "echo 't0002'; echo \"  node:  $chosen\" >&2\n")
    p.chmod(0o755)
    return str(p)


def test_submit_preview_parses(monkeypatch, tmp_path):
    import aion.fleetdispatch as fd
    monkeypatch.setattr(fd, "DISPATCH_SCRIPT", _fake_dispatch(tmp_path))
    # default args bind at def; pass the (patched) script explicitly
    jid, node = submit_preview("echo hi", script=fd.DISPATCH_SCRIPT)
    assert (jid, node) == ("t0001", "picked-ts")


def test_submit_preview_no_candidate(monkeypatch, tmp_path):
    import aion.fleetdispatch as fd
    p = tmp_path / "dispatch.sh"
    p.write_text("#!/usr/bin/env bash\necho 'no node satisfies' >&2; exit 1\n")
    p.chmod(0o755)
    monkeypatch.setattr(fd, "DISPATCH_SCRIPT", str(p))
    with pytest.raises(RuntimeError, match="no node satisfies"):
        submit_preview("echo hi", needs=["tool:nope"],
                       script=fd.DISPATCH_SCRIPT)


def test_mesh_dispatch_palette_preview_and_confirm(monkeypatch, tmp_path):
    import aion.fleetdispatch as fd
    from aion.ui.app import AiOSApp
    monkeypatch.setattr(fd, "DISPATCH_SCRIPT",
                        _fake_dispatch(tmp_path, chosen="omo-ts"))

    class _FakeApp:
        pass

    out = _aio.run(AiOSApp._handle_mesh_command(
        _FakeApp(), "mesh dispatch echo hi --needs tool:cargo"))
    assert "preview" in out and "omo-ts" in out and "tool:cargo" in out
    out = _aio.run(AiOSApp._handle_mesh_command(
        _FakeApp(), "mesh dispatch echo hi yes"))
    assert "submitted t0002" in out
    out = _aio.run(AiOSApp._handle_mesh_command(_FakeApp(), "mesh dispatch"))
    assert out.startswith("usage:")


def test_submit_preview_live():
    """Against the REAL dispatch.sh (FLEET_LIVE=1): preview agrees."""
    if os.environ.get("FLEET_LIVE") != "1":
        pytest.skip("needs FLEET_LIVE=1")
    jid, node = submit_preview("echo hi", needs=["tool:cargo"])
    assert jid and node in ("omo-ts", "pansa-ts", "feather-ts")
