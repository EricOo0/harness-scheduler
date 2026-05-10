from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class AgentRun:
    id: str
    task_id: str
    stage: str
    status: str
