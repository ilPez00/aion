"""runs.py — the Runs workspace: agent work, split into live and finished.

Every harness tagged "agent" (web/research/factory/opencode/cyclops) produces
tasks. The Tasks workspace lists *all* tasks flatly; Runs is the focused view
of agent work with two tabs:

    Processes   what is running right now — watch progress, kill a runaway loop
    Results     what finished — status + the output it produced

Pure and testable: it takes the task list + which harness ids count as agent
work, and returns display rows. The store owns the tab state; the app renders.
"""
from __future__ import annotations

from dataclasses import dataclass

TAB_PROCESSES = "processes"
TAB_RESULTS = "results"

# A task is "live" (a process) while it can still make progress, and a "result"
# once it can't. interrupted/failed are results you can re-run, not processes.
_PROCESS_STATES = {"running", "pending"}
_RESULT_STATES = {"done", "failed", "cancelled", "interrupted"}


def agent_harness_ids(harnesses: dict) -> set[str]:
    """Harness ids whose work belongs in Runs — those tagged 'agent' in config,
    so a new agent harness joins automatically without touching this module."""
    out = set()
    for hid, h in harnesses.items():
        tags = getattr(getattr(h, "cfg", None), "context_tags", ()) or ()
        if "agent" in tags:
            out.add(hid)
    return out


@dataclass
class RunRow:
    id: str
    harness: str
    label: str
    state: str
    progress: float
    eta: float | None
    output: list[str]      # recent, meaningful log lines
    created: float

    def as_dict(self) -> dict:
        return {
            "type": "run", "id": self.id, "harness": self.harness,
            "label": self.label, "state": self.state, "progress": self.progress,
            "eta": self.eta, "output": self.output, "created": self.created,
        }


def _output_lines(log: list[str], limit: int = 4) -> list[str]:
    """The last few log lines worth showing — drop the noisy step pings."""
    keep = [ln for ln in log
            if ln.strip() and not ln.lstrip().startswith(("[research] plan",
                                                           "[research] search",
                                                           "[research] reflect",
                                                           "[factory] iter"))]
    return (keep or log)[-limit:]


def collect_runs(tasks, agent_ids: set[str], tab: str) -> list[RunRow]:
    """Agent tasks for the active tab. Processes oldest-first (the one that has
    been running longest is likeliest to need attention); results newest-first."""
    wanted = _PROCESS_STATES if tab == TAB_PROCESSES else _RESULT_STATES
    rows = [
        RunRow(id=t.id, harness=t.harness, label=t.label,
               state=t.state.value, progress=t.progress, eta=t.eta,
               output=_output_lines(t.log), created=t.created)
        for t in tasks
        if t.harness in agent_ids and t.state.value in wanted
    ]
    rows.sort(key=lambda r: r.created, reverse=(tab == TAB_RESULTS))
    return rows


def tab_counts(tasks, agent_ids: set[str]) -> dict[str, int]:
    """How many agent tasks sit under each tab — shown in the tab bar."""
    proc = res = 0
    for t in tasks:
        if t.harness not in agent_ids:
            continue
        if t.state.value in _PROCESS_STATES:
            proc += 1
        elif t.state.value in _RESULT_STATES:
            res += 1
    return {TAB_PROCESSES: proc, TAB_RESULTS: res}


def other_tab(tab: str) -> str:
    return TAB_RESULTS if tab == TAB_PROCESSES else TAB_PROCESSES
