"""packets_board.py — multi-year work skeleton, visible (AION-003).

Read-only board over packet frontmatters. Claiming happens in the repo;
the HUD never edits.

Frontmatter: tiny YAML-subset parser (no new deps by design — the stdlib
covers `key: value` + `[a, b]` lists + `#` comments; full YAML would be a
dependency for 8 keys). Duplicate ids across repos surface as collisions.
Stale-claimed: claimed_at older than 7 days with no matching branch work.
Scan covers the node the HUD runs on; pansa holds the canonical copy
(mesh dev-sync direction) — stated in the panel footer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

STALE_CLAIM_DAYS = 7.0
SCAN_GLOBS = ("~/dev/*/packets/*.md", "~/ops/packets/*.md")


def parse_frontmatter(text: str) -> dict:
    """Parse the leading --- block. Missing/malformed -> {} (never raises)."""
    out: dict = {}
    try:
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            return {}
        try:
            end = lines.index("---", 1)
        except ValueError:
            return {}
        for line in lines[1:end]:
            line = line.split("#", 1)[0].rstrip()
            if not line.strip() or ":" not in line:
                continue
            key, _, val = line.partition(":")
            key, val = key.strip(), val.strip().strip("\"'")
            if val.startswith("[") and val.endswith("]"):
                out[key] = [v.strip().strip("\"'") for v in val[1:-1].split(",")
                            if v.strip()]
            else:
                out[key] = val
    except (ValueError, AttributeError, IndexError):
        return {}
    return out


@dataclass
class Packet:
    id: str
    path: str = ""
    track: str = ""
    phase: str = ""
    status: str = "todo"
    rung: str = ""
    depends: list = field(default_factory=list)
    claimed_by: str = ""
    claimed_at: str = ""

    @classmethod
    def from_dict(cls, fm: dict, path: str = "") -> "Packet | None":
        if not fm.get("id"):
            return None
        deps = fm.get("depends", [])
        if isinstance(deps, str):
            deps = [deps]
        return cls(id=str(fm["id"]), path=path, track=str(fm.get("track", "")),
                   phase=str(fm.get("phase", "")), status=str(fm.get("status", "todo")),
                   rung=str(fm.get("rung", "")), depends=list(deps or []),
                   claimed_by=str(fm.get("claimed_by", "")),
                   claimed_at=str(fm.get("claimed_at", "")))

    def stale_claim(self, now: datetime | None = None) -> bool:
        """Claimed but untouched for >7 days (protocol revert rule)."""
        if self.status != "claimed" or not self.claimed_at:
            return False
        try:
            ts = datetime.fromisoformat(self.claimed_at.replace("Z", "+00:00"))
            base = now or datetime.now(timezone.utc)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return (base - ts).total_seconds() > STALE_CLAIM_DAYS * 86400
        except (ValueError, TypeError):
            return False


@dataclass
class PacketsBoard:
    packets: list = field(default_factory=list)  # list[Packet]

    @classmethod
    def from_files(cls, paths: list) -> "PacketsBoard":
        out = []
        for p in paths:
            try:
                fm = parse_frontmatter(Path(p).read_text(encoding="utf-8",
                                                         errors="replace"))
            except OSError:
                continue
            pkt = Packet.from_dict(fm, path=str(p))
            if pkt is not None:
                out.append(pkt)
        return cls(packets=out)

    def by_status(self) -> dict:
        grouped: dict = {}
        for p in self.packets:
            grouped.setdefault(p.status or "todo", []).append(p)
        return grouped

    def by_phase(self) -> dict:
        grouped: dict = {}
        for p in self.packets:
            grouped.setdefault(p.phase or "?", []).append(p)
        return grouped

    def stale_claims(self, now: datetime | None = None) -> list:
        return [p for p in self.packets if p.stale_claim(now)]

    def blocked(self) -> list:
        return [p for p in self.packets if p.status == "blocked"]

    def collisions(self) -> dict:
        """Same id in >1 file — surfaced, never hidden."""
        seen: dict = {}
        for p in self.packets:
            seen.setdefault(p.id, []).append(p.path)
        return {i: ps for i, ps in seen.items() if len(ps) > 1}

    def summary(self) -> str:
        by = self.by_status()
        todo = len(by.get("todo", []))
        return (f"{len(self.packets)} packets · {todo} todo · "
                f"{len(by.get('review', []))} review · "
                f"{len(by.get('done', []))} done · "
                f"{len(by.get('blocked', []))} blocked")


def scan_packets() -> list:
    """Harness: expand SCAN_GLOBS on this node. Missing dirs -> []."""
    import glob
    out: list = []
    for pattern in SCAN_GLOBS:
        try:
            out.extend(sorted(glob.glob(str(Path(pattern).expanduser()))))
        except OSError:
            continue
    return [p for p in out if Path(p).name != "_TEMPLATE.md"]


def render_packets_board(board: PacketsBoard, theme: dict,
                         now: datetime | None = None) -> str:
    di = theme.get("dim", "#9aabbb")
    a = theme.get("accent", "#5ad1ff")
    ok_ = theme.get("ok", "#7CFFB2")
    warn = theme.get("warn", "#FFD479")
    err = theme.get("err", "#FF8A8A")
    out = [f"[{a}]▤ PACKETS[/]  [{di}]{board.summary()}[/]"]
    if not board.packets:
        out.append(f"  [{di}]no packets found — check mesh dev-sync[/]")
        return "\n".join(out)
    for pid, paths in board.collisions().items():
        out.append(f"  [{err}]◆ collision {pid}: {len(paths)} files[/]")
    for b in board.blocked():
        out.append(f"  [{err}]■ {b.id} blocked ({b.path})[/]")
    for s in board.stale_claims(now):
        out.append(f"  [{warn}]◐ {s.id} stale claim by {s.claimed_by or '?'}[/]")
    for phase in sorted(board.by_phase()):
        rows = sorted(board.by_phase()[phase], key=lambda p: p.id)
        states = " ".join(f"{p.id}:{p.status}" for p in rows)
        out.append(f"  [{di}]ph{phase}[/] {states}")
    out.append(f"  [{di}]read-only · canonical copy on pansa (mesh dev-sync)[/]")
    return "\n".join(out)
