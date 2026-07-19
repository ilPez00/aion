"""remotes.py — control remote aion instances from one cockpit.

Architecture:
  - Each aion instance runs a `RemoteServer` (async HTTP listener) on a port.
  - A "controller" aion uses `RemoteClient` to poll status and send commands.
  - `RemoteNode` stores the last-known state for one remote instance.
  - Discovery: via config file (static) or mDNS (future).

Transport: plain HTTP/1.1 JSON. No dependencies beyond stdlib.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any


# ── Data ─────────────────────────────────────────────────────────────────────
@dataclass
class RemoteNode:
    """Snapshot of one remote aion instance."""
    id: str                      # short name (e.g. "workstation", "pi5")
    host: str                    # IP or hostname
    port: int = 8765
    alive: bool = False
    last_seen: float = 0.0
    label: str = ""

    # remote state (polled)
    tasks: list[dict] = field(default_factory=list)
    running_count: int = 0
    active_harness: str = ""
    stats: dict = field(default_factory=dict)
    version: str = ""
    hostname: str = ""

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def age_s(self) -> float:
        return time.time() - self.last_seen if self.last_seen else 9999


# ── Client ────────────────────────────────────────────────────────────────────
class RemoteClient:
    """Async HTTP client to talk to a remote aion instance."""

    def __init__(self, timeout: float = 5.0) -> None:
        self._timeout = timeout

    async def fetch_status(self, node: RemoteNode) -> dict | None:
        """GET /status → dict or None on failure."""
        try:
            data = await self._request(node, "GET", "/status")
            if data:
                node.alive = True
                node.last_seen = time.time()
                node.tasks = data.get("tasks", [])
                node.running_count = data.get("running_count", 0)
                node.active_harness = data.get("active_harness", "")
                node.stats = data.get("stats", {})
                node.version = data.get("version", "")
                node.hostname = data.get("hostname", "")
            return data
        except Exception:
            node.alive = False
            return None

    async def run_task(self, node: RemoteNode, prompt: str,
                       harness: str = "") -> dict | None:
        """POST /run → task_id dict or None."""
        body = json.dumps({"prompt": prompt, "harness": harness or node.active_harness})
        return await self._request(node, "POST", "/run", body)

    async def cancel_task(self, node: RemoteNode, task_id: str) -> dict | None:
        """POST /cancel → result dict or None."""
        body = json.dumps({"task_id": task_id})
        return await self._request(node, "POST", "/cancel", body)

    async def _request(self, node: RemoteNode, method: str, path: str,
                       body: str | None = None) -> dict | None:
        """Raw HTTP/1.1 request via stdlib asyncio."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(node.host, node.port),
                timeout=self._timeout,
            )
            req_line = f"{method} {path} HTTP/1.1\r\n"
            headers = f"Host: {node.host}:{node.port}\r\nContent-Type: application/json\r\n"
            if body:
                headers += f"Content-Length: {len(body)}\r\n"
            headers += "Connection: close\r\n\r\n"
            writer.write((req_line + headers + (body or "")).encode())
            await writer.drain()

            # read response
            buf = b""
            while True:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=self._timeout)
                if not chunk:
                    break
                buf += chunk

            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

            # parse body after headers
            raw = buf.decode(errors="replace")
            _, _, rest = raw.partition("\r\n\r\n")
            if rest:
                return json.loads(rest)
            return None
        except Exception:
            return None


# ── Server ────────────────────────────────────────────────────────────────────
class RemoteServer:
    """Async HTTP listener that exposes a running aion instance.

    Callbacks:
      on_status() → dict : current state snapshot
      on_run(prompt, harness) → dict : run a task, return {task_id, ...}
      on_cancel(task_id) → dict : cancel a task
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 8765) -> None:
        self.host = host
        self.port = port
        self._server: asyncio.AbstractServer | None = None
        self.on_status = lambda: {}
        self.on_run = lambda p, h: {}
        self.on_cancel = lambda tid: {}

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, self.host, self.port
        )

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.read(65536), timeout=10)
            raw_str = raw.decode(errors="replace")
            lines = raw_str.split("\r\n")
            if not lines:
                return
            method, path, _ = lines[0].split(" ", 2)

            # read body
            body = ""
            in_body = False
            for line in lines[1:]:
                if not line.strip():
                    in_body = True
                    continue
                if in_body:
                    body += line

            if method == "GET" and path == "/status":
                result = self.on_status() if callable(self.on_status) else {}
                await self._reply(writer, 200, result)
            elif method == "POST" and path == "/run":
                try:
                    params = json.loads(body) if body else {}
                except json.JSONDecodeError:
                    params = {"prompt": body, "harness": ""}
                result = self.on_run(
                    params.get("prompt", ""),
                    params.get("harness", ""),
                ) if callable(self.on_run) else {"error": "no handler"}
                await self._reply(writer, 200, result)
            elif method == "POST" and path == "/cancel":
                try:
                    params = json.loads(body) if body else {}
                except json.JSONDecodeError:
                    params = {"task_id": body}
                result = self.on_cancel(params.get("task_id", "")) if callable(self.on_cancel) else {}
                await self._reply(writer, 200, result)
            else:
                await self._reply(writer, 404, {"error": "not found"})
        except Exception:
            try:
                await self._reply(writer, 500, {"error": "server error"})
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _reply(self, writer: asyncio.StreamWriter, status: int,
                     data: dict) -> None:
        body = json.dumps(data)
        reason = {200: "OK", 404: "Not Found", 500: "Internal Server Error"}.get(status, "OK")
        resp = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n"
            f"{body}"
        )
        writer.write(resp.encode())
        await writer.drain()
