from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Callable

from .config import ServiceConfig
from .errors import AgentError
from .models import AgentEvent, Issue, RunResult, now_utc
from .tracker import execute_linear_graphql
from .workflow import StrictTemplate
from .workspace import WorkspaceManager

EventCallback = Callable[[AgentEvent], None]
APP_SERVER_STREAM_LIMIT = 16 * 1024 * 1024


def _text_input(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text, "text_elements": []}


def _extract_usage(payload: dict[str, Any]) -> dict[str, int] | None:
    candidates = [
        payload.get("usage"),
        payload.get("total_token_usage"),
        payload.get("totalTokenUsage"),
        payload.get("tokenUsage"),
        payload.get("token_usage"),
        payload,
    ]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        values: dict[str, int] = {}
        for target, names in {
            "input_tokens": ("input_tokens", "inputTokens", "prompt_tokens", "input"),
            "output_tokens": ("output_tokens", "outputTokens", "completion_tokens", "output"),
            "total_tokens": ("total_tokens", "totalTokens", "total"),
        }.items():
            for name in names:
                if isinstance(candidate.get(name), int):
                    values[target] = candidate[name]
                    break
        if values:
            return values
    return None


class JsonRpcAppServerSession:
    def __init__(
        self,
        *,
        config: ServiceConfig,
        workspace_manager: WorkspaceManager,
        workspace_path: str,
        issue: Issue,
        on_event: EventCallback,
    ):
        self.config = config
        self.workspace_manager = workspace_manager
        self.workspace_path = workspace_path
        self.issue = issue
        self.on_event = on_event
        self.process: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[Any, asyncio.Future] = {}
        self._turn_waiters: dict[str, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self.thread_id: str | None = None
        self.last_stderr: str | None = None

    async def start(self) -> None:
        self.workspace_manager.validate_agent_cwd(self.workspace_path, self.workspace_path)
        try:
            self.process = await asyncio.create_subprocess_exec(
                "bash",
                "-lc",
                self.config.codex.command,
                cwd=self.workspace_path,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=APP_SERVER_STREAM_LIMIT,
            )
        except FileNotFoundError as exc:
            raise AgentError("codex command not found", code="codex_not_found") from exc

        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        await self._request(
            "initialize",
            {
                "clientInfo": {"name": "symphony", "title": "Symphony", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True, "optOutNotificationMethods": []},
            },
            timeout_ms=self.config.codex.read_timeout_ms,
        )
        thread_params = {
            "cwd": os.path.abspath(self.workspace_path),
            "approvalPolicy": self.config.codex.approval_policy,
            "sandbox": None if self.config.codex.permission_profile is not None else self.config.codex.thread_sandbox,
            "permissionProfile": self.config.codex.permission_profile,
            "serviceName": "symphony",
            "baseInstructions": None,
            "developerInstructions": None,
            "dynamicTools": [self._linear_graphql_tool_spec()] if self.config.tracker.kind == "linear" else [],
            "experimentalRawEvents": False,
            "persistExtendedHistory": True,
        }
        thread = await self._request("thread/start", thread_params, timeout_ms=self.config.codex.read_timeout_ms)
        self.thread_id = str((thread.get("thread") or {}).get("id") or "")
        if not self.thread_id:
            raise AgentError("thread/start returned no thread id", code="response_error")

    async def run_turn(self, prompt: str, turn_number: int) -> RunResult:
        if not self.thread_id:
            raise AgentError("session has not started", code="response_error")
        turn_params = {
            "threadId": self.thread_id,
            "input": [_text_input(prompt)],
            "cwd": os.path.abspath(self.workspace_path),
            "approvalPolicy": self.config.codex.approval_policy,
            "sandboxPolicy": None if self.config.codex.permission_profile is not None else self.config.codex.turn_sandbox_policy,
            "permissionProfile": self.config.codex.permission_profile,
            "responsesapiClientMetadata": {
                "symphony_issue_id": self.issue.id,
                "symphony_issue_identifier": self.issue.identifier,
                "symphony_turn_number": str(turn_number),
            },
        }
        response = await self._request("turn/start", turn_params, timeout_ms=self.config.codex.read_timeout_ms)
        turn = response.get("turn") or {}
        turn_id = str(turn.get("id") or "")
        if not turn_id:
            raise AgentError("turn/start returned no turn id", code="response_error")
        waiter = asyncio.get_running_loop().create_future()
        self._turn_waiters[turn_id] = waiter
        try:
            result = await asyncio.wait_for(waiter, timeout=max(self.config.codex.turn_timeout_ms / 1000.0, 0.001))
        except TimeoutError:
            await self.interrupt_turn(turn_id)
            return RunResult(ok=False, reason="TimedOut", error="turn_timeout")
        finally:
            self._turn_waiters.pop(turn_id, None)
        status = ((result.get("turn") or {}).get("status") or "").lower()
        if status == "completed":
            return RunResult(ok=True)
        if status == "interrupted":
            return RunResult(ok=False, reason="Failed", error="turn_cancelled")
        return RunResult(ok=False, reason="Failed", error=self._turn_failure_error(result, self.last_stderr))

    async def interrupt_turn(self, turn_id: str) -> None:
        if not self.thread_id:
            return
        try:
            await self._request(
                "turn/interrupt",
                {"threadId": self.thread_id, "turnId": turn_id},
                timeout_ms=self.config.codex.read_timeout_ms,
            )
        except Exception:
            pass

    async def close(self) -> None:
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=2)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, ValueError, OSError):
                pass
        if self._stderr_task:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, ValueError, OSError):
                pass

    async def _request(self, method: str, params: Any, *, timeout_ms: int) -> Any:
        if not self.process or not self.process.stdin:
            raise AgentError("app-server process is not running", code="port_exit")
        request_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self.process.stdin.write((json.dumps({"id": request_id, "method": method, "params": params}) + "\n").encode("utf-8"))
        await self.process.stdin.drain()
        try:
            return await asyncio.wait_for(future, timeout=max(timeout_ms / 1000.0, 0.001))
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise AgentError(f"{method} timed out", code="response_timeout") from exc

    async def _read_loop(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    self._fail_pending("app-server stdout closed")
                    return
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except json.JSONDecodeError:
                    self._emit("malformed", {"line": text[:500]})
                    continue
                await self._handle_message(message)
        except asyncio.CancelledError:
            raise
        except ValueError as exc:
            self._emit("stdout_read_error", {"message": str(exc)[:500]})
            self._fail_pending(f"app-server stdout read failed: {exc}")
        except OSError as exc:
            self._emit("stdout_read_error", {"message": str(exc)[:500]})
            self._fail_pending(f"app-server stdout read failed: {exc}")

    async def _drain_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        try:
            while True:
                line = await self.process.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    self.last_stderr = text[:1000]
                    self._emit("stderr", {"message": text[:500]})
        except asyncio.CancelledError:
            raise
        except (ValueError, OSError) as exc:
            self.last_stderr = str(exc)[:1000]
            self._emit("stderr_read_error", {"message": str(exc)[:500]})

    async def _handle_message(self, message: dict[str, Any]) -> None:
        if "id" in message and ("result" in message or "error" in message):
            future = self._pending.pop(message["id"], None)
            if future and not future.done():
                if "error" in message:
                    future.set_exception(AgentError(str(message["error"]), code="response_error"))
                else:
                    future.set_result(message.get("result"))
            return
        method = message.get("method")
        params = message.get("params") or {}
        if "id" in message and method:
            await self._handle_server_request(message["id"], str(method), params)
            return
        if method:
            self._handle_notification(str(method), params)

    async def _handle_server_request(self, request_id: Any, method: str, params: dict[str, Any]) -> None:
        if method == "item/commandExecution/requestApproval":
            self._emit("approval_auto_approved", {"method": method, **params})
            await self._respond(request_id, {"decision": "acceptForSession"})
            return
        if method == "item/fileChange/requestApproval":
            self._emit("approval_auto_approved", {"method": method, **params})
            await self._respond(request_id, {"decision": "acceptForSession"})
            return
        if method == "item/tool/requestUserInput":
            self._emit("turn_input_required", {"method": method, **params})
            await self._respond(request_id, {"answers": {}})
            self._fail_turn(params.get("turnId"), "turn_input_required")
            return
        if method == "item/tool/call":
            result = await self._handle_dynamic_tool(params)
            await self._respond(request_id, result)
            return
        self._emit("unsupported_tool_call", {"method": method, **params})
        await self._respond(request_id, {"success": False, "contentItems": [{"type": "inputText", "text": f"Unsupported server request: {method}"}]})

    async def _respond(self, request_id: Any, result: Any) -> None:
        if not self.process or not self.process.stdin:
            return
        self.process.stdin.write((json.dumps({"id": request_id, "result": result}) + "\n").encode("utf-8"))
        await self.process.stdin.drain()

    async def _handle_dynamic_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        tool = params.get("tool")
        if tool != "linear_graphql":
            self._emit("unsupported_tool_call", params)
            return {"success": False, "contentItems": [{"type": "inputText", "text": f"Unsupported tool: {tool}"}]}
        result = execute_linear_graphql(self.config, params.get("arguments"))
        self._emit("linear_graphql", {"success": result.get("success"), "callId": params.get("callId")})
        return {"success": bool(result.get("success")), "contentItems": [{"type": "inputText", "text": json.dumps(result, sort_keys=True)}]}

    def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        payload = {"method": method, **params}
        if method == "thread/started":
            thread_id = str(((params.get("thread") or {}).get("id")) or "")
            if thread_id:
                self.thread_id = thread_id
            self._emit("session_started", payload)
            return
        if method == "turn/started":
            self._emit("turn_started", payload)
            return
        if method == "turn/completed":
            self._emit("turn_completed", payload)
            turn = params.get("turn") or {}
            turn_id = turn.get("id")
            waiter = self._turn_waiters.get(turn_id)
            if waiter and not waiter.done():
                waiter.set_result(params)
            return
        if method == "error":
            self._emit("turn_ended_with_error", payload)
            self._fail_turn(params.get("turnId"), "turn_failed")
            return
        if method == "thread/tokenUsage/updated":
            self._emit(method, payload, usage=_extract_usage(params))
            return
        if method == "account/rateLimits/updated":
            self._emit(method, {"rate_limits": params.get("rateLimits")})
            return
        self._emit(method, payload, usage=_extract_usage(params))

    def _emit(self, event: str, payload: dict[str, Any], usage: dict[str, int] | None = None) -> None:
        pid = self.process.pid if self.process else None
        self.on_event(AgentEvent(event=event, timestamp=now_utc(), codex_app_server_pid=pid, usage=usage, payload=payload))

    def _fail_turn(self, turn_id: Any, error: str) -> None:
        if not turn_id:
            for waiter in self._turn_waiters.values():
                if not waiter.done():
                    waiter.set_result({"turn": {"status": "failed", "error": error}})
            return
        waiter = self._turn_waiters.get(str(turn_id))
        if waiter and not waiter.done():
            waiter.set_result({"turn": {"id": str(turn_id), "status": "failed", "error": error}})

    def _fail_pending(self, message: str) -> None:
        error = AgentError(message, code="port_exit")
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
        for waiter in self._turn_waiters.values():
            if not waiter.done():
                waiter.set_result({"turn": {"status": "failed", "error": "port_exit"}})

    @staticmethod
    def _turn_failure_error(result: dict[str, Any], last_stderr: str | None) -> str:
        turn = result.get("turn") if isinstance(result.get("turn"), dict) else {}
        status = str(turn.get("status") or "turn_failed").lower()
        detail = turn.get("error") or turn.get("reason") or turn.get("lastError")
        if detail:
            return f"{status}: {str(detail)}"
        if last_stderr:
            return f"{status}: {last_stderr}"
        return status or "turn_failed"

    @staticmethod
    def _linear_graphql_tool_spec() -> dict[str, Any]:
        return {
            "name": "linear_graphql",
            "description": "Execute one Linear GraphQL operation using Symphony tracker credentials.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "variables": {"type": "object"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        }


class CodexAppServerClient:
    def __init__(self, config: ServiceConfig, workspace_manager: WorkspaceManager):
        self.config = config
        self.workspace_manager = workspace_manager

    def session(self, *, workspace_path: str, issue: Issue, on_event: EventCallback) -> JsonRpcAppServerSession:
        return JsonRpcAppServerSession(
            config=self.config,
            workspace_manager=self.workspace_manager,
            workspace_path=workspace_path,
            issue=issue,
            on_event=on_event,
        )


class AgentRunner:
    def __init__(self, config: ServiceConfig, workspace_manager: WorkspaceManager, client: CodexAppServerClient):
        self.config = config
        self.workspace_manager = workspace_manager
        self.client = client

    async def run(
        self,
        *,
        issue: Issue,
        prompt_template: str,
        attempt: int | None,
        refresh_issue: Callable[[str], Any],
        on_event: EventCallback,
    ) -> RunResult:
        workspace = self.workspace_manager.create_for_issue(issue.identifier)
        self._write_run_marker(workspace.path, issue, attempt)
        if self.config.hooks.before_run:
            self.workspace_manager.run_hook("before_run", workspace.path, fatal=True)
        template = StrictTemplate(prompt_template)
        session = self.client.session(workspace_path=workspace.path, issue=issue, on_event=on_event)
        try:
            await session.start()
            current = issue
            for turn_number in range(1, self.config.agent.max_turns + 1):
                prompt = (
                    template.render(issue=current, attempt=attempt)
                    if turn_number == 1
                    else f"Continue work on {current.identifier}. Do not resend prior context; proceed to the next required step."
                )
                result = await session.run_turn(prompt, turn_number)
                if not result.ok:
                    return result
                return RunResult(ok=True)
            return RunResult(ok=False, reason="Failed", error="max_turns_exhausted")
        finally:
            await session.close()
            if self.config.hooks.after_run:
                self.workspace_manager.run_hook("after_run", workspace.path, fatal=False)

    @staticmethod
    def _write_run_marker(workspace_path: str, issue: Issue, attempt: int | None) -> None:
        marker = {
            "issue_id": issue.id,
            "issue_identifier": issue.identifier,
            "attempt": attempt,
            "started_at": now_utc().isoformat(),
        }
        path = os.path.join(workspace_path, ".symphony_run.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(marker, handle, sort_keys=True)
