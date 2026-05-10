from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from symphony.agents.base import AgentRunContext, AgentRunResult
from symphony.agents.process import merged_env, safe_cwd


class CodexAppServerAdapter:
    kind = "codex_app_server"

    def run(self, ctx: AgentRunContext) -> AgentRunResult:
        cwd = safe_cwd(repository_path=ctx.repository_path, workspace_path=ctx.workspace_path)
        args = [ctx.profile.command, *ctx.profile.args]
        if "app-server" not in args:
            args.append("app-server")
        try:
            proc = subprocess.Popen(
                args,
                cwd=str(cwd),
                env=merged_env(ctx.profile.env),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            return AgentRunResult(status="failed", summary="Codex command not found.", error=str(exc))

        client = _JsonRpcClient(proc)
        deadline = time.monotonic() + ctx.profile.timeout_seconds
        events: list[dict[str, Any]] = []
        try:
            conversation_id = ctx.previous_session_id or client.request("newConversation", self._conversation_params(ctx, cwd), deadline).get("conversationId")
            if conversation_id:
                client.request("addConversationListener", {"conversationId": conversation_id}, deadline)
            client.request("sendUserMessage", self._message_params(str(conversation_id), ctx.prompt), deadline)
            completed = client.read_until_done(events, deadline)
            if not completed:
                proc.kill()
                return AgentRunResult(status="timeout", summary="Codex app-server run timed out.", external_session_id=str(conversation_id))
            return AgentRunResult(
                status="completed",
                summary="Codex app-server completed.",
                suggested_status=ctx.stage_spec["review"],
                external_session_id=str(conversation_id),
                raw_result={"events": events},
            )
        except Exception as exc:
            proc.kill()
            stderr = proc.stderr.read() if proc.stderr else ""
            return AgentRunResult(status="failed", summary="Codex app-server failed.", raw_result={"events": events}, error=f"{exc}\n{stderr}".strip())
        finally:
            if proc.poll() is None:
                proc.terminate()

    def _conversation_params(self, ctx: AgentRunContext, cwd: Path) -> dict[str, Any]:
        params: dict[str, Any] = {
            "cwd": str(cwd),
            "approvalPolicy": "never",
            "sandboxPolicy": {"mode": "danger-full-access"},
        }
        if ctx.profile.model:
            params["model"] = ctx.profile.model
        return params

    @staticmethod
    def _message_params(conversation_id: str, prompt: str) -> dict[str, Any]:
        return {"conversationId": conversation_id, "items": [{"type": "text", "data": {"text": prompt}}]}


class _JsonRpcClient:
    def __init__(self, proc: subprocess.Popen[str]):
        self.proc = proc
        self.next_id = 1

    def request(self, method: str, params: dict[str, Any], deadline: float) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        self._write({"id": request_id, "method": method, "params": params})
        while time.monotonic() < deadline:
            message = self._read_message(deadline)
            if not message:
                continue
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(message["error"])
                return message.get("result") or {}
            self._auto_approve(message)
        raise TimeoutError(method)

    def read_until_done(self, events: list[dict[str, Any]], deadline: float) -> bool:
        while time.monotonic() < deadline:
            message = self._read_message(deadline)
            if not message:
                if self.proc.poll() is not None:
                    return True
                continue
            events.append(message)
            self._auto_approve(message)
            name = str(message.get("method") or "")
            payload = message.get("params") or {}
            if any(token in name.lower() for token in ("completed", "turn/complete", "done")):
                return True
            if isinstance(payload, dict) and str(payload.get("type") or "").lower() in {"completed", "turn_complete", "done"}:
                return True
        return False

    def _auto_approve(self, message: dict[str, Any]) -> None:
        if "id" not in message or "method" not in message:
            return
        method = str(message["method"]).lower()
        if "approval" in method or "approve" in method:
            self._write({"id": message["id"], "result": {"decision": "approve", "approved": True}})

    def _write(self, payload: dict[str, Any]) -> None:
        if not self.proc.stdin:
            raise RuntimeError("codex stdin closed")
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def _read_message(self, deadline: float) -> dict[str, Any] | None:
        if not self.proc.stdout:
            return None
        if time.monotonic() >= deadline:
            return None
        line = self.proc.stdout.readline()
        if not line:
            return None
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return {"method": "raw_stdout", "params": {"line": line.rstrip()}}
