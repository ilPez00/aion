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
                    self._stat(target=target, recv=len(buf))
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
        self.path = extra.get("path") or str(Path.home() / ".aion" / "health.json")
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
            root = str(Path(__file__).resolve().parents[2] / "notes")
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
        from pathlib import Path as _P
        flag = _P.home() / ".aion" / "vault_setup_done"
        return flag.exists()

    def _prompt_storage_setup(self) -> None:
        from pathlib import Path as _P
        flag = _P.home() / ".aion" / "vault_setup_done"
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
    "system": SystemHarness,
    "health": HealthHarness,
    "vault": VaultHarness,
    "physis": PhysisHarness,
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
