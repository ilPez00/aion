"""Tests for fleettask.py — cockpit view over the agent-task queue (no fleet)."""
import json
import os
import subprocess

import pytest

from aion import fleettask
from aion.fleettask import FleetTask, parse_status, parse_status_all, read_local_tasks, submit_queued

SCRIPT = os.path.expanduser("~/dev/randomesh/scripts/fleet/agent-task.sh")

TABLE = """\
  ID                       STATUS     ENGINE/MODEL                       RC   TITLE
  t20260913-120000-12345   running    opencode/ollama_omo/qwen2.5:7b-ins  12   Fix the loader
  t20260913-120100-12346   queued     shell                                   Probe omo disk
  t20260913-120200-12347   done       opencode/ollama_omo/qwen2.5:7b-ins  0    Write docs
  t20260913-120300-12348   lost       loom                                     Nightly sweep
  (no tasks)
"""

STATUS_ALL = """\
== pansa-ts ==
  ID                       STATUS     ENGINE/MODEL                       RC   TITLE
  t20260913-120000-1       running    shell                                    Local job
== omo-ts ==
  (unreachable)
== air-ts ==
  ID                       STATUS     ENGINE/MODEL                       RC   TITLE
  t20260913-120000-2       done       shell                              0    Remote job
"""


def test_parse_status_rows():
    tasks = parse_status(TABLE, host="pansa")
    assert [(t.id, t.status) for t in tasks] == [
        ("t20260913-120000-12345", "running"),
        ("t20260913-120100-12346", "queued"),
        ("t20260913-120200-12347", "done"),
        ("t20260913-120300-12348", "lost")]
    assert tasks[0].rc == "12" and tasks[0].host == "pansa"
    assert tasks[1].rc == "" and tasks[1].title == "Probe omo disk"
    assert tasks[2].terminal is True and tasks[0].terminal is False


def test_parse_status_all_sections():
    tasks = parse_status_all(STATUS_ALL)
    by_id = {t.id: t for t in tasks}
    assert set(by_id) == {"t20260913-120000-1", "t20260913-120000-2"}
    assert by_id["t20260913-120000-1"].host == "pansa-ts"
    assert by_id["t20260913-120000-2"].host == "air-ts"
    # unreachable host yields no rows — never phantom "all done"
    assert not [t for t in tasks if t.host == "omo-ts"]


def test_parse_status_junk_lines():
    assert parse_status("\n  garbage without status\n", host="h") == []
    assert parse_status_all("== lone ==\n") == []


def test_read_local_tasks_empty_and_broken(tmp_path):
    assert read_local_tasks(tmp_path) == []
    (tmp_path / "t1").mkdir()
    (tmp_path / "t1" / "meta.json").write_text("{broken")
    (tmp_path / "t2").mkdir()
    (tmp_path / "t2" / "meta.json").write_text(json.dumps(
        {"id": "t2", "title": "T", "status": "timed-out",
         "engine": "shell", "rc": None, "host": "h"}))
    tasks = read_local_tasks(tmp_path)
    assert len(tasks) == 1 and tasks[0].status == "timed-out"
    assert tasks[0].terminal is True and tasks[0].rc == ""


def test_submit_queued_parity_with_real_script(tmp_path):
    """Cockpit submit ↔ queue status parity, against the REAL script.

    Isolated via HOME override (STATE respects $HOME); engine=shell queued
    only — nothing executes, nothing leaves tmp.
    """
    if not os.path.exists(SCRIPT):
        pytest.skip("agent-task.sh not present")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = dict(os.environ, HOME=str(home))
    tid = submit_queued("echo parity", title="Parity probe", engine="shell",
                        cwd=str(tmp_path), env=env)
    assert tid.startswith("t")
    out = subprocess.run([SCRIPT, "status"], capture_output=True, text=True,
                         timeout=60, env=env).stdout
    rows = parse_status(out)
    assert [t.id for t in rows] == [tid]
    assert rows[0].status == "queued" and rows[0].title == "Parity probe"
    direct = read_local_tasks(home / ".local/state/randomesh/tasks")
    assert len(direct) == 1 and direct[0].id == tid
    assert direct[0].as_dict()["terminal"] is False
