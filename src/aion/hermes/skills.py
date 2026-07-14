from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


SKILL_DIRS = [
    Path.home() / ".agents" / "skills",
    Path.home() / ".hermes" / "skills",
]


@dataclass
class SkillInfo:
    name: str
    path: Path
    description: str
    source: str

    @property
    def label(self) -> str:
        return self.name.replace("-", " ").title()


class SkillLoader:
    """Scan and search skills from local skill directories."""

    def __init__(self, dirs: list[Path] | None = None) -> None:
        self._dirs = dirs or SKILL_DIRS

    def list_all(self) -> list[SkillInfo]:
        seen: set[str] = set()
        out: list[SkillInfo] = []
        for d in self._dirs:
            if not d.exists():
                continue
            source = d.parent.name
            for child in sorted(d.iterdir()):
                if not child.is_dir():
                    continue
                name = child.name
                if name in seen:
                    continue
                seen.add(name)
                sk = child / "SKILL.md"
                desc = ""
                if sk.exists():
                    first = sk.read_text().strip().split("\n")[0]
                    desc = first.lstrip("# ").strip()[:120]
                out.append(SkillInfo(name=name, path=child,
                                    description=desc, source=source))
        return out

    def search(self, query: str) -> list[SkillInfo]:
        q = query.lower()
        return [s for s in self.list_all()
                if q in s.name.lower() or q in s.description.lower()]

    def load(self, name: str) -> SkillInfo | None:
        for s in self.list_all():
            if s.name == name:
                return s
        return None
