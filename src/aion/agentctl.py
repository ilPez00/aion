"""agentctl.py — what may be done to a task, and why not.

The TUI has always known these rules; they lived inline in `store._control`
and `store._activate` as a chain of `elif`s on `task.state.value`. Porting
agent control to the web HUD meant either duplicating that chain in a second
place — where it would drift on the first change — or lifting it out. This is
the lift.

Two things live here and nothing else:

  * `legal()` — a pure state machine over (action, state, paused). No harness,
    no bus, no event loop, so every branch is testable directly.
  * `Outcome` — what a caller should report, including the REASON a refused
    action was refused. "Nothing happened" is the worst possible answer for a
    button press; the HUD says "already finished" or "not running" instead.

Execution stays in the cockpit process. The web HUD never runs a harness: it
asks the cockpit over the authenticated transport, and the cockpit applies its
own HITL gates. That asymmetry is deliberate and is the reason approval gates
still mean something when the HUD is open on someone's phone.
"""
from __future__ import annotations

from dataclasses import dataclass

# Terminal states: the work is over, and only re-running can change that.
FINISHED = ("done", "failed", "cancelled", "interrupted")
# States a fresh run can be started from.
RERUNNABLE = ("interrupted", "cancelled", "failed")
LIVE = ("running", "pending")

ACTIONS = ("pause", "resume", "cancel", "rerun")


@dataclass
class Outcome:
    """Result of asking for an action. `ok=False` always carries a reason."""
    ok: bool
    action: str
    task_id: str = ""
    reason: str = ""
    state: str = ""

    def as_dict(self) -> dict:
        return {"ok": self.ok, "action": self.action, "task_id": self.task_id,
                "reason": self.reason, "state": self.state}


def legal(action: str, state: str, paused: bool = False) -> tuple[bool, str]:
    """May `action` be applied to a task in `state`? Pure.

    The refusal strings are user-facing. They say what is true of the task
    rather than what the code checked, because the person reading them is
    looking at a node on a graph, not at this function.
    """
    if action not in ACTIONS:
        return False, f"unknown action {action!r}"

    if action == "pause":
        if state != "running":
            return False, f"only a running task can be paused (this is {state})"
        if paused:
            return False, "already paused"
        return True, ""

    if action == "resume":
        if state != "running":
            return False, f"only a running task can be resumed (this is {state})"
        if not paused:
            return False, "not paused"
        return True, ""

    if action == "cancel":
        if state in FINISHED:
            return False, f"already {state}"
        return True, ""

    # rerun
    if state in RERUNNABLE:
        return True, ""
    if state in LIVE:
        return False, f"still {state} — cancel it first"
    return False, f"cannot re-run a {state} task"


# ── swarm agents ─────────────────────────────────────────────────────────────
# A swarm agent is not a process. What it owns is a task id, and the task is
# what a harness actually pauses, resumes or kills. So the two vocabularies
# have to meet somewhere, and this is that place: `AgentStatus` values written
# in the task states `legal()` already reasons about.
#
# The alternative — `swarm.can` growing its own copy of the terminal-state and
# pause rules — is what the codebase already had, and the two copies had
# already started to disagree.
AGENT_AS_TASK = {
    "idle": "pending", "planning": "pending",
    "waiting": "pending", "blocked": "pending",
    "working": "running",
    "done": "done", "failed": "failed", "cancelled": "cancelled",
}

# Actions with no meaning at the agent layer. An agent cannot be suspended;
# only the work it is currently doing can be, so these route to its task.
DELEGATED = ("pause", "resume")


def route(action: str, agent_status: str, task_state: str = "",
          paused: bool = False) -> tuple[str, str]:
    """Where a swarm action lands: "agent", "task", or "" (refused). Pure.

    `task_state` empty means the agent owns no task yet — a remote spawn still
    in flight, or a status set by hand. That is a refusal rather than a crash,
    and it says which of the two it is, because "nothing happened" on a pause
    button is indistinguishable from a bug.
    """
    if action not in DELEGATED:
        return "agent", ""
    if agent_status != "working":
        return "", (f"only a working agent can be {describe(action)} "
                    f"(this is {agent_status})")
    if not task_state:
        return "", ("this agent is not attached to a task yet — it is still "
                    "starting")
    ok, why = legal(action, task_state, paused)
    return ("task", "") if ok else ("", why)


def next_state(action: str) -> str | None:
    """The state an accepted action moves the task to, if it is immediate.

    pause/resume flip a transient flag rather than the state, so they return
    None: the caller must not report a state change that did not happen.
    """
    return {"cancel": "cancelled", "rerun": "pending"}.get(action)


def describe(action: str) -> str:
    """Verb for logs and spoken feedback."""
    return {"pause": "paused", "resume": "resumed",
            "cancel": "cancelled", "rerun": "re-running"}.get(action, action)
