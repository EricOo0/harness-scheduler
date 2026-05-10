from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def beijing_now_display() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


@dataclass(slots=True)
class HarnessPaths:
    root: Path
    data_dir: Path
    workspace_root: Path
    db_path: Path

    @classmethod
    def from_project(cls, project_root: str | Path, data_dir: str | None = None) -> "HarnessPaths":
        root = Path(project_root).resolve()
        base = Path(data_dir).expanduser().resolve() if data_dir else root / ".harness"
        return cls(root=root, data_dir=base, workspace_root=base / "tasks", db_path=base / "harness.db")

    def learning_exports_dir(self) -> Path:
        return self.data_dir / "exports"

    def learning_export_path(self, task_id: str) -> Path:
        return self.learning_exports_dir() / f"{task_id}.learning.md"


class SQLiteDatabase:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def connection(self):
        conn = self.connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()
