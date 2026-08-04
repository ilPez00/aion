"""How a DAG looks when you are staring at it at 3am.

The swarm already knew its own shape; nothing rendered it. `workflows.swarm_dag`
drew a flat list with `← deps` glued on the end, which answers "what steps exist"
— a question nobody asks — and cannot answer either question that actually gets
asked of a running DAG:

    what runs next, and who is holding up whom.

So this module computes the two things a flat list throws away:

  * `waves()` — the execution ORDER, as topological layers. Wave 1 can start
    now, wave 2 starts when wave 1 finishes. Reading order becomes running
    order, which is the whole point of drawing a DAG rather than listing it.
  * `frontier()` / `explain()` — the live edge of that order, in a sentence.
    "Nothing is running" has about six different causes (all done, all blocked,
    inside a backoff, out of budget, waiting on a human, cyclic deps) and they
    call for six different actions. A panel that shows six zeroes makes the
    operator derive which one; a panel that says *which* makes it obvious.

Pure and clock-injected: everything here takes a list of agent dicts (the
`SwarmAgent.as_dict()` shape) and returns text or data. No orchestrator, no
event loop, no I/O — so the hard part (layering, cycle detection, the wording of
the reason) is testable without starting a swarm.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

from .workflows import _swarm_stage, _theme, stage_glyph, stage_theme_key

__all__ = ["waves", "frontier", "explain", "render", "unresolved_deps",
           "spend", "capacity", "render_dead_letters"]

# A dependency names an agent by NAME (see the comment on
# SwarmAgent.dependencies). A dep that matches no agent is not an error the
# swarm raises anywhere — it just silently never becomes ready, which looks
# exactly like "still waiting". Naming it is most of the value of this module.
_DONE_STATES = ("done", "cancelled")


def _deps(agent: dict) -> list[str]:
    raw = agent.get("deps") or agent.get("dependencies") or []
    return [str(d) for d in raw if str(d)]


def _name(agent: dict) -> str:
    return str(agent.get("name") or agent.get("id") or "?")


def unresolved_deps(agents: Sequence[dict]) -> dict[str, list[str]]:
    """Dependency names that match no agent, per agent name.

    A typo in `swarm add writer ... << reserch` produces a step that waits
    forever and reports itself as merely "waiting". Callers surface these.
    """
    known = {_name(a) for a in agents}
    out: dict[str, list[str]] = {}
    for a in agents:
        missing = [d for d in _deps(a) if d not in known]
        if missing:
            out[_name(a)] = missing
    return out


def waves(agents: Sequence[dict]) -> list[list[dict]]:
    """Group agents into execution layers. Wave N waits only on waves < N.

    Kahn's algorithm, with two deliberate choices:

    * Unknown dependency names do NOT hold a node back. They are already
      reported by `unresolved_deps`; letting them sink a node to the bottom
      would hide the typo behind a layout that merely looks odd.
    * Anything left over after the algorithm stalls is a CYCLE. It is returned
      as one final wave rather than dropped — a cyclic DAG is precisely when
      the operator most needs to see the nodes involved, and a renderer that
      silently omits them is worse than no renderer.
    """
    items = list(agents)
    known = {_name(a) for a in items}
    pending = {_name(a): [d for d in _deps(a) if d in known] for a in items}
    placed: dict[str, int] = {}
    out: list[list[dict]] = []

    while True:
        layer = [a for a in items
                 if _name(a) not in placed
                 and all(d in placed for d in pending[_name(a)])]
        if not layer:
            break
        depth = len(out)
        for a in layer:
            placed[_name(a)] = depth
        out.append(layer)

    leftover = [a for a in items if _name(a) not in placed]
    if leftover:
        out.append(leftover)
    return out


def _is_cyclic(agents: Sequence[dict], layers: Sequence[Sequence[dict]]) -> set[str]:
    """Names in the trailing cycle wave, if there is one."""
    if not layers:
        return set()
    known = {_name(a) for a in agents}
    last = layers[-1]
    depth_of = {_name(a): i for i, layer in enumerate(layers) for a in layer}
    cyc = set()
    for a in last:
        for d in _deps(a):
            if d in known and depth_of.get(d, -1) >= len(layers) - 1:
                cyc.add(_name(a))
    return cyc


def frontier(agents: Sequence[dict], now: float | None = None) -> dict[str, Any]:
    """The live edge of the DAG: what runs, what is next, what is stuck.

    Everything a "why is nothing happening" answer needs, as data, so the TUI
    panel and the web HUD can word it differently without re-deriving it.
    """
    at = time.time() if now is None else now
    items = list(agents)
    by_name = {_name(a): a for a in items}

    def status(a: dict) -> str:
        return str(a.get("status") or "idle")

    running = [_name(a) for a in items if status(a) == "working"]
    done = [_name(a) for a in items if status(a) in _DONE_STATES]
    failed = [_name(a) for a in items if status(a) == "failed"]
    retrying: list[dict] = []
    ready: list[str] = []
    for a in items:
        if status(a) not in ("idle", "planning"):
            continue
        due = float(a.get("retry_at") or 0.0)
        deps_done = all(status(by_name[d]) in _DONE_STATES
                        for d in _deps(a) if d in by_name)
        if due > at:
            retrying.append({"name": _name(a), "in_s": due - at,
                             "attempts": int(a.get("attempts") or 0)})
        elif deps_done:
            ready.append(_name(a))

    # "Blocked" here is the honest kind: waiting on something that will never
    # finish. A step waiting on a step that is still running is not blocked,
    # it is next — conflating the two is what makes a swarm look broken while
    # it is working fine.
    blocked: list[dict] = []
    for a in items:
        if status(a) in _DONE_STATES or status(a) == "working":
            continue
        dead = [d for d in _deps(a)
                if d in by_name and status(by_name[d]) == "failed"]
        if dead:
            blocked.append({"name": _name(a), "needs": dead})

    layers = waves(items)
    return {
        "running": running,
        "ready": ready,
        "retrying": sorted(retrying, key=lambda r: r["in_s"]),
        "blocked": blocked,
        "failed": failed,
        "done": done,
        "total": len(items),
        "waves": len(layers),
        "cyclic": sorted(_is_cyclic(items, layers)),
        "unresolved": unresolved_deps(items),
    }


def explain(agents: Sequence[dict], now: float | None = None) -> str:
    """One sentence naming the swarm's current condition, and what to do.

    Ordered by what the operator can act on, not by severity: a cycle or a typo
    needs an edit, a failure needs a decision, a backoff needs patience, and an
    idle-but-ready DAG needs one keystroke. The most actionable true statement
    wins.
    """
    if not agents:
        return "no swarm — `swarm create <goal>` to start"
    f = frontier(agents, now=now)
    if f["cyclic"]:
        return f"cycle: {', '.join(f['cyclic'][:3])} depend on each other — nothing can start"
    if f["unresolved"]:
        who, missing = next(iter(f["unresolved"].items()))
        return f"{who} waits on '{missing[0]}', which is not a step here"
    if f["running"]:
        names = ", ".join(f["running"][:3])
        more = f" +{len(f['running']) - 3}" if len(f["running"]) > 3 else ""
        return f"{len(f['running'])} running: {names}{more}"
    if f["retrying"]:
        r = f["retrying"][0]
        return f"{r['name']} retries in {r['in_s']:.0f}s (attempt {r['attempts'] + 1})"
    if f["ready"]:
        return f"{len(f['ready'])} ready — `swarm run` to start"
    if f["blocked"]:
        b = f["blocked"][0]
        extra = f" +{len(f['blocked']) - 1} more" if len(f["blocked"]) > 1 else ""
        return f"blocked: {b['name']} needs {b['needs'][0]}, which failed{extra}"
    if f["failed"]:
        return f"{len(f['failed'])} failed, nothing left to run"
    if len(f["done"]) == f["total"]:
        return f"all {f['total']} steps done"
    return "idle"


def spend(status: dict) -> str:
    """What this DAG has cost so far, against what it is allowed to cost.

    The budget governor could already stop a swarm, and nothing showed the
    number it was stopping on — so the first time an operator met it was as a
    refusal. Estimates are marked as estimates every time they are printed:
    the figures are characters over four times a configured price, not a bill,
    and a number that looks authoritative gets believed.
    """
    ledger = (status or {}).get("ledger") or {}
    committed = float(ledger.get("committed") or 0.0)
    budget = float((status or {}).get("budget") or 0.0)
    if not committed and not budget:
        return ""
    out = f"~{_money(committed)}"
    if budget:
        pct = int(round(100 * committed / budget)) if budget else 0
        out += f" of {_money(budget)} ({pct}%)"
    retried = float(ledger.get("retried") or 0.0)
    if retried:
        out += f" · ~{_money(retried)} on retries"
    return out + " est"


def _money(x: float) -> str:
    """Cents are the wrong resolution for a single step.

    One prompt costs fractions of a cent, so a two-decimal figure reads "$0.00"
    for the entire early life of a swarm — a cost display that says zero while
    spending is worse than none, because it gets believed.
    """
    return f"${x:.2f}" if x >= 0.1 else f"${x:.3f}"


def capacity(status: dict) -> str:
    """Why fewer steps are running than are ready.

    `deferred` already carried this per step; this is the standing headline, so
    "ready but not started" stops looking like a bug the moment it is seen.
    """
    st = status or {}
    in_flight = int(st.get("in_flight") or 0)
    max_par = int(st.get("max_parallel") or 0)
    if not max_par:
        return ""
    out = f"{in_flight}/{max_par} slots"
    total = float(st.get("vram_total") or 0.0)
    if total:
        used = float(st.get("vram_used") or 0.0)
        out += f" · vram {used / 1024:.1f}/{total / 1024:.1f}G"
    return out


def render_dead_letters(status: dict, theme: dict | None = None,
                        limit: int = 3) -> list[str]:
    """Steps that ran out of attempts, and what each is holding up.

    The remediation question is never "what failed" — the ✗ already said that.
    It is "what is stuck behind it", which decides whether this is a fix-now or
    a fix-Monday. So `blocks` leads and the error text follows.
    """
    th = _theme(theme)
    letters = (status or {}).get("dead_letters") or []
    if not letters:
        return []
    out = [f"[{th['err']}]─ Needs you ──────────────────────────────────[/]"]
    for d in letters[:limit]:
        blocks = d.get("blocks") or []
        tail = (f"blocks {', '.join(blocks[:2])}" if blocks
                else "blocks nothing else")
        out.append(f"  [{th['err']}]✗ {str(d.get('name', '?'))[:16]}[/] "
                   f"[{th['dim']}]{d.get('kind', 'unknown')} · {tail}[/]")
        err = str(d.get("error") or "").strip().replace("\n", " ")
        if err:
            out.append(f"    [{th['dim']}]{err[:44]}[/]")
    if len(letters) > limit:
        out.append(f"  [{th['dim']}]… {len(letters) - limit} more[/]")
    return out


def _bar(progress: float, width: int, colour: str, dim: str,
         status: str = "") -> str:
    # A step marked done draws a full bar whatever its last progress ping said.
    # Not every path sets progress on completion, and an empty bar next to a ✓
    # reads as "finished but did nothing" — the glyph is the authority here.
    # Cancelled is deliberately NOT included: it stopped, it did not finish.
    p = 1.0 if status == "done" else min(max(float(progress or 0.0), 0.0), 1.0)
    filled = int(round(p * width))
    return f"[{colour}]{'█' * filled}[/][{dim}]{'░' * (width - filled)}[/]"


def render(agents: Sequence[dict], theme: dict | None = None,
           now: float | None = None, name_width: int = 14,
           max_rows: int = 14, goal_width: int = 20,
           bars: bool = True) -> str:
    """The DAG drawn as waves, top to bottom, in running order.

    Each row carries the one fact that explains its own state — a retry
    countdown, the upstream that failed, the attempt count — so the view never
    forces a jump to the log to find out why a row is the colour it is.
    """
    th = _theme(theme)
    a, er, di = th["accent"], th["err"], th["dim"]
    items = list(agents)
    if not items:
        return f"[{di}](no steps)[/]"

    at = time.time() if now is None else now
    layers = waves(items)
    cyclic = _is_cyclic(items, layers)
    by_name = {_name(x): x for x in items}
    known = set(by_name)
    lines: list[str] = []
    rows = 0

    for i, layer in enumerate(layers):
        cyc_wave = bool(cyclic) and i == len(layers) - 1
        label = "cycle" if cyc_wave else f"wave {i + 1}"
        head_col = er if cyc_wave else di
        if i:
            lines.append(f"  [{di}]│[/]")
        lines.append(f"  [{head_col}]{label}[/]")
        for ag in layer:
            if rows >= max_rows:
                left = sum(len(x) for x in layers[i:]) - rows
                lines.append(f"    [{di}]… {left} more[/]")
                return "\n".join(lines)
            rows += 1
            st = _swarm_stage(str(ag.get("status") or "idle"))
            col = th.get(stage_theme_key(st), di)
            nm = _name(ag)[:name_width]
            # A plan that has not been created yet has no progress to show, and
            # a column of identical empty bars is noise that costs the row the
            # width its goal needs.
            bar = _bar(float(ag.get("progress") or 0.0), 6, col, di,
                       str(ag.get("status") or "")) if bars else ""

            note = ""
            due = float(ag.get("retry_at") or 0.0)
            attempts = int(ag.get("attempts") or 0)
            missing = [d for d in _deps(ag) if d not in known]
            dead = [d for d in _deps(ag)
                    if d in by_name
                    and str(by_name[d].get("status")) == "failed"]
            if missing:
                note = f"[{er}]no step '{missing[0][:12]}'[/]"
            elif due > at:
                note = f"[{th['warn']}]retry {due - at:.0f}s ({attempts + 1})[/]"
            elif dead:
                note = f"[{er}]needs {dead[0][:12]}[/]"
            elif str(ag.get("status")) == "failed":
                tries = f" ×{attempts}" if attempts > 1 else ""
                note = f"[{er}]failed{tries}[/]"
            elif ag.get("instance"):
                # Instances are machine names and they collide at the head
                # ("workstation" / "workstation-2"), so cut less here than for
                # harness names, which are short and distinct by their first
                # letters (claude / codex / opencode / hermes).
                note = f"[{di}]@{str(ag['instance'])[:12]}[/]"
            elif ag.get("harness"):
                note = f"[{di}]{str(ag['harness'])[:10]}[/]"
            elif int(ag.get("generation") or 0) > 0:
                # A step nobody typed. Saying so on the row is the difference
                # between "I do not remember adding that" and "the swarm added
                # that, and here is how deep the chain goes".
                note = (f"[{th['warn']}]+g{ag['generation']}[/] "
                        f"[{di}]{str(ag.get('goal') or '')[:goal_width - 4]}[/]")
            elif ag.get("goal"):
                # Nothing to warn about, so the row spends its width saying
                # what the step is FOR. This is what lets one block replace
                # the old agent-list-plus-DAG pair: no row is ever blank, and
                # nothing is printed twice.
                note = f"[{di}]{str(ag['goal'])[:goal_width]}[/]"

            row = f"    [{col}]{stage_glyph(st)}[/] [{a}]{nm:{name_width}s}[/]"
            lines.append(f"{row} {bar} {note}" if bars else f"{row} {note}")
    return "\n".join(lines)
