"""Proactive Jarvis — watches aion state and surfaces actionable suggestions.

Pure, testable module: given a ViewState (snapshot) + config, return a list of
Suggestion objects (text + optional action). No I/O.
"""
from __future__ import annotations

from dataclasses import dataclass

from .store import ViewState


@dataclass
class Suggestion:
    """A proactive Jarvis suggestion with an optional one-key action."""
    text: str
    # action: a command string the cockpit can run (e.g. "rerun", "run demo hello").
    # If None, the suggestion is display-only.
    action: str | None = None


def suggest(state: ViewState, cfg: dict | None = None) -> list[Suggestion]:
    """Return actionable suggestions based on current state.

    Inspired by talon_hud's always-visible state + jarvis_ai panels: surface
    what needs attention AND let the user act on it in one key.
    """
    cfg = cfg or {}
    out: list[Suggestion] = []

    # 1. Failed tasks -> offer rerun
    failed = [t for t in state.tasks if t.state.value == "failed"]
    if failed:
        out.append(Suggestion(
            f"⚠ {len(failed)} task(s) failed — press r to retry", "rerun"))

    # 2. High resource usage
    sys_stats = state.stats.get("system", {})
    cpu = sys_stats.get("cpu_pct", 0)
    ram = sys_stats.get("ram_pct", 0)
    disk = sys_stats.get("disk_pct", 0)
    if cpu > 85:
        out.append(Suggestion(f"🔥 CPU at {cpu:.0f}% — consider pausing heavy harnesses"))
    if ram > 85:
        out.append(Suggestion(f"🧠 RAM at {ram:.0f}% — free memory before big loads"))
    if disk > 90:
        out.append(Suggestion(f"💾 Disk at {disk:.0f}% — clean up before builds"))

    # 3. Swarm has blocked agents
    sd = state.swarm_dashboard
    if isinstance(sd, str) and "blocked" in sd.lower():
        out.append(Suggestion("🚧 Swarm has blocked agents — check dependencies"))
    elif isinstance(sd, dict) and sd.get("blocked"):
        out.append(Suggestion("🚧 Swarm has blocked agents — check dependencies"))

    # 4. Vault has many notes -> suggest indexing
    vault = state.stats.get("vault", {})
    if isinstance(vault, dict):
        n = vault.get("node_count", 0)
        if isinstance(n, int) and n > 10:
            out.append(Suggestion(
                f"📚 {n} vault notes — try 'mem <query>' to recall", "mem"))

    # 5. Idle -> suggest starting something
    running = [t for t in state.tasks if t.state.value in ("running", "pending")]
    if not running and not failed:
        out.append(Suggestion(
            "💡 Idle — 'run demo hello' or 'swarm create <goal>'", "run demo hello"))

    return out
