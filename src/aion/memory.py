"""
memory.py — aion's persistent memory (the Jarvis "remember this" half).

Facts are one-line strings persisted to ~/.aion/memory.json (same crash-safe
philosophy as the session store: rewrite on every change, load at boot).

Commands (palette, voice, or deck):
    note <text>       remember a fact
    mem <query>       recall — substring search, newest first
    forget <n>        drop fact #n (as listed in the memory workspace)

The memory workspace lists everything newest-first; searching narrows it.
"""
from __future__ import annotations

import json
import time
from pathlib import Path


class MemoryStore:
    def __init__(self, path: str | Path | None = None) -> None:
        from .fleet import shared_path
        self.path = Path(path or shared_path("memory.json"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.facts: list[dict] = self._load()
        self.query: str = ""          # active filter shown by the workspace

    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text())
            return [f for f in data if isinstance(f, dict) and "text" in f]
        except Exception:  # noqa: BLE001
            return []

    def _save(self) -> None:
        try:
            self.path.write_text(json.dumps(self.facts, indent=2))
        except Exception as e:  # noqa: BLE001
            print(f"[memory] save failed: {e}")

    # ---- operations -------------------------------------------------------
    def add(self, text: str) -> dict:
        fact = {"text": text.strip(), "ts": time.time()}
        self.facts.append(fact)
        self._save()
        return fact

    def search(self, query: str) -> list[dict]:
        q = query.strip().lower()
        hits = [f for f in self.facts if q in f["text"].lower()] if q else list(self.facts)
        return sorted(hits, key=lambda f: f["ts"], reverse=True)

    def forget(self, index: int) -> bool:
        """Drop by 1-based index into the current (filtered, newest-first) view."""
        view = self.search(self.query)
        if not 1 <= index <= len(view):
            return False
        self.facts.remove(view[index - 1])
        self._save()
        return True

    def items(self) -> list[dict]:
        """Rows for the memory workspace, honoring the active query."""
        out = []
        for i, f in enumerate(self.search(self.query), start=1):
            age_d = (time.time() - f["ts"]) / 86400
            when = "today" if age_d < 1 else f"{int(age_d)}d ago"
            out.append({"id": f"m{i}", "n": i, "text": f["text"], "when": when})
        return out
