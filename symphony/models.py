from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo


BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_beijing() -> datetime:
    return datetime.now(BEIJING_TZ)


@dataclass(slots=True)
class BlockerRef:
    id: str | None = None
    identifier: str | None = None
    state: str | None = None


@dataclass(slots=True)
class IssueComment:
    id: str
    body: str
    created_at: datetime | None = None
    user_name: str | None = None


@dataclass(slots=True)
class Issue:
    id: str
    identifier: str
    title: str
    state: str
    description: str | None = None
    dispatch_state: str | None = None
    priority: int | None = None
    branch_name: str | None = None
    url: str | None = None
    labels: list[str] = field(default_factory=list)
    blocked_by: list[BlockerRef] = field(default_factory=list)
    comments: list[IssueComment] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def to_template_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "identifier": self.identifier,
            "title": self.title,
            "description": self.description,
            "priority": self.priority,
            "state": self.state,
            "dispatch_state": self.dispatch_state or self.state,
            "branch_name": self.branch_name,
            "url": self.url,
            "labels": self.labels,
            "blocked_by": [
                {"id": b.id, "identifier": b.identifier, "state": b.state}
                for b in self.blocked_by
            ],
            "comments": [
                {
                    "id": c.id,
                    "body": c.body,
                    "created_at": c.created_at.isoformat() if c.created_at else None,
                    "user_name": c.user_name,
                }
                for c in self.comments
            ],
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


@dataclass(slots=True)
class WorkflowDefinition:
    config: dict[str, Any]
    prompt_template: str
    path: str
    mtime_ns: int | None = None


@dataclass(slots=True)
class Workspace:
    path: str
    workspace_key: str
    created_now: bool


@dataclass(slots=True)
class AgentEvent:
    event: str
    timestamp: datetime
    codex_app_server_pid: int | None = None
    usage: dict[str, Any] | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RunResult:
    ok: bool
    reason: str = "normal"
    error: str | None = None


@dataclass(slots=True)
class RunningEntry:
    issue: Issue
    worker: Any
    identifier: str
    started_at: datetime
    source_state: str | None = None
    retry_attempt: int | None = None
    workspace_path: str | None = None
    session_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    codex_app_server_pid: int | None = None
    last_codex_event: str | None = None
    last_codex_timestamp: datetime | None = None
    last_codex_message: str | None = None
    codex_input_tokens: int = 0
    codex_output_tokens: int = 0
    codex_total_tokens: int = 0
    last_reported_input_tokens: int = 0
    last_reported_output_tokens: int = 0
    last_reported_total_tokens: int = 0
    turn_count: int = 0
    last_error: str | None = None
    recent_events: list[dict[str, Any]] = field(default_factory=list)
    delta_buffers: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class RetryEntry:
    issue_id: str
    identifier: str
    attempt: int
    due_at_ms: int
    due_at: datetime
    timer_handle: object | None = None
    error: str | None = None


@dataclass(slots=True)
class CodexTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    seconds_running: float = 0.0
