"""swarmio.py — what a step writes, and who else is writing it.

Everything a step passed downstream went through one channel: its stdout,
spliced into the next prompt as text. That is fine for "here is what I found"
and useless for the thing agents actually do, which is WRITE FILES. Two steps
in the same wave editing `docs/api.md` is not a merge conflict — there is no
merge, no branch and no lock. It is one of them silently losing.

So a step may declare what it writes:

    swarm add writer "draft the API page" >> docs/api.md

and this module answers the two questions that declaration makes possible:

  * `conflicts()` — which pairs of steps write the same path with NO ordering
    between them. Not "which pairs share a path": a writer that depends on
    another writer is a sequence, which is normal and fine. Only unordered
    writers race, and finding them needs reachability over the DAG, not a
    set intersection.
  * `blocking_writes()` — the same question at admission time, for the runner:
    a step must not start while something already running writes a path it
    writes. That turns a race into a queue, which is what the operator would
    have done by hand.

Paths are normalised, not resolved: `./docs/api.md` and `docs/api.md` are the
same declaration, but nothing here touches a filesystem. This module is pure —
it never opens, stats, or globs anything, so it behaves identically on a
machine where none of those paths exist yet, which is the normal case when the
DAG is still being planned.
"""

from __future__ import annotations

import posixpath
from collections.abc import Sequence

__all__ = ["writes_of", "normalise", "conflicts", "blocking_writes",
           "artifact_note"]

# A declaration list long enough to be a directory listing is a step that has
# not decided what it writes, and it would make every conflict check useless.
MAX_WRITES = 20


def normalise(path: str) -> str:
    """One spelling per path. Textual only — nothing is resolved on disk.

    `posixpath.normpath` collapses `./` and `a/../b`. Trailing slashes go, so
    a directory declared two ways is one declaration. Case is left alone: two
    of aion's three target platforms are case-sensitive, and folding it here
    would invent conflicts that do not exist on the machine that matters.
    """
    p = str(path or "").strip()
    if not p:
        return ""
    p = posixpath.normpath(p.replace("\\", "/"))
    return p.rstrip("/") or "/"


def writes_of(agent) -> list[str]:
    """Normalised, de-duplicated paths a step declares it writes."""
    raw = getattr(agent, "writes", None)
    if raw is None and isinstance(agent, dict):
        raw = agent.get("writes")
    out: list[str] = []
    for p in (raw or [])[:MAX_WRITES]:
        n = normalise(p)
        if n and n not in out:
            out.append(n)
    return out


def _name(agent) -> str:
    if isinstance(agent, dict):
        return str(agent.get("name") or agent.get("id") or "?")
    return str(getattr(agent, "name", "") or getattr(agent, "id", "") or "?")


def _deps(agent) -> list[str]:
    if isinstance(agent, dict):
        raw = agent.get("deps") or agent.get("dependencies") or []
    else:
        raw = getattr(agent, "dependencies", None) or []
    return [str(d) for d in raw]


def _ancestors(agents: Sequence) -> dict[str, set[str]]:
    """Every step each step transitively waits for.

    Iterative rather than recursive: a cycle in a hand-edited checkpoint must
    not blow the stack in a function whose whole job is safety checking.
    """
    by_name = {_name(a): a for a in agents}
    out: dict[str, set[str]] = {}
    for a in agents:
        seen: set[str] = set()
        stack = list(_deps(a))
        while stack:
            d = stack.pop()
            if d in seen or d not in by_name:
                continue
            seen.add(d)
            stack.extend(_deps(by_name[d]))
        out[_name(a)] = seen
    return out


def conflicts(agents: Sequence) -> list[dict]:
    """Pairs of steps writing the same path with no ordering between them.

    Ordered writers are a sequence and perfectly normal — `edit` writing what
    `draft` wrote is the point of depending on it. What is never intended is
    two steps that may run at the same time, or in either order, both writing
    one file: the result depends on scheduling, which is the definition of a
    race.
    """
    items = list(agents)
    anc = _ancestors(items)
    writers: dict[str, list[str]] = {}
    for a in items:
        for path in writes_of(a):
            writers.setdefault(path, []).append(_name(a))

    out: list[dict] = []
    for path, names in sorted(writers.items()):
        for i, first in enumerate(names):
            for second in names[i + 1:]:
                if second in anc.get(first, ()) or first in anc.get(second, ()):
                    continue        # one waits for the other: a sequence
                out.append({"path": path, "steps": sorted((first, second))})
    return out


def blocking_writes(agent, running: Sequence) -> list[dict]:
    """Paths this step writes that something already running also writes.

    Admission-time half of `conflicts`. A step held back for this is not
    blocked in the DAG sense — it is next, as soon as the other writer is
    done — so callers report it as deferred rather than refused.
    """
    mine = set(writes_of(agent))
    if not mine:
        return []
    out = []
    for other in running:
        if _name(other) == _name(agent):
            continue
        shared = sorted(mine & set(writes_of(other)))
        if shared:
            out.append({"step": _name(other), "paths": shared})
    return out


def artifact_note(upstream: Sequence) -> str:
    """One line per upstream step listing what it wrote, for the prompt.

    Splicing stdout tells a downstream agent what its input SAID. It does not
    tell it where the work landed, so an agent asked to "polish the draft"
    would go looking for a file it was never told the name of — or write a
    second one beside it.
    """
    lines = []
    for a in upstream:
        paths = writes_of(a)
        if paths:
            lines.append(f"- {_name(a)} wrote: {', '.join(paths)}")
    if not lines:
        return ""
    return "Files written by the steps this depends on:\n" + "\n".join(lines)
