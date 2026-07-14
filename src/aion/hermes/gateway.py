from __future__ import annotations

import asyncio
import json as _json
from pathlib import Path


class GatewayBridge:
    """Bridges aion's event bus with the Hermes gateway.

    Current: reads gateway status via the Hermes CLI (read-only).
    Future: can subscribe to the gateway's event stream for bidirectional IPC.
    """

    def __init__(self) -> None:
        self._running = False

    async def status(self) -> dict:
        from .client import HermesClient
        try:
            raw = await HermesClient().gateway_status()
            return {"ok": True, "output": raw}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def start_polling(self, callback, interval: float = 30.0) -> None:
        self._running = True
        while self._running:
            try:
                st = await self.status()
                if callable(callback):
                    callback(st)
            except Exception:
                pass
            await asyncio.sleep(interval)

    def stop(self) -> None:
        self._running = False
