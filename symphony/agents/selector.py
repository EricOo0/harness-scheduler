from __future__ import annotations

from symphony.agents.base import AgentProfile


class AgentSelector:
    def __init__(self, store):
        self.store = store

    def select(self, task: dict, stage: str) -> AgentProfile:
        profile_data = self.store.resolve_agent_profile(task, stage)
        if not profile_data:
            raise ValueError(f"未找到可用 Agent：stage={stage}")
        return self._to_profile(profile_data)

    @staticmethod
    def _to_profile(data: dict) -> AgentProfile:
        return AgentProfile(
            id=data["id"],
            name=data["name"],
            kind=data["kind"],
            command=data["command"],
            args=data.get("args") or [],
            env=data.get("env") or {},
            model=data.get("model"),
            enabled=bool(data.get("enabled", True)),
            max_concurrency=int(data.get("max_concurrency") or 1),
            timeout_seconds=int(data.get("timeout_seconds") or 1800),
            dangerously_skip_permissions=bool(data.get("dangerously_skip_permissions", True)),
        )
