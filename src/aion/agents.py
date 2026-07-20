from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any


class AgentStatus(Enum):
    IDLE = "idle"
    WORKING = "working"
    BLOCKED = "blocked"


@dataclass
class AgentMemory:
    ts: float
    text: str
    kind: str = "note"

    def as_dict(self) -> dict:
        return {"ts": self.ts, "text": self.text, "kind": self.kind}

    @classmethod
    def from_dict(cls, d: dict) -> AgentMemory:
        return cls(ts=d.get("ts", 0.0), text=d.get("text", ""), kind=d.get("kind", "note"))


@dataclass
class AgentEntity:
    name: str
    status: AgentStatus = AgentStatus.IDLE
    goal: str = ""
    capabilities: list[str] = field(default_factory=list)
    memory_entries: list[AgentMemory] = field(default_factory=list)
    current_task_id: str | None = None
    assigned_board: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.updated = time.time()

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status.value,
            "goal": self.goal,
            "capabilities": self.capabilities,
            "memory_entries": [m.as_dict() for m in self.memory_entries],
            "current_task_id": self.current_task_id,
            "assigned_board": self.assigned_board,
            "created": self.created,
            "updated": self.updated,
        }

    @classmethod
    def from_dict(cls, d: dict) -> AgentEntity:
        status_str = d.get("status", "idle")
        try:
            status = AgentStatus(status_str)
        except ValueError:
            status = AgentStatus.IDLE
        mems = [AgentMemory.from_dict(m) for m in d.get("memory_entries", [])]
        return cls(
            id=d.get("id", uuid.uuid4().hex[:12]),
            name=d["name"],
            status=status,
            goal=d.get("goal", ""),
            capabilities=d.get("capabilities", []),
            memory_entries=mems,
            current_task_id=d.get("current_task_id"),
            assigned_board=d.get("assigned_board"),
            created=d.get("created", time.time()),
            updated=d.get("updated", time.time()),
        )


class AgentStore:
    def __init__(self, path: str | Path | None = None) -> None:
        from .fleet import shared_path
        self.path = Path(path or shared_path("agents.json"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._agents: dict[str, AgentEntity] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
            for d in raw:
                a = AgentEntity.from_dict(d)
                self._agents[a.id] = a
        except Exception:
            self._agents = {}

    def save(self) -> None:
        try:
            data = [a.as_dict() for a in self._agents.values()]
            self.path.write_text(json.dumps(data, indent=2))
        except Exception as e:
            print(f"[agents] save failed: {e}")

    def create(self, name: str, goal: str = "",
               capabilities: list[str] | None = None) -> AgentEntity:
        a = AgentEntity(name=name, goal=goal,
                        capabilities=capabilities or [])
        self._agents[a.id] = a
        self.save()
        return a

    def get(self, agent_id: str) -> AgentEntity | None:
        return self._agents.get(agent_id)

    def get_by_name(self, name: str) -> AgentEntity | None:
        for a in self._agents.values():
            if a.name.lower() == name.lower():
                return a
        return None

    def update(self, agent: AgentEntity) -> None:
        agent.touch()
        self._agents[agent.id] = agent
        self.save()

    def delete(self, agent_id: str) -> bool:
        if agent_id in self._agents:
            del self._agents[agent_id]
            self.save()
            return True
        return False

    def list_all(self) -> list[AgentEntity]:
        return sorted(self._agents.values(), key=lambda a: a.created, reverse=True)

    def assign_task(self, agent_id: str, task_id: str) -> AgentEntity | None:
        a = self.get(agent_id)
        if a is None:
            return None
        a.current_task_id = task_id
        a.status = AgentStatus.WORKING
        self.update(a)
        return a

    def release_task(self, agent_id: str) -> AgentEntity | None:
        a = self.get(agent_id)
        if a is None:
            return None
        a.current_task_id = None
        a.status = AgentStatus.IDLE
        self.update(a)
        return a

    def add_memory(self, agent_id: str, text: str,
                   kind: str = "note") -> AgentEntity | None:
        a = self.get(agent_id)
        if a is None:
            return None
        a.memory_entries.append(AgentMemory(ts=time.time(), text=text, kind=kind))
        a.touch()
        self.save()
        return a

    def set_goal(self, agent_id: str, goal: str) -> AgentEntity | None:
        a = self.get(agent_id)
        if a is None:
            return None
        a.goal = goal
        self.update(a)
        return a
