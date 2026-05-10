from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from symphony.domain.workflow import ALLOWED_NEXT_STATUSES, STAGES
from symphony.storage.db import HarnessPaths
from symphony.storage.task_store import LocalTaskStore


class PromptBuilder:
    def __init__(self, store: LocalTaskStore, paths: HarnessPaths):
        self.store = store
        self.paths = paths

    def build(self, task: dict[str, Any]) -> str:
        stage = task["phase"]
        spec = STAGES.get(stage)
        prompt_config = self.store.get_stage_prompt(stage) if spec else None
        return self.build_for_stage(task, stage, prompt_config)

    def build_for_stage(self, task: dict[str, Any], stage: str, prompt_config: dict[str, Any] | None) -> str:
        spec = STAGES.get(stage)
        if not spec or not prompt_config:
            return "当前任务不在可调度阶段。"

        sections = prompt_config["editable_sections"]
        skill_lines = self._collect_skill_hints(prompt_config.get("enabled_skill_names") or [])
        skill_block = "\n".join(skill_lines) if skill_lines else "（本阶段未声明可用 Skill/MCP）"
        learning_export_path = self._learning_export_path(task)

        required_reads = self._format_lines(prompt_config.get("required_reads") or [])
        allowed_actions = self._format_lines(prompt_config.get("allowed_actions") or [])
        forbidden_actions = self._format_lines(prompt_config.get("forbidden_actions") or [])
        output_requirements = self._format_lines(prompt_config.get("output_requirements") or [])
        pending_comments = self._pending_comments_block(task, sections)
        allowed_statuses = ALLOWED_NEXT_STATUSES.get(stage, [spec["review"], "已阻塞"])
        allowed_status_text = " / ".join(allowed_statuses)
        allowed_status_json = "|".join(allowed_statuses)

        return f"""# Harness Scheduler Agent Prompt

## BaseSystemPrompt
你正在执行 Harness Scheduler 的「{spec['title']}」阶段。
请遵守以下系统约束：
- Artifact 是真实 HTML 文件，必须直接读取并编辑指定路径。
- 评论也是 HTML 的一部分，不要把评论迁移到外部系统。
- 浏览器运行时由服务端渲染时注入；artifact.html 持久化文件中只应保留正文、导航、script#harness-comments 和 script#harness-render-assets。
- 不要新增 script/style/on* 事件处理器；只允许维护 script#harness-comments(type=application/json) 中的评论状态，以及 script#harness-render-assets(type=application/json) 中的渲染资源。
- 不要修改 aside#harness-section-nav。
- 不要删除 7 个主 section。
- 如需画流程图/架构图/状态图，在正文位置写 <div data-render="mermaid" data-diagram-id="唯一ID"></div>，并在 script#harness-render-assets 的 mermaid 数组中维护同 ID 的结构化 flowchart 数据：{{id,title,direction,nodes,edges}}；不要使用 <pre data-render="mermaid">，不要引入 Mermaid 脚本或保存渲染后的 SVG。
- Mermaid 默认不要手写 source DSL；用 nodes/edges 表达图，runtime 会统一生成带引号 label，避免括号、逗号、斜杠、冒号或 HTML label 被 Mermaid 解析为语法 token。
- 信息不足时先写清楚缺口，不要猜测需求、仓库、分支或验证方式。
- 当前运行在全自动权限模式；不要等待人工审批，能执行就直接执行，不能执行就写明阻塞原因。

## TaskContext
当前任务：{task['title']}
当前状态：{task['status']}
任务 ID：{task['id']}
仓库路径：{task.get('repository_path') or '未配置'}
目标分支：{task.get('target_branch') or '未配置'}
工作区路径：{task['workspace_path']}
HTML 产物路径：{task['artifact_path']}
经验导出路径：{learning_export_path}

## ArtifactContextForCurrentStage
请直接读取并编辑上述 HTML 文件。评论也在 HTML 中：
- 正文锚点：mark[data-comment-anchor-id]，旧产物可能仍有 mark[data-comment-id]
- 评论数据：script#harness-comments(type=application/json)，comments 是主对象，anchors 是可选展示锚点
- 必须优先处理 status=pending 的评论；处理完成后把对应 comment.status 更新为 done，并同步该 anchor/mark 的 data-comment-status。
- Mermaid 图表：正文只写 <div data-render="mermaid" data-diagram-id="..."></div> 占位符；script#harness-render-assets(type=application/json) 的 mermaid 数组默认写结构化对象：{{"id":"...","title":"...","direction":"TD","nodes":[{{"id":"A","label":"API 请求入口<br/>AgentSandboxHTTPNet.request","shape":"rect"}}],"edges":[{{"from":"A","to":"B","label":"成功"}}]}}。
- 只有非 flowchart 图或结构化 edges 无法表达时，才使用 source 字符串；source 里的节点 label 必须写成 A["label"]，不要写 A[label] 承载复杂文案。

本阶段重点修改模块：
{chr(10).join(f'- section#{section}' for section in sections)}

## PendingComments
{pending_comments}

本阶段必须读取：
{required_reads}

## StagePrompt
阶段目标：
{prompt_config.get('stage_goal') or prompt_config['objective']}

阶段补充指令：
{prompt_config['prompt']}

允许动作：
{allowed_actions}

禁止动作：
{forbidden_actions}

输出要求：
{output_requirements}

## SkillHints
本阶段可用 Skill/MCP：
{skill_block}

## OutputContract
- 保存 artifact.html。
- 返回修改摘要。
- 建议下一状态必须是当前阶段允许的精确状态名之一：{allowed_status_text}。
- 不要写解释文本、多个状态或自造状态；信息不足或外部工具失败时，建议「已阻塞」，并写清阻塞原因和用户下一步。
- 最终回复末尾输出 HARNESS_RESULT_JSON: {{"status":"completed|blocked","summary":"...","suggested_status":"{allowed_status_json}","reason":"..."}}
"""

    def _format_lines(self, items: list[str]) -> str:
        return "\n".join(f"- {item}" for item in items) if items else "- 无"

    def _collect_skill_hints(self, enabled_names: list[str]) -> list[str]:
        if not enabled_names:
            return []
        skills = {s["name"]: s for s in self.store.list_skills() if s.get("enabled")}
        lines: list[str] = []
        for name in enabled_names:
            if "*" in name:
                pattern = "^" + re.escape(name).replace("\\*", ".*") + "$"
                matched = [skill for skill_name, skill in skills.items() if re.match(pattern, skill_name)]
                if matched:
                    for skill in sorted(matched, key=lambda item: item["name"]):
                        hint = skill.get("prompt_hint") or skill.get("description") or ""
                        lines.append(f"- {skill['name']}：{hint}" if hint else f"- {skill['name']}")
                else:
                    lines.append(f"- {name}（通配符，未在 Skills 列表中展开；Agent 可按意图自行选择匹配 skill）")
                continue
            skill = skills.get(name)
            if not skill:
                lines.append(f"- {name}（未在 Skills 列表中找到）")
                continue
            hint = skill.get("prompt_hint") or skill.get("description") or ""
            suffix = f"：{hint}" if hint else ""
            path_text = f"（路径 {skill['path']}）" if skill.get("path") else ""
            lines.append(f"- {skill['name']}{path_text}{suffix}")
        return lines

    def _learning_export_path(self, task: dict[str, Any]) -> str:
        task_id = str(task.get("id") or "").strip()
        return str(self.paths.learning_export_path(task_id)) if task_id else "未配置"

    def _pending_comments_block(self, task: dict[str, Any], editable_sections: list[str]) -> str:
        artifact_path = task.get("artifact_path")
        if not artifact_path:
            return "- 无 artifact 路径，无法读取评论。"
        try:
            content = Path(str(artifact_path)).read_text(encoding="utf-8")
        except OSError as exc:
            return f"- 无法读取 artifact 评论：{exc}"
        comments = self._extract_comments(content)
        if not comments:
            return "- 当前没有评论。"

        editable = set(editable_sections)
        pending = [comment for comment in comments if str(comment.get("status") or "pending").lower() != "done"]
        if not pending:
            return "- 当前没有待处理评论。"

        lines: list[str] = []
        for comment in pending:
            section = str(comment.get("section") or "unknown")
            scope_hint = "本阶段可编辑" if section in editable else "非本阶段重点，但仍需判断是否影响本阶段输出"
            lines.append(
                "\n".join(
                    [
                        f"- 评论 ID：{comment.get('id') or '(missing id)'}",
                        f"  section：{section}（{scope_hint}）",
                        f"  引用：{self._compact(comment.get('quote'))}",
                        f"  反馈：{self._compact(comment.get('content'))}",
                    ]
                )
            )
        return "\n".join(lines)

    def _extract_comments(self, artifact_html: str) -> list[dict[str, Any]]:
        match = re.search(r'<script[^>]*id=["\']harness-comments["\'][^>]*>(.*?)</script>', artifact_html, flags=re.S | re.I)
        if not match:
            return []
        raw = match.group(1).strip()
        try:
            payload = json.loads(raw or '{"comments":[]}')
        except json.JSONDecodeError:
            return [{"id": "parse_error", "section": "unknown", "quote": "", "content": "评论 JSON 解析失败，请先修复 script#harness-comments。", "status": "pending"}]
        comments = payload.get("comments") if isinstance(payload, dict) else None
        return [comment for comment in comments if isinstance(comment, dict)] if isinstance(comments, list) else []

    @staticmethod
    def _compact(value: Any, limit: int = 240) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if not text:
            return "（空）"
        return text if len(text) <= limit else text[: limit - 1] + "…"
