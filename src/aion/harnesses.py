"""
harnesses.py — the "multi-harness" half of aion.

A Harness is anything that can take a Task, do work, and push progress +
stats through the bus. The UI never knows what backend is running; it only
sees Tasks and Stats. Adding a new backend (OpenCode, Claude Code, a remote
API, your Cyclops agent) = write one new subclass + register it.

Lessons folded in (see docs/RESEARCH.md):
  #2 pause/resume/kill per task
  #3 tiered harness strategy (cheap|standard|premium)
  #4 iteration/safe-run guard on autonomous loops
  #6 remote-harness as a first-class type (stub)
"""
from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .core import Bus, Task, TaskRegistry, TaskState, TOPIC_STATS


# tiers used by the "route by tier" command
TIER_CHEAP = "cheap"
TIER_STANDARD = "standard"
TIER_PREMIUM = "premium"


@dataclass
class HarnessConfig:
    id: str
    type: str
    name: str
    enabled: bool = True
    vram_mb: int = 0
    tier: str = TIER_STANDARD
    max_steps: int | None = None     # lesson #4: safe-run guard
    remote: str | None = None        # lesson #6: "host:port" of a remote kernel
    command: str = ""
    extra: dict = None  # type: ignore

    @classmethod
    def from_dict(cls, d: dict) -> "HarnessConfig":
        return cls(
            id=d["id"], type=d["type"], name=d.get("name", d["id"]),
            enabled=d.get("enabled", True), vram_mb=d.get("vram_mb", 0),
            tier=d.get("tier", TIER_STANDARD),
            max_steps=d.get("max_steps"),
            remote=d.get("remote"),
            command=d.get("command", ""),
            extra={k: v for k, v in d.items()
                   if k not in {"id", "type", "name", "enabled", "vram_mb",
                                "tier", "max_steps", "remote", "command"}},
        )


class Harness(ABC):
    def __init__(self, cfg: HarnessConfig, bus: Bus, registry: TaskRegistry,
                 store=None) -> None:
        self.cfg = cfg
        self.bus = bus
        self.registry = registry
        self.store = store          # SessionStore (for crash-safe checkpoint)
        self._running: set[str] = set()      # task ids currently executing
        self._paused: set[str] = set()      # suspended (loop waiting)
        self._kill: set[str] = set()        # requested cancel

    @property
    def id(self) -> str:
        return self.cfg.id

    @property
    def name(self) -> str:
        return self.cfg.name

    @property
    def tier(self) -> str:
        return self.cfg.tier

    @property
    def vram_mb(self) -> int:
        return self.cfg.vram_mb

    @abstractmethod
    async def run(self, task: Task, prompt: str = "") -> None:
        """Do the work for `task`, updating progress/stats on the bus."""
        ...

    # ---- lifecycle control (lesson #2) -----------------------------------
    def pause(self, task: Task) -> None:
        if task.id in self._running:
            self._paused.add(task.id)
            task.paused = True
            self._checkpoint()

    def resume(self, task: Task) -> None:
        self._paused.discard(task.id)
        task.paused = False
        self._checkpoint()

    def cancel(self, task: Task) -> None:
        self._kill.add(task.id)
        self._paused.discard(task.id)   # unblock the loop so it can exit
        task.paused = False

    # ---- helpers shared by subclasses -----------------------------------
    async def _wait_if_paused(self, task: Task, poll: float = 0.1) -> bool:
        """Block while paused. Returns False if a kill arrived."""
        while task.id in self._paused and task.id not in self._kill:
            await asyncio.sleep(poll)
        return task.id not in self._kill

    def _killed(self, task: Task) -> bool:
        return task.id in self._kill

    def _finish(self, task: Task) -> None:
        self._running.discard(task.id)
        self._paused.discard(task.id)
        self._kill.discard(task.id)

    def _checkpoint(self) -> None:
        if self.store:
            self.store.save(self.registry.tasks)

    async def _stat(self, **metrics) -> None:
        await self.bus.publish(TOPIC_STATS, {"harness": self.id, "metrics": metrics})


class DemoHarness(Harness):
    """Synthetic work — great for testing the UI with no backend."""

    async def run(self, task: Task, prompt: str = "") -> None:
        self._running.add(task.id)
        self.registry.set_state(task, TaskState.RUNNING)
        steps = self.cfg.max_steps or 20
        start = time.time()
        for i in range(steps + 1):
            if self._killed(task):
                self.registry.set_state(task, TaskState.CANCELLED)
                break
            if not await self._wait_if_paused(task):
                self.registry.set_state(task, TaskState.CANCELLED)
                break
            await asyncio.sleep(0.15)
            prog = i / steps
            eta = (steps - i) * 0.15
            self.registry.set_progress(task, prog, eta)
            self.registry.log(task, f"demo step {i}/{steps} — {prompt[:40]}")
            self._checkpoint()
            await self._stat(step=i, progress=round(prog, 3), vram=420 + i)
        else:
            self.registry.set_state(task, TaskState.DONE)
        self._finish(task)


class ShellHarness(Harness):
    """Runs a shell command per step (config "command", {n}=step, {p}=prompt)."""

    async def run(self, task: Task, prompt: str = "") -> None:
        import subprocess

        self._running.add(task.id)
        self.registry.set_state(task, TaskState.RUNNING)
        steps = self.cfg.max_steps or 12
        for i in range(steps + 1):
            if self._killed(task):
                self.registry.set_state(task, TaskState.CANCELLED)
                break
            if not await self._wait_if_paused(task):
                self.registry.set_state(task, TaskState.CANCELLED)
                break
            out = None
            cmd = self.cfg.command.replace("{n}", str(i)).replace("{p}", prompt)
            try:
                out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5)
                line = (out.stdout or out.stderr or "").strip().splitlines()
                if line:
                    self.registry.log(task, line[-1][:120])
            except Exception as e:  # noqa: BLE001
                self.registry.log(task, f"err: {e}")
            self.registry.set_progress(task, i / steps, (steps - i) * 0.3)
            self._checkpoint()
            await self._stat(step=i, stdout=len(out.stdout) if out else 0)
        else:
            self.registry.set_state(task, TaskState.DONE)
        self._finish(task)


class RemoteHarness(Harness):
    """Stub for lesson #6: a harness that runs work on a remote AIOS kernel
    (AIOS's ARM/AUM split). Talks over a socket to a remote runtime. The
    local side only relays progress/stats it receives. Wire transport here."""

    async def run(self, task: Task, prompt: str = "") -> None:
        self._running.add(task.id)
        self.registry.set_state(task, TaskState.RUNNING)
        target = self.cfg.remote or "localhost:8765"
        self.registry.log(task, f"[remote] would dispatch to {target}: {prompt[:50]}")
        await self._stat(target=target, dispatched=True)
        # TODO: open websocket/socket to target, stream its task events back
        # into self.registry.set_progress / set_state / log.
        await asyncio.sleep(0.3)
        self.registry.set_progress(task, 1.0)
        self.registry.set_state(task, TaskState.DONE)
        self._finish(task)


class CyclopsHarness(Harness):
    """Stub for the real Cyclops agent (cyclops/agent). Swaps in cleanly later."""

    async def run(self, task: Task, prompt: str = "") -> None:
        self._running.add(task.id)
        self.registry.set_state(task, TaskState.RUNNING)
        self.registry.log(task, f"[cyclops] would route prompt: {prompt[:60]}")
        await self._stat(loaded=True)
        # honor pause/kill even in the stub
        for _ in range(4):
            if self._killed(task):
                self.registry.set_state(task, TaskState.CANCELLED)
                self._finish(task)
                return
            if not await self._wait_if_paused(task):
                self.registry.set_state(task, TaskState.CANCELLED)
                self._finish(task)
                return
            await asyncio.sleep(0.1)
        self.registry.set_progress(task, 1.0)
        self.registry.set_state(task, TaskState.DONE)
        self._finish(task)


class TelemetryHarness(Harness):
    """Lesson #5: real per-harness stats from the host.

    Polls a local source for VRAM/throughput and republishes as stats. Sources:
      - "ollama":    `ollama ps` (model VRAM)
      - "nvidia":    `nvidia-smi` (GPU util / mem)
      - "vllm":      hit a vLLM /metrics endpoint
    Runs as a background poller (no task needed) so the right rail shows live
    numbers instead of fakes. Call .start() once at boot.
    """

    def __init__(self, cfg: HarnessConfig, bus: Bus, registry: TaskRegistry, store=None):
        super().__init__(cfg, bus, registry, store)
        self.source = (cfg.extra or {}).get("source", "none")
        self.interval = float((cfg.extra or {}).get("interval", 2.0))
        self._task: asyncio.Task | None = None

    async def run(self, task: Task, prompt: str = "") -> None:  # pragma: no cover
        return

    async def start(self) -> None:
        if self.source == "none":
            return
        self._task = asyncio.create_task(self._poll())

    async def _poll(self) -> None:
        while True:
            try:
                if self.source == "ollama":
                    await self._poll_ollama()
                elif self.source == "nvidia":
                    await self._poll_nvidia()
            except Exception as e:  # noqa: BLE001
                await self._stat(error=str(e)[:60])
            await asyncio.sleep(self.interval)

    async def _poll_ollama(self) -> None:
        proc = await asyncio.create_subprocess_shell(
            "ollama ps --format json",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await proc.communicate()
        try:
            import json as _json
            data = _json.loads(out or b"{}")
            models = data.get("models", [])
            total_vram = sum(m.get("size_vram", 0) for m in models)
            await self._stat(loaded_models=len(models), vram_bytes=total_vram)
        except Exception:
            await self._stat(loaded_models=0, vram_bytes=0)

    async def _poll_nvidia(self) -> None:
        proc = await asyncio.create_subprocess_shell(
            "nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await proc.communicate()
        lines = (out or b"").decode().strip().splitlines()[:1]
        if lines:
            parts = [p.strip() for p in lines[0].split(",")]
            if len(parts) == 2:
                await self._stat(gpu_util_pct=int(parts[0]), gpu_mem_mb=int(parts[1]))


class StatsHarness(Harness):
    """Jarvis HUD poller: republishes REAL token/cost/live-agent numbers.

    Reads Hermes' own ~/.hermes/state.db (read-only) via StatsReader and
    publishes a StatsSnapshot dict on TOPIC_STATS under harness id "stats".
    The header + right rail render from it. No task needed — call .start()
    once at boot, like TelemetryHarness.

    Config extras:
      window:   "today" | "24h" | "7d" | "all"   (default "today")
      interval: seconds between reads             (default 3.0)
      db_path:  override the state.db location     (default ~/.hermes/state.db)
    """

    def __init__(self, cfg: HarnessConfig, bus: Bus, registry: TaskRegistry, store=None):
        super().__init__(cfg, bus, registry, store)
        extra = cfg.extra or {}
        self.window = extra.get("window", "today")
        self.interval = float(extra.get("interval", 3.0))
        self._db_path = extra.get("db_path")
        self._task: asyncio.Task | None = None
        self._reader = None  # lazy: import inside to keep harnesses import-light

    async def run(self, task: Task, prompt: str = "") -> None:  # pragma: no cover
        return

    def _ensure_reader(self):
        if self._reader is None:
            from .stats import StatsReader
            self._reader = StatsReader(db_path=self._db_path, window=self.window)
        return self._reader

    async def poll_once(self) -> dict:
        """Read one snapshot and publish it. Returns the metrics dict."""
        reader = self._ensure_reader()
        # sqlite read is blocking; keep it off the event loop
        snap = await asyncio.to_thread(reader.snapshot)
        metrics = snap.as_metrics()
        await self._stat(**metrics)
        return metrics

    async def start(self) -> None:
        self._task = asyncio.create_task(self._poll())

    async def _poll(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception as e:  # noqa: BLE001
                await self._stat(ok=False, error=str(e)[:60])
            await asyncio.sleep(self.interval)


# registry of built-in harness types -> class
class AppHarness(Harness):
    """Spawns a real program as a task (lesson: Jarvis spawns tools).

    The task is RUNNING while the process lives. pause/resume map to
    SIGSTOP/SIGCONT (the process genuinely freezes), cancel to SIGTERM with a
    SIGKILL fallback. `command` is the program line; "{p}" splices the prompt
    so `run mpv <file>` style invocations work. While the deck is in APP mode
    its virtual gamepad drives whatever this spawned.
    """

    async def run(self, task: Task, prompt: str = "") -> None:
        import signal

        self._running.add(task.id)
        cmd = (self.cfg.command or "{p}").replace("{p}", prompt).strip()
        if not cmd:
            self.registry.log(task, "app: empty command")
            self.registry.set_state(task, TaskState.FAILED)
            self._finish(task)
            return
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True)   # own pgid: signal the whole tree
        except Exception as e:  # noqa: BLE001
            self.registry.log(task, f"app spawn failed: {e}")
            self.registry.set_state(task, TaskState.FAILED)
            self._finish(task)
            return
        self.registry.set_state(task, TaskState.RUNNING)
        self.registry.log(task, f"[app] pid {proc.pid}: {cmd[:80]}")
        await self._stat(pid=proc.pid, cmd=cmd[:40])

        import os
        pgid = proc.pid  # start_new_session -> pgid == pid
        stopped = False
        while proc.returncode is None:
            if self._killed(task):
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(pgid, sig)
                    except ProcessLookupError:
                        break
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
                        break
                    except asyncio.TimeoutError:
                        continue
                self.registry.set_state(task, TaskState.CANCELLED)
                self._finish(task)
                return
            want_stop = task.id in self._paused
            if want_stop != stopped:
                try:
                    os.killpg(pgid, signal.SIGSTOP if want_stop else signal.SIGCONT)
                    stopped = want_stop
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=0.2)
            except asyncio.TimeoutError:
                pass
        self.registry.set_progress(task, 1.0)
        self.registry.set_state(
            task, TaskState.DONE if proc.returncode == 0 else TaskState.FAILED)
        self.registry.log(task, f"[app] exit {proc.returncode}")
        self._finish(task)


HARNESS_TYPES = {
    "demo": DemoHarness,
    "shell": ShellHarness,
    "remote": RemoteHarness,
    "cyclops": CyclopsHarness,
    "telemetry": TelemetryHarness,
    "stats": StatsHarness,
    "app": AppHarness,
}


def build_harnesses(cfgs: list[dict], bus: Bus, registry: TaskRegistry,
                    store=None) -> dict[str, Harness]:
    out: dict[str, Harness] = {}
    for c in cfgs:
        cfg = HarnessConfig.from_dict(c)
        if not cfg.enabled:
            continue
        cls = HARNESS_TYPES.get(cfg.type, DemoHarness)
        out[cfg.id] = cls(cfg, bus, registry, store)
    return out
