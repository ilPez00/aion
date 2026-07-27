"""Worktree engine — porcelain parsing, repo discovery, task linking."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from aion import worktrees as wt  # noqa: E402

needs_git = pytest.mark.skipif(not shutil.which("git"), reason="git not installed")

PORCELAIN = """\
worktree /home/gio/dev/aion
HEAD b6f04cc247c295e0647cf9ff2291368d09d0fdd8
branch refs/heads/freerouting-omniroute

worktree /home/gio/aion-slice4
HEAD af89f51d61e965c9ff5b15b37b2cb5f58b599558
branch refs/heads/slice/tui-pane
prunable il file gitdir punta a un percorso non esistente

worktree /home/gio/detached
HEAD 1234567890abcdef1234567890abcdef12345678
detached

worktree /home/gio/bare.git
bare
"""


# ── parsing ──────────────────────────────────────────────────────────────
def test_parses_every_record():
    got = wt.parse_worktree_list(PORCELAIN)
    assert [w.name for w in got] == ["aion", "aion-slice4", "detached", "bare.git"]


def test_first_record_is_the_main_worktree():
    got = wt.parse_worktree_list(PORCELAIN)
    assert got[0].is_main is True
    assert all(not w.is_main for w in got[1:])


def test_branch_ref_is_shortened():
    got = wt.parse_worktree_list(PORCELAIN)
    assert got[0].branch == "freerouting-omniroute"
    assert got[1].branch == "slice/tui-pane"     # keeps the slash


def test_flags_are_read_as_keys_not_messages():
    """`prunable`'s reason is localised — this box reports git in Italian."""
    got = wt.parse_worktree_list(PORCELAIN)
    assert got[1].prunable is True
    assert got[2].detached is True and got[2].branch == ""
    assert got[3].bare is True


def test_unknown_keys_are_ignored_not_fatal():
    """A newer git adding an attribute must not break the parse."""
    got = wt.parse_worktree_list(
        "worktree /x\nHEAD abc\nbranch refs/heads/m\nquantum entangled\n")
    assert len(got) == 1 and got[0].branch == "m"


def test_empty_input_is_empty_output():
    assert wt.parse_worktree_list("") == []


def test_a_stray_key_before_any_worktree_is_dropped():
    assert wt.parse_worktree_list("HEAD abc\nbranch refs/heads/x\n") == []


# ── state ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("kw,expected", [
    ({}, "clean"),
    ({"dirty": 3}, "dirty"),
    ({"detached": True}, "detached"),
    ({"locked": True}, "locked"),
    ({"prunable": True}, "prunable"),
    ({"prunable": True, "dirty": 5}, "prunable"),   # most urgent wins
])
def test_state_summarises_by_urgency(kw, expected):
    assert wt.Worktree(path="/x", **kw).state == expected


# ── discovery ────────────────────────────────────────────────────────────
def test_find_repos_does_not_descend_into_a_repo(tmp_path):
    repo = tmp_path / "proj"
    (repo / ".git").mkdir(parents=True)
    (repo / "nested" / ".git").mkdir(parents=True)
    got = wt.find_repos(tmp_path)
    assert got == [repo]


def test_find_repos_skips_build_dirs(tmp_path):
    (tmp_path / "node_modules" / "pkg" / ".git").mkdir(parents=True)
    (tmp_path / "real" / ".git").mkdir(parents=True)
    assert [p.name for p in wt.find_repos(tmp_path)] == ["real"]


def test_find_repos_honours_the_cap(tmp_path):
    for i in range(10):
        (tmp_path / f"r{i}" / ".git").mkdir(parents=True)
    assert len(wt.find_repos(tmp_path, max_repos=4)) == 4


def test_find_repos_handles_a_worktree_file_not_dir(tmp_path):
    """A linked worktree has `.git` as a FILE, not a directory."""
    repo = tmp_path / "linked"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: /elsewhere/.git/worktrees/linked\n")
    assert wt.find_repos(tmp_path) == [repo]


# ── task linking ─────────────────────────────────────────────────────────
def test_links_a_task_by_full_path():
    w = wt.Worktree(path="/home/gio/dev/parser")
    tasks = [{"id": "t1", "label": "build in /home/gio/dev/parser", "log": []}]
    assert wt.link_tasks([w], tasks)[0].tasks == ["t1"]


def test_links_a_task_by_directory_name():
    w = wt.Worktree(path="/home/gio/dev/parser")
    tasks = [{"id": "t1", "label": "Factory Loop: parser", "log": []}]
    assert wt.link_tasks([w], tasks)[0].tasks == ["t1"]


def test_matches_whole_words_only():
    """A worktree called `api` must not claim every task mentioning `rapid`."""
    w = wt.Worktree(path="/home/gio/dev/api")
    tasks = [{"id": "t1", "label": "rapid prototyping", "log": []}]
    assert wt.link_tasks([w], tasks)[0].tasks == []


def test_searches_task_logs_too():
    w = wt.Worktree(path="/home/gio/dev/parser")
    tasks = [{"id": "t1", "label": "unrelated", "log": ["cd parser && make"]}]
    assert wt.link_tasks([w], tasks)[0].tasks == ["t1"]


def test_a_task_with_no_log_key_does_not_explode():
    w = wt.Worktree(path="/x/parser")
    assert wt.link_tasks([w], [{"id": "t1", "label": "parser"}])[0].tasks == ["t1"]


# ── graph ────────────────────────────────────────────────────────────────
@needs_git
def test_graph_of_a_real_repo(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, env=env, check=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@t"); run("config", "user.name", "t")
    (repo / "f.txt").write_text("hello")
    run("add", "."); run("commit", "-qm", "init")

    g = wt.graph(tmp_path, probe=True)
    assert g["summary"]["repos"] == 1
    entry = g["repos"][0]
    assert entry["error"] is None
    main = entry["worktrees"][0]
    assert main["branch"] == "main" and main["is_main"] is True and main["state"] == "clean"


@needs_git
def test_graph_reports_a_dirty_tree(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, env=env, check=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@t"); run("config", "user.name", "t")
    (repo / "f.txt").write_text("hello")
    run("add", "."); run("commit", "-qm", "init")
    (repo / "f.txt").write_text("changed")

    g = wt.graph(tmp_path, probe=True)
    assert g["repos"][0]["worktrees"][0]["state"] == "dirty"
    assert g["summary"]["dirty"] == 1


def test_a_broken_repo_is_shown_as_an_error_not_dropped(tmp_path):
    """A repo missing from the view is worse than one shown as broken."""
    broken = tmp_path / "broken"
    (broken / ".git").mkdir(parents=True)      # a .git dir with nothing in it
    g = wt.graph(tmp_path, probe=False)
    assert g["summary"]["repos"] == 1
    assert g["repos"][0]["error"]
    assert g["summary"]["errors"] == 1


def test_graph_of_an_empty_root_is_empty(tmp_path):
    g = wt.graph(tmp_path)
    assert g["repos"] == [] and g["summary"]["worktrees"] == 0


def test_probe_can_be_skipped_for_speed(tmp_path):
    (tmp_path / "r" / ".git").mkdir(parents=True)
    g = wt.graph(tmp_path, probe=False)
    assert g["summary"]["dirty"] == 0


# ── search ───────────────────────────────────────────────────────────────
def test_search_finds_repos_and_branches():
    snap = {"repos": [{"name": "parser", "path": "/dev/parser", "worktrees": [
        {"name": "parser", "branch": "feat/lexer", "state": "dirty",
         "path": "/dev/parser", "prunable": False}]}]}
    assert any(h["type"] == "repo" for h in wt.search("parser", snap))
    assert any(h["type"] == "worktree" for h in wt.search("lexer", snap))


def test_search_hits_carry_jump_coordinates():
    snap = {"repos": [{"name": "parser", "path": "/dev/parser", "worktrees": []}]}
    for h in wt.search("parser", snap):
        assert h["module"] == "repos" and h["node"]


def test_search_without_a_snapshot_is_empty():
    assert wt.search("anything", None) == []
