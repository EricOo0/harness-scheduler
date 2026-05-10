from __future__ import annotations

import html
import json
import re

from symphony.agents.base import AgentRunContext, AgentRunResult
from symphony.storage.db import beijing_now_display


class MockAgentAdapter:
    """Temporary adapter used before real Agent/Codex invocation is enabled."""

    kind = "mock"

    def run(self, ctx: AgentRunContext) -> AgentRunResult:
        stamp = beijing_now_display()
        task = ctx.task
        stage_title = ctx.stage_spec["title"]
        additions = {
            "background": f"<p><strong>Mock Agent：</strong>{stage_title} 已读取任务背景，并确认产物路径可用。</p>",
            "interaction": f"<p><strong>Mock Agent：</strong>{stage_title} 阶段暂无需要用户补充的问题。</p>",
            "solution": f"<p><strong>Mock Agent 方案摘要：</strong>围绕“{html.escape(task['title'])}”生成初步方案，真实 Agent 接入后将在此处展开。</p>",
            "task-breakdown": "<ul><li>Mock 子任务 1：完善实现</li><li>Mock 子任务 2：补充验证</li></ul>",
            "validation": "<p><strong>Mock 验证：</strong>已确认 HTML 模板、评论 JSON 和 runtime 标识存在。</p>",
            "history": f"<p><strong>{stamp}</strong> Mock Agent 完成「{stage_title}」阶段。</p>",
            "learning": "<p><strong>Mock 沉淀：</strong>真实 Agent 接入后生成 Markdown 经验草稿。</p>",
        }
        content = ctx.artifact_path.read_text(encoding="utf-8")
        for section in ctx.stage_spec["editable_sections"]:
            addition = additions.get(section)
            if addition:
                content = self._append_to_section(content, section, addition)
        content, handled_count = self._handle_pending_comments(content, set(ctx.stage_spec["editable_sections"]))
        if handled_count:
            content = self._append_to_section(content, "history", f"<p><strong>Mock Agent：</strong>已读取并标记 {handled_count} 条待处理评论。</p>")
        ctx.artifact_path.write_text(content, encoding="utf-8")
        return AgentRunResult(
            status="completed",
            summary=f"Mock Agent updated artifact.html for {stage_title}.",
            suggested_status=ctx.stage_spec["review"],
            raw_result={"adapter": self.__class__.__name__},
        )

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

    def _handle_pending_comments(self, content: str, editable_sections: set[str]) -> tuple[str, int]:
        match = re.search(r'(<script[^>]*id=["\']harness-comments["\'][^>]*>)(.*?)(</script>)', content, flags=re.S | re.I)
        if not match:
            return content, 0
        try:
            payload = json.loads(match.group(2).strip() or '{"comments":[]}')
        except json.JSONDecodeError:
            return content, 0
        comments = payload.get("comments") if isinstance(payload, dict) else []
        handled = 0
        for comment in comments:
            if not isinstance(comment, dict) or str(comment.get("status") or "pending").lower() == "done":
                continue
            section = str(comment.get("section") or "")
            if section in editable_sections or section == "history":
                content = self._append_to_section(
                    content,
                    section if section in editable_sections else "history",
                    f"<p><strong>Mock Agent 处理评论 {html.escape(str(comment.get('id') or ''))}：</strong>{html.escape(str(comment.get('content') or ''))}</p>",
                )
            comment["status"] = "done"
            handled += 1
            comment["handledBy"] = "MockAgentAdapter"
        if handled:
            new_json = json.dumps(payload, ensure_ascii=False, indent=2)
            content = re.sub(
                r'(<script[^>]*id=["\']harness-comments["\'][^>]*>)(.*?)(</script>)',
                lambda m: m.group(1) + "\n    " + new_json + "\n  " + m.group(3),
                content,
                count=1,
                flags=re.S | re.I,
            )
            for comment in comments:
                comment_id = comment.get("id")
                if comment_id:
                    content = re.sub(
                        rf'(<mark\b(?=[^>]*data-comment-id="{re.escape(str(comment_id))}")(?=[^>]*data-comment-status=")[^>]*data-comment-status=")[^"]*(")',
                        r"\1done\2",
                        content,
                    )
                anchor_id = comment.get("anchorId")
                if anchor_id:
                    content = re.sub(
                        rf'(<mark\b(?=[^>]*data-comment-anchor-id="{re.escape(str(anchor_id))}")(?=[^>]*data-comment-status=")[^>]*data-comment-status=")[^"]*(")',
                        r"\1done\2",
                        content,
                    )
        return content, handled
