from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Card:
    title: str
    description: str = ""
    column: str = "backlog"
    agent_id: str | None = None
    task_id: str | None = None
    priority: int = 0
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    created: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "column": self.column,
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "priority": self.priority,
            "created": self.created,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Card:
        return cls(
            id=d.get("id", uuid.uuid4().hex[:8]),
            title=d["title"],
            description=d.get("description", ""),
            column=d.get("column", "backlog"),
            agent_id=d.get("agent_id"),
            task_id=d.get("task_id"),
            priority=d.get("priority", 0),
            created=d.get("created", time.time()),
        )


@dataclass
class Board:
    title: str
    columns: list[str] = field(default_factory=lambda: ["backlog", "active", "done"])
    cards: list[Card] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    created: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "columns": self.columns,
            "cards": [c.as_dict() for c in self.cards],
            "created": self.created,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Board:
        cards = [Card.from_dict(c) for c in d.get("cards", [])]
        return cls(
            id=d.get("id", uuid.uuid4().hex[:8]),
            title=d["title"],
            columns=d.get("columns", ["backlog", "active", "done"]),
            cards=cards,
            created=d.get("created", time.time()),
        )

    def add_card(self, title: str, description: str = "",
                 column: str | None = None) -> Card:
        card = Card(title=title, description=description,
                    column=column or self.columns[0])
        self.cards.append(card)
        return card

    def move_card(self, card_id: str, to_column: str) -> Card | None:
        card = self.get_card(card_id)
        if card is None or to_column not in self.columns:
            return None
        card.column = to_column
        return card

    def assign_card(self, card_id: str, agent_id: str) -> Card | None:
        card = self.get_card(card_id)
        if card is None:
            return None
        card.agent_id = agent_id
        if card.column != self.columns[-1]:
            card.column = self.columns[1] if len(self.columns) > 1 else card.column
        return card

    def get_card(self, card_id: str) -> Card | None:
        for c in self.cards:
            if c.id == card_id:
                return c
        return None

    def cards_in_column(self, column: str) -> list[Card]:
        return sorted(
            [c for c in self.cards if c.column == column],
            key=lambda c: (-c.priority, c.created),
        )


class BoardStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or (Path.home() / ".aion" / "boards.json"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._boards: dict[str, Board] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
            for d in raw:
                b = Board.from_dict(d)
                self._boards[b.id] = b
        except Exception:
            self._boards = {}

    def save(self) -> None:
        try:
            data = [b.as_dict() for b in self._boards.values()]
            self.path.write_text(json.dumps(data, indent=2))
        except Exception as e:
            print(f"[board] save failed: {e}")

    def create(self, title: str,
               columns: list[str] | None = None) -> Board:
        b = Board(title=title, columns=columns or ["backlog", "active", "done"])
        self._boards[b.id] = b
        self.save()
        return b

    def get(self, board_id: str) -> Board | None:
        return self._boards.get(board_id)

    def get_by_title(self, title: str) -> Board | None:
        for b in self._boards.values():
            if b.title.lower() == title.lower():
                return b
        return None

    def update(self, board: Board) -> None:
        self._boards[board.id] = board
        self.save()

    def delete(self, board_id: str) -> bool:
        if board_id in self._boards:
            del self._boards[board_id]
            self.save()
            return True
        return False

    def list_all(self) -> list[Board]:
        return sorted(self._boards.values(), key=lambda b: b.created, reverse=True)

    def add_card(self, board_id: str, title: str,
                 description: str = "",
                 column: str | None = None) -> Card | None:
        b = self.get(board_id)
        if b is None:
            return None
        card = b.add_card(title, description, column)
        self.save()
        return card

    def move_card(self, board_id: str, card_id: str,
                  to_column: str) -> Card | None:
        b = self.get(board_id)
        if b is None:
            return None
        card = b.move_card(card_id, to_column)
        if card:
            self.save()
        return card

    def assign_card(self, board_id: str, card_id: str,
                    agent_id: str) -> Card | None:
        b = self.get(board_id)
        if b is None:
            return None
        card = b.assign_card(card_id, agent_id)
        if card:
            self.save()
        return card
