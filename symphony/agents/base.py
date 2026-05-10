from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol


AgentKind = Literal["mock", "codex_app_server", "claude_cli", "claude_sdk"]
AgentRunStatus = Literal["completed", "blocked", "failed", "cancelled", "timeout"]


@dataclass(slots=True)
class AgentProfile:
    id: str
    name: str
    kind: AgentKind
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    model: str | None = None
    enabled: bool = True
    max_concurrency: int = 1
    timeout_seconds: int = 1800
    dangerously_skip_permissions: bool = True


@dataclass(slots=True)
class AgentRunContext:
    run_id: str
    task: dict[str, Any]
    stage: str
    stage_spec: dict[str, Any]
    prompt: str
    artifact_path: Path
    workspace_path: Path
    repository_path: Path | None
    profile: AgentProfile
    previous_session_id: str | None = None


@dataclass(slots=True)
class AgentRunResult:
    status: AgentRunStatus
    summary: str
    suggested_status: str | None = None
    external_session_id: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    raw_result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass(slots=True)
class AgentEvent:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)


class AgentAdapter(Protocol):
    kind: AgentKind

    def run(self, ctx: AgentRunContext) -> AgentRunResult:
        ...
