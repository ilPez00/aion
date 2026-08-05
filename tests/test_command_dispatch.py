"""Two dispatcher branches that could not fire.

`_run_command` starts with `parts = text.split(" ", 1)`, so `parts` is never
longer than two. Two branches were written against a list that had been split
on every space:

    run <harness> <prompt>    tested `len(parts) >= 3`, read `parts[1]` and
                              `parts[2]`. Never true, so the command fell
                              through to the final fallback — which spawns the
                              ACTIVE harness with the whole line. `run claude
                              explain X` ran on whatever was selected, with
                              "run claude " still glued to the prompt.
                              `_agent_run_tool` emits exactly this form, so
                              the model's only way to choose a harness quietly
                              did nothing.
    setup set KEY VAL         tested `len(parts) >= 4`. Never true either, so
                              it fell to the scope parser and printed a usage
                              line. The env writer behind it had never run.

Both are the same mistake and neither is visible by reading the branch alone —
you have to hold the split from 100 lines earlier in your head. So these tests
go through `_run_command` with real text rather than calling the helpers.
"""

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aion.core import Bus, load_config  # noqa: E402
from aion.store import Store  # noqa: E402


@pytest.fixture()
def store():
    s = Store(cfg=load_config(), bus=Bus())
    s.harnesses = {"demo": object(), "claude": object()}
    s.state.active_harness = "demo"
    s.spawned = []

    async def fake_spawn(hid, prompt):
        s.spawned.append((hid, prompt))
    s._spawn = fake_spawn
    return s


def run(store, text):
    asyncio.run(store._run_command(text, _interpreted=True))
    return store.spawned


# ── run <harness> <prompt> ──────────────────────────────────────────────────

def test_run_uses_the_harness_it_was_given():
    """The whole point of naming one. It was going to the active harness."""
    s = Store(cfg=load_config(), bus=Bus())
    s.harnesses = {"demo": object(), "claude": object()}
    s.state.active_harness = "demo"
    s.spawned = []

    async def fake(hid, prompt):
        s.spawned.append((hid, prompt))
    s._spawn = fake
    assert run(s, "run claude explain recursion") == [
        ("claude", "explain recursion")]


def test_the_command_prefix_does_not_end_up_in_the_prompt(store):
    """It was spawning with the literal text, so every prompt the agent sent
    began with "run <harness> " — inside the prompt, not around it."""
    hid, prompt = run(store, "run claude write a haiku")[0]
    assert prompt == "write a haiku"
    assert "run" not in prompt.split()


def test_an_unknown_harness_is_not_silently_run_somewhere_else(store):
    """Falling back is fine; falling back while looking like it obeyed is
    not. The fallback keeps the text intact so the log shows what was typed."""
    assert run(store, "run nosuch do a thing") == [
        ("demo", "run nosuch do a thing")]


def test_run_with_no_prompt_is_not_an_empty_task(store):
    assert run(store, "run claude") == [("demo", "run claude")]


def test_the_bare_harness_form_still_works(store):
    assert run(store, "demo hello there") == [("demo", "hello there")]


def test_the_agent_tool_emits_a_form_the_dispatcher_understands(store):
    """`_agent_run_tool` builds `run {hid} {prompt}`. That string is the
    contract between the model's tool surface and this dispatcher, and it was
    broken on the dispatcher side for as long as both existed."""
    store._emit = lambda intent: None
    out = store._agent_run_tool("claude", "summarise the repo")
    assert "unknown harness" not in out
    assert run(store, "run claude summarise the repo") == [
        ("claude", "summarise the repo")]


# ── setup set KEY VAL ───────────────────────────────────────────────────────

@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A fake HOME. `Path.home()` reads $HOME, and this test writes a file
    that on a real machine holds provider API keys."""
    monkeypatch.setenv("HOME", str(tmp_path))
    assert Path.home() == tmp_path
    return tmp_path


def test_setup_set_writes_the_key(store, home):
    run(store, "setup set openai_api_key sk-test-123")
    assert (home / ".env").read_text() == "OPENAI_API_KEY=sk-test-123\n"


def test_setup_set_replaces_rather_than_appends(store, home):
    (home / ".env").write_text("OPENAI_API_KEY=old\nOTHER=keep\n")
    run(store, "setup set openai_api_key new")
    body = (home / ".env").read_text()
    assert "OPENAI_API_KEY=new" in body
    assert "OPENAI_API_KEY=old" not in body
    assert "OTHER=keep" in body


def test_a_pre_existing_duplicate_is_collapsed(store, home):
    """Two lines for one key leaves which value wins up to whoever parses it."""
    (home / ".env").write_text("K=one\nK=two\n")
    run(store, "setup set k three")
    assert (home / ".env").read_text().count("K=") == 1


def test_a_value_containing_spaces_survives(store, home):
    run(store, "setup set note hello there world")
    assert (home / ".env").read_text() == "NOTE=hello there world\n"


def test_comments_and_blank_lines_are_left_alone(store, home):
    (home / ".env").write_text("# keys\n\nA=1\n")
    run(store, "setup set b 2")
    body = (home / ".env").read_text()
    assert body.startswith("# keys\n\nA=1\n")
    assert "B=2" in body


def test_the_file_is_not_world_readable(store, home):
    """It holds API keys. This is the first version of this code that ever
    actually created the file."""
    run(store, "setup set openai_api_key sk-test")
    assert (home / ".env").stat().st_mode & 0o077 == 0


def test_the_value_is_never_logged(store, home):
    """Logs are on screen, published to the HUD, and kept for 50 lines."""
    run(store, "setup set openai_api_key sk-secret-value")
    assert not any("sk-secret-value" in line for line in store.state.logs)
    assert any("OPENAI_API_KEY" in line for line in store.state.logs)


def test_setup_without_set_still_reaches_the_scope_parser(store, home):
    run(store, "setup nonsense")
    assert any("usage: setup" in line for line in store.state.logs)
    assert not (home / ".env").exists()
