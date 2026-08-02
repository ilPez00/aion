"""swarmplan.py — turn a goal into a dependency DAG.

`decompose()` created an empty `SwarmPlan` and appended it to a list. Nothing
read it. So every swarm had to be hand-built: name each agent, write each goal,
wire each dependency, in an order that satisfies `add_checked`. That is a
construction kit, not orchestration — and it is the actual friction, now that
the executor can run a DAG once one exists.

This proposes the DAG from a goal, and then refuses most of what comes back.

The model is untrusted input, exactly as in `voicecmd`: its reply is parsed,
structurally validated, and either becomes a plan or does not. The stakes are
higher here than for a voice command, because every step in an accepted plan
becomes a prompt that a harness will execute:

  * **step count is capped.** A model that returns two hundred steps is an
    amplification bomb, not an ambitious plan.
  * **cycles are refused at creation**, not discovered later. `stalled()` can
    report a cycle, but by then the agents exist and someone has to clean up.
  * **dependencies must resolve** inside the plan or against agents that
    already exist. An invented name is a step that can never start.
  * **harnesses are checked against what is installed.** A hallucinated
    harness silently falls back to the default rather than failing at run time.
  * **applying a plan does not run it.** Creating work and starting work are
    separate decisions, the same way routing plans before it dispatches.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

MAX_STEPS = 12
MAX_NAME = 40
MAX_GOAL_CHARS = 500
NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]*$")


@dataclass
class Step:
    name: str
    goal: str
    deps: list[str] = field(default_factory=list)
    harness: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "goal": self.goal,
                "deps": list(self.deps), "harness": self.harness}


@dataclass
class Plan:
    goal: str = ""
    steps: list[Step] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    source: str = "rules"          # "llm" once a model proposed it

    @property
    def ok(self) -> bool:
        return bool(self.steps) and not self.problems

    def as_dict(self) -> dict:
        return {"ok": self.ok, "goal": self.goal, "source": self.source,
                "steps": [s.as_dict() for s in self.steps],
                "problems": list(self.problems)}


# ── validation ───────────────────────────────────────────────────────────────
def validate(goal: str, raw_steps, *, known_harnesses=(),
             existing_names=()) -> Plan:
    """Turn whatever arrived into a Plan, or into a list of reasons why not.

    Every problem is collected rather than raising on the first, because a
    plan is reviewed as a whole — being told about one bad dependency at a
    time is a miserable way to fix a five-step DAG.
    """
    plan = Plan(goal=(goal or "").strip()[:MAX_GOAL_CHARS])
    if not plan.goal:
        plan.problems.append("no goal")
    if not isinstance(raw_steps, list) or not raw_steps:
        plan.problems.append("no steps")
        return plan
    if len(raw_steps) > MAX_STEPS:
        plan.problems.append(
            f"{len(raw_steps)} steps proposed, the limit is {MAX_STEPS}")
        return plan

    known = {str(h) for h in known_harnesses}
    seen: set[str] = set()
    prior = {str(n) for n in existing_names}
    steps: list[Step] = []

    for i, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            plan.problems.append(f"step {i + 1} is not an object")
            continue
        name = str(raw.get("name", "")).strip()[:MAX_NAME]
        step_goal = str(raw.get("goal", "")).strip()[:MAX_GOAL_CHARS]
        if not name:
            plan.problems.append(f"step {i + 1} has no name")
            continue
        if not NAME_OK.match(name):
            plan.problems.append(f"{name!r} is not a usable name")
            continue
        # Names are the dependency key, so a duplicate is not a second agent —
        # it is an ambiguity that resolves arbitrarily.
        if name in seen or name in prior:
            plan.problems.append(f"{name!r} is already used")
            continue
        if not step_goal:
            plan.problems.append(f"{name!r} has no goal")
            continue

        deps_raw = raw.get("deps") or []
        if not isinstance(deps_raw, list):
            plan.problems.append(f"{name!r}: deps must be a list")
            continue
        deps = [str(d).strip() for d in deps_raw if str(d).strip()]

        harness = str(raw.get("harness", "")).strip()
        if harness and known and harness not in known:
            # Not fatal: the step is still valid work. Falling back to the
            # default beats refusing a whole plan over a guessed model name.
            plan.problems.append(
                f"{name!r}: no harness {harness!r} — using the default")
            harness = ""

        seen.add(name)
        steps.append(Step(name=name, goal=step_goal, deps=deps, harness=harness))

    # Dependencies resolve against this plan plus what already exists.
    resolvable = seen | prior
    for s in steps:
        for d in s.deps:
            if d not in resolvable:
                plan.problems.append(f"{s.name!r} depends on {d!r}, which does not exist")
            if d == s.name:
                plan.problems.append(f"{s.name!r} depends on itself")

    ordered = topo_order(steps)
    if ordered is None:
        plan.problems.append(
            "the steps form a dependency cycle — no order can satisfy them")
    else:
        steps = ordered

    plan.steps = steps
    return plan


def topo_order(steps: list[Step]) -> list[Step] | None:
    """Steps in an order where every dependency precedes its dependent.

    None when the graph is cyclic. Two reasons this matters: `add_checked`
    refuses a dependency that does not exist yet, so insertion order is not
    cosmetic; and a cycle caught here never becomes agents somebody has to
    delete by hand.

    Kahn's algorithm, with ties broken by declaration order so the same plan
    always produces the same DAG.
    """
    by_name = {s.name: s for s in steps}
    # Only edges inside this plan constrain the ORDER; a dependency on an
    # agent that already exists is satisfied before the first insert.
    indegree = {s.name: sum(1 for d in s.deps if d in by_name) for s in steps}
    ready = [s for s in steps if indegree[s.name] == 0]
    out: list[Step] = []

    while ready:
        node = ready.pop(0)
        out.append(node)
        for other in steps:
            if node.name in other.deps and other.name in indegree:
                indegree[other.name] -= 1
                if indegree[other.name] == 0:
                    ready.append(other)
    return out if len(out) == len(steps) else None


# ── proposing ────────────────────────────────────────────────────────────────
_PROMPT = """You break a goal into a small dependency graph of agent steps.

Reply with ONLY a JSON array. Each element:
  {"name": "short-name", "goal": "what this step does", "deps": ["earlier-name"]}

Rules:
- At most %(max)d steps. Fewer is better; do not invent work.
- `name` is short, unique, and is how other steps refer to this one.
- `deps` lists names of EARLIER steps whose output this step needs.
- No cycles. A step must not depend on itself.
- A step's goal is a self-contained instruction; it will be run on its own.
%(harnesses)s
Goal: %(goal)s"""


def _harness_hint(harnesses) -> str:
    ids = [str(h) for h in harnesses][:12]
    if not ids:
        return ""
    return ("- You may add \"harness\": one of "
            + ", ".join(ids) + " if a step clearly needs it.\n")


def propose(goal: str, *, harnesses=(), existing_names=(),
            timeout: int = 30, chat=None) -> Plan:
    """Ask a model for a DAG, then refuse most of what it says.

    `chat` is injectable so the validation path is testable without a model
    and without a network. Any failure — no provider, a refusal, unparseable
    output — comes back as a Plan carrying the reason, never an exception:
    this is called from a request handler.
    """
    goal = (goal or "").strip()
    if not goal:
        return Plan(goal="", problems=["no goal"], source="llm")

    if chat is None:
        def chat(prompt: str) -> str:
            from .llm import ChatSession, chat_send
            return chat_send(ChatSession(), prompt, timeout=timeout)

    prompt = _PROMPT % {"max": MAX_STEPS, "goal": goal[:MAX_GOAL_CHARS],
                        "harnesses": _harness_hint(harnesses)}
    try:
        reply = chat(prompt)
    except Exception as e:  # noqa: BLE001
        return Plan(goal=goal, source="llm",
                    problems=[f"planner unavailable: {type(e).__name__}"])
    if not reply or str(reply).startswith("⚠"):
        return Plan(goal=goal, source="llm",
                    problems=["no answer from the planner"])

    raw = _extract_json(str(reply))
    if raw is None:
        return Plan(goal=goal, source="llm",
                    problems=["the planner did not return usable JSON"])

    plan = validate(goal, raw, known_harnesses=harnesses,
                    existing_names=existing_names)
    plan.source = "llm"
    return plan


def _extract_json(reply: str):
    """The array out of a reply, tolerating fences and surrounding chatter.

    Models wrap JSON in ```json fences and prose more often than not; refusing
    those would make the feature fail for a formatting habit rather than a
    substantive problem.
    """
    text = reply.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.strip())
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return None


# ── applying ─────────────────────────────────────────────────────────────────
def apply(orchestrator, plan: Plan) -> dict:
    """Create the plan's agents. Does NOT start them.

    Creating work and starting work stay separate decisions, the same way
    routing plans before it dispatches — a DAG is N prompts that a harness will
    execute, and reviewing it before it runs is the whole point of showing it.

    Inserts in topological order because `add_checked` refuses a dependency
    that does not exist yet.
    """
    if not plan.ok:
        return {"ok": False, "created": [],
                "reason": "; ".join(plan.problems) or "empty plan"}
    created, failed = [], []
    for step in plan.steps:
        out = orchestrator.add_checked(step.name, step.goal, step.deps,
                                       harness=step.harness)
        if out.get("ok"):
            created.append(step.name)
        else:
            failed.append({"name": step.name, "reason": out.get("reason", "")})
    return {"ok": not failed, "created": created, "failed": failed,
            "reason": "" if not failed else
                      "; ".join(f"{f['name']}: {f['reason']}" for f in failed),
            "hint": "review it, then 'run ready' to start" if created else ""}
