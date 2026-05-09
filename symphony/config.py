from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .models import WorkflowDefinition


def _get_map(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _as_int(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str_list(value: Any, default: list[str]) -> list[str]:
    if value is None:
        return list(default)
    if isinstance(value, list):
        return [str(v) for v in value]
    return list(default)


def _resolve_secret(value: Any, default_env: str | None = None) -> str | None:
    if isinstance(value, str) and value.startswith("$") and len(value) > 1:
        return os.environ.get(value[1:]) or None
    if value in (None, "") and default_env:
        return os.environ.get(default_env) or None
    return str(value) if value not in (None, "") else None


def _resolve_path(value: Any, workflow_dir: Path, default: Path) -> str:
    raw = str(value) if value not in (None, "") else str(default)
    expanded = os.path.expandvars(os.path.expanduser(raw))
    path = Path(expanded)
    if not path.is_absolute():
        path = workflow_dir / path
    return str(path.resolve())


@dataclass(slots=True)
class TrackerConfig:
    kind: str | None
    endpoint: str
    api_key: str | None
    project_slug: str | None
    base_token: str | None
    task_table_id: str | None
    subtask_table_id: str | None
    doc_template_url: str | None
    cli_command: str
    field_names: dict[str, str]
    active_states: list[str]
    dispatch_states: list[str]
    running_states: list[str]
    handoff_states: list[str]
    terminal_states: list[str]
    start_state: str | None = None
    success_state: str | None = None
    failure_state: str | None = None
    start_state_by_dispatch: dict[str, str] = field(default_factory=dict)
    success_state_by_dispatch: dict[str, str] = field(default_factory=dict)
    failure_state_by_dispatch: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class HooksConfig:
    after_create: str | None = None
    before_run: str | None = None
    after_run: str | None = None
    before_remove: str | None = None
    timeout_ms: int = 60000


@dataclass(slots=True)
class AgentConfig:
    max_concurrent_agents: int = 10
    max_turns: int = 20
    max_retry_attempts: int = 2
    max_retry_backoff_ms: int = 300000
    stale_running_recovery_ms: int = 1800000
    max_concurrent_agents_by_state: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class CodexConfig:
    command: str = "codex app-server"
    approval_policy: str | None = None
    thread_sandbox: str | None = None
    turn_sandbox_policy: Any = None
    permission_profile: Any = None
    turn_timeout_ms: int = 3600000
    read_timeout_ms: int = 60000
    stall_timeout_ms: int = 300000


@dataclass(slots=True)
class ServerConfig:
    port: int | None = None
    host: str = "127.0.0.1"


@dataclass(slots=True)
class AuthMonitorConfig:
    enabled: bool = False
    interval_ms: int = 600000
    warning_before_ms: int = 3600000
    system_table_id: str | None = None
    system_record_id: str | None = None


@dataclass(slots=True)
class ServiceConfig:
    workflow_path: str
    workflow_mtime_ns: int | None
    polling_interval_ms: int
    workspace_root: str
    tracker: TrackerConfig
    hooks: HooksConfig
    agent: AgentConfig
    codex: CodexConfig
    server: ServerConfig
    auth_monitor: AuthMonitorConfig

    @property
    def active_state_set(self) -> set[str]:
        return {s.lower() for s in self.tracker.active_states}

    @property
    def dispatch_state_set(self) -> set[str]:
        return {s.lower() for s in self.tracker.dispatch_states}

    @property
    def running_state_set(self) -> set[str]:
        return {s.lower() for s in self.tracker.running_states}

    @property
    def handoff_state_set(self) -> set[str]:
        return {s.lower() for s in self.tracker.handoff_states}

    @property
    def terminal_state_set(self) -> set[str]:
        return {s.lower() for s in self.tracker.terminal_states} | self.handoff_state_set

    def start_state_for(self, source_state: str) -> str | None:
        return self.tracker.start_state_by_dispatch.get(source_state.lower()) or self.tracker.start_state

    def success_state_for(self, source_state: str) -> str | None:
        return self.tracker.success_state_by_dispatch.get(source_state.lower()) or self.tracker.success_state

    def failure_state_for(self, source_state: str) -> str | None:
        return self.tracker.failure_state_by_dispatch.get(source_state.lower()) or self.tracker.failure_state


def _as_state_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key).lower(): str(mapped) for key, mapped in value.items() if mapped not in (None, "")}


def _as_str_map(value: Any, default: dict[str, str]) -> dict[str, str]:
    if not isinstance(value, dict):
        return dict(default)
    result = dict(default)
    for key, mapped in value.items():
        if mapped not in (None, ""):
            result[str(key)] = str(mapped)
    return result


def build_config(workflow: WorkflowDefinition) -> ServiceConfig:
    raw = workflow.config
    workflow_dir = Path(workflow.path).resolve().parent
    tracker = _get_map(raw, "tracker")
    polling = _get_map(raw, "polling")
    workspace = _get_map(raw, "workspace")
    hooks = _get_map(raw, "hooks")
    agent = _get_map(raw, "agent")
    codex = _get_map(raw, "codex")
    server = _get_map(raw, "server")
    auth_monitor = _get_map(raw, "auth_monitor")

    kind = tracker.get("kind")
    endpoint = str(tracker.get("endpoint") or "https://api.linear.app/graphql")
    api_key = _resolve_secret(tracker.get("api_key"), "LINEAR_API_KEY" if kind == "linear" else None)
    field_names = _as_str_map(
        tracker.get("field_names"),
        {
            "title": "标题",
            "description": "任务说明",
            "state": "状态",
            "reference_url": "参考资料链接",
            "ai_doc_url": "AI 交互文档链接",
            "pr_url": "PR 链接",
            "handoff": "Agent 交接说明",
            "suggested_next_state": "Agent 建议下一状态",
            "latest_error": "最近错误",
            "blocker_reason": "阻塞原因",
            "subtasks": "任务子表",
            "created_at": "创建时间",
            "updated_at": "更新时间",
        },
    )
    dispatch_states = _as_str_list(tracker.get("dispatch_states"), [])
    running_states = _as_str_list(tracker.get("running_states"), [])
    handoff_states = _as_str_list(tracker.get("handoff_states"), [])
    if dispatch_states or running_states or handoff_states:
        if not dispatch_states:
            dispatch_states = ["Todo"]
        if not running_states:
            running_states = ["In Progress"]
        active_states = list(dict.fromkeys(dispatch_states + running_states))
    else:
        active_states = _as_str_list(tracker.get("active_states"), ["Todo", "In Progress"])
        dispatch_states = active_states
        running_states = []
    terminal_states = _as_str_list(
        tracker.get("terminal_states"),
        ["Closed", "Cancelled", "Canceled", "Duplicate", "Done"],
    )
    start_state = str(tracker.get("start_state") or (running_states[0] if running_states else "In Progress"))
    success_state = str(tracker.get("success_state") or (handoff_states[0] if handoff_states else "Review"))
    failure_state = str(tracker.get("failure_state") or (dispatch_states[-1] if dispatch_states else "Todo"))

    by_state: dict[str, int] = {}
    raw_by_state = agent.get("max_concurrent_agents_by_state")
    if isinstance(raw_by_state, dict):
        for key, value in raw_by_state.items():
            limit = _as_int(value, 0)
            if limit > 0:
                by_state[str(key).lower()] = limit

    workspace_root = _resolve_path(
        workspace.get("root"),
        workflow_dir,
        Path(tempfile.gettempdir()) / "symphony_workspaces",
    )

    return ServiceConfig(
        workflow_path=workflow.path,
        workflow_mtime_ns=workflow.mtime_ns,
        polling_interval_ms=_as_int(polling.get("interval_ms"), 30000),
        workspace_root=workspace_root,
        tracker=TrackerConfig(
            kind=str(kind) if kind is not None else None,
            endpoint=endpoint,
            api_key=api_key,
            project_slug=str(tracker.get("project_slug")) if tracker.get("project_slug") else None,
            base_token=_resolve_secret(tracker.get("base_token")),
            task_table_id=str(tracker.get("task_table_id")) if tracker.get("task_table_id") else None,
            subtask_table_id=str(tracker.get("subtask_table_id")) if tracker.get("subtask_table_id") else None,
            doc_template_url=str(tracker.get("doc_template_url")) if tracker.get("doc_template_url") else None,
            cli_command=str(tracker.get("cli_command") or "lark-cli"),
            field_names=field_names,
            active_states=active_states,
            dispatch_states=dispatch_states,
            running_states=running_states,
            handoff_states=handoff_states,
            terminal_states=terminal_states,
            start_state=start_state,
            success_state=success_state,
            failure_state=failure_state,
            start_state_by_dispatch=_as_state_map(tracker.get("start_state_by_dispatch")),
            success_state_by_dispatch=_as_state_map(tracker.get("success_state_by_dispatch")),
            failure_state_by_dispatch=_as_state_map(tracker.get("failure_state_by_dispatch")),
        ),
        hooks=HooksConfig(
            after_create=hooks.get("after_create"),
            before_run=hooks.get("before_run"),
            after_run=hooks.get("after_run"),
            before_remove=hooks.get("before_remove"),
            timeout_ms=_as_int(hooks.get("timeout_ms"), 60000),
        ),
        agent=AgentConfig(
            max_concurrent_agents=max(_as_int(agent.get("max_concurrent_agents"), 10), 1),
            max_turns=max(_as_int(agent.get("max_turns"), 20), 1),
            max_retry_attempts=max(_as_int(agent.get("max_retry_attempts"), 2), 0),
            max_retry_backoff_ms=max(_as_int(agent.get("max_retry_backoff_ms"), 300000), 1000),
            stale_running_recovery_ms=max(_as_int(agent.get("stale_running_recovery_ms"), 1800000), 0),
            max_concurrent_agents_by_state=by_state,
        ),
        codex=CodexConfig(
            command=str(codex.get("command") or "codex app-server"),
            approval_policy=codex.get("approval_policy"),
            thread_sandbox=codex.get("thread_sandbox"),
            turn_sandbox_policy=codex.get("turn_sandbox_policy"),
            permission_profile=codex.get("permission_profile"),
            turn_timeout_ms=_as_int(codex.get("turn_timeout_ms"), 3600000),
            read_timeout_ms=_as_int(codex.get("read_timeout_ms"), 60000),
            stall_timeout_ms=_as_int(codex.get("stall_timeout_ms"), 300000),
        ),
        server=ServerConfig(
            port=_as_int(server.get("port"), -1) if "port" in server else None,
            host=str(server.get("host") or "127.0.0.1"),
        ),
        auth_monitor=AuthMonitorConfig(
            enabled=bool(auth_monitor.get("enabled", False)),
            interval_ms=max(_as_int(auth_monitor.get("interval_ms"), 600000), 1000),
            warning_before_ms=max(_as_int(auth_monitor.get("warning_before_ms"), 3600000), 0),
            system_table_id=str(auth_monitor.get("system_table_id")) if auth_monitor.get("system_table_id") else None,
            system_record_id=str(auth_monitor.get("system_record_id")) if auth_monitor.get("system_record_id") else None,
        ),
    )


def validate_dispatch_config(config: ServiceConfig) -> None:
    if config.tracker.kind not in {"linear", "lark_base"}:
        raise ConfigError("tracker.kind must be 'linear' or 'lark_base'", code="unsupported_tracker_kind")
    if config.tracker.kind == "linear" and not config.tracker.api_key:
        raise ConfigError("tracker.api_key is required", code="missing_tracker_api_key")
    if config.tracker.kind == "linear" and not config.tracker.project_slug:
        raise ConfigError("tracker.project_slug is required", code="missing_tracker_project_slug")
    if config.tracker.kind == "lark_base" and not config.tracker.base_token:
        raise ConfigError("tracker.base_token is required", code="missing_tracker_base_token")
    if config.tracker.kind == "lark_base" and not config.tracker.task_table_id:
        raise ConfigError("tracker.task_table_id is required", code="missing_tracker_task_table_id")
    if not config.codex.command.strip():
        raise ConfigError("codex.command is required", code="missing_codex_command")
