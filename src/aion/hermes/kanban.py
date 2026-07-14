from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path


KANBAN_DB = Path.home() / ".hermes" / "kanban.db"


@dataclass
class KanbanTask:
    id: str
    title: str
    body: str
    status: str
    assignee: str
    priority: int
    created_at: float
    completed_at: float | None
    started_at: float | None
    result: str | None

    @property
    def age_s(self) -> float:
        return time.time() - self.created_at

    @property
    def status_emoji(self) -> str:
        return {"done": "✓", "ready": "▶", "blocked": "⊘",
                "in_progress": "●", "cancelled": "✗"}.get(self.status, "○")


class KanbanReader:
    """Read-only access to Hermes kanban.db."""

    def __init__(self, db_path: str | Path = KANBAN_DB) -> None:
        self._db_path = Path(db_path)

    def _connect(self) -> sqlite3.Connection | None:
        if not self._db_path.exists():
            return None
        try:
            uri = f"file:{self._db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=0.5)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            conn.execute("PRAGMA busy_timeout = 400")
            return conn
        except sqlite3.Error:
            return None

    def tasks(self, status: str | None = None,
              limit: int = 20) -> list[KanbanTask]:
        conn = self._connect()
        if conn is None:
            return []
        try:
            where = "WHERE status = ?" if status else ""
            params: tuple = (status,) if status else ()
            rows = conn.execute(
                f"""SELECT id, COALESCE(title,'') AS title,
                          COALESCE(body,'') AS body, status,
                          COALESCE(assignee,'') AS assignee,
                          COALESCE(priority,0) AS priority,
                          COALESCE(created_at,0) AS created_at,
                          started_at, completed_at,
                          COALESCE(result,'') AS result
                   FROM tasks {where}
                   ORDER BY created_at DESC LIMIT ?""",
                params + (limit,)
            ).fetchall()
            return [
                KanbanTask(
                    id=r["id"], title=r["title"], body=r["body"],
                    status=r["status"], assignee=r["assignee"],
                    priority=r["priority"], created_at=r["created_at"],
                    started_at=r["started_at"], completed_at=r["completed_at"],
                    result=r["result"],
                )
                for r in rows
            ]
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    def stats(self) -> dict:
        conn = self._connect()
        if conn is None:
            return {"ok": False}
        try:
            total = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            done = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status='done'"
            ).fetchone()[0]
            ready = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status='ready'"
            ).fetchone()[0]
            blocked = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status='blocked'"
            ).fetchone()[0]
            return {"ok": True, "total": total, "done": done,
                    "ready": ready, "blocked": blocked}
        except sqlite3.Error:
            return {"ok": False}
        finally:
            conn.close()
