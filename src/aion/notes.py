"""notes.py — agent vault note writer for aion (Cycle C).

Pure, testable: write a note to <root>/<path>.md with frontmatter. Used by the
agent's `vault` tool so the cockpit can persist structured notes that survive
sessions and are greppable alongside the rest of the knowledge graph.

(Named `notes.py` to avoid colliding with the HUD `aion.vault` VaultReader.)
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path


def vault_root() -> Path:
    from .fleet import shared_path
    return shared_path("vault")


def write_note(path: str, content: str, *, root: Path | None = None,
               tags: list[str] | None = None) -> str:
    """Write a markdown note. `path` is a slash-separated note name
    (e.g. 'ideas/agent-loop'). Returns the absolute path written as a string.

    Creates parent dirs. Adds YAML frontmatter (title, date, tags). Idempotent
    on filename; overwrites content.
    """
    root = root or vault_root()
    # sanitize: no absolute paths, no '..'
    clean = path.strip().lstrip("/")
    safe = "/".join(seg for seg in clean.split("/") if seg not in ("", ".", ".."))
    if not safe:
        raise ValueError("vault path must be non-empty")
    target = root / f"{safe}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    title = safe.rsplit("/", 1)[-1].replace("_", " ").title()
    today = datetime.now().strftime("%Y-%m-%d")
    tag_line = ", ".join(tags or [])
    fm = (
        f"---\ntitle: {title}\n"
        f"date: {today}\n"
        f"tags: [{tag_line}]\n---\n\n"
    )
    target.write_text(fm + content.strip() + "\n", encoding="utf-8")
    return str(target)
