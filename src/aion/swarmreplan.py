"""swarmreplan.py — a DAG that can grow from what it learns.

Until now a swarm's shape was decided once, before any of it ran. Every step's
goal had to be written by someone who did not yet know what step one would
find, which is the single biggest thing separating this from orchestration:
the plan could not react to its own results. A research step that discovers
three subsystems worth auditing had no way to say so; a human had to read the
output and type three `swarm add` lines.

This lets a finished step propose follow-up work — and then refuses most of
what comes back, for the same reason `swarmplan` does at creation time, only
more so. At creation a person is sitting there reading the plan. Here the
proposal arrives from a model, about the output of another model, while nobody
is watching. An unbounded version of this is a machine that writes its own
budget.

The bounds are the feature:

  * **Off unless asked** (`max_new_steps=0`). A swarm that never replans is
    the swarm everyone already has.
  * **Depth, not just width.** A step created by a replan carries a
    `generation`, and past `max_generations` its own children cannot replan
    again. Width alone bounds one round; generations bound the recursion, and
    the recursion is what runs up a bill overnight.
  * **A hard ceiling on the whole DAG** (`max_total_steps`), checked against
    what already exists, so ten steps each adding three cannot cooperate their
    way past it.
  * **Every new step depends on its parent.** It was proposed FROM that
    output; it must not be schedulable before it. The parent is DONE by the
    time this runs, so the edge costs nothing and buys ordering, provenance,
    and a wave view that puts the new work where it belongs.
  * **Nothing invented is accepted quietly.** A step whose dependency does not
    resolve, or whose name collides with an existing step, is dropped with a
    reason — never renamed, never silently rewired.

Pure: no orchestrator, no network, no clock. `propose()` takes an injectable
`chat`, and `validate()` takes plain dicts, so the rules are testable without
a model and without a swarm.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .swarmplan import MAX_GOAL_CHARS, NAME_OK, Step, _extract_json, topo_order

__all__ = ["ReplanPolicy", "Expansion", "validate", "propose"]

# A proposal is about ONE step's output. Anything longer than this is not a
# follow-up plan, it is a new project, and it should be typed by a person.
MAX_OUTPUT_CHARS = 4000


@dataclass(frozen=True)
class ReplanPolicy:
    """How much a swarm may grow itself, and how deep.

    Defaults are the old behaviour exactly: `max_new_steps=0` means a finished
    step proposes nothing and the DAG is as static as it ever was.
    """

    max_new_steps: int = 0        # per finished step; 0 = replanning is off
    max_total_steps: int = 24     # ceiling on the DAG, existing steps included
    max_generations: int = 2      # how deep a chain of expansions may go

    @property
    def enabled(self) -> bool:
        return self.max_new_steps > 0


@dataclass
class Expansion:
    """What a finished step wants to add, after the rules have had their say."""

    parent: str = ""
    steps: list[Step] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)   # why nothing was taken
    dropped: list[str] = field(default_factory=list)    # per-step refusals

    @property
    def ok(self) -> bool:
        return bool(self.steps) and not self.problems

    def as_dict(self) -> dict:
        return {"parent": self.parent, "ok": self.ok,
                "steps": [s.as_dict() for s in self.steps],
                "problems": list(self.problems),
                "dropped": list(self.dropped)}


def validate(raw_steps, *, parent: str, parent_generation: int,
             existing: dict[str, str], policy: ReplanPolicy,
             known_harnesses=()) -> Expansion:
    """Turn a model's suggestion into steps a swarm may actually accept.

    `existing` maps step name -> status for the whole current DAG: names are
    the dependency key, so this is both the collision check and the set a
    dependency may point at.
    """
    exp = Expansion(parent=parent)
    if not policy.enabled:
        exp.problems.append("replanning is off")
        return exp
    if parent_generation >= policy.max_generations:
        exp.problems.append(
            f"generation {parent_generation} is as deep as replanning goes")
        return exp
    room = policy.max_total_steps - len(existing)
    if room <= 0:
        exp.problems.append(
            f"the swarm is already at its {policy.max_total_steps}-step ceiling")
        return exp
    if not isinstance(raw_steps, list):
        exp.problems.append("the planner did not return a list of steps")
        return exp
    if not raw_steps:
        return exp          # "nothing further is needed" is a valid answer

    budget = min(policy.max_new_steps, room)
    taken: list[Step] = []
    names_here: set[str] = set()

    for raw in raw_steps:
        if len(taken) >= budget:
            exp.dropped.append(
                f"{len(raw_steps) - len(taken)} more not taken: "
                f"{budget} is this step's limit")
            break
        if not isinstance(raw, dict):
            exp.dropped.append("a step that was not an object")
            continue
        name = str(raw.get("name", "")).strip()
        goal = str(raw.get("goal", "")).strip()
        if not name or not NAME_OK.match(name):
            exp.dropped.append(f"{name or '(unnamed)'}: not a usable name")
            continue
        if name in existing or name in names_here:
            # Renaming would invent an identity nobody asked for, and every
            # dependency written against that name would then be ambiguous.
            exp.dropped.append(f"{name}: a step by that name already exists")
            continue
        if not goal:
            exp.dropped.append(f"{name}: no goal")
            continue

        deps, bad = [], []
        for d in (raw.get("deps") or []):
            d = str(d).strip()
            if not d:
                continue
            if d in existing or d in names_here:
                deps.append(d)
            else:
                bad.append(d)
        if bad:
            # A dependency on something that does not exist is a step that can
            # never start. Dropping it beats creating permanent furniture.
            exp.dropped.append(f"{name}: depends on {', '.join(bad)}, "
                               f"which is not in this swarm")
            continue
        if parent and parent not in deps:
            deps.append(parent)

        harness = str(raw.get("harness", "")).strip()
        if harness and known_harnesses and harness not in known_harnesses:
            harness = ""        # fall back rather than fail at run time
        taken.append(Step(name=name, goal=goal[:MAX_GOAL_CHARS], deps=deps,
                          harness=harness))
        names_here.add(name)

    if taken and topo_order(taken) is None:
        # Only reachable when the model wrote a cycle among its own new steps:
        # every other dependency points at something already placed.
        exp.problems.append("the new steps depend on each other in a cycle")
        return exp
    exp.steps = taken
    return exp


_PROMPT = """You ran this step of a plan:

  goal: %(goal)s

and it produced:

%(output)s

Propose ONLY the follow-up steps this result makes necessary, as a JSON array:

  [{"name": "short-name", "goal": "what to do", "deps": ["other-new-step"]}]

Rules:
- Return [] if the result needs no further work. That is the common answer.
- At most %(max)d steps.
- "deps" may name other steps in THIS array, or existing steps: %(existing)s
- Do not repeat work that is already a step.
Answer with the JSON array and nothing else.
"""


def propose(goal: str, output: str, *, existing_names=(), max_new: int = 3,
            timeout: int = 30, chat=None):
    """Ask a model what this result makes necessary. Returns raw steps.

    Deliberately returns the RAW list rather than an `Expansion`: proposing and
    accepting are different jobs, and keeping them apart is what lets `validate`
    be the only place the rules live. Any failure — no provider, a refusal,
    unparseable output — comes back as an empty list, because the honest
    default for "the planner had nothing to say" is to change nothing.
    """
    text = (output or "").strip()
    if not text:
        return []
    if chat is None:
        def chat(prompt: str) -> str:
            from .llm import ChatSession, chat_send
            return chat_send(ChatSession(), prompt, timeout=timeout)

    names = ", ".join(str(n) for n in list(existing_names)[:20]) or "(none)"
    prompt = _PROMPT % {"goal": (goal or "")[:MAX_GOAL_CHARS],
                        "output": text[:MAX_OUTPUT_CHARS],
                        "max": max(1, max_new), "existing": names}
    try:
        reply = chat(prompt)
    except Exception:  # noqa: BLE001  (a dead planner changes nothing)
        return []
    if not reply or str(reply).startswith("⚠"):
        return []
    raw = _extract_json(str(reply))
    return raw if isinstance(raw, list) else []


def policy_from_config(cfg) -> ReplanPolicy:
    """Read a policy out of aion's config. Never raises.

    Accepts the shorthand `{"swarm_replan": 3}` (three new steps per finished
    step, everything else default) or the full dict. A typo must not stop the
    cockpit booting, so anything unparseable falls back to replanning off —
    the safe direction, since the alternative is a swarm that grows because a
    config key was misspelled.
    """
    raw = (cfg or {}).get("swarm_replan") if isinstance(cfg, dict) else None
    if raw is None:
        return ReplanPolicy()
    if isinstance(raw, bool):
        return ReplanPolicy(max_new_steps=3) if raw else ReplanPolicy()
    if isinstance(raw, (int, float)):
        return ReplanPolicy(max_new_steps=max(0, int(raw)))
    if not isinstance(raw, dict):
        return ReplanPolicy()
    def _int(key, default):
        try:
            return max(0, int(raw.get(key, default)))
        except (TypeError, ValueError):
            return default
    return ReplanPolicy(
        max_new_steps=_int("max_new_steps", 0),
        max_total_steps=_int("max_total_steps", 24),
        max_generations=_int("max_generations", 2),
    )
