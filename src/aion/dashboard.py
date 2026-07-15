"""
dashboard.py — The agentic OS desktop data model.

Collects all live state into a structured dict the UI renders as a
multi-column "desktop" landing view. No Textual imports — pure data.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .core import TaskState


@dataclass
class DashboardData:
    """Snapshot of everything the desktop panel displays."""
    # — system —
    uptime: str = ""
    cpu_pct: float = 0.0
    cpu_per_core: list[float] = field(default_factory=list)
    ram_pct: float = 0.0
    ram_used_gb: float = 0.0
    ram_total_gb: float = 0.0
    disk_pct: float = 0.0
    net_up: str = ""
    net_down: str = ""
    gpu_util: float = 0.0
    gpu_mem_mb: float = 0.0

    # — agents —
    active_tasks: list[dict] = field(default_factory=list)
    task_history: list[dict] = field(default_factory=list)
    total_tasks: int = 0
    tasks_running: int = 0
    tasks_done: int = 0
    tasks_failed: int = 0

    # — models —
    active_model: str = ""
    models_available: int = 0
    token_window: str = "today"
    token_models: list[dict] = field(default_factory=list)
    live_agents: int = 0

    # — swarm —
    swarm_agents: list[dict] = field(default_factory=list)
    swarm_working: int = 0
    swarm_done: int = 0
    swarm_total: int = 0

    # — vault —
    vault_notes: int = 0
    vault_links: int = 0

    # — memory —
    mem_count: int = 0

    # — health —
    steps: int = 0
    heart_rate: float = 0.0

    # — mode —
    active_mode: str = "default"
    mode_icon: str = "◉"

    # — identity —
    hostname: str = ""
    app_name: str = "aion"
    version: str = "1.0.0"
    timestamp: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


def collect_dashboard(state, cfg: dict) -> DashboardData:
    """Build a DashboardData snapshot from ViewState + config."""
    d = DashboardData()
    d.app_name = cfg.get("app_name", "aion")
    d.hostname = _safe_get("hostname", "")
    d.active_mode = state.active_mode

    # System stats
    sys_stats = state.stats.get("system", {})
    if sys_stats:
        d.cpu_pct = sys_stats.get("cpu_pct", 0.0)
        d.cpu_per_core = sys_stats.get("cpu_per_core", [])
        d.ram_pct = sys_stats.get("ram_pct", 0.0)
        d.ram_used_gb = sys_stats.get("ram_used_gb", 0.0)
        d.ram_total_gb = sys_stats.get("ram_total_gb", 16.0)
        d.disk_pct = sys_stats.get("disk_pct", 0.0)
        d.net_up = sys_stats.get("net_up_str", "0 B/s")
        d.net_down = sys_stats.get("net_down_str", "0 B/s")
        d.gpu_util = sys_stats.get("gpu_util_pct", -1.0)
        d.gpu_mem_mb = sys_stats.get("gpu_mem_mb", -1.0)
        d.uptime = sys_stats.get("uptime_str", "")

    # Tasks
    all_tasks = list(getattr(state, 'tasks', []))
    d.total_tasks = len(all_tasks)
    active = []
    for t in all_tasks:
        entry = {"id": t.id, "label": t.label, "harness": t.harness,
                 "state": t.state.value, "progress": t.progress, "paused": t.paused}
        active.append(entry)
        if t.state.value in ("running", "pending"):
            d.tasks_running += 1
        if t.state.value == "done":
            d.tasks_done += 1
        if t.state.value == "failed":
            d.tasks_failed += 1
    d.active_tasks = sorted(active, key=lambda x: x["progress"], reverse=True)[:10]
    d.task_history = list(state.task_history)[-8:]

    # Models / Stats
    st = state.stats.get("stats", {})
    if st:
        d.token_window = st.get("window", "today")
        d.token_models = st.get("models", [])[:5]
        d.live_agents = st.get("live", 0)
    d.models_available = len(state.stats)  # rough

    # Swarm
    swarm_d = state.swarm_dashboard
    if isinstance(swarm_d, dict):
        d.swarm_agents = swarm_d.get("agents", [])
        d.swarm_working = swarm_d.get("working", 0)
        d.swarm_done = swarm_d.get("done", 0)
        d.swarm_total = swarm_d.get("total", 0)
    elif isinstance(swarm_d, str):
        pass  # old format

    # Vault
    vault = state.stats.get("vault", {})
    if vault:
        d.vault_notes = vault.get("node_count", vault.get("ok", 0) and len(vault.get("nodes", [])) or 0)
        d.vault_links = vault.get("link_count", 0)

    # Memory
    d.mem_count = len(getattr(state, 'hermes_memory', []))

    # Mode icon
    mode_icons = {"default": "◉", "focus": "◎", "deep": "⬡",
                  "monitor": "◈", "stealth": "◌", "demo": "◆"}
    d.mode_icon = mode_icons.get(d.active_mode, "◉")

    # Active model
    d.active_model = state.active_harness

    return d


def _safe_get(key: str, default: Any = "") -> Any:
    try:
        import platform
        return getattr(platform, key, default)()
    except Exception:
        return default
