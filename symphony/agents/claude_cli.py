from __future__ import annotations

import json
import subprocess

from symphony.agents.base import AgentRunContext, AgentRunResult
from symphony.agents.process import merged_env, run_command, safe_cwd


class ClaudeCodeCliAdapter:
    kind = "claude_cli"

    def run(self, ctx: AgentRunContext) -> AgentRunResult:
        cwd = safe_cwd(repository_path=ctx.repository_path, workspace_path=ctx.workspace_path)
        args = [ctx.profile.command, *ctx.profile.args, "-p", ctx.prompt]
        if "--output-format" not in args:
            args.extend(["--output-format", "stream-json", "--verbose", "--include-partial-messages"])
        if ctx.profile.dangerously_skip_permissions and "--dangerously-skip-permissions" not in args:
            args.append("--dangerously-skip-permissions")
        if ctx.profile.model and "--model" not in args:
            args.extend(["--model", ctx.profile.model])
        if ctx.previous_session_id and "--resume" not in args and "--continue" not in args:
            args.extend(["--resume", ctx.previous_session_id])

        try:
            completed = run_command(args, cwd=cwd, env=merged_env(ctx.profile.env), timeout_seconds=ctx.profile.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            return AgentRunResult(status="timeout", summary="Claude Code run timed out.", error=str(exc))
        except FileNotFoundError as exc:
            return AgentRunResult(status="failed", summary="Claude Code command not found.", error=str(exc))

        raw = self._parse_output(completed.stdout)
        session_id = raw.get("session_id") or raw.get("external_session_id")
        if completed.returncode != 0:
            return AgentRunResult(
                status="failed",
                summary="Claude Code exited with non-zero status.",
                external_session_id=session_id,
                raw_result=raw,
                error=completed.stderr.strip() or completed.stdout[-2000:],
            )
        return AgentRunResult(
            status="completed",
            summary=raw.get("result") or "Claude Code completed.",
            suggested_status=ctx.stage_spec["review"],
            external_session_id=session_id,
            usage=raw.get("usage") or {},
            raw_result={**raw, "stdout": completed.stdout, "stderr": completed.stderr},
        )

    def _parse_output(self, output: str) -> dict:
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        if not lines:
            return {}
        parsed: list[dict] = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, list):
                parsed.extend(item for item in value if isinstance(item, dict))
            elif isinstance(value, dict):
                parsed.append(value)
        if not parsed:
            return {"result": output.strip()}
        result = next((item for item in reversed(parsed) if item.get("type") == "result"), parsed[-1])
        if "result" not in result:
            text = "".join(str(item.get("delta") or item.get("text") or "") for item in parsed)
            if text:
                result = {**result, "result": text}
        return result
