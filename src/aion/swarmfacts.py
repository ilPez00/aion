"""swarmfacts.py — the values a step produces, apart from the prose about them.

A step hands its successors one thing: stdout, spliced into the next prompt.
`swarmio` fixed half of what that misses — where the work LANDED — but not the
other half: the small number of VALUES a run turns on. The base URL the scout
found. The migration id. The port the server came up on. Those arrive buried in
a page of reasoning, and three things go wrong with them:

    they get truncated       the upstream budget clips prose by character
                             count, and the one line that mattered is as
                             likely to be past the cut as anything else
    they get re-derived      a downstream agent re-reads the prose and
                             re-decides what the value was, differently
    nothing else can see     the cockpit, the HUD and any policy can only
                             show "produced 4kB of output"

So a step may state a value outright, one per line:

    FACT api_base=https://api.example.com

`parse()` pulls those out of the output. They are then carried WHOLE into every
downstream prompt, in their own block, exempt from the prose budget — which is
the actual point: the sentence explaining a value is compressible, the value is
not. Keys are qualified by the step that stated them (`scout.api_base`), because
two upstream steps naming a thing `path` is the normal case, and silently
letting one win is how a swarm produces a confidently wrong answer.

Line-oriented on purpose. Asking a CLI harness for a JSON block gets valid JSON
most of the time, and "most of the time" over a hundred steps is a parser that
fails for reasons no one can reproduce. A line either matches or is prose.

Nothing here is required: a swarm whose steps state no facts behaves exactly as
before, and a step is only ASKED for them when something downstream would read
them.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

__all__ = ["parse", "note", "instruction", "render", "MAX_FACTS"]

# Bounds. Facts ride in every downstream prompt uncompressed, so they are the
# one part of the handoff that a step could use to crowd out its own context.
MAX_FACTS = 24
MAX_KEY = 48
MAX_VALUE = 300

# `FACT key=value`, `AION_FACT key: value`, leading bullet or blockquote marks
# allowed — a harness that formats its output as markdown should not silently
# stop producing facts. Trailing backticks/commas are stripped as punctuation,
# since a value wrapped in code fences is the single most common shape.
_FACT = re.compile(
    r"^[\s>*\-•]*(?:AION_)?FACT\s+([A-Za-z_][A-Za-z0-9_.\-]*)\s*[:=]\s*(.+?)\s*$",
    re.IGNORECASE)


def _clean(value: str) -> str:
    value = value.strip().strip("`").strip()
    # A value quoted by the model is a quoted value, not a value with quotes.
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value[:MAX_VALUE]


def parse(output: str, limit: int = MAX_FACTS) -> dict[str, str]:
    """The facts a step stated, in the order it stated them.

    A key stated twice keeps the LAST value: a step that corrects itself
    halfway through its output has corrected itself, and preferring the first
    reading would preserve exactly the mistake it noticed.
    """
    facts: dict[str, str] = {}
    for line in (output or "").splitlines():
        # Anchored, so a minified blob with `FACT x=y` somewhere in the middle
        # of it is prose. Length is deliberately NOT the test: a long line that
        # really does start with FACT states a long value, and the answer to
        # that is to clip the value, not to drop the fact on the floor.
        m = _FACT.match(line)
        if not m:
            continue
        key = m.group(1)[:MAX_KEY]
        value = _clean(m.group(2))
        if not value:
            continue
        facts.pop(key, None)          # restate = move to the end, not just win
        facts[key] = value
        if len(facts) > limit:
            facts.pop(next(iter(facts)))
    return facts


def note(upstream: Sequence) -> str:
    """The block naming every value the steps this one depends on produced.

    Qualified by step, and deliberately not merged into one flat namespace:
    two upstream steps each stating `path` is normal, and picking a winner
    silently is worse than showing both.
    """
    lines = []
    for agent in upstream:
        facts = getattr(agent, "facts", None) or {}
        name = getattr(agent, "name", "") or "?"
        for key, value in facts.items():
            lines.append(f"- {name}.{key} = {value}")
    if not lines:
        return ""
    return "Values stated by the steps this depends on:\n" + "\n".join(lines)


def instruction() -> str:
    """What to tell a step whose output something else will read.

    Only added when the step HAS dependents. Asking a leaf step to publish
    values nothing can read is noise in a prompt that has better uses for the
    room, and it trains the operator to ignore the block.
    """
    return ("If you produce a value a later step needs (a path, id, URL, port, "
            "name), state it on its own line as `FACT key=value` — one per "
            "line, short values only. Explain in prose as usual; the FACT "
            "lines are what survives into the next step's prompt intact.")


def render(facts: dict, theme: dict | None = None, limit: int = 8) -> str:
    """The values on screen, for the panel that shows a finished step."""
    from .workflows import _theme

    th = _theme(theme)
    if not facts:
        return ""
    lines = []
    for key, value in list(facts.items())[:limit]:
        shown = value if len(value) <= 40 else value[:37] + "…"
        lines.append(f"  [{th['dim']}]{key[:18]:18s}[/] {shown}")
    more = len(facts) - limit
    if more > 0:
        lines.append(f"  [{th['dim']}]+{more} more[/]")
    return "\n".join(lines)
