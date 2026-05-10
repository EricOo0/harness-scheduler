from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class AgentEventSink:
    def __init__(self, *, store: Any, task_id: str, run_id: str, agent_profile_id: str, event_log_path: Path):
        self.store = store
        self.task_id = task_id
        self.run_id = run_id
        self.agent_profile_id = agent_profile_id
        self.event_log_path = event_log_path
        self.event_log_path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        payload = payload or {}
        self.store.append_agent_run_event(
            run_id=self.run_id,
            task_id=self.task_id,
            agent_profile_id=self.agent_profile_id,
            event_type=event_type,
            payload=payload,
        )
        with self.event_log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": event_type, "payload": payload}, ensure_ascii=False) + "\n")
