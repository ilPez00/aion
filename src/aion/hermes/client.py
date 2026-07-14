from __future__ import annotations

import asyncio
import json as _json
import shlex
from pathlib import Path
from typing import AsyncIterator

HERMES_CLI = str(Path.home() / ".local" / "bin" / "hermes")


class HermesClient:
    """Async subprocess wrapper around the `hermes` CLI."""

    def __init__(self, cli: str = HERMES_CLI) -> None:
        self._cli = cli

    async def run(self, *args: str, timeout: int = 120,
                  stdin: str | None = None) -> str:
        proc = await asyncio.create_subprocess_exec(
            self._cli, *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(input=stdin.encode() if stdin else None),
                timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise
        stderr = err.decode().strip()
        if stderr:
            raise RuntimeError(stderr)
        return out.decode().strip()

    async def chat(self, prompt: str, skills: list[str] | None = None,
                   model: str | None = None) -> AsyncIterator[str]:
        args = [self._cli, "chat", "-z", prompt, "--cli"]
        if skills:
            args += ["--skills", ",".join(skills)]
        if model:
            args += ["-m", model]
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            yield line.decode().rstrip()
        await proc.wait()
        if proc.returncode != 0:
            err = (await proc.stderr.read()).decode().strip() if proc.stderr else ""
            raise RuntimeError(err or f"hermes exited {proc.returncode}")

    async def kanban_list(self, board: str | None = None) -> str:
        args = ["kanban", "list"]
        if board:
            args += ["--board", board]
        return await self.run(*args)

    async def memory_sections(self) -> str:
        return await self.run("memory", "status")

    async def skills_list(self) -> str:
        return await self.run("skills", "list")

    async def gateway_status(self) -> str:
        return await self.run("gateway", "status")

    async def is_available(self) -> bool:
        try:
            await self.run("version", timeout=5)
            return True
        except (FileNotFoundError, RuntimeError, asyncio.TimeoutError):
            return False
