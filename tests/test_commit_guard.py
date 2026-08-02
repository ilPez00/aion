"""The pre-commit guard: what must never reach a commit.

AGENTS.md says "Never `git add -A`. Stage explicit paths." A hook cannot see
how the index was built, so these test the SHAPE a blind add-all leaves behind
— unreviewed paths, credential filenames, blobs, keys in added lines — and, as
importantly, that ordinary work is not refused. A guard that fires on normal
commits gets bypassed by reflex, which is worse than no guard at all.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from commit_guard import (  # noqa: E402
    bulk_problem,
    content_problems, path_problem, report, size_problem,
)


# ── paths ────────────────────────────────────────────────────────────────────
def test_project_paths_are_allowed():
    for p in ("src/aion/store.py", "tests/test_swarm_store.py", "plan.md",
              "docs/IDENTITY.md", "config/layout.json", "scripts/static/hud.js",
              ".github/workflows/ci.yml", "static/index.html"):
        assert path_problem(p) == "", p


def test_a_path_outside_the_layout_is_refused():
    """The signature of `git add -A`: something from the working tree that was
    never part of the project."""
    why = path_problem("worker2.py")
    assert why and "project layout" in why


def test_a_stray_directory_is_refused():
    assert path_problem("scratch/notes/experiment.py") != ""


def test_credential_filenames_are_refused_even_in_allowed_dirs():
    for p in ("config/.env", "config/.env.local", "src/aion/id_rsa",
              "scripts/server.pem", "config/credentials.json",
              "docs/secrets.yaml", "src/aion/aion.sqlite"):
        assert path_problem(p) != "", p


def test_the_word_key_alone_is_not_enough_to_refuse():
    """`keys.py`, `keybindings.json` and `apikey_test.py` are ordinary source.
    Refusing them teaches people to pass --no-verify."""
    for p in ("src/aion/keys.py", "config/keybindings.json",
              "tests/test_api_keys.py"):
        assert path_problem(p) == "", p


# ── content ──────────────────────────────────────────────────────────────────
def test_a_real_looking_key_in_an_added_line_is_caught():
    diff = "+++ b/src/aion/llm.py\n+API = 'sk-" + "a" * 40 + "'\n"
    assert [r for r, _ in content_problems(diff)] == ["OpenAI key"]


def test_removing_a_leaked_key_is_not_blocked():
    """A diff that DELETES a key is the fix. Blocking it is the exact opposite
    of what the guard is for."""
    diff = "--- a/src/aion/llm.py\n-API = 'AIza" + "b" * 35 + "'\n"
    assert content_problems(diff) == []


def test_the_dummy_keys_in_the_env_tests_do_not_trip_it():
    """test_env.py's fixture is full of placeholders. If they trip the guard,
    the file can never be tracked — which is the state we are undoing."""
    fixture = ROOT / "tests" / "test_env.py"
    if fixture.exists():
        diff = "".join(f"+{line}\n" for line in
                       fixture.read_text().splitlines())
        assert content_problems(diff) == []


def test_placeholders_of_the_right_shape_but_wrong_length_pass():
    assert content_problems("+key = 'sk-abc123def456'\n") == []
    assert content_problems("+key = 'AIzaSyDummyKey'\n") == []


def test_a_private_key_block_is_caught():
    diff = ("+-----BEGIN OPENSSH PRIVATE KEY-----\n"
            "+" + "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAAB" + "\n"
            "+-----END OPENSSH PRIVATE KEY-----\n")
    assert [r for r, _ in content_problems(diff)] == ["private key block"]


def test_the_header_alone_is_not_a_key():
    """A private key is a header AND a body. Matching the header on its own
    flags every doc that mentions the format and every test that asserts on
    it — this guard refused its own test file the first time it ran."""
    assert content_problems("+    assert '-----BEGIN RSA PRIVATE KEY-----' in x\n") == []


def test_the_match_is_truncated_in_the_report():
    """The refusal is printed to a terminal and may be pasted into a bug
    report. Echoing the whole key back would leak it a second way."""
    secret = "AKIA" + "Z" * 16
    (_, shown), = content_problems(f"+aws = '{secret}'\n")
    assert secret not in shown and shown.endswith("…")


# ── size ─────────────────────────────────────────────────────────────────────
def test_a_large_blob_is_refused():
    assert size_problem("docs/dump.bin", 5_000_000) != ""


def test_an_ordinary_source_file_is_not():
    assert size_problem("src/aion/store.py", 90_000) == ""


# ── the whole report ─────────────────────────────────────────────────────────
def test_a_normal_commit_is_allowed():
    assert report(["src/aion/swarm.py", "tests/test_swarmctl.py"],
                  "+def can(self):\n+    return True\n",
                  {"src/aion/swarm.py": 20_000}) == []


def test_every_problem_is_reported_not_just_the_first():
    """One refusal at a time means three commit attempts to learn three
    things."""
    problems = report(["junk.bin", "config/.env"],
                      "+token = 'ghp_" + "c" * 36 + "'\n",
                      {"junk.bin": 9_000_000})
    assert len(problems) == 4


# ── bulk ─────────────────────────────────────────────────────────────────────
# The allowlist cannot catch the incident this repo actually had: another
# agent's untracked WIP lives under `src/`, so a sweep of it passes every path
# rule. Bulk is the shape that distinguishes a sweep from a cycle.
def test_a_handful_of_new_files_is_a_normal_cycle():
    assert bulk_problem([f"src/aion/mod{i}.py" for i in range(8)]) == ""


def test_a_sweep_of_new_files_is_refused():
    why = bulk_problem([f"src/aion/agents/f{i}.py" for i in range(30)])
    assert "30 new files" in why and "sweep" in why


def test_the_refusal_names_files_so_you_can_see_what_it_caught():
    why = bulk_problem(["src/aion/agents/node.py"] +
                       [f"src/x{i}.py" for i in range(20)])
    assert "src/aion/agents/node.py" in why


def test_modifying_many_files_is_not_a_sweep():
    """A rename or a refactor touches plenty of files. Only ADDITIONS carry
    the risk of sweeping in something nobody has read."""
    assert report([f"src/aion/mod{i}.py" for i in range(40)], "", None, []) == []
