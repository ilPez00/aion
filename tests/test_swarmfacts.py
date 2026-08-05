"""The values a run turns on, as values.

A step's result used to be a page of prose, and the base URL inside it was as
likely to be past the truncation cut as anything else. These tests are about
the small number of things a downstream step must receive exactly.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aion.swarm import SwarmOrchestrator  # noqa: E402
from aion.swarmfacts import instruction, note, parse, render  # noqa: E402
from aion.swarmrun import SwarmRunner  # noqa: E402


# ── reading them out of an output ───────────────────────────────────────────

def test_a_stated_value_is_read_back_exactly():
    assert parse("FACT api_base=https://api.example.com") == {
        "api_base": "https://api.example.com"}


def test_prose_around_the_line_does_not_disturb_it():
    """The line lives in a page of reasoning, which is the whole situation."""
    out = ("I looked at three services and the second one responds.\n"
           "FACT api_base=https://api.example.com\n"
           "It needs a token in the header.")
    assert parse(out) == {"api_base": "https://api.example.com"}


def test_a_colon_reads_the_same_as_an_equals():
    assert parse("FACT port: 8080") == {"port": "8080"}


def test_a_markdown_bullet_still_counts():
    """A harness that formats its output must not silently stop producing
    facts — the operator has no way to see that it did."""
    assert parse("- FACT path=docs/api.md")["path"] == "docs/api.md"
    assert parse("> AION_FACT path=docs/api.md")["path"] == "docs/api.md"


def test_a_value_in_backticks_is_the_value_not_the_backticks():
    assert parse("FACT path=`docs/api.md`")["path"] == "docs/api.md"
    assert parse('FACT name="the thing"')["name"] == "the thing"


def test_a_correction_later_in_the_output_wins():
    """A step that noticed its own mistake has noticed it; preferring the
    first reading preserves exactly the value it retracted."""
    assert parse("FACT port=80\nno, wrong\nFACT port=8080") == {"port": "8080"}


def test_prose_that_merely_mentions_a_fact_is_not_one():
    assert parse("I could emit a FACT here if you wanted one.") == {}
    assert parse("factory=running") == {}


def test_an_empty_value_is_not_a_fact():
    assert parse("FACT token=") == {}


def test_output_with_no_facts_is_no_facts_not_an_error():
    assert parse("just some prose") == {}
    assert parse("") == {}


def test_a_fact_buried_mid_line_in_a_blob_is_prose():
    """Anchored, not length-limited: a long line that really does start with
    FACT states a long value, and the answer to that is to clip the value."""
    assert parse("x" * 3000 + " FACT a=b") == {}


def test_the_number_of_facts_is_bounded():
    """They ride in every downstream prompt uncompressed, so a step could
    otherwise use them to crowd out its successor's context."""
    out = "\n".join(f"FACT k{i}=v{i}" for i in range(200))
    assert len(parse(out)) <= 24


def test_a_long_value_is_clipped_rather_than_carried_whole():
    assert len(parse("FACT blob=" + "x" * 5000)["blob"]) <= 300


# ── handing them downstream ─────────────────────────────────────────────────

class FakeAgent:
    def __init__(self, name, facts):
        self.name, self.facts = name, facts


def test_the_note_names_the_step_that_stated_each_value():
    """Two upstream steps each stating `path` is the normal case. Merging them
    into one namespace picks a winner silently, which is how a swarm produces
    a confidently wrong answer."""
    text = note([FakeAgent("scout", {"path": "a.md"}),
                 FakeAgent("draft", {"path": "b.md"})])
    assert "scout.path = a.md" in text and "draft.path = b.md" in text


def test_no_upstream_values_means_no_block_at_all():
    assert note([]) == ""
    assert note([FakeAgent("scout", {})]) == ""


def test_the_instruction_says_how_to_state_one():
    assert "FACT key=value" in instruction()


# ── on screen ───────────────────────────────────────────────────────────────

def test_values_render_for_the_panel():
    assert "api_base" in render({"api_base": "https://x.example.com"})


def test_nothing_stated_renders_nothing():
    assert render({}) == ""


def test_a_long_value_is_shortened_on_screen_only():
    out = render({"k": "y" * 200})
    assert "…" in out and "y" * 200 not in out


# ── through the runner ──────────────────────────────────────────────────────

def build(output="", extra_deps=True):
    orch = SwarmOrchestrator(persist=False)
    orch.add_agent("scout", "find the API")
    if extra_deps:
        orch.add_checked("writer", "write it up", deps=["scout"])
    prompts = []

    def spawn(agent, prompt):
        prompts.append((agent.name, prompt))
        return f"t{len(prompts)}"

    return orch, SwarmRunner(orch, spawn=spawn, max_parallel=4), prompts


def test_finishing_a_step_records_what_it_stated():
    orch, r, _ = build()
    r.pump()
    r.finish(orch.agent_by_name("scout").id,
             "found it\nFACT api_base=https://api.example.com")
    assert orch.agent_by_name("scout").facts["api_base"] == \
        "https://api.example.com"


def test_a_downstream_prompt_carries_the_value_verbatim():
    orch, r, prompts = build()
    r.pump()
    r.finish(orch.agent_by_name("scout").id, "FACT api_base=https://x.test")
    r.pump()
    writer = [p for n, p in prompts if n == "writer"][0]
    assert "scout.api_base = https://x.test" in writer


def test_a_value_survives_an_upstream_output_too_long_to_include():
    """The reason this exists: the budget clips prose by character count, and
    the one line that mattered is as likely to be past the cut as anything."""
    orch, r, prompts = build()
    r.pump()
    r.finish(orch.agent_by_name("scout").id,
             "FACT api_base=https://x.test\n" + "padding. " * 5000)
    r.pump()
    writer = [p for n, p in prompts if n == "writer"][0]
    assert "truncated" in writer                       # prose WAS cut
    assert "scout.api_base = https://x.test" in writer  # the value was not


def test_a_step_something_depends_on_is_asked_to_state_values():
    orch, r, prompts = build()
    r.pump()
    scout = [p for n, p in prompts if n == "scout"][0]
    assert "FACT key=value" in scout


def test_a_leaf_step_is_not_asked_for_values_nobody_reads():
    orch, r, prompts = build(extra_deps=False)
    r.pump()
    assert "FACT key=value" not in prompts[0][1]


def test_stated_values_survive_a_checkpoint():
    orch, r, _ = build()
    r.pump()
    r.finish(orch.agent_by_name("scout").id, "FACT port=8080")
    from aion.swarm import SwarmAgent
    back = SwarmAgent.from_record(orch.agent_by_name("scout").as_record())
    assert back.facts == {"port": "8080"}


def test_the_display_shape_carries_them_so_the_browser_can_show_them():
    orch, r, _ = build()
    r.pump()
    r.finish(orch.agent_by_name("scout").id, "FACT port=8080")
    rows = r.status()["agents"]
    assert [a for a in rows if a["name"] == "scout"][0]["facts"] == \
        {"port": "8080"}
