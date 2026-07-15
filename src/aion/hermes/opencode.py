from __future__ import annotations

import asyncio
import os
import re
import signal as _signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator


ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


@dataclass
class OpenCodeConfig:
    model: str = "deepseek/deepseek-v4-pro"
    binary_path: str = "opencode"
    auto_approve: bool = False
    timeout: float = 300.0
    dir: str = ""
    session: str = ""


def strip_ansi(text: str) -> str:
    return ANSI_PATTERN.sub("", text)


class OpenCodeClient:
    """Async wrapper around the `opencode run` CLI.

    Spawns `opencode run <prompt> -m <model> [--auto]` as a subprocess and
    yields response lines as an async iterator. Handles ANSI stripping,
    timeout, and cleanup.
    """

    def __init__(self, config: OpenCodeConfig | None = None) -> None:
        self.config = config or OpenCodeConfig()

    @property
    def binary(self) -> str:
        return self.config.binary_path

    @property
    def model(self) -> str:
        return self.config.model

    async def run(self, prompt: str) -> AsyncIterator[str]:
        args = [self.binary, "run", prompt, "-m", self.model]
        if self.config.auto_approve:
            args.append("--auto")
        if self.config.session:
            args += ["-s", self.config.session]

        cwd = self.config.dir or None

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
        except FileNotFoundError as e:
            raise RuntimeError(
                f"opencode binary not found: {self.binary}"
            ) from e
        except Exception as e:
            raise RuntimeError(f"failed to spawn opencode: {e}") from e

        assert proc.stdout is not None
        assert proc.stderr is not None

        line_count = 0
        try:
            while True:
                line = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(
                        None, proc.stdout.readline
                    ),
                    timeout=self.config.timeout,
                )
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if not text.strip():
                    continue
                clean = strip_ansi(text).strip()
                if not clean or clean.startswith("timestamp="):
                    continue
                line_count += 1
                yield clean
        except asyncio.TimeoutError:
            proc.kill()
            raise TimeoutError(
                f"opencode timed out after {self.config.timeout}s"
                f" ({line_count} lines read)"
            )
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except Exception:
                    pass
            else:
                await proc.wait()

        stderr_text = ""
        try:
            stderr_bytes = await asyncio.wait_for(
                proc.stderr.read(), timeout=1.0
            )
            stderr_text = stderr_bytes.decode("utf-8", errors="replace")
        except Exception:
            pass

        if proc.returncode != 0 and line_count == 0:
            error_line = "no output"
            for line in stderr_text.splitlines():
                if "error" in line.lower():
                    error_line = line[:120]
                    break
            raise RuntimeError(
                f"opencode exited {proc.returncode}: {error_line}"
            )

    async def is_available(self) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                self.binary, "--version",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=5.0)
            return proc.returncode == 0
        except (FileNotFoundError, asyncio.TimeoutError):
            return False
