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
from pathlib import Path

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
    context_tags: tuple[str, ...] = ("system",)
    extra: dict = None  # type: ignore

    @classmethod
    def from_dict(cls, d: dict) -> "HarnessConfig":
        tags = d.get("context_tags", None)
        if isinstance(tags, list):
            tags = tuple(tags)
        elif tags is None:
            tags = ("system",)
        else:
            tags = (tags,)
        return cls(
            id=d["id"], type=d["type"], name=d.get("name", d["id"]),
            enabled=d.get("enabled", True), vram_mb=d.get("vram_mb", 0),
            tier=d.get("tier", TIER_STANDARD),
            max_steps=d.get("max_steps"),
            remote=d.get("remote"),
            command=d.get("command", ""),
            context_tags=tags,
            # extra = unknown top-level keys, plus an explicit nested "extra"
            # block (the convention opencode/factory use). Nested wins so a
            # config can put harness-specific settings in either place.
            extra={
                **{k: v for k, v in d.items()
                   if k not in {"id", "type", "name", "enabled", "vram_mb",
                                "tier", "max_steps", "remote", "command",
                                "context_tags", "extra"}},
                **(d.get("extra") or {}),
            },
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
    """lesson #6: runs work on a remote AIOS kernel (aion's ARM/AUM
    split). Opens a line socket to `remote` (host:port), sends the
    prompt, and relays every line it gets back as progress/stats/log
    until the remote side closes. Real transport, not a stub."""

    async def run(self, task: Task, prompt: str = "") -> None:
        import socket
        self._running.add(task.id)
        self.registry.set_state(task, TaskState.RUNNING)
        target = self.cfg.remote or "localhost:8765"
        host, _, port = target.partition(":")
        self.registry.log(task, f"[remote] dispatch -> {target}: {prompt[:50]}")
        try:
            with socket.create_connection((host, int(port or 8765)), timeout=10) as s:
                s.sendall((prompt + "\n").encode())
                buf = b""
                steps = 0
                while True:
                    if self._killed(task):
                        self.registry.set_state(task, TaskState.CANCELLED)
                        break
                    if not await self._wait_if_paused(task):
                        self.registry.set_state(task, TaskState.CANCELLED)
                        break
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                    text = buf.decode(errors="replace")
                    lines = text.splitlines()
                    if lines:
                        self.registry.log(task, lines[-1][:120])
                    steps += 1
                    self.registry.set_progress(task, min(0.99, steps / max(1, self.cfg.max_steps or 20)))
                    await self._stat(target=target, recv=len(buf))
                else:
                    self.registry.set_progress(task, 1.0)
                    self.registry.set_state(task, TaskState.DONE)
        except Exception as e:  # noqa: BLE001
            self.registry.log(task, f"[remote] error: {e}")
            self.registry.set_state(task, TaskState.FAILED)
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


class WebHarness(Harness):
    """DeepSearch harness: answers a query using the live web via DuckDuckGo,
    then asks the LLM (Groq) to synthesize a cited answer. Streams progress +
    the final answer into the task's log so it shows natively in the Tasks view.

    Respects pause/kill (lesson #2). Network failures degrade to a routed reply
    rather than crashing the task.
    """

    async def run(self, task: Task, prompt: str = "") -> None:
        self._running.add(task.id)
        self.registry.set_state(task, TaskState.RUNNING)
        try:
            from .web import deepsearch_answer
        except Exception:  # pragma: no cover
            deepsearch_answer = None
        self.registry.set_progress(task, 0.15, 6)
        self.registry.log(task, f"[web] searching: {prompt[:60]}")
        self._checkpoint()
        if deepsearch_answer is None:
            self.registry.log(task, "[web] web module unavailable")
            self.registry.set_state(task, TaskState.FAILED)
            self._finish(task)
            return
        try:
            res = deepsearch_answer(prompt)
        except Exception as e:  # noqa: BLE001
            self.registry.log(task, f"[web] error: {e}")
            self.registry.set_state(task, TaskState.FAILED)
            self._finish(task)
            return
        self.registry.set_progress(task, 0.7, 2)
        self.registry.log(task, f"[web] answer: {res['answer'][:200]}")
        for i, s in enumerate(res.get("sources", [])[:4]):
            self.registry.log(task, f"  src[{i}] {s.get('title','?')} — {s.get('url','')[:60]}")
        self.registry.set_progress(task, 1.0)
        self.registry.set_state(task, TaskState.DONE)
        self._finish(task)


class ResearchHarness(Harness):
    """DeepResearch harness: the iterative cousin of WebHarness.

    Where WebHarness does one search + one synthesis, this plans several
    queries, searches each in its own round, accumulates deduped citations,
    and stops when the findings cover the question or the round budget
    (max_steps) runs out. The loop lives in research.run_research; this class
    only bridges its per-step callback to the task's progress/log and honours
    pause/kill.
    """

    async def run(self, task: Task, prompt: str = "") -> None:
        from . import research
        from .web import chat, web_search

        self._running.add(task.id)
        self.registry.set_state(task, TaskState.RUNNING)
        max_rounds = self.cfg.max_steps or 4

        def report_step(phase: str, detail: str, done: int, total: int) -> bool:
            # Runs in the to_thread worker, off the event loop, so it cannot use
            # registry methods (they schedule bus publishes via create_task).
            # Mutate task fields directly instead -- the UI redraw polls the
            # registry, so progress is still visible -- and checkpoint straight
            # to disk (a plain file write, thread-safe). kill/pause are the only
            # window back into the harness while the sync loop runs.
            if self._killed(task):
                return False
            task.progress = min(0.99, done / max(1, total))
            task.eta = max(0, total - done)
            task.log.append(f"[research] {phase}: {detail[:70]}")
            if self.store:
                self.store.save(self.registry.tasks)
            return True

        try:
            # research.run_research is blocking (requests + LLM); keep the event
            # loop responsive by running it off-thread.
            report = await asyncio.to_thread(
                research.run_research, prompt, web_search, chat,
                max_rounds, report_step,
            )
        except Exception as e:  # noqa: BLE001
            self.registry.log(task, f"[research] error: {e}")
            self.registry.set_state(task, TaskState.FAILED)
            self._finish(task)
            return

        if report.stopped == "aborted" or self._killed(task):
            self.registry.set_state(task, TaskState.CANCELLED)
            self._finish(task)
            return

        self.registry.log(task, f"[research] {report.rounds} round(s), "
                                f"{len(report.sources)} source(s), stop: {report.stopped}")
        for line in report.answer.splitlines():
            if line.strip():
                self.registry.log(task, line[:200])
        for i, s in enumerate(report.sources[:6]):
            self.registry.log(task, f"  [{i+1}] {s.title[:50]} — {s.url[:60]}")
        await self._stat(rounds=report.rounds, sources=len(report.sources))
        # feed the brain: register this research run as a holarchy node linked to
        # its sources, so knowledge accumulates across runs and the swarm can
        # reconstruct it. Coherence: a covered answer flowed (+1), budget = idle.
        await asyncio.to_thread(self._ingest_research, task, report)
        self.registry.set_progress(task, 1.0)
        self.registry.set_state(task, TaskState.DONE)
        self._finish(task)

    @staticmethod
    def _ingest_research(task: Task, report) -> None:
        """Push a research run into physis (holarchy + coherence). Soft-fails."""
        from .physis import get_client, record_outcome
        node = f"research:{task.id}"
        edges = [s.url for s in report.sources[:8] if s.url]
        try:
            get_client().ingest(node, edges or None)
        except Exception:  # noqa: BLE001  (brain optional, never fatal)
            pass
        record_outcome(node, 1.0 if report.stopped == "covered" else 0.0,
                       task.domain or None)


class FactoryHarness(Harness):
    """Factory loop: run an agent command over and over until it signals done
    or the iteration budget (max_steps) runs out — Ralph-style orchestration.

    Config (extra):
      command:      template run each round; {n} iteration, {p} prompt,
                    {last} tail of the previous output
      done_marker:  output substring meaning "finished" (e.g. TASK_COMPLETE)
      done_command: shell check, exit 0 == finished (e.g. "pytest -q")
      stop_on_error: end the loop on a non-zero agent exit (default True)
      per_iter_timeout: seconds per agent run (default 120)

    The loop lives in factory.run_factory; this class supplies the real
    subprocess runner and bridges the per-iteration callback to task
    progress/log, honouring kill.
    """

    async def run(self, task: Task, prompt: str = "") -> None:
        from . import factory

        self._running.add(task.id)
        self.registry.set_state(task, TaskState.RUNNING)
        extra = self.cfg.extra or {}
        command = extra.get("command") or self.cfg.command
        if not command:
            self.registry.log(task, "[factory] no command configured")
            self.registry.set_state(task, TaskState.FAILED)
            self._finish(task)
            return

        fcfg = factory.FactoryConfig(
            command=command,
            max_iters=self.cfg.max_steps or 10,
            done_marker=extra.get("done_marker", ""),
            done_command=extra.get("done_command", ""),
            stop_on_error=extra.get("stop_on_error", True),
            # a spinning Ralph loop wastes the whole budget; bail after this many
            # near-identical rounds. 0 in config == off; default the harness on.
            stall_window=int(extra.get("stall_window", 3)),
            stall_novelty=float(extra.get("stall_novelty", 0.1)),
            # Off unless asked for, and only meaningful with `coherence` on —
            # see below, where a window without a scorer is refused rather
            # than left to look armed.
            coherence_window=int(extra.get("coherence_window", 0)),
            coherence_floor=float(extra.get("coherence_floor", -0.2)),
        )
        timeout = float(extra.get("per_iter_timeout", 120))
        # physis coherence per round is opt-in (a classify call per iteration).
        # It feeds the HUD, and with `coherence_window` set it also ends a run
        # that has drifted. Runs in the worker thread with run_factory —
        # blocking urllib, no registry access. OK.
        coherence_fn = None
        if extra.get("coherence"):
            from .physis import score_text
            coherence_fn = score_text
        elif fcfg.coherence_window:
            # A window with nothing scoring is a guard that will never fire and
            # a config that reads as if it will. Say so once, here, rather than
            # let someone conclude the brain approved of a run it never saw.
            fcfg.coherence_window = 0
            self.registry.log(task, "[factory] coherence_window ignored: "
                                    "set \"coherence\": true to score rounds")

        def run_cmd(cmd: str) -> tuple[int, str]:
            import subprocess
            try:
                p = subprocess.run(cmd, shell=True, capture_output=True,
                                   text=True, timeout=timeout)
                return p.returncode, (p.stdout or "") + (p.stderr or "")
            except subprocess.TimeoutExpired:
                return 124, f"(timed out after {timeout}s)"

        def check_cmd(cmd: str) -> int:
            import subprocess
            try:
                return subprocess.run(cmd, shell=True, capture_output=True,
                                      text=True, timeout=timeout).returncode
            except subprocess.TimeoutExpired:
                return 1

        def report_step(n: int, total: int, exit_code: int, tail: str,
                        it=None) -> bool:
            # Runs off the event loop (to_thread), so mutate task fields
            # directly and checkpoint to disk — same constraint as research.
            if self._killed(task):
                return False
            task.progress = min(0.99, n / max(1, total))
            task.eta = max(0, total - n)
            if it is not None:                 # live coherence/novelty for the HUD
                task.coherence = it.coherence
                task.novelty = it.novelty
            if tail:
                task.log.append(f"[factory] iter {n}/{total} (exit {exit_code})")
                for line in tail.strip().splitlines()[-3:]:
                    task.log.append(f"  {line[:100]}")
            if self.store:
                self.store.save(self.registry.tasks)
            return True

        try:
            result = await asyncio.to_thread(
                factory.run_factory, prompt, fcfg, run_cmd, check_cmd,
                report_step, coherence_fn,
            )
        except Exception as e:  # noqa: BLE001
            self.registry.log(task, f"[factory] error: {e}")
            self.registry.set_state(task, TaskState.FAILED)
            self._finish(task)
            return

        if result.stopped == factory.STOP_ABORTED or self._killed(task):
            self.registry.set_state(task, TaskState.CANCELLED)
            self._finish(task)
            return

        self.registry.log(task, f"[factory] stopped: {result.stopped} "
                                f"after {result.count} iteration(s)")
        if result.stopped == factory.STOP_STALLED:
            self.registry.log(task, "[factory] output stopped changing — bailed "
                                    "out of a spinning loop instead of burning "
                                    "the budget")
        if result.stopped == factory.STOP_INCOHERENT:
            recent = [round(it.coherence, 2) for it in result.iterations[-3:]]
            self.registry.log(task, "[factory] the brain stopped recognising this "
                                    f"work (last scores {recent}) — stopped before "
                                    "the budget went on drift")
        await self._stat(iterations=result.count, stopped=result.stopped)
        # feed the outcome back to physis: this task-domain flowed (+1) or got
        # blocked (-1). Soft-fails if physis is down; off the loop thread.
        outcome = (1.0 if result.stopped == factory.STOP_DONE
                   else -1.0 if result.stopped in (factory.STOP_ERROR,
                                                   factory.STOP_STALLED,
                                                   factory.STOP_INCOHERENT)
                   else 0.0)
        from .physis import record_outcome
        await asyncio.to_thread(record_outcome, f"task:{task.id}", outcome,
                                task.domain or None)
        self.registry.set_progress(task, 1.0)
        # a loop that ran out of budget or died is not a success
        final = TaskState.DONE if result.stopped == factory.STOP_DONE \
            else TaskState.FAILED if result.stopped == factory.STOP_ERROR \
            else TaskState.INTERRUPTED
        self.registry.set_state(task, final)
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


class SystemHarness(Harness):
    """Iron Man HUD poller: real-time COMPUTER statistics.

    Reads CPU / RAM / disk / network / GPU through psutil (+ reuses the
    TelemetryHarness GPU probe) and publishes a structured dict on
    TOPIC_STATS under harness id "system". The `sys` workspace renders from
    it. No task needed — call .start() once at boot, like the other pollers.

    Degrades gracefully if psutil is missing (emits ok:False so the HUD can
    show "(stats unavailable)").

    Config extras:
      interval: seconds between reads   (default 2.0)
      disk:     list of mount points     (default: all physical mounts)
    """

    def __init__(self, cfg: HarnessConfig, bus: Bus, registry: TaskRegistry, store=None):
        super().__init__(cfg, bus, registry, store)
        extra = cfg.extra or {}
        self.interval = float(extra.get("interval", 2.0))
        self.disk_paths = extra.get("disk") or None
        self._task: asyncio.Task | None = None
        self._reader = None

    async def run(self, task: Task, prompt: str = "") -> None:  # pragma: no cover
        return

    def _ensure_reader(self):
        if self._reader is None:
            from .sysinfo import SystemReader
            self._reader = SystemReader(disk_paths=self.disk_paths)
        return self._reader

    async def poll_once(self) -> dict:
        reader = self._ensure_reader()
        snap = await asyncio.to_thread(reader.snapshot)
        metrics = snap
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


class HealthHarness(Harness):
    """Iron Man HUD poller: REAL-LIFE statistics (health / fitness / sleep).

    Reads from a pluggable HealthReader (Google Fit / Apple Health / JSON)
    and publishes a normalized summary on TOPIC_STATS under harness id
    "health". The `sys` workspace renders it. .start() once at boot.

    Config extras:
      source: "json" | "google" | "apple"   (default "json")
      path:   file/dir for the source        (default ~/.aion/health.json)
      interval: seconds between reads         (default 30.0)
    """

    def __init__(self, cfg: HarnessConfig, bus: Bus, registry: TaskRegistry, store=None):
        super().__init__(cfg, bus, registry, store)
        extra = cfg.extra or {}
        self.source = extra.get("source", "json")
        from .fleet import shared_path
        self.path = extra.get("path") or str(shared_path("health.json"))
        self.interval = float(extra.get("interval", 30.0))
        self._task: asyncio.Task | None = None
        self._reader = None

    async def run(self, task: Task, prompt: str = "") -> None:  # pragma: no cover
        return

    def _ensure_reader(self):
        if self._reader is None:
            from .health import HealthReader
            self._reader = HealthReader(source=self.source, path=self.path)
        return self._reader

    async def poll_once(self) -> dict:
        reader = self._ensure_reader()
        summary = await asyncio.to_thread(reader.summary)
        await self._stat(**summary)
        return summary

    async def start(self) -> None:
        self._task = asyncio.create_task(self._poll())

    async def _poll(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception as e:  # noqa: BLE001
                await self._stat(ok=False, error=str(e)[:60])
            await asyncio.sleep(self.interval)


class VaultHarness(Harness):
    """Iron Man HUD poller: OBSIDIAN-VAULT graph for notes.

    Reads the markdown vault (notes/ by default) and publishes a graph
    (nodes + edges + backlinks) on TOPIC_STATS under harness id "vault".
    The `vault` workspace renders it. On first boot it PROMPTS the user to
    set up their storage (path). .start() once at boot.

    Config extras:
      root:     vault directory             (default <repo>/notes)
      interval: seconds between re-reads     (default 15.0)
      prompt_setup: bool, ask user to set path first run (default True)
    """

    def __init__(self, cfg: HarnessConfig, bus: Bus, registry: TaskRegistry, store=None):
        super().__init__(cfg, bus, registry, store)
        extra = cfg.extra or {}
        self.interval = float(extra.get("interval", 15.0))
        self.prompt_setup = bool(extra.get("prompt_setup", True))
        root = extra.get("root")
        if not root:
            from .paths import notes_dir
            root = str(notes_dir())
        self.root = root
        self._task: asyncio.Task | None = None
        self._reader = None

    async def run(self, task: Task, prompt: str = "") -> None:  # pragma: no cover
        return

    def _ensure_reader(self):
        if self._reader is None:
            from .vault import VaultReader
            self._reader = VaultReader(self.root)
        return self._reader

    async def poll_once(self) -> dict:
        reader = self._ensure_reader()
        graph = await asyncio.to_thread(reader.graph)
        graph["root"] = str(reader.root)
        graph["ok"] = reader.exists()
        await self._stat(**graph)
        return graph

    async def start(self) -> None:
        # first-run storage setup prompt (non-blocking, one-shot)
        if self.prompt_setup and not self._reader_setup_done():
            self._prompt_storage_setup()
        self._task = asyncio.create_task(self._poll())

    def _reader_setup_done(self) -> bool:
        from .fleet import shared_path
        flag = shared_path("vault_setup_done")
        return flag.exists()

    def _prompt_storage_setup(self) -> None:
        import os
        import sys

        from .fleet import shared_path
        flag = shared_path("vault_setup_done")
        # AION_VAULT skips the question entirely; so does having no terminal to
        # ask on. Without this the prompt printed on every headless run (and
        # ~15 times per test suite) before falling back to the default anyway.
        env_root = os.environ.get("AION_VAULT", "").strip()
        if env_root or not sys.stdin.isatty():
            if env_root:
                self.root = env_root
                self._reader = None
            self._mark_vault_setup_done(flag)
            return
        print("\n[VAULT SETUP] Choose your notes vault (Obsidian-style storage):")
        print(f"  [Enter] keep default: {self.root}")
        print("  or type an absolute path to a markdown vault (e.g. ~/Obsidian):")
        try:
            ans = input("  vault path > ").strip()
        except (EOFError, OSError):
            ans = ""  # headless / non-interactive -> keep default
        if ans:
            self.root = ans
            self._reader = None  # rebuild reader for new path
        print(f"[VAULT] using: {self.root}")
        self._mark_vault_setup_done(flag)

    def _mark_vault_setup_done(self, flag) -> None:
        try:
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.write_text(self.root)
        except Exception:  # noqa: BLE001
            pass

    async def _poll(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception as e:  # noqa: BLE001
                await self._stat(ok=False, error=str(e)[:60])
            await asyncio.sleep(self.interval)


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


class ProjectsHarness(Harness):
    """Projects workspace poller: live git + PR + session status per repo.

    Reads ground-truth git status for a configured repo list and joins in
    Hermes session activity, publishing a list of project cards on
    TOPIC_STATS under harness id "projects". No task needed — call .start()
    once at boot. All git/gh calls run off the event loop with timeouts.

    Config extras:
      repos:     list of repo paths      (default cyclops/aion/praxis_webapp)
      interval:  seconds between reads    (default 8.0; git is heavier)
      check_prs: bool, run `gh pr list`   (default False — CGNAT-safe)
      db_path:   override state.db path
    """

    def __init__(self, cfg: HarnessConfig, bus: Bus, registry: TaskRegistry, store=None):
        super().__init__(cfg, bus, registry, store)
        extra = cfg.extra or {}
        self.repos = extra.get("repos")
        self.interval = float(extra.get("interval", 8.0))
        self.check_prs = bool(extra.get("check_prs", False))
        self._db_path = extra.get("db_path")
        self._task: asyncio.Task | None = None
        self._reader = None

    async def run(self, task: Task, prompt: str = "") -> None:  # pragma: no cover
        return

    def _ensure_reader(self):
        if self._reader is None:
            from .projects import ProjectsReader
            self._reader = ProjectsReader(
                repos=self.repos, db_path=self._db_path,
                check_prs=self.check_prs)
        return self._reader

    async def poll_once(self) -> dict:
        reader = self._ensure_reader()
        items = await asyncio.to_thread(reader.as_items)
        metrics = {"ok": True, "projects": items}
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


class HermesHarness(Harness):
    """Runs a prompt through the Hermes agent CLI."""

    async def run(self, task: Task, prompt: str = "") -> None:
        self._running.add(task.id)
        self.registry.set_state(task, TaskState.RUNNING)
        self.registry.log(task, f"[hermes] dispatching: {prompt[:60]}")
        try:
            from .hermes.client import HermesClient
            client = HermesClient()
            lines: list[str] = []
            async for line in client.chat(prompt):
                lines.append(line)
                self.registry.log(task, line[:120])
            self.registry.set_progress(task, 1.0)
            self.registry.set_state(task, TaskState.DONE)
            self.registry.log(task, f"[hermes] done ({len(lines)} lines)")
        except Exception as e:
            self.registry.log(task, f"[hermes] error: {e}")
            self.registry.set_state(task, TaskState.FAILED)
        self._finish(task)


class OpenCodeHarness(Harness):
    """Runs OpenCode as a sub-harness via OpenCodeClient.

    Spawns ``opencode run <prompt> -m <model>`` for each task and streams
    response lines into the task log.  Supports pause / resume / cancel.
    """

    model: str = ""

    async def run(self, task: Task, prompt: str = "") -> None:
        self._running.add(task.id)
        self.registry.set_state(task, TaskState.RUNNING)
        from .hermes.opencode import OpenCodeClient, OpenCodeConfig

        cfg = OpenCodeConfig(
            model=self.model or self.cfg.extra.get("model", "deepseek/deepseek-v4-pro"),
            auto_approve=self.cfg.extra.get("auto", False),
            timeout=float(self.cfg.extra.get("timeout", 300)),
            dir=self.cfg.extra.get("dir", ""),
        )
        client = OpenCodeClient(cfg)
        lines = 0
        try:
            async for line in client.run(prompt):
                if self._killed(task):
                    self.registry.set_state(task, TaskState.CANCELLED)
                    self._finish(task)
                    return
                if not await self._wait_if_paused(task):
                    self.registry.set_state(task, TaskState.CANCELLED)
                    self._finish(task)
                    return
                self.registry.log(task, f"[opencode] {line}")
                lines += 1
            self.registry.set_state(task, TaskState.DONE)
            self.registry.log(task, f"[opencode] done ({lines} lines)")
        except (RuntimeError, TimeoutError) as e:
            self.registry.log(task, f"[opencode] error: {e}")
            self.registry.set_state(task, TaskState.FAILED)
            self._finish(task)


class SkillHarness(Harness):
    """Loads and runs a skill's workflow from a skill directory."""

    async def run(self, task: Task, prompt: str = "") -> None:
        self._running.add(task.id)
        self.registry.set_state(task, TaskState.RUNNING)
        skill_name = task.label.split(":", 1)[0].strip() if ":" in task.label else task.label
        self.registry.log(task, f"[skill] loading: {skill_name}")
        from .hermes.skills import SkillLoader
        info = SkillLoader().load(skill_name)
        if info is None:
            self.registry.log(task, f"[skill] '{skill_name}' not found")
            self.registry.set_state(task, TaskState.FAILED)
            self._finish(task)
            return
        sk_path = info.path / "SKILL.md"
        if not sk_path.exists():
            self.registry.log(task, f"[skill] no SKILL.md in {info.path}")
            self.registry.set_state(task, TaskState.FAILED)
            self._finish(task)
            return
        body = sk_path.read_text()
        self.registry.log(task, f"[skill] {info.name}: {body[:80]}...")
        steps = body.strip().split("\n\n")
        for i, chunk in enumerate(steps):
            if self._killed(task):
                self.registry.set_state(task, TaskState.CANCELLED)
                self._finish(task)
                return
            if not await self._wait_if_paused(task):
                self.registry.set_state(task, TaskState.CANCELLED)
                self._finish(task)
                return
            self.registry.log(task, f"[skill] step {i+1}/{len(steps)}: {chunk[:100]}")
            self.registry.set_progress(task, (i + 1) / len(steps))
            await asyncio.sleep(0.1)
        self.registry.set_state(task, TaskState.DONE)
        self.registry.log(task, f"[skill] {info.name} complete")
        self._finish(task)


class PhysisHarness(Harness):
    """The coherence brain, surfaced as a live HUD panel.

    Polls the running physis_pro engine (:19876) for embedder health
    and the reconstructed holarchy graph, publishing on TOPIC_PHYSIS so
    the `physis` workspace renders it. No task needed — call
    .start() once at boot like the other pollers. Soft-fails if
    physis is down (publishes a degraded marker, never throws)."""

    def __init__(self, cfg: HarnessConfig, bus: Bus, registry: TaskRegistry, store=None):
        super().__init__(cfg, bus, registry, store)
        self._task: asyncio.Task | None = None

    async def run(self, task: Task, prompt: str = "") -> None:  # pragma: no cover
        return

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._poll())

    async def _poll(self) -> None:
        from .core import TOPIC_PHYSIS
        from .physis import get_client
        client = get_client()
        while True:
            try:
                health = client.embedder_health()
                recon = client.reconstruct() or {}
                await self.bus.publish(TOPIC_PHYSIS, {
                    "action": "snapshot",
                    "degraded": bool(health.get("degraded")),
                    "kind": health.get("kind", "unknown"),
                    "semantic": bool(health.get("semantic")),
                    "graph": recon,
                })
            except Exception as e:  # noqa: BLE001
                await self.bus.publish(TOPIC_PHYSIS, {"action": "error", "detail": str(e)[:160]})
            await asyncio.sleep(float(self.cfg.extra.get("interval", 5.0)))


class LifeHarness(Harness):
    """Real-life HUD poller: money · fitness · social · computer.

    Wraps aion.life.collect_life — the same pure collector the tests use —
    and publishes its snapshot on TOPIC_STATS under harness id "life", so
    `state.stats["life"]` is all the panel needs. The computer domain comes
    from whatever SystemHarness already published (read out of store stats)
    so both panels agree on machine truth. Soft-fails per domain by design;
    .start() once at boot like the other pollers.

    Config extras:
      interval: seconds between polls (default 60.0 — life moves slower
                than telemetry; praxis/money do not change by the second)
    """

    def __init__(self, cfg: HarnessConfig, bus: Bus, registry: TaskRegistry, store=None):
        super().__init__(cfg, bus, registry, store)
        self.interval = float((cfg.extra or {}).get("interval", 60.0))
        self._task: asyncio.Task | None = None

    async def run(self, task: Task, prompt: str = "") -> None:  # pragma: no cover
        return

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._poll())

    async def _poll(self) -> None:
        from .life import LifeConfig, collect_life
        cfg = LifeConfig.from_env(cfg=self.cfg.extra or {})
        while True:
            try:
                sys_stats = {}
                if self.store is not None:
                    sys_stats = getattr(self.store.state, "stats", {}).get("system", {})
                snap = await asyncio.to_thread(collect_life, cfg, None, sys_stats)
                # publish through the standard stats channel
                await self.bus.publish(TOPIC_STATS, {
                    "harness": self.id,
                    "metrics": {"snapshot": snap, "ok": True},
                })
            except Exception as e:  # noqa: BLE001
                await self._stat(ok=False, error=str(e)[:80],
                                 snapshot={"domains": {}})
            await asyncio.sleep(self.interval)


class AgentEntityHarness(Harness):
    """Persistent agent entity harness with peripheral health context.

    Polls the agent store for each registered agent, checks the task
    registry for any running task assigned to that agent, and publishes
    agent status on TOPIC_STATS under harness id "agent_entity".
    Also reads peripheral health data (Cyclops/CyclUno) and injects
    health snapshots into agent memory so agents are aware of user
    health state.
    No task needed — call .start() once at boot.

    Config extras:
      interval: seconds between polls (default 3.0)
    """

    def __init__(self, cfg: HarnessConfig, bus: Bus, registry: TaskRegistry, store=None):
        super().__init__(cfg, bus, registry, store)
        extra = cfg.extra or {}
        self.interval = float(extra.get("interval", 3.0))
        self.health_path = extra.get("health_path")
        self._task: asyncio.Task | None = None
        self._agent_store = None
        self._health_reader = None
        self._last_health_date: str = ""

    def _ensure_store(self):
        if self._agent_store is None:
            from .agents import AgentStore
            self._agent_store = AgentStore()
        return self._agent_store

    def _ensure_health_reader(self):
        if self._health_reader is None:
            from .health import HealthReader
            self._health_reader = HealthReader(path=self.health_path)
        return self._health_reader

    async def run(self, task: Task, prompt: str = "") -> None:
        return

    async def start(self) -> None:
        self._task = asyncio.create_task(self._poll())

    async def poll_once(self) -> dict:
        store = self._ensure_store()
        agents = store.list_all()

        # peripheral health context for agents
        reader = self._ensure_health_reader()
        summary = await asyncio.to_thread(reader.summary)
        health_context = {}
        if summary.get("ok"):
            latest = summary.get("latest") or {}
            av = summary.get("avg_7d", {})
            health_context = {
                "steps": latest.get("steps", 0),
                "heart_rate": latest.get("heart_rate", 0),
                "sleep_hours": latest.get("sleep_hours", 0),
                "active_calories": latest.get("active_calories", 0),
                "avg_steps_7d": av.get("steps", 0),
                "avg_sleep_7d": av.get("sleep_hours", 0),
            }
            # inject health snapshot into each agent's memory once per day
            today = latest.get("date", "")
            if today and today != self._last_health_date:
                self._last_health_date = today
                snapshot = (f"Health snapshot {today}: "
                            f"{latest.get('steps',0)} steps, "
                            f"{latest.get('heart_rate',0)} bpm, "
                            f"{latest.get('sleep_hours',0)}h sleep, "
                            f"{latest.get('active_calories',0)} kcal")
                for a in agents:
                    store.add_memory(a.id, snapshot, kind="health")

        items = []
        for a in agents:
            task_status = "idle"
            task_label = ""
            task_progress = 0.0
            if a.current_task_id and a.current_task_id in self.registry.tasks:
                t = self.registry.tasks[a.current_task_id]
                task_status = t.state.value
                task_label = t.label
                task_progress = t.progress
            items.append({
                "id": a.id,
                "name": a.name,
                "status": a.status.value,
                "goal": a.goal,
                "capabilities": a.capabilities,
                "mem_count": len(a.memory_entries),
                "task_status": task_status,
                "task_label": task_label,
                "task_progress": task_progress,
                "assigned_board": a.assigned_board,
            })
        metrics = {"ok": True, "agents": items, "count": len(items),
                   "health_context": health_context}
        await self._stat(**metrics)
        return metrics

    async def _poll(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception as e:
                await self._stat(ok=False, error=str(e)[:60])
            await asyncio.sleep(self.interval)


class BoardHarness(Harness):
    """Board (kanban) poller.

    Reads the board store and publishes board + card data on TOPIC_STATS
    under harness id "board". The board workspace renders from it.
    No task needed — .start() once at boot.

    Config extras:
      interval: seconds between polls (default 5.0)
    """

    def __init__(self, cfg: HarnessConfig, bus: Bus, registry: TaskRegistry, store=None):
        super().__init__(cfg, bus, registry, store)
        extra = cfg.extra or {}
        self.interval = float(extra.get("interval", 5.0))
        self._task: asyncio.Task | None = None
        self._board_store = None

    def _ensure_store(self):
        if self._board_store is None:
            from .board import BoardStore
            self._board_store = BoardStore()
        return self._board_store

    async def run(self, task: Task, prompt: str = "") -> None:
        return

    async def start(self) -> None:
        self._task = asyncio.create_task(self._poll())

    async def poll_once(self) -> dict:
        store = self._ensure_store()
        boards = store.list_all()
        items = []
        for b in boards:
            columns = {}
            for col in b.columns:
                columns[col] = [c.as_dict() for c in b.cards_in_column(col)]
            items.append({
                "id": b.id,
                "title": b.title,
                "columns": b.columns,
                "column_data": columns,
                "card_count": len(b.cards),
            })
        metrics = {"ok": True, "boards": items, "count": len(items)}
        await self._stat(**metrics)
        return metrics

    async def _poll(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception as e:
                await self._stat(ok=False, error=str(e)[:60])
            await asyncio.sleep(self.interval)


HARNESS_TYPES = {
    "demo": DemoHarness,
    "shell": ShellHarness,
    "remote": RemoteHarness,
    "cyclops": CyclopsHarness,
    "telemetry": TelemetryHarness,
    "stats": StatsHarness,
    "projects": ProjectsHarness,
    "app": AppHarness,
    "hermes": HermesHarness,
    "skill": SkillHarness,
    "opencode": OpenCodeHarness,
    "web": WebHarness,
    "research": ResearchHarness,
    "factory": FactoryHarness,
    "system": SystemHarness,
    "health": HealthHarness,
    "vault": VaultHarness,
    "physis": PhysisHarness,
    "life": LifeHarness,
    "agent_entity": AgentEntityHarness,
    "board": BoardHarness,
}


def build_harnesses(cfgs: list[dict], bus: Bus, registry: TaskRegistry,
                    store=None) -> dict[str, Harness]:
    out: dict[str, Harness] = {}
    for c in cfgs:
        cfg = HarnessConfig.from_dict(c)
        if not cfg.enabled:
            continue
        if cfg.type == "term":
            # lazy import to avoid a circular import (term <-> harnesses)
            from .term import TermHarness
            cls = TermHarness
        else:
            cls = HARNESS_TYPES.get(cfg.type, DemoHarness)
        out[cfg.id] = cls(cfg, bus, registry, store)
    return out
