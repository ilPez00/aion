"""vault.py — Obsidian-style vault reader.

Scans a directory of markdown notes, extracts [[wikilinks]] + #tags, and
builds a graph (nodes + edges + backlinks + previews) the HUD can render.
Pluggable storage: path is configurable, defaulting to the repo ``notes/``
dir, but it can point at a real Obsidian vault (e.g. ~/Obsidian/).

No Obsidian DB needed — we parse the markdown ourselves so it works on any
vault, online or off.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
_TAG = re.compile(r"(?:^|\s)#([A-Za-z0-9_/-]+)", re.MULTILINE)
_HEADING = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)


@dataclass
class Note:
    name: str
    path: str
    title: str
    links: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    headings: list[str] = field(default_factory=list)
    preview: str = ""
    backlinks: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "name": self.name, "title": self.title, "path": self.path,
            "links": self.links, "tags": self.tags, "headings": self.headings,
            "preview": self.preview, "backlinks": self.backlinks,
            "degree": len(self.links) + len(self.backlinks),
        }


class VaultReader:
    """Parse a markdown vault into a navigable graph."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def exists(self) -> bool:
        return self.root.is_dir()

    def _name_for(self, p: Path) -> str:
        return p.stem

    def read(self) -> dict[str, Note]:
        notes: dict[str, Note] = {}
        if not self.exists():
            return notes
        for p in sorted(self.root.rglob("*.md")):
            if ".git" in p.parts or p.name.lower() == "readme.md":
                continue
            text = p.read_text(encoding="utf-8", errors="ignore")
            links = []
            for m in _WIKILINK.findall(text):
                # [[Target|alias]] -> Target ; [[Target]] -> Target
                target = m.split("|", 1)[0].strip()
                if target:
                    links.append(target)
            tags = _TAG.findall(text)
            headings = [h.group(1).strip() for h in _HEADING.finditer(text)]
            # preview = first non-empty line that isn't a heading/link-only
            preview_lines = [ln.strip() for ln in text.splitlines()
                             if ln.strip() and not ln.strip().startswith("#")]
            preview = preview_lines[0][:120] if preview_lines else ""
            # title: prefer the first H1 heading; fall back to a prettified name
            if headings:
                title = headings[0]
            else:
                title = self._name_for(p).replace("_", " ").replace("-", " ")
            note = Note(
                name=self._name_for(p), path=str(p.relative_to(self.root)),
                title=title,
                links=links, tags=tags, headings=headings, preview=preview,
            )
            notes[note.name] = note
        # resolve backlinks by name (note links may use title text)
        by_title = {n.title.lower(): n.name for n in notes.values()}
        for n in notes.values():
            for l in n.links:
                target = notes.get(l) or notes.get(by_title.get(l.lower(), ""))
                if target is not None and target.name != n.name:
                    if n.name not in target.backlinks:
                        target.backlinks.append(n.name)
        return notes

    def graph(self) -> dict[str, Any]:
        notes = self.read()
        nodes = [n.as_dict() for n in notes.values()]
        edges = []
        title_to_name = {n.title.lower(): n.name for n in notes.values()}
        for n in notes.values():
            for l in n.links:
                target = notes.get(l) or notes.get(title_to_name.get(l.lower(), ""))
                if target is not None and target.name != n.name:
                    edges.append({"from": n.name, "to": target.name})
                elif l not in notes and l.lower() not in title_to_name:
                    # dangling link (a real Obsidian feature: future note)
                    edges.append({"from": n.name, "to": l, "dangling": True})
        # order nodes by graph degree (most-connected first) for an
        # instantly-readable "hub" view
        nodes.sort(key=lambda d: d["degree"], reverse=True)
        return {
            "root": str(self.root),
            "count": len(nodes),
            "nodes": nodes,
            "edges": edges,
            "tags": sorted({t for n in notes.values() for t in n.tags}),
        }


def render_tree(graph: dict[str, Any], root: str | None = None, depth: int = 2) -> str:  # noqa: A002
    root_name: str = root or ""
    """A compact indented link-tree (reads like an Obsidian graph,
    minus the canvas). Returns plain text."""
    by_name = {n["name"]: n for n in graph["nodes"]}
    title_to_name = {n["title"].lower(): n["name"] for n in graph["nodes"]}
    if not root_name:
        # pick the highest-degree node as the seed
        root_name = graph["nodes"][0]["name"] if graph["nodes"] else ""
    if root_name not in by_name:
        return f"(unknown note: {root_name})"
    seen: set[str] = set()
    lines: list[str] = []

    def walk(name: str, indent: int) -> None:  # type: ignore[no-redef]
        if indent > depth or name in seen or name not in by_name:
            return
        seen.add(name)
        n = by_name[name]
        lines.append(f"{'  ' * indent}• {n['title']}  "
                     f"[#5a6b7b][{len(n['links'])}→{len(n['backlinks'])}][/]")
        for l in n["links"][:6]:
            target = by_name.get(l) or by_name.get(title_to_name.get(l.lower(), ""))
            if target is not None:
                walk(target["name"], indent + 1)

    walk(root_name, 0)
    return "\n".join(lines) if lines else "(empty vault)"
