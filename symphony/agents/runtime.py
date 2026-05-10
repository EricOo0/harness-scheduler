from __future__ import annotations

import html
import json
import re
from pathlib import Path

from symphony.agents.base import AgentAdapter, AgentRunContext, AgentRunResult, AgentProfile
from symphony.agents.claude_cli import ClaudeCodeCliAdapter
from symphony.agents.codex_app_server import CodexAppServerAdapter
from symphony.agents.events import AgentEventSink
from symphony.agents.mock import MockAgentAdapter
from symphony.agents.selector import AgentSelector
from symphony.agents.trace_normalizer import normalize_stream_events
from symphony.storage.db import beijing_now_display


class AgentRuntime:
    def __init__(self, *, store, artifacts, selector: AgentSelector | None = None):
        self.store = store
        self.artifacts = artifacts
        self.selector = selector or AgentSelector(store)
        self.adapters: dict[str, AgentAdapter] = {
            "mock": MockAgentAdapter(),
            "claude_cli": ClaudeCodeCliAdapter(),
            "codex_app_server": CodexAppServerAdapter(),
        }

    def run_stage(self, *, run_id: str, task: dict, stage: str, stage_spec: dict, prompt: str) -> tuple[AgentRunResult, Path]:
        profile = self.selector.select(task, stage)
        runs_dir = self.artifacts.ensure_artifact_path(task["artifact_path"]).parent / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        event_log_path = runs_dir / f"{run_id}.events.jsonl"
        sink = AgentEventSink(store=self.store, task_id=task["id"], run_id=run_id, agent_profile_id=profile.id, event_log_path=event_log_path)
        sink.emit("agent_selected", {"profile": self._profile_log(profile), "dangerously_skip_permissions": profile.dangerously_skip_permissions})
        self.store.append_trace_step(
            run_id=run_id,
            task_id=task["id"],
            stage=stage,
            step_type="agent.select",
            title="Agent selected",
            summary=f"Selected {profile.name} ({profile.kind}).",
            detail={"profile": self._profile_log(profile), "dangerously_skip_permissions": profile.dangerously_skip_permissions},
        )
        self.store.update_run_agent(run_id, agent_profile_id=profile.id, event_log_path=str(event_log_path), command=self._profile_command(profile))
        session = self.store.create_agent_session(agent_profile_id=profile.id, task_id=task["id"], stage=stage)
        self.store.update_run_result(run_id, agent_session_id=session)
        self.store.update_trace_run(run_id, agent_profile_id=profile.id, agent_session_id=session)
        ctx = AgentRunContext(
            run_id=run_id,
            task=task,
            stage=stage,
            stage_spec=stage_spec,
            prompt=prompt,
            artifact_path=self.artifacts.ensure_artifact_path(task["artifact_path"]),
            workspace_path=Path(task["workspace_path"]).resolve(),
            repository_path=Path(task["repository_path"]).resolve() if task.get("repository_path") else None,
            profile=profile,
            previous_session_id=self.store.latest_external_session_id(task["id"], stage, profile.id),
        )
        adapter = self.adapters.get(profile.kind)
        if not adapter:
            raise ValueError(f"unsupported agent kind: {profile.kind}")
        pending_before = self._pending_comments(ctx.artifact_path.read_text(encoding="utf-8"))
        sink.emit("agent_started", {"adapter": adapter.__class__.__name__, "session_id": session})
        self.store.append_trace_artifact(run_id=run_id, artifact_type="event_log", title="Agent Raw Event Log", path=str(event_log_path), content_type="application/jsonl")
        self.store.append_trace_step(
            run_id=run_id,
            task_id=task["id"],
            stage=stage,
            step_type="agent.start",
            title="Agent started",
            summary=f"{adapter.__class__.__name__} started.",
            detail={"adapter": adapter.__class__.__name__, "session_id": session, "command": self._profile_command(profile)},
        )
        result = adapter.run(ctx)
        self._apply_harness_result_json(result)
        self._append_native_trace_steps(ctx, result)
        archived_count = 0
        if result.status == "completed":
            archived_count = self._archive_resolved_comments(ctx, pending_before)
            if archived_count:
                sink.emit("comments_archived", {"count": archived_count, "run_id": run_id, "stage": stage})
                self.store.append_trace_step(
                    run_id=run_id,
                    task_id=task["id"],
                    stage=stage,
                    step_type="comment.archive",
                    title="Resolved comments archived",
                    summary=f"Archived {archived_count} resolved comments.",
                    detail={"count": archived_count},
                )
        if result.external_session_id:
            self.store.update_agent_session(session, external_session_id=result.external_session_id, status=result.status)
        else:
            self.store.update_agent_session(session, status=result.status)
        self.store.update_trace_run(
            run_id,
            status=result.status,
            summary=result.summary,
            suggested_status=result.suggested_status,
            external_run_id=result.external_session_id,
            error=result.error,
        )
        sink.emit(
            "agent_finished",
            {
                "status": result.status,
                "summary": result.summary,
                "suggested_status": result.suggested_status,
                "external_session_id": result.external_session_id,
                "usage": result.usage,
                "error": result.error,
                "archived_resolved_comments": archived_count,
            },
        )
        self.store.append_trace_step(
            run_id=run_id,
            task_id=task["id"],
            stage=stage,
            step_type="agent.final",
            title="Agent result",
            status=result.status,
            source="native_agent",
            summary=result.summary,
            detail={
                "status": result.status,
                "summary": result.summary,
                "suggested_status": result.suggested_status,
                "external_session_id": result.external_session_id,
                "usage": result.usage,
                "error": result.error,
            },
            raw_event=result.raw_result,
        )
        return result, event_log_path

    @staticmethod
    def _apply_harness_result_json(result: AgentRunResult) -> None:
        payload = AgentRuntime._extract_harness_result_json(result)
        if not payload:
            return
        result.raw_result = {**result.raw_result, "harness_result_json": payload}
        if payload.get("summary"):
            result.summary = str(payload["summary"])
        if payload.get("suggested_status"):
            result.suggested_status = str(payload["suggested_status"])
        if str(payload.get("status") or "").lower() == "blocked" and result.status == "completed":
            result.status = "blocked"
            result.error = str(payload.get("reason") or payload.get("summary") or "Agent suggested blocked.")

    @staticmethod
    def _extract_harness_result_json(result: AgentRunResult) -> dict | None:
        candidates = [result.summary]
        for key in ("result", "stdout", "stderr", "message"):
            value = result.raw_result.get(key)
            if isinstance(value, str):
                candidates.append(value)
        if result.raw_result:
            candidates.append(json.dumps(result.raw_result, ensure_ascii=False))
        for text in candidates:
            payload = AgentRuntime._parse_harness_result_json(str(text or ""))
            if payload:
                return payload
        return None

    @staticmethod
    def _parse_harness_result_json(text: str) -> dict | None:
        marker = "HARNESS_RESULT_JSON:"
        index = text.rfind(marker)
        if index < 0:
            return None
        tail = text[index + len(marker) :].strip()
        start = tail.find("{")
        if start < 0:
            return None
        decoder = json.JSONDecoder()
        try:
            payload, _ = decoder.raw_decode(tail[start:])
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _profile_command(profile: AgentProfile) -> dict:
        return {"command": profile.command, "args": profile.args, "model": profile.model, "kind": profile.kind}

    @staticmethod
    def _profile_log(profile: AgentProfile) -> dict:
        return json.loads(json.dumps({**AgentRuntime._profile_command(profile), "id": profile.id, "name": profile.name}, ensure_ascii=False))

    def _append_native_trace_steps(self, ctx: AgentRunContext, result: AgentRunResult) -> None:
        events = result.raw_result.get("events")
        if not isinstance(events, list):
            return
        for step in normalize_stream_events(ctx.profile.kind, events):
            self.store.append_trace_step(
                run_id=ctx.run_id,
                task_id=ctx.task["id"],
                stage=ctx.stage,
                step_type=step["step_type"],
                title=step["title"],
                status=step.get("status", "completed"),
                source="native_agent",
                summary=step.get("summary"),
                detail=step.get("detail") or {},
                raw_event=step.get("raw_event") or {},
            )

    def _archive_resolved_comments(self, ctx: AgentRunContext, pending_before: dict[str, dict]) -> int:
        if not pending_before:
            return 0
        content = ctx.artifact_path.read_text(encoding="utf-8")
        if f'data-comment-resolution-run="{ctx.run_id}"' in content:
            return 0
        comments_after = {str(comment.get("id")): comment for comment in self._comments_from_html(content) if comment.get("id")}
        resolved = [
            comments_after[comment_id]
            for comment_id in pending_before
            if comment_id in comments_after and str(comments_after[comment_id].get("status") or "").lower() == "done"
        ]
        if not resolved:
            return 0
        table = self._resolution_table(ctx, resolved)
        updated = self._append_to_section(content, "history", table)
        self.artifacts.save(str(ctx.artifact_path), updated)
        return len(resolved)

    def _resolution_table(self, ctx: AgentRunContext, comments: list[dict]) -> str:
        rows = "\n".join(
            "<tr>"
            f"<td>{html.escape(str(comment.get('id') or ''))}</td>"
            f"<td>{html.escape(str(comment.get('section') or ''))}</td>"
            f"<td>{html.escape(self._compact(comment.get('quote'), 120))}</td>"
            f"<td>{html.escape(self._compact(comment.get('content'), 180))}</td>"
            "</tr>"
            for comment in comments
        )
        return (
            f'<div data-comment-resolution-run="{html.escape(ctx.run_id)}">'
            f"<h3>评论处理记录：{html.escape(ctx.stage_spec['title'])} · {html.escape(ctx.run_id)}</h3>"
            f"<p><strong>{html.escape(beijing_now_display())}</strong> 本轮已解决 {len(comments)} 条评论，未解决评论继续保留在原文锚点和评论数据中。</p>"
            '<table class="comment-resolution-table">'
            '<colgroup><col class="col-id"><col class="col-section"><col class="col-quote"><col class="col-feedback"></colgroup>'
            "<thead><tr><th>评论 ID</th><th>位置</th><th>引用</th><th>反馈内容</th></tr></thead>"
            f"<tbody>{rows}</tbody>"
            "</table>"
            "</div>"
        )

    @staticmethod
    def _pending_comments(content: str) -> dict[str, dict]:
        return {
            str(comment.get("id")): comment
            for comment in AgentRuntime._comments_from_html(content)
            if comment.get("id") and str(comment.get("status") or "pending").lower() != "done"
        }

    @staticmethod
    def _comments_from_html(content: str) -> list[dict]:
        match = re.search(r'<script[^>]*id=["\']harness-comments["\'][^>]*>(.*?)</script>', content, flags=re.S | re.I)
        if not match:
            return []
        try:
            payload = json.loads(match.group(1).strip() or '{"comments":[]}')
        except json.JSONDecodeError:
            return []
        comments = payload.get("comments") if isinstance(payload, dict) else None
        return [comment for comment in comments if isinstance(comment, dict)] if isinstance(comments, list) else []

    @staticmethod
    def _append_to_section(content: str, section_id: str, addition: str) -> str:
        marker = f'id="{section_id}"'
        start = content.find(marker)
        if start < 0:
            return content
        close_at = content.find("</section>", start)
        if close_at < 0:
            return content
        return content[:close_at] + "\n        " + addition + "\n      " + content[close_at:]

    @staticmethod
    def _compact(value, limit: int) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if not text:
            return "（空）"
        return text if len(text) <= limit else text[: limit - 1] + "…"
