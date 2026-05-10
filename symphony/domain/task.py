from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class TaskCreate:
    title: str
    description: str = ""
    priority: str = "P1"
    repository_path: str | None = None
    target_branch: str | None = None
