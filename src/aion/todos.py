"""
todos.py — the dashboard TODO list.

Markdown-checklist-backed (`~/.aion/todos.md`, `- [ ] text` / `- [x] text`)
so the list is editable from any editor (or `app edit ~/.aion/todos.md`)
and survives outside aion. Pure stdlib, no UI imports.

Palette commands (wired in store._run_command):
    todo <text>       add an item
    todo done <n>     check item n
    todo rm <n>       delete item n
"""
from __future__ import annotations

import re
from pathlib import Path

def default_path() -> Path:
    """Resolved lazily: importing a module should not create directories."""
    from .fleet import shared_path
    return shared_path("todos.md")
_LINE = re.compile(r"^- \[( |x)\] (.*)$")


class TodoStore:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else default_path()

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        items = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            m = _LINE.match(line.strip())
            if m:
                items.append({"done": m.group(1) == "x", "text": m.group(2)})
        return items

    def _write(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"- [{'x' if it['done'] else ' '}] {it['text']}" for it in items]
        self.path.write_text("\n".join(lines) + ("\n" if lines else ""),
                             encoding="utf-8")

    # ---- API used by store + dashboard ----------------------------------
    def items(self) -> list[dict]:
        """Numbered items, open first (stable order within each group)."""
        raw = self._read()
        numbered = [{"n": i + 1, **it} for i, it in enumerate(raw)]
        return [it for it in numbered if not it["done"]] + \
               [it for it in numbered if it["done"]]

    def add(self, text: str) -> None:
        items = self._read()
        items.append({"done": False, "text": text.strip()})
        self._write(items)

    def done(self, n: int) -> bool:
        items = self._read()
        if 1 <= n <= len(items):
            items[n - 1]["done"] = True
            self._write(items)
            return True
        return False

    def rm(self, n: int) -> bool:
        items = self._read()
        if 1 <= n <= len(items):
            items.pop(n - 1)
            self._write(items)
            return True
        return False

    def open_count(self) -> int:
        return sum(1 for it in self._read() if not it["done"])
