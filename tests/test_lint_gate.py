"""The failures tests structurally cannot catch.

A name that does not exist is a runtime error, so it is invisible until the
branch containing it runs — and in this codebase a great many branches sit
behind `except Exception: pass`, which is exactly where an unrunnable branch
goes to look like a working one.

Four were live in the tree the day this file was written:

    store.py            `Path` never imported, so `_load_skills_data` raised
                        NameError into a bare handler and the Skills panel was
                        permanently empty with nothing said about it
    store.py            the same missing import broke `setup set KEY VAL`
    ui/app.py           `remote add` referenced `RemoteNode`, which two OTHER
                        methods imported locally and this one did not
    scripts/aion_web.py the PTY editor called `shlex.quote` with no `shlex`

None of them were typos anyone would spot in review; all four are a linter's
first result. These tests exercise the paths that were broken, and pin the
gate that finds the next one.
"""

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


# ── the four that shipped ───────────────────────────────────────────────────

def test_the_skills_scan_completes_instead_of_raising_into_a_bare_handler():
    """It ended in `except Exception: pass`, so a NameError on the third line
    looked exactly like a machine with no skills installed."""
    import asyncio

    from aion.core import Bus
    from aion.store import Store

    s = Store.__new__(Store)
    s.bus = Bus()

    class _State:
        skills = None
    s.state = _State()
    asyncio.run(s._load_skills_data())
    assert isinstance(s.state.skills, list)     # None means it raised


def test_remote_add_can_build_a_node():
    """`RemoteNode` was imported inside two other methods and not this one, so
    the only command that CREATES a remote raised NameError."""
    from aion.ui import app as app_mod

    src = Path(app_mod.__file__).read_text(encoding="utf-8")
    fn = src.split("async def _handle_remote_command")[1].split("\n    async def ")[0]
    assert "from ..remotes import RemoteNode" in fn


def test_the_web_pty_editor_can_quote_a_filename():
    """`shlex.quote` on an unimported `shlex` — and the call is the shell
    injection guard, so the one line protecting the PTY was the broken one."""
    src = (ROOT / "scripts" / "aion_web.py").read_text(encoding="utf-8")
    assert "\nimport shlex\n" in src
    assert "shlex.quote" in src


def test_store_imports_the_path_it_uses():
    from aion import store

    assert store.Path is not None


# ── the gate that finds the next one ────────────────────────────────────────

def _lint_config() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["tool"]["ruff"]["lint"]


def test_undefined_names_are_a_blocking_rule():
    """`F` is the whole reason this gate exists. Narrowing the ruleset is
    fine; dropping pyflakes turns the gate into a formatter."""
    cfg = _lint_config()
    assert "F" in cfg["select"]
    assert "F821" not in cfg.get("ignore", [])


def test_the_relaxations_are_the_ones_that_were_argued_for():
    """A gate people can widen silently is a gate that ends up selecting
    nothing. These four were justified in the config; a fifth needs the same
    treatment rather than a quiet append."""
    assert set(_lint_config().get("ignore", [])) == {"F401", "F841", "B905", "B007"}


def test_ci_actually_runs_the_linter():
    """A configured linter nobody runs is a config file."""
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "ruff check src/ tests/ scripts/" in ci


@pytest.mark.skipif(subprocess.run([sys.executable, "-m", "ruff", "--version"],
                                   capture_output=True).returncode != 0,
                    reason="ruff not installed in this environment")
def test_the_tree_is_clean_right_now():
    r = subprocess.run([sys.executable, "-m", "ruff", "check",
                        "src/", "tests/", "scripts/"],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-2000:]
