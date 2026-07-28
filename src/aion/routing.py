"""routing.py — decide which instance in the fleet should run a task.

The Fleet workspace has always been able to *show* you every box. It could not
send work to one. This is the decision half of that: given the fleet's state
and a task, pick a target and be able to say why.

Explainability is the point
---------------------------
`plan()` returns the winner **and every rejection with its reason**. A
scheduler that silently picks a box is untrustworthy the first time it picks a
surprising one, and "why did it go there" is the question an operator actually
asks. Every candidate carries a score breakdown, so the answer is in the
payload rather than in someone's head.

Safety
------
Nothing here dispatches. `plan()` is pure: fleet state and a task in, a
decision out. The caller performs the dispatch, and the caller is the one that
must enforce that the chosen target came from real discovery rather than from
a request body — routing a task is remote code execution, and the target must
never be an arbitrary host somebody typed.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Scoring weights. These encode the routing policy and are the knob worth
# turning: raise BUSY_PENALTY to spread work harder, raise LOCAL_BONUS to keep
# work on this box unless it is genuinely loaded.
IDLE_BONUS = 40.0        # nothing running here
BUSY_PENALTY = 18.0      # per task already running
HARNESS_BONUS = 25.0     # already has the harness the task wants active
LOCAL_BONUS = 6.0        # this machine: no network, no token surprise
CPU_PENALTY = 30.0       # scaled by reported CPU load (0..1)
STALE_PENALTY = 20.0     # heartbeat is lagging but not dead

# A heartbeat older than this means the instance is not a safe target even if
# its pid is alive — it may be wedged.
MAX_HEARTBEAT_AGE_S = 30.0


@dataclass
class Candidate:
    """One routable aion instance."""
    id: str
    host: str = "127.0.0.1"
    port: int = 8765
    alive: bool = False
    local: bool = True
    running_count: int = 0
    active_harness: str = ""
    harnesses: list[str] = field(default_factory=list)
    cpu: float = 0.0            # 0..1, 0 when unknown
    age_s: float = 0.0          # heartbeat age
    is_self: bool = False
    # Why this candidate is in the state it is in, when the transport knows
    # something the scheduler cannot see -- an ssh peer's "Permission denied
    # (publickey)" beats a bare "offline" when you are staring at the HUD
    # wondering which of six boxes is misconfigured.
    note: str = ""


@dataclass
class Verdict:
    """Why one candidate won or lost."""
    id: str
    score: float
    reasons: list[str] = field(default_factory=list)
    eligible: bool = True
    rejection: str = ""


@dataclass
class RoutePlan:
    target: Candidate | None
    verdicts: list[Verdict]
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.target is not None

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "target": (vars(self.target) if self.target else None),
            "verdicts": [vars(v) for v in self.verdicts],
        }


def eligible(c: Candidate, harness: str = "") -> tuple[bool, str]:
    """Can this candidate run the task at all?

    Eligibility is separate from scoring on purpose: a dead box is not a
    low-scoring box, it is not a box. Collapsing the two makes a fleet where
    everything is offline quietly route to the least-dead option.
    """
    if not c.alive:
        return False, f"offline ({c.note})" if c.note else "offline"
    if c.age_s > MAX_HEARTBEAT_AGE_S:
        return False, f"heartbeat {c.age_s:.0f}s old"
    if harness and c.harnesses and harness not in c.harnesses:
        return False, f"no {harness!r} harness"
    return True, ""


def score(c: Candidate, harness: str = "") -> Verdict:
    """Fitness for running a task. Higher is better."""
    ok, why = eligible(c, harness)
    if not ok:
        return Verdict(id=c.id, score=float("-inf"), eligible=False, rejection=why)

    total = 0.0
    reasons: list[str] = []

    if c.running_count == 0:
        total += IDLE_BONUS
        reasons.append(f"idle (+{IDLE_BONUS:.0f})")
    else:
        pen = BUSY_PENALTY * c.running_count
        total -= pen
        reasons.append(f"{c.running_count} running (-{pen:.0f})")

    if harness and c.active_harness == harness:
        total += HARNESS_BONUS
        reasons.append(f"{harness} already active (+{HARNESS_BONUS:.0f})")

    if c.local:
        total += LOCAL_BONUS
        reasons.append(f"local (+{LOCAL_BONUS:.0f})")

    if c.cpu > 0:
        pen = CPU_PENALTY * min(1.0, c.cpu)
        total -= pen
        reasons.append(f"cpu {c.cpu * 100:.0f}% (-{pen:.0f})")

    # Alive but lagging: usable, but the last resort.
    if c.age_s > MAX_HEARTBEAT_AGE_S / 2:
        total -= STALE_PENALTY
        reasons.append(f"stale heartbeat {c.age_s:.0f}s (-{STALE_PENALTY:.0f})")

    return Verdict(id=c.id, score=round(total, 2), reasons=reasons)


def plan(candidates: list[Candidate], *, harness: str = "",
         target_id: str | None = None) -> RoutePlan:
    """Choose where a task should run.

    `target_id` pins a specific instance — the drag-onto-a-node gesture — but
    it is still checked for eligibility rather than obeyed blindly: dropping a
    task on a box that died two seconds ago should say so, not fail silently
    somewhere in the transport.
    """
    verdicts = [score(c, harness) for c in candidates]
    by_id = {c.id: c for c in candidates}
    vmap = {v.id: v for v in verdicts}

    if target_id is not None:
        c = by_id.get(target_id)
        if c is None:
            return RoutePlan(None, verdicts, f"no instance named {target_id!r}")
        v = vmap[target_id]
        if not v.eligible:
            return RoutePlan(None, verdicts,
                             f"{target_id} cannot take work: {v.rejection}")
        return RoutePlan(c, verdicts, f"pinned to {target_id}")

    ranked = sorted((v for v in verdicts if v.eligible),
                    key=lambda v: -v.score)
    if not ranked:
        why = ", ".join(f"{v.id}: {v.rejection}" for v in verdicts) or "no instances"
        return RoutePlan(None, verdicts, f"nothing eligible ({why})")

    best = ranked[0]
    detail = "; ".join(best.reasons) or "no distinguishing factors"
    runner_up = f", next best {ranked[1].id} at {ranked[1].score}" if len(ranked) > 1 else ""
    return RoutePlan(by_id[best.id], verdicts,
                     f"{best.id} scored {best.score} ({detail}){runner_up}")


def candidates_from_fleet(peers: list, harnesses: list[str] | None = None,
                          *, now_cpu: float = 0.0) -> list[Candidate]:
    """Adapt `fleet.LocalPeer` objects into candidates.

    Kept separate from `plan()` so the decision logic never has to know how the
    fleet was discovered — a remote-peer adapter drops in the same way.
    """
    out: list[Candidate] = []
    for p in peers:
        age = p.age_s() if callable(getattr(p, "age_s", None)) else 0.0
        out.append(Candidate(
            id=getattr(p, "id", "?"),
            host=getattr(p, "host", "127.0.0.1"),
            port=int(getattr(p, "port", 8765) or 8765),
            alive=True,                      # discover_local prunes dead pids
            local=True,
            running_count=int(getattr(p, "running_count", 0) or 0),
            active_harness=getattr(p, "active_harness", "") or "",
            harnesses=list(harnesses or []),
            cpu=now_cpu if getattr(p, "is_self", False) else 0.0,
            age_s=age,
            is_self=bool(getattr(p, "is_self", False)),
        ))
    return out


def candidates_from_ssh(rows: list[dict]) -> list[Candidate]:
    """Adapt `sshlink.describe()` rows into candidates.

    A peer counts as alive only when its tunnel is up AND its /status answered
    through that tunnel. Either alone is a lie: an ssh forward survives the
    remote aion crashing (ssh is still connected, the port still accepts, the
    connection is just refused one hop further on), so trusting the tunnel
    would route work into a hole.

    `age_s` is 0 because the status here was fetched just now — remote peers
    have no heartbeat file to be stale relative to. `local=False` costs them
    LOCAL_BONUS, which is the intent: same-machine work is cheaper, so a remote
    box has to be meaningfully more idle to win.
    """
    out: list[Candidate] = []
    for row in rows:
        status = row.get("status") or {}
        alive = bool(row.get("up")) and bool(status)
        out.append(Candidate(
            id=str(row.get("id", "?")),
            host="127.0.0.1",              # the local end of the tunnel
            port=int(row.get("local_port", 0) or 0),
            alive=alive,
            local=False,
            running_count=int(status.get("running_count", 0) or 0),
            active_harness=str(status.get("active_harness", "") or ""),
            harnesses=[str(h) for h in (status.get("harnesses") or [])],
            cpu=float(status.get("cpu", 0.0) or 0.0),
            age_s=0.0,
            is_self=False,
            note=_ssh_note(row, status),
        ))
    return out


def _ssh_note(row: dict, status: dict) -> str:
    """The most specific thing we can say about why a peer is not usable."""
    if row.get("up") and not status:
        return "tunnel up, aion not answering on the far end"
    if not row.get("enabled", True):
        return "disabled"
    return str(row.get("error", "") or "")[:120]
