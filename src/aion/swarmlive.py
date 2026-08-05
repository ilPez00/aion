"""swarmlive.py — is a running step still alive?

`stalled()` answers "why is nothing happening", and its first line is `if
self._in_flight(): return ""`. So the one condition it will not diagnose is the
one where a swarm dies quietly: a step stuck in WORKING. A harness whose
subprocess wedged, a peer that slept, a model call that never returned — the
agent stays WORKING, `_in_flight()` stays 1, every panel reports a healthy busy
swarm, and the DAG waits forever on a step nothing is running.

Nothing timed that out, because nothing was even watching. Tasks report
progress (`registry.set_progress`, called by most harnesses); the swarm threw
those updates away — `on_task_state` returned early on "running" — so a step's
progress was 0.0 until the moment it was 1.0.

Two different silences, and conflating them is the trap:

    never heard from     the harness reported nothing since the step began.
                         This says NOTHING about the step. Plenty of CLI
                         harnesses block until they exit and report once. A
                         watchdog that kills on this kills healthy work.
    went quiet           it reported, repeatedly, and then stopped. That is
                         evidence: something WAS talking and no longer is.

Same discipline as a physis coherence of 0.0 meaning "no reading" rather than
"bad". Absence of a signal is not a signal.

So `stall_after` only fires on the second kind. The first is bounded, if at
all, by `max_runtime` — a cruder instrument, and the only one that catches a
mute harness, which is why it is separate and separately off.

Everything here is off by default (`HeartbeatPolicy()` ends nothing). A run
whose behaviour changes because this module was added would be the bug.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["HeartbeatPolicy", "Liveness", "assess", "sweep",
           "policy_from_config", "render_live"]


@dataclass(frozen=True)
class HeartbeatPolicy:
    """When a running step stops counting as alive. All bounds in seconds."""

    # Show it as quiet. Display only — never ends anything.
    quiet_after: float = 0.0
    # End a step that WAS reporting and stopped. 0 = never.
    stall_after: float = 0.0
    # End a step for taking too long regardless of what it reported. 0 = never.
    # The only bound that catches a harness which never reports at all, and a
    # blunt one: a legitimately long step and a wedged one look identical to it.
    max_runtime: float = 0.0

    @property
    def enabled(self) -> bool:
        return self.stall_after > 0 or self.max_runtime > 0


@dataclass(frozen=True)
class Liveness:
    """What can honestly be said about one running step."""

    state: str          # "working" | "quiet" | "stalled" | "overrun"
    elapsed: float      # seconds since it started
    silent_for: float   # seconds since anything was heard; elapsed if never
    heard: bool         # has anything been heard from it at all?
    why: str = ""       # set when the state is one that ends a step

    @property
    def ending(self) -> bool:
        return self.state in ("stalled", "overrun")


def _f(value) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _field(agent, name: str):
    """Read one field off a SwarmAgent or off its `as_dict()` shape.

    The renderers are handed display dicts and the runner holds objects, and
    both need to ask the same question. Same accommodation `swarmio` makes,
    for the same reason: forcing one caller to convert is how the two views
    end up assessing different things.
    """
    if isinstance(agent, dict):
        return agent.get(name)
    return getattr(agent, name, None)


def assess(agent, now: float,
           policy: HeartbeatPolicy = HeartbeatPolicy()) -> Liveness:
    """Judge one running step. Pure — no clock, no registry, no I/O.

    `started` is the fallback for `last_seen` so a step that has never been
    heard from reports its silence as its whole life, which is true and is
    exactly what `stall_after` must not act on.
    """
    started = _f(_field(agent, "started"))
    last_seen = _f(_field(agent, "last_seen"))
    # `>=`, not `>`: a harness that reports the instant it starts has reported,
    # and treating that as silence makes the chattiest steps the ones a
    # watchdog cannot see. The comparison is against `started` rather than 0 so
    # a stamp left by a PREVIOUS attempt is not read as this attempt's pulse.
    heard = last_seen > 0.0 and last_seen >= started
    elapsed = max(0.0, now - started) if started else 0.0
    silent_for = max(0.0, now - (last_seen or started)) if started else 0.0

    if policy.max_runtime > 0 and elapsed >= policy.max_runtime:
        return Liveness("overrun", elapsed, silent_for, heard,
                        f"still running after {elapsed / 60:.0f}m "
                        f"(limit {policy.max_runtime / 60:.0f}m)")
    # Only for a step that WAS talking. A harness that reports once at the end
    # is not a hung one, and killing it is a watchdog inventing the failure it
    # was installed to catch.
    if heard and policy.stall_after > 0 and silent_for >= policy.stall_after:
        return Liveness("stalled", elapsed, silent_for, heard,
                        f"silent for {silent_for / 60:.0f}m after reporting")
    if policy.quiet_after > 0 and silent_for >= policy.quiet_after and heard:
        return Liveness("quiet", elapsed, silent_for, heard)
    return Liveness("working", elapsed, silent_for, heard)


def sweep(agents, now: float,
          policy: HeartbeatPolicy = HeartbeatPolicy()) -> list[dict]:
    """Every running step this policy would end, with the reason.

    Returns rather than acts: what to DO with a wedged step (fail it into the
    retry policy, cancel it outright) belongs to the runner, and a pure sweep
    stays testable without one.
    """
    if not policy.enabled:
        return []
    out = []
    for agent in agents:
        live = assess(agent, now, policy)
        if live.ending:
            out.append({"id": _field(agent, "id") or "",
                        "name": _field(agent, "name") or "",
                        "state": live.state, "why": live.why})
    return out


def policy_from_config(cfg: dict | None) -> HeartbeatPolicy:
    """Read `swarm_heartbeat` off a config. Anything unparseable is OFF.

    A misread bound here does not degrade a run, it ENDS steps in it, so the
    failure direction is not negotiable.
    """
    raw = (cfg or {}).get("swarm_heartbeat")
    if not isinstance(raw, dict):
        return HeartbeatPolicy()
    try:
        return HeartbeatPolicy(
            quiet_after=max(0.0, float(raw.get("quiet_after", 0) or 0)),
            stall_after=max(0.0, float(raw.get("stall_after", 0) or 0)),
            max_runtime=max(0.0, float(raw.get("max_runtime", 0) or 0)))
    except (TypeError, ValueError):
        return HeartbeatPolicy()


def render_live(live: Liveness) -> str:
    """The running step's condition, in the words both surfaces use.

    A bare elapsed time is the useful part: "scout 4m" answers "is this the
    step that has been going since I made coffee" without anyone having to
    configure a policy first.
    """
    if not live.elapsed:
        return ""
    mins = live.elapsed / 60
    age = f"{live.elapsed:.0f}s" if live.elapsed < 90 else f"{mins:.0f}m"
    if live.state == "working":
        return age
    if live.state == "quiet":
        quiet = (f"{live.silent_for:.0f}s" if live.silent_for < 90
                 else f"{live.silent_for / 60:.0f}m")
        return f"{age}, quiet {quiet}"
    return f"{age}, {live.state}"
