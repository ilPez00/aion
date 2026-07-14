from __future__ import annotations

from pathlib import Path


MEMORY_FILE = Path.home() / ".hermes" / "memories" / "MEMORY.md"


class HermesMemoryReader:
    """Read-only access to Hermes MEMORY.md (section-separated)."""

    def __init__(self, path: str | Path = MEMORY_FILE) -> None:
        self._path = Path(path)

    def sections(self) -> list[dict]:
        if not self._path.exists():
            return []
        text = self._path.read_text()
        parts = text.split("\n§\n")
        return [
            {"index": i, "body": p.strip(), "preview": p.strip()[:120]}
            for i, p in enumerate(parts)
            if p.strip()
        ]

    def search(self, query: str) -> list[dict]:
        q = query.lower()
        return [
            s for s in self.sections()
            if q in s["body"].lower()
        ]
