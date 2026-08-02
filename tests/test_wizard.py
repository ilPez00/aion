"""The setup wizard's decisions — above all, what it does to ~/.env.

`_wizard_finish` rewrites the file holding every API key the user owns. It had
no test and used `write_text`, which truncates before writing: a crash or a
full disk between the two left an empty file and no keys. These are the cases
that file is actually in, because people hand-edit it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aion.ui.wizard import (  # noqa: E402
    FAILED, FOUND, install_advice, install_result, key_preview, merge_env,
    next_action, parse_env, write_env,
)


# ── reading ──────────────────────────────────────────────────────────────────
def test_comments_and_blanks_are_not_variables():
    env = parse_env("# a note\n\nOPENAI_API_KEY=abc\n")
    assert env == {"OPENAI_API_KEY": "abc"}


def test_a_value_containing_an_equals_sign_survives():
    """Base64 and connection strings both contain '='. Splitting on every one
    would silently truncate the key."""
    assert parse_env("TOKEN=a=b=c")["TOKEN"] == "a=b=c"


def test_whitespace_around_the_assignment_is_stripped():
    assert parse_env("  KEY = value  \n")["KEY"] == "value"


# ── merging ──────────────────────────────────────────────────────────────────
def test_an_existing_key_is_replaced_where_it_stands():
    """Ordering and grouping in this file is built up by hand. Appending a
    second OPENAI_API_KEY and leaving the first would be worse: which one wins
    depends on whoever reads it."""
    out = merge_env("A=1\nOPENAI_API_KEY=old\nB=2\n", {"OPENAI_API_KEY": "new"})
    assert out == "A=1\nOPENAI_API_KEY=new\nB=2\n"


def test_a_new_key_is_appended():
    out = merge_env("A=1\n", {"B": "2"})
    assert out == "A=1\nB=2\n"


def test_comments_and_spacing_are_preserved():
    """A wizard that strips a user's comments out of their own credentials
    file has overstepped."""
    original = "# provider keys\n\nA=1\n\n# scratch\nB=2\n"
    out = merge_env(original, {"A": "9"})
    assert "# provider keys" in out and "# scratch" in out
    assert out.count("\n\n") == original.count("\n\n")


def test_an_empty_value_is_not_written():
    """`KEY=` shadows a real value exported elsewhere in the shell, so an
    empty answer must leave the file alone rather than blank the key."""
    assert merge_env("A=1\n", {"A": "", "B": ""}) == "A=1\n"


def test_a_commented_out_key_is_left_commented():
    """Someone disabled that line on purpose."""
    out = merge_env("#A=1\n", {"A": "2"})
    assert "#A=1" in out and out.strip().endswith("A=2")


def test_writing_to_a_file_that_does_not_exist_yet():
    assert merge_env("", {"A": "1"}) == "A=1\n"


def test_nothing_to_write_produces_nothing():
    assert merge_env("", {}) == ""


def test_a_duplicate_key_in_the_file_is_only_replaced_once():
    """A hand-edited file can hold the same key twice. Replacing both would
    change behaviour for whichever reader takes the last one."""
    out = merge_env("A=1\nA=2\n", {"A": "9"})
    assert out == "A=9\nA=2\n"


# ── writing ──────────────────────────────────────────────────────────────────
def test_write_env_replaces_the_file(tmp_path):
    p = tmp_path / ".env"
    p.write_text("OLD=1\n")
    write_env(p, "NEW=2\n")
    assert p.read_text() == "NEW=2\n"


def test_the_original_survives_a_failed_write(tmp_path, monkeypatch):
    """The whole reason this is not `write_text`: that opens with O_TRUNC, so
    the keys are gone before a byte of the replacement lands."""
    p = tmp_path / ".env"
    p.write_text("OPENAI_API_KEY=precious\n")

    def boom(*a, **k):
        raise OSError("no space left on device")

    monkeypatch.setattr(Path, "write_text", boom)
    with pytest.raises(OSError):
        write_env(p, "NEW=2\n")
    assert p.read_text() == "OPENAI_API_KEY=precious\n"


def test_no_temporary_file_is_left_behind(tmp_path):
    p = tmp_path / ".env"
    write_env(p, "A=1\n")
    assert not [f.name for f in tmp_path.iterdir() if f.name.endswith(".tmp")]


# ── showing a secret ─────────────────────────────────────────────────────────
def test_a_key_preview_never_shows_the_whole_key():
    secret = "sk-" + "x" * 40
    shown = key_preview(secret)
    assert secret not in shown and shown.startswith("sk-") and len(shown) < 20


def test_a_short_value_is_still_not_shown_in_full():
    """Short does not mean harmless — a PIN is short."""
    assert key_preview("hunter2") == "hu…"


def test_no_value_previews_as_nothing():
    assert key_preview("") == ""


# ── step transitions ─────────────────────────────────────────────────────────
def test_enter_advances_on_a_plain_step():
    assert next_action("info", "", False) == "advance"
    assert next_action("env", "", False) == "advance"


def test_a_missing_binary_offers_to_install_it():
    assert next_action("install", "missing", False) == "install"


def test_enter_does_nothing_while_an_install_is_running():
    """Otherwise a second Enter walks past work still in flight and the wizard
    reports on a step it never finished."""
    assert next_action("install", "installing", False) == "wait"


@pytest.mark.parametrize("status", ["found", "skipped", "failed"])
def test_a_settled_step_moves_on(status):
    assert next_action("install", status, False) == "advance"


def test_finding_the_binary_beats_a_stale_status():
    """Installed in another terminal while the wizard sat open."""
    assert next_action("install", "missing", True) == "advance"


# ── install outcome ──────────────────────────────────────────────────────────
def test_a_clean_exit_without_the_binary_is_a_failure():
    """npm exits 0 having installed something PATH cannot see often enough
    that the exit code alone reports success for a binary that is not there."""
    assert install_result(0, present=False) == FAILED


def test_success_needs_both_conditions():
    assert install_result(0, present=True) == FOUND
    assert install_result(1, present=True) == FAILED


# ── advice ───────────────────────────────────────────────────────────────────
def test_a_step_with_its_own_installer_gets_that_command():
    assert "install.sh" in install_advice({"install_cmd": ["x"]})


def test_everything_else_falls_back_to_npm():
    assert install_advice({"pkg": "opencode"}) == "npm install -g opencode"
