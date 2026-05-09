from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .agent import AgentRunner
from .config import ServiceConfig, build_config, validate_dispatch_config
from .errors import ConfigError, SymphonyError
from .models import BEIJING_TZ, AgentEvent, CodexTotals, Issue, RetryEntry, RunningEntry, now_utc
from .tracker import build_tracker
from .workflow import WorkflowDefinition, load_workflow
from .workspace import WorkspaceManager


@dataclass
class OrchestratorState:
    poll_interval_ms: int
    max_concurrent_agents: int
    running: dict[str, RunningEntry] = field(default_factory=dict)
    claimed: set[str] = field(default_factory=set)
    retry_attempts: dict[str, RetryEntry] = field(default_factory=dict)
    retry_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    completed: set[str] = field(default_factory=set)
    system_comments_sent: set[tuple[str, str]] = field(default_factory=set)
    codex_totals: CodexTotals = field(default_factory=CodexTotals)
    codex_rate_limits: Any = None
    auth_monitor_task: asyncio.Task | None = None
    last_auth_status: dict[str, Any] | None = None
    last_auth_error: str | None = None


class Orchestrator:
    def __init__(
        self,
        workflow: WorkflowDefinition,
        config: ServiceConfig,
        *,
        tracker: Any,
        runner_factory: Callable[[ServiceConfig, WorkspaceManager], AgentRunner],
        logger: logging.Logger,
    ):
        self.workflow = workflow
        self.config = config
        self.tracker = tracker
        self.runner_factory = runner_factory
        self.logger = logger
        self.workspace_manager = WorkspaceManager(config)
        self.state = OrchestratorState(config.polling_interval_ms, config.agent.max_concurrent_agents)
        self._stopping = False
        self._tick_requested = asyncio.Event()

    async def start(self) -> None:
        validate_dispatch_config(self.config)
        self.start_auth_monitor()
        await self.startup_terminal_workspace_cleanup()
        while not self._stopping:
            await self.tick()
            try:
                await asyncio.wait_for(self._tick_requested.wait(), timeout=max(self.state.poll_interval_ms / 1000.0, 0.001))
                self._tick_requested.clear()
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stopping = True
        self._tick_requested.set()
        if self.state.auth_monitor_task:
            self.state.auth_monitor_task.cancel()
        for task in list(self.state.retry_tasks.values()):
            task.cancel()
        for entry in list(self.state.running.values()):
            task = entry.worker
            if hasattr(task, "cancel"):
                task.cancel()

    def request_tick(self) -> bool:
        already = self._tick_requested.is_set()
        self._tick_requested.set()
        return already

    async def startup_terminal_workspace_cleanup(self) -> None:
        try:
            for issue in self.tracker.fetch_issues_by_states(self.config.tracker.terminal_states):
                self.workspace_manager.remove_for_issue(issue.identifier)
        except Exception as exc:
            self.logger.warning("startup_terminal_workspace_cleanup failed reason=%s", exc)

    def start_auth_monitor(self) -> None:
        if not self.config.auth_monitor.enabled or self.state.auth_monitor_task:
            return
        self.state.auth_monitor_task = asyncio.create_task(self._auth_monitor_loop())

    async def _auth_monitor_loop(self) -> None:
        while not self._stopping:
            await self.check_lark_auth_status()
            try:
                await asyncio.sleep(max(self.config.auth_monitor.interval_ms / 1000.0, 0.001))
            except asyncio.CancelledError:
                return

    async def check_lark_auth_status(self) -> None:
        if self.config.tracker.kind != "lark_base":
            return
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                [self.config.tracker.cli_command, "auth", "status"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                check=False,
            )
        except Exception as exc:
            self.state.last_auth_error = str(exc)
            self.logger.error("auth_monitor check failed reason=%s", exc)
            return
        output = (completed.stdout or "").strip()
        if completed.returncode != 0:
            error = (completed.stderr or output or f"exit {completed.returncode}").strip()
            self.state.last_auth_error = error
            self.logger.error("auth_monitor status unavailable reason=%s", error)
            return
        try:
            status = json.loads(output)
        except json.JSONDecodeError:
            self.state.last_auth_error = output[:500]
            self.logger.error("auth_monitor non_json_output output=%s", output[:500])
            return
        self.state.last_auth_status = status
        expires_at = self._parse_auth_time(status.get("expiresAt"))
        refresh_expires_at = self._parse_auth_time(status.get("refreshExpiresAt"))
        token_status = str(status.get("tokenStatus") or "")
        if not expires_at:
            self.logger.warning("auth_monitor missing_expires_at token_status=%s", token_status)
            return
        now = now_utc()
        remaining_ms = int((expires_at - now).total_seconds() * 1000)
        if remaining_ms <= 0 or token_status.lower() not in {"valid", ""}:
            self.logger.error(
                "auth_monitor expired_or_invalid token_status=%s expires_at=%s remaining_ms=%s",
                token_status,
                expires_at.isoformat(),
                remaining_ms,
            )
            return
        next_state = "登录态即将过期" if remaining_ms <= self.config.auth_monitor.warning_before_ms else "正常"
        try:
            await asyncio.to_thread(self._write_auth_monitor_status, next_state, expires_at, refresh_expires_at, remaining_ms, None)
        except Exception as exc:
            self.state.last_auth_error = str(exc)
            self.logger.error("auth_monitor base_update failed reason=%s", exc)

    @staticmethod
    def _parse_auth_time(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    def _write_auth_monitor_status(self, state: str, expires_at: datetime, refresh_expires_at: datetime | None, remaining_ms: int, error: str | None) -> None:
        table_id = self.config.auth_monitor.system_table_id
        record_id = self.config.auth_monitor.system_record_id
        if not table_id or not record_id or not self.config.tracker.base_token:
            return
        remaining_minutes = max(int(remaining_ms / 60000), 0)
        advice = "无需处理。"
        if state == "登录态即将过期":
            advice = "请在登录态过期前执行 `lark-cli auth login` 或重新完成 lark-cli 鉴权，避免后台无法继续读取 Base。"
        values: dict[str, Any] = {
            "名称": "Symphony 后台",
            "状态": state,
            "最近检查时间": self._format_lark_datetime(now_utc()),
            "访问令牌过期时间": self._format_lark_datetime(expires_at),
            "剩余分钟": remaining_minutes,
            "处理建议": advice,
            "最近错误": error or "",
        }
        if refresh_expires_at:
            values["刷新令牌过期时间"] = self._format_lark_datetime(refresh_expires_at)
        completed = subprocess.run(
            [
                self.config.tracker.cli_command,
                "base",
                "+record-upsert",
                "--as",
                "user",
                "--base-token",
                self.config.tracker.base_token,
                "--table-id",
                table_id,
                "--record-id",
                record_id,
                "--json",
                json.dumps(values, ensure_ascii=False),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout or f"exit {completed.returncode}").strip())

    @staticmethod
    def _format_lark_datetime(value: datetime) -> str:
        return value.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")

    async def tick(self) -> None:
        self.reload_workflow_if_changed()
        await self.reconcile_running_issues()
        try:
            validate_dispatch_config(self.config)
        except ConfigError as exc:
            self.logger.error("dispatch_validation failed code=%s", exc.code, extra={"error": exc.code})
            return
        try:
            candidates = self.tracker.fetch_dispatch_issues()
        except Exception as exc:
            self.logger.error("candidate_fetch failed reason=%s", exc)
            return
        dispatched = 0
        skipped: dict[str, int] = {}
        for issue in sorted(candidates, key=self.dispatch_sort_key):
            if self.available_slots() <= 0:
                skipped["no_available_slots"] = skipped.get("no_available_slots", 0) + 1
                break
            reason = self.dispatch_block_reason(issue)
            if reason is None:
                self.dispatch_issue(issue, attempt=None)
                dispatched += 1
            else:
                skipped[reason] = skipped.get(reason, 0) + 1
                self.logger.info(
                    "dispatch skipped issue_id=%s issue_identifier=%s reason=%s state=%s",
                    issue.id,
                    issue.identifier,
                    reason,
                    issue.state,
                    extra={"issue_id": issue.id, "issue_identifier": issue.identifier},
                )
        self.logger.info(
            "poll completed candidate_count=%s dispatched_count=%s skipped=%s dispatch_states=%s running_states=%s project_slug=%s",
            len(candidates),
            dispatched,
            skipped,
            ",".join(self.config.tracker.dispatch_states),
            ",".join(self.config.tracker.running_states),
            self.config.tracker.project_slug,
        )
        recovered = await self.recover_stale_running_issues()
        if recovered:
            self.logger.info("recovery completed recovered_count=%s", recovered)

    def reload_workflow_if_changed(self) -> None:
        try:
            stat = Path(self.config.workflow_path).stat()
        except OSError:
            return
        if stat.st_mtime_ns == self.config.workflow_mtime_ns:
            return
        try:
            workflow = load_workflow(self.config.workflow_path)
            config = build_config(workflow)
            validate_dispatch_config(config)
        except SymphonyError as exc:
            self.logger.error("workflow_reload failed code=%s", exc.code, extra={"error": exc.code})
            return
        self.workflow = workflow
        self.config = config
        self.tracker = build_tracker(config)
        self.workspace_manager = WorkspaceManager(config)
        self.state.poll_interval_ms = config.polling_interval_ms
        self.state.max_concurrent_agents = config.agent.max_concurrent_agents
        self.logger.info("workflow_reload completed")

    @staticmethod
    def dispatch_sort_key(issue: Issue) -> tuple[int, datetime, str]:
        priority = issue.priority if isinstance(issue.priority, int) else 9999
        created = issue.created_at or datetime.max.replace(tzinfo=timezone.utc)
        return (priority, created, issue.identifier)

    def available_slots(self) -> int:
        return max(self.config.agent.max_concurrent_agents - len(self.state.running), 0)

    def should_dispatch(self, issue: Issue) -> bool:
        return self.dispatch_block_reason(issue) is None

    def dispatch_block_reason(self, issue: Issue) -> str | None:
        if not issue.id or not issue.identifier or not issue.title or not issue.state:
            return "missing_required_issue_fields"
        state = issue.state.lower()
        if state not in self.config.dispatch_state_set:
            return "not_dispatch_state"
        if state in self.config.terminal_state_set:
            return "terminal_or_handoff_state"
        if issue.id in self.state.running or issue.id in self.state.claimed:
            return "already_claimed_or_running"
        if self.available_slots() <= 0:
            return "no_available_slots"
        per_state_limit = self.config.agent.max_concurrent_agents_by_state.get(state, self.config.agent.max_concurrent_agents)
        running_in_state = sum(1 for entry in self.state.running.values() if entry.issue.state.lower() == state)
        if running_in_state >= per_state_limit:
            return "state_concurrency_limit"
        if state == "todo":
            for blocker in issue.blocked_by:
                if not blocker.state or blocker.state.lower() not in self.config.terminal_state_set:
                    return "todo_blocked_by_non_terminal_issue"
        return None

    def dispatch_issue(self, issue: Issue, attempt: int | None) -> None:
        source_state = issue.state
        issue.dispatch_state = source_state
        preparer = getattr(self.tracker, "prepare_issue_for_dispatch", None)
        if callable(preparer):
            try:
                issue = preparer(issue)
            except Exception as exc:
                self.logger.error(
                    "dispatch_prepare failed issue_id=%s issue_identifier=%s error=%s",
                    issue.id,
                    issue.identifier,
                    exc,
                    extra={"issue_id": issue.id, "issue_identifier": issue.identifier, "error": str(exc)},
                )
                return
        start_state = self.config.start_state_for(source_state)
        self._move_issue_state(issue, start_state, "start")
        self._add_system_comment(issue, self._start_comment_body(issue, attempt, start_state), key="start:retry" if attempt else "start:initial")
        runner = self.runner_factory(self.config, self.workspace_manager)
        task = asyncio.create_task(self._run_worker(runner, issue, attempt))
        entry = RunningEntry(
            issue=issue,
            worker=task,
            identifier=issue.identifier,
            started_at=now_utc(),
            source_state=source_state,
            retry_attempt=attempt,
            workspace_path=self.workspace_manager.path_for_issue(issue.identifier).as_posix(),
        )
        self.state.running[issue.id] = entry
        self.state.claimed.add(issue.id)
        self.state.retry_attempts.pop(issue.id, None)
        old_retry_task = self.state.retry_tasks.pop(issue.id, None)
        if old_retry_task:
            old_retry_task.cancel()
        task.add_done_callback(lambda done, issue_id=issue.id: asyncio.create_task(self._worker_done(issue_id, done)))
        self.logger.info(
            "dispatch completed issue_id=%s issue_identifier=%s",
            issue.id,
            issue.identifier,
            extra={"issue_id": issue.id, "issue_identifier": issue.identifier},
        )

    async def _run_worker(self, runner: AgentRunner, issue: Issue, attempt: int | None):
        return await runner.run(
            issue=issue,
            prompt_template=self.workflow.prompt_template,
            attempt=attempt,
            refresh_issue=self._refresh_one_issue,
            on_event=lambda event: self.handle_agent_event(issue.id, event),
        )

    def _refresh_one_issue(self, issue_id: str) -> Issue | None:
        issues = self.tracker.fetch_issue_states_by_ids([issue_id])
        return issues[0] if issues else None

    async def _worker_done(self, issue_id: str, task: asyncio.Task) -> None:
        entry = self.state.running.pop(issue_id, None)
        if entry is None:
            return
        elapsed = (now_utc() - entry.started_at).total_seconds()
        self.state.codex_totals.seconds_running += max(elapsed, 0)
        try:
            result = task.result()
            ok = bool(result.ok)
            error = result.error
        except asyncio.CancelledError:
            ok = False
            error = "canceled"
        except Exception as exc:
            ok = False
            error = str(exc)
        if ok:
            completion_error = self._workspace_artifact_block_reason(entry) or self._completion_block_reason(entry)
            if completion_error:
                ok = False
                error = completion_error
            else:
                success_state, success_state_error = self._completion_success_state_for(entry)
                if success_state_error:
                    ok = False
                    error = success_state_error
                else:
                    moved = self._move_issue_state(entry.issue, success_state, "success", clear_suggested_next_state=True)
                    if not moved:
                        ok = False
                        error = f"failed to move issue to success state: {success_state}"
                    else:
                        self.state.completed.add(issue_id)
                        self._add_system_comment(
                            entry.issue,
                            f"Symphony system: agent run completed successfully. State target: {success_state}.",
                        )
                        self.logger.info(
                            "worker completed issue_id=%s issue_identifier=%s elapsed_seconds=%.3f",
                            issue_id,
                            entry.identifier,
                            elapsed,
                            extra={"issue_id": issue_id, "issue_identifier": entry.identifier},
                        )
                        self.schedule_retry(issue_id, entry.identifier, 1, error=None, continuation=True)
        if ok:
            self.logger.info(
                "worker post_complete issue_id=%s issue_identifier=%s elapsed_seconds=%.3f",
                issue_id,
                entry.identifier,
                elapsed,
                extra={"issue_id": issue_id, "issue_identifier": entry.identifier},
            )
        else:
            next_attempt = (entry.retry_attempt or 0) + 1
            failure_state = self.config.failure_state_for(entry.source_state or entry.issue.state)
            self._move_issue_state(entry.issue, failure_state, "failure", failure_reason=error)
            self._add_system_comment(
                entry.issue,
                self._failure_comment_body(error, failure_state),
                key=self._failure_comment_key(error),
            )
            self.logger.error(
                "worker failed issue_id=%s issue_identifier=%s error=%s elapsed_seconds=%.3f",
                issue_id,
                entry.identifier,
                error,
                elapsed,
                extra={"issue_id": issue_id, "issue_identifier": entry.identifier, "error": error},
            )
            self.schedule_retry(issue_id, entry.identifier, next_attempt, error=error, continuation=False)
        self._tick_requested.set()

    def _completion_success_state_for(self, entry: RunningEntry) -> tuple[str | None, str | None]:
        source_state = entry.source_state or entry.issue.state
        default_state = self.config.success_state_for(source_state)
        resolver = getattr(self.tracker, "completion_success_state_for", None)
        if not callable(resolver):
            return default_state, None
        try:
            return resolver(entry.issue, source_state, default_state)
        except Exception as exc:
            self.logger.warning(
                "success_state_resolution failed issue_id=%s issue_identifier=%s error=%s",
                entry.issue.id,
                entry.identifier,
                exc,
                extra={"issue_id": entry.issue.id, "issue_identifier": entry.identifier, "error": str(exc)},
            )
            return None, f"success state resolution failed: {exc}"

    def _completion_block_reason(self, entry: RunningEntry) -> str | None:
        validator = getattr(self.tracker, "completion_block_reason", None)
        if not callable(validator):
            return None
        try:
            return validator(entry.issue, entry.source_state or entry.issue.state)
        except Exception as exc:
            self.logger.warning(
                "completion_validation failed issue_id=%s issue_identifier=%s error=%s",
                entry.issue.id,
                entry.identifier,
                exc,
                extra={"issue_id": entry.issue.id, "issue_identifier": entry.identifier, "error": str(exc)},
            )
            return f"completion validation failed: {exc}"

    def _workspace_artifact_block_reason(self, entry: RunningEntry) -> str | None:
        if entry.source_state not in {"待规划", "待方案设计", "方案需修改"}:
            return None
        workspace_path = Path(entry.workspace_path or self.workspace_manager.path_for_issue(entry.identifier))
        if not workspace_path.exists():
            return None
        unexpected: list[str] = []
        for path in workspace_path.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(workspace_path).as_posix()
            if relative == ".symphony_run.json" or relative == ".DS_Store" or relative.startswith("log/"):
                continue
            unexpected.append(relative)
        if not unexpected:
            return None
        sample = ", ".join(unexpected[:5])
        return (
            f"{entry.source_state} 阶段不应在 Symphony 临时 workspace 中创建交付文件。"
            f"发现：{sample}。请改用 lark-cli 更新 AI 交互文档；代码文件只能在实施阶段的目标仓库路径中创建。"
        )

    def schedule_retry(self, issue_id: str, identifier: str, attempt: int, *, error: str | None, continuation: bool) -> None:
        if not continuation and attempt > self.config.agent.max_retry_attempts:
            self.state.claimed.discard(issue_id)
            self.logger.error(
                "retry exhausted issue_id=%s issue_identifier=%s attempt=%s max_retry_attempts=%s error=%s",
                issue_id,
                identifier,
                attempt,
                self.config.agent.max_retry_attempts,
                error,
                extra={"issue_id": issue_id, "issue_identifier": identifier, "error": error},
            )
            return
        old_task = self.state.retry_tasks.pop(issue_id, None)
        if old_task:
            old_task.cancel()
        delay_ms = 1000 if continuation else min(10000 * (2 ** max(attempt - 1, 0)), self.config.agent.max_retry_backoff_ms)
        due_at_ms = int(time.monotonic() * 1000) + delay_ms
        due_at = datetime.fromtimestamp(time.time() + delay_ms / 1000.0, tz=timezone.utc)
        task = asyncio.create_task(self._retry_after(issue_id, delay_ms / 1000.0))
        self.state.retry_tasks[issue_id] = task
        self.state.retry_attempts[issue_id] = RetryEntry(
            issue_id=issue_id,
            identifier=identifier,
            attempt=attempt,
            due_at_ms=due_at_ms,
            due_at=due_at,
            timer_handle=task,
            error=error,
        )
        self.logger.info("retrying scheduled issue_id=%s issue_identifier=%s attempt=%s", issue_id, identifier, attempt)

    async def _retry_after(self, issue_id: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            await self.handle_retry(issue_id)
        except asyncio.CancelledError:
            return

    async def handle_retry(self, issue_id: str) -> None:
        self.state.retry_tasks.pop(issue_id, None)
        retry = self.state.retry_attempts.pop(issue_id, None)
        if retry is None:
            return
        try:
            candidates = self.tracker.fetch_dispatch_issues() + self.tracker.fetch_recovery_issues()
        except Exception:
            self.schedule_retry(issue_id, retry.identifier, retry.attempt + 1, error="retry poll failed", continuation=False)
            return
        issue = next((item for item in candidates if item.id == issue_id), None)
        if issue is None:
            self.state.claimed.discard(issue_id)
            return
        retry_block = self.retry_dispatch_block_reason(issue)
        if retry_block is not None:
            if retry_block == "no_available_slots":
                self.schedule_retry(issue_id, issue.identifier, retry.attempt + 1, error="no available orchestrator slots", continuation=False)
            else:
                self.state.claimed.discard(issue_id)
                self.logger.info(
                    "retry released issue_id=%s issue_identifier=%s reason=%s state=%s",
                    issue.id,
                    issue.identifier,
                    retry_block,
                    issue.state,
                    extra={"issue_id": issue.id, "issue_identifier": issue.identifier},
                )
            return
        self.state.claimed.discard(issue_id)
        self.dispatch_issue(issue, attempt=retry.attempt)

    def retry_dispatch_block_reason(self, issue: Issue) -> str | None:
        was_claimed = issue.id in self.state.claimed
        self.state.claimed.discard(issue.id)
        try:
            state = issue.state.lower()
            if state in self.config.dispatch_state_set:
                return self.dispatch_block_reason(issue)
            if state in self.config.running_state_set:
                return self.recovery_block_reason(issue)
            return "not_dispatch_or_running_state"
        finally:
            if was_claimed:
                self.state.claimed.add(issue.id)

    async def reconcile_running_issues(self) -> None:
        await self.reconcile_stalled_runs()
        if not self.state.running:
            return
        ids = list(self.state.running.keys())
        try:
            refreshed = {issue.id: issue for issue in self.tracker.fetch_issue_states_by_ids(ids)}
        except Exception as exc:
            self.logger.debug("running_state_refresh failed keep_workers_running reason=%s", exc)
            return
        for issue_id, entry in list(self.state.running.items()):
            issue = refreshed.get(issue_id)
            if issue is None:
                continue
            state = issue.state.lower()
            if state in self.config.terminal_state_set:
                await self.terminate_running_issue(issue_id, cleanup_workspace=True)
            elif state in self.config.active_state_set:
                entry.issue = issue
            else:
                await self.terminate_running_issue(issue_id, cleanup_workspace=False)

    async def recover_stale_running_issues(self) -> int:
        if not self.config.tracker.running_states or self.config.agent.stale_running_recovery_ms <= 0:
            return 0
        try:
            candidates = self.tracker.fetch_recovery_issues()
        except Exception as exc:
            self.logger.error("recovery_fetch failed reason=%s", exc)
            return 0
        recovered = 0
        for issue in sorted(candidates, key=self.dispatch_sort_key):
            if self.available_slots() <= 0:
                break
            reason = self.recovery_block_reason(issue)
            if reason is None:
                self.dispatch_issue(issue, attempt=1)
                recovered += 1
            else:
                self.logger.info(
                    "recovery skipped issue_id=%s issue_identifier=%s reason=%s state=%s",
                    issue.id,
                    issue.identifier,
                    reason,
                    issue.state,
                    extra={"issue_id": issue.id, "issue_identifier": issue.identifier},
                )
        return recovered

    def recovery_block_reason(self, issue: Issue) -> str | None:
        if not issue.id or not issue.identifier or not issue.title or not issue.state:
            return "missing_required_issue_fields"
        state = issue.state.lower()
        if state not in self.config.running_state_set:
            return "not_running_state"
        if issue.id in self.state.running or issue.id in self.state.claimed:
            return "already_claimed_or_running"
        if self.available_slots() <= 0:
            return "no_available_slots"
        workspace_path = self.workspace_manager.path_for_issue(issue.identifier)
        if not workspace_path.exists():
            return "no_existing_workspace"
        marker = workspace_path / ".symphony_run.json"
        if not marker.exists():
            return "missing_symphony_run_marker"
        try:
            marker_mtime = datetime.fromtimestamp(marker.stat().st_mtime, tz=timezone.utc)
        except OSError:
            return "unreadable_symphony_run_marker"
        age_ms = (now_utc() - marker_mtime).total_seconds() * 1000
        if age_ms < self.config.agent.stale_running_recovery_ms:
            return "running_state_not_stale"
        return None

    async def reconcile_stalled_runs(self) -> None:
        timeout_ms = self.config.codex.stall_timeout_ms
        if timeout_ms <= 0:
            return
        now = now_utc()
        for issue_id, entry in list(self.state.running.items()):
            anchor = entry.last_codex_timestamp or entry.started_at
            if (now - anchor).total_seconds() * 1000 > timeout_ms:
                entry.last_error = "stalled"
                await self.terminate_running_issue(issue_id, cleanup_workspace=False, retry=True, error="stalled")

    async def terminate_running_issue(self, issue_id: str, *, cleanup_workspace: bool, retry: bool = False, error: str | None = None) -> None:
        entry = self.state.running.pop(issue_id, None)
        if entry is None:
            return
        if hasattr(entry.worker, "cancel"):
            entry.worker.cancel()
        elapsed = (now_utc() - entry.started_at).total_seconds()
        self.state.codex_totals.seconds_running += max(elapsed, 0)
        if cleanup_workspace:
            self.workspace_manager.remove_for_issue(entry.identifier)
        if retry:
            failure_state = self.config.failure_state_for(entry.source_state or entry.issue.state)
            self._move_issue_state(entry.issue, failure_state, error or "retry", failure_reason=error)
            self._add_system_comment(entry.issue, self._failure_comment_body(error, failure_state), key=self._failure_comment_key(error))
            next_attempt = (entry.retry_attempt or 0) + 1
            self.schedule_retry(issue_id, entry.identifier, next_attempt, error=error, continuation=False)
        else:
            self.state.claimed.discard(issue_id)

    def _move_issue_state(self, issue: Issue, state_name: str | None, reason: str, *, clear_suggested_next_state: bool = False, failure_reason: str | None = None) -> bool:
        if not state_name:
            return True
        if issue.state.lower() == state_name.lower() and not clear_suggested_next_state:
            return True
        mover = getattr(self.tracker, "move_issue_to_state", None)
        if not callable(mover):
            return False
        try:
            if clear_suggested_next_state:
                moved = bool(mover(issue.id, state_name, clear_suggested_next_state=True))
            elif failure_reason:
                moved = bool(mover(issue.id, state_name, failure_reason=failure_reason))
            else:
                moved = bool(mover(issue.id, state_name))
        except Exception as exc:
            self.logger.error(
                "state_move failed issue_id=%s issue_identifier=%s target_state=%s reason=%s error=%s",
                issue.id,
                issue.identifier,
                state_name,
                reason,
                exc,
                extra={"issue_id": issue.id, "issue_identifier": issue.identifier, "error": str(exc)},
            )
            return False
        if moved:
            issue.state = state_name
            self.logger.info(
                "state_move completed issue_id=%s issue_identifier=%s target_state=%s reason=%s",
                issue.id,
                issue.identifier,
                state_name,
                reason,
                extra={"issue_id": issue.id, "issue_identifier": issue.identifier},
            )
            return True
        else:
            self.logger.warning(
                "state_move skipped issue_id=%s issue_identifier=%s target_state=%s reason=%s",
                issue.id,
                issue.identifier,
                state_name,
                reason,
                extra={"issue_id": issue.id, "issue_identifier": issue.identifier},
            )
            return False

    def _add_system_comment(self, issue: Issue, body: str, *, key: str | None = None) -> None:
        comment_key = (issue.id, key or body)
        if comment_key in self.state.system_comments_sent:
            return
        commenter = getattr(self.tracker, "add_comment", None)
        if not callable(commenter):
            return
        try:
            ok = bool(commenter(issue.id, body))
        except Exception as exc:
            self.logger.warning(
                "system_comment failed issue_id=%s issue_identifier=%s error=%s",
                issue.id,
                issue.identifier,
                exc,
                extra={"issue_id": issue.id, "issue_identifier": issue.identifier, "error": str(exc)},
            )
            return
        if not ok:
            self.logger.warning(
                "system_comment skipped issue_id=%s issue_identifier=%s",
                issue.id,
                issue.identifier,
                extra={"issue_id": issue.id, "issue_identifier": issue.identifier},
            )
            return
        self.state.system_comments_sent.add(comment_key)

    def _start_comment_body(self, issue: Issue, attempt: int | None, target_state: str | None) -> str:
        action = "resumed" if attempt else "started"
        details = f" attempt={attempt}" if attempt else ""
        return f"Symphony system: {action} work on this issue.{details} State target: {target_state}."

    def _failure_comment_body(self, error: str | None, target_state: str | None) -> str:
        suffix = ""
        if error:
            normalized = " ".join(str(error).split())
            suffix = f" Error: {normalized[:500]}"
        return f"Symphony system: agent run did not complete successfully. State target: {target_state}.{suffix}"

    @staticmethod
    def _failure_comment_key(error: str | None) -> str:
        normalized = " ".join(str(error or "unknown").split())
        return f"failure:{normalized[:200]}"

    def handle_agent_event(self, issue_id: str, event: AgentEvent) -> None:
        entry = self.state.running.get(issue_id)
        if entry is None:
            return
        entry.codex_app_server_pid = event.codex_app_server_pid
        entry.last_codex_event = event.event
        entry.last_codex_timestamp = event.timestamp
        message = self._summarize_agent_event(event)
        entry.last_codex_message = message[:500]
        thread = event.payload.get("thread") if isinstance(event.payload.get("thread"), dict) else {}
        turn = event.payload.get("turn") if isinstance(event.payload.get("turn"), dict) else {}
        thread_id = event.payload.get("thread_id") or event.payload.get("threadId") or thread.get("id")
        turn_id = event.payload.get("turn_id") or event.payload.get("turnId") or turn.get("id")
        if thread_id:
            entry.thread_id = str(thread_id)
        if turn_id:
            entry.turn_id = str(turn_id)
        if entry.thread_id and entry.turn_id:
            entry.session_id = f"{entry.thread_id}-{entry.turn_id}"
        if event.event in {"session_started", "turn_started"}:
            entry.turn_count += 1
        if event.event in {"item/agentMessage/delta", "item/plan/delta"}:
            item_id = str(event.payload.get("itemId") or "")
            delta = event.payload.get("delta")
            if item_id and isinstance(delta, str):
                entry.delta_buffers[item_id] = (entry.delta_buffers.get(item_id, "") + delta)[-4000:]
                entry.last_codex_message = entry.delta_buffers[item_id][-500:]
            return
        if event.event in {
            "session_started",
            "turn_started",
            "turn_completed",
            "turn_ended_with_error",
            "turn_input_required",
            "approval_auto_approved",
            "linear_graphql",
            "stderr",
            "malformed",
            "item/completed",
            "turn/plan/updated",
            "thread/tokenUsage/updated",
        }:
            if event.event == "item/completed":
                message = self._message_from_completed_item(entry, event) or message
                entry.last_codex_message = message[:500]
            entry.recent_events.append(
                {
                    "at": event.timestamp.isoformat(),
                    "event": event.event,
                    "message": message,
                }
            )
            entry.recent_events = entry.recent_events[-50:]
            self.logger.info(
                "agent event issue_id=%s issue_identifier=%s event=%s message=%s",
                issue_id,
                entry.identifier,
                event.event,
                message,
                extra={"issue_id": issue_id, "issue_identifier": entry.identifier, "session_id": entry.session_id, "event": event.event},
            )
        usage = event.usage or {}
        self._apply_usage(entry, usage)
        if "rate_limits" in event.payload:
            self.state.codex_rate_limits = event.payload["rate_limits"]

    @staticmethod
    def _message_from_completed_item(entry: RunningEntry, event: AgentEvent) -> str | None:
        item = event.payload.get("item")
        if not isinstance(item, dict):
            return None
        item_id = str(item.get("id") or "")
        item_type = item.get("type")
        if item_type in {"agentMessage", "plan"}:
            text = item.get("text")
            if isinstance(text, str) and text:
                if item_id:
                    entry.delta_buffers.pop(item_id, None)
                return text
            if item_id and entry.delta_buffers.get(item_id):
                return entry.delta_buffers.pop(item_id)
        if item_type == "commandExecution":
            command = item.get("command") or ""
            status = item.get("status") or ""
            exit_code = item.get("exitCode")
            return f"command status={status} exit_code={exit_code} command={command}"
        if item_type == "dynamicToolCall":
            tool = item.get("tool") or ""
            status = item.get("status") or ""
            success = item.get("success")
            return f"dynamic tool status={status} success={success} tool={tool}"
        if isinstance(item_type, str):
            return f"item completed type={item_type}"
        return None

    @staticmethod
    def _summarize_agent_event(event: AgentEvent) -> str:
        payload = event.payload
        for key in ("message", "text", "delta", "summary", "status"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        turn = payload.get("turn")
        if isinstance(turn, dict):
            status = turn.get("status")
            error = turn.get("error")
            if error:
                return f"turn status={status} error={error}"
            if status:
                return f"turn status={status}"
        thread = payload.get("thread")
        if isinstance(thread, dict):
            status = thread.get("status")
            if status:
                return f"thread status={status}"
        if event.usage:
            return f"usage={event.usage}"
        if "rate_limits" in payload:
            return "rate limits updated"
        return ""

    def _apply_usage(self, entry: RunningEntry, usage: dict[str, Any]) -> None:
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        total_tokens = usage.get("total_tokens")
        if isinstance(input_tokens, int):
            self.state.codex_totals.input_tokens += max(input_tokens - entry.last_reported_input_tokens, 0)
            entry.last_reported_input_tokens = max(entry.last_reported_input_tokens, input_tokens)
            entry.codex_input_tokens = input_tokens
        if isinstance(output_tokens, int):
            self.state.codex_totals.output_tokens += max(output_tokens - entry.last_reported_output_tokens, 0)
            entry.last_reported_output_tokens = max(entry.last_reported_output_tokens, output_tokens)
            entry.codex_output_tokens = output_tokens
        if isinstance(total_tokens, int):
            self.state.codex_totals.total_tokens += max(total_tokens - entry.last_reported_total_tokens, 0)
            entry.last_reported_total_tokens = max(entry.last_reported_total_tokens, total_tokens)
            entry.codex_total_tokens = total_tokens

    def snapshot(self) -> dict[str, Any]:
        now = now_utc()
        generated_at = now.astimezone(BEIJING_TZ)
        active_seconds = sum(max((now - e.started_at).total_seconds(), 0) for e in self.state.running.values())
        return {
            "generated_at": generated_at.isoformat(),
            "counts": {"running": len(self.state.running), "retrying": len(self.state.retry_attempts)},
            "running": [self._running_row(entry) for entry in self.state.running.values()],
            "retrying": [self._retry_row(entry) for entry in self.state.retry_attempts.values()],
            "codex_totals": {
                "input_tokens": self.state.codex_totals.input_tokens,
                "output_tokens": self.state.codex_totals.output_tokens,
                "total_tokens": self.state.codex_totals.total_tokens,
                "seconds_running": self.state.codex_totals.seconds_running + active_seconds,
            },
            "auth_monitor": {
                "enabled": self.config.auth_monitor.enabled,
                "last_status": self.state.last_auth_status,
                "last_error": self.state.last_auth_error,
            },
            "rate_limits": self.state.codex_rate_limits,
        }

    def issue_snapshot(self, identifier: str) -> dict[str, Any] | None:
        for entry in self.state.running.values():
            if entry.identifier == identifier:
                return {
                    "issue_identifier": identifier,
                    "issue_id": entry.issue.id,
                    "status": "running",
                    "workspace": {"path": self.workspace_manager.path_for_issue(identifier).as_posix()},
                    "running": self._running_row(entry),
                    "retry": None,
                    "recent_events": entry.recent_events,
                    "last_error": entry.last_error,
                    "tracked": {},
                }
        for retry in self.state.retry_attempts.values():
            if retry.identifier == identifier:
                return {
                    "issue_identifier": identifier,
                    "issue_id": retry.issue_id,
                    "status": "retrying",
                    "workspace": {"path": self.workspace_manager.path_for_issue(identifier).as_posix()},
                    "running": None,
                    "retry": self._retry_row(retry),
                    "last_error": retry.error,
                    "tracked": {},
                }
        return None

    @staticmethod
    def _running_row(entry: RunningEntry) -> dict[str, Any]:
        return {
            "issue_id": entry.issue.id,
            "issue_identifier": entry.identifier,
            "state": entry.issue.state,
            "session_id": entry.session_id,
            "turn_count": entry.turn_count,
            "last_event": entry.last_codex_event,
            "last_message": entry.last_codex_message,
            "started_at": entry.started_at.isoformat(),
            "last_event_at": entry.last_codex_timestamp.isoformat() if entry.last_codex_timestamp else None,
            "recent_events": entry.recent_events[-10:],
            "tokens": {
                "input_tokens": entry.codex_input_tokens,
                "output_tokens": entry.codex_output_tokens,
                "total_tokens": entry.codex_total_tokens,
            },
        }

    @staticmethod
    def _retry_row(entry: RetryEntry) -> dict[str, Any]:
        return {
            "issue_id": entry.issue_id,
            "issue_identifier": entry.identifier,
            "attempt": entry.attempt,
            "due_at_ms": entry.due_at_ms,
            "due_at": entry.due_at.isoformat(),
            "error": entry.error,
        }
