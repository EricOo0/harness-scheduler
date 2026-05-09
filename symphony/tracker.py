from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from .config import ServiceConfig
from .errors import TrackerError
from .models import BlockerRef, Issue, IssueComment, now_beijing


_GRAPHQL_OPERATION_RE = re.compile(r"\b(query|mutation|subscription)\b")
_DOCX_TOKEN_RE = re.compile(r"/docx/([A-Za-z0-9]+)")
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\((https?://[^)]+)\)")
_STATE_TRANSITION_TITLE = "### 4.1 状态流转记录（调度器填写）"
_ROUND_RECORD_TITLE = "### 4.2 轮次记录（Agent 填写）"
_LEGACY_HANDOFF_DETAIL_TITLE = "### 交接记录详情（Agent 填写）"
_ALLOWED_LARK_SUGGESTED_NEXT_STATES: dict[str, set[str]] = {
    "待规划": {"需求确认"},
    "待方案设计": {"需求确认", "方案评审"},
    "方案需修改": {"需求确认", "方案评审"},
    "待实施": {"代码评审"},
    "代码需修改": {"代码评审"},
}


def build_tracker(config: ServiceConfig):
    if config.tracker.kind == "lark_base":
        return LarkBaseClient(config)
    return LinearClient(config)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def normalize_issue(node: dict[str, Any]) -> Issue:
    labels_node = node.get("labels") or {}
    labels_edges = labels_node.get("nodes") or labels_node.get("edges") or []
    labels: list[str] = []
    for item in labels_edges:
        label = item.get("node", item) if isinstance(item, dict) else {}
        name = label.get("name") if isinstance(label, dict) else None
        if name:
            labels.append(str(name).lower())

    blockers: list[BlockerRef] = []
    relations = node.get("inverseRelations") or {}
    for rel in relations.get("nodes") or []:
        if not isinstance(rel, dict) or str(rel.get("type", "")).lower() != "blocks":
            continue
        issue = rel.get("issue") or rel.get("relatedIssue") or {}
        state = issue.get("state") or {}
        blockers.append(
            BlockerRef(
                id=issue.get("id"),
                identifier=issue.get("identifier"),
                state=state.get("name") if isinstance(state, dict) else issue.get("state"),
            )
        )

    state = node.get("state") or {}
    priority = node.get("priority")
    if not isinstance(priority, int):
        priority = None

    comments: list[IssueComment] = []
    comments_node = node.get("comments") or {}
    for comment in comments_node.get("nodes") or []:
        if not isinstance(comment, dict):
            continue
        user = comment.get("user") or {}
        comments.append(
            IssueComment(
                id=str(comment.get("id") or ""),
                body=str(comment.get("body") or ""),
                created_at=_parse_time(comment.get("createdAt") or comment.get("created_at")),
                user_name=user.get("name") if isinstance(user, dict) else None,
            )
        )

    return Issue(
        id=str(node.get("id") or ""),
        identifier=str(node.get("identifier") or ""),
        title=str(node.get("title") or ""),
        description=node.get("description"),
        priority=priority,
        state=str(state.get("name") if isinstance(state, dict) else node.get("state") or ""),
        branch_name=node.get("branchName") or node.get("branch_name"),
        url=node.get("url"),
        labels=labels,
        blocked_by=blockers,
        comments=comments,
        created_at=_parse_time(node.get("createdAt") or node.get("created_at")),
        updated_at=_parse_time(node.get("updatedAt") or node.get("updated_at")),
    )


def _stringify_lark_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return ", ".join(_stringify_lark_value(item) for item in value if item not in (None, ""))
    if isinstance(value, dict):
        for key in ("text", "name", "url", "link", "id"):
            if key in value:
                return _stringify_lark_value(value[key])
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _parse_lark_time(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        raw = float(value)
        if raw > 10_000_000_000:
            raw /= 1000
        return datetime.fromtimestamp(raw, tz=timezone.utc)
    return _parse_time(value)


class LarkBaseClient:
    def __init__(self, config: ServiceConfig):
        self.config = config

    @property
    def fields(self) -> dict[str, str]:
        return self.config.tracker.field_names

    def fetch_candidate_issues(self) -> list[Issue]:
        return self.fetch_dispatch_issues()

    def fetch_dispatch_issues(self) -> list[Issue]:
        return self._fetch_issues_by_states(self.config.tracker.dispatch_states)

    def fetch_recovery_issues(self) -> list[Issue]:
        return self._fetch_issues_by_states(self.config.tracker.running_states)

    def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
        if not state_names:
            return []
        return self._fetch_issues_by_states(state_names)

    def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        if not issue_ids:
            return []
        wanted = set(issue_ids)
        return [issue for issue in self._fetch_all_issues() if issue.id in wanted]

    def prepare_issue_for_dispatch(self, issue: Issue) -> Issue:
        if issue.state != "待规划":
            return issue
        existing_doc_url = self._normalize_doc_url(self._extract_field_from_description(issue.description, "AI 交互文档链接"))
        if existing_doc_url:
            self._ensure_planning_doc_initialized(issue, existing_doc_url)
            issue.url = existing_doc_url
            return issue
        template = self._fetch_template_markdown()
        if not template:
            return issue
        reference = self._extract_field_from_description(issue.description, "参考资料链接")
        markdown = self._render_ai_doc_template(template, issue, reference)
        created = self._run_cli([
            "docs",
            "+create",
            "--as",
            "user",
            "--title",
            f"{issue.title} - AI 交互文档",
            "--markdown",
            markdown,
        ])
        doc_url = (((created.get("data") or {}).get("doc_url")) or "")
        if not doc_url:
            return issue
        self._update_record(issue.id, {self.fields["ai_doc_url"]: doc_url})
        issue.url = doc_url
        issue.description = self._build_description(
            {
                self.fields["description"]: self._extract_field_from_description(issue.description, "任务说明"),
                self.fields["reference_url"]: reference,
                self.fields["ai_doc_url"]: doc_url,
                self.fields["pr_url"]: self._extract_field_from_description(issue.description, "PR 链接"),
                self.fields["handoff"]: self._extract_field_from_description(issue.description, "Agent 交接说明"),
                self.fields["suggested_next_state"]: self._extract_field_from_description(issue.description, "Agent 建议下一状态"),
                self.fields["latest_error"]: self._extract_field_from_description(issue.description, "最近错误"),
                self.fields["blocker_reason"]: self._extract_field_from_description(issue.description, "阻塞原因"),
            }
        )
        return issue

    def move_issue_to_state(self, issue_id: str, state_name: str, *, clear_suggested_next_state: bool = False, failure_reason: str | None = None) -> bool:
        try:
            existing_record = self._fetch_record(issue_id)
        except TrackerError:
            existing_record = {}
        previous_state = _stringify_lark_value(existing_record.get(self.fields["state"])).strip()
        payload: dict[str, Any] = {self.fields["state"]: state_name}
        if clear_suggested_next_state:
            payload[self.fields["suggested_next_state"]] = ""
        if state_name == "已阻塞":
            reason = self._normalize_failure_reason(failure_reason) or "Agent 执行失败或超时，请查看最近错误和 AI 交互文档。"
            payload[self.fields["blocker_reason"]] = reason
            payload[self.fields["latest_error"]] = reason
        self._update_record(issue_id, payload)
        self._append_state_transition_to_ai_doc(existing_record, previous_state, state_name)
        return True

    def add_comment(self, issue_id: str, body: str) -> bool:
        if body.startswith("Symphony system: started") or body.startswith("Symphony system: resumed"):
            return True
        if body.startswith("Symphony system: agent run completed successfully"):
            return True
        update = {self.fields["handoff"]: body}
        is_failure = "did not complete successfully" in body
        if is_failure:
            update[self.fields["latest_error"]] = body
            update[self.fields["blocker_reason"]] = body
        self._update_record(issue_id, update)
        if is_failure:
            self._append_system_handoff_to_ai_doc(issue_id, body)
        return True

    def completion_block_reason(self, issue: Issue, source_state: str) -> str | None:
        if source_state not in {"待实施", "代码需修改"}:
            return None
        refreshed = self.fetch_issue_states_by_ids([issue.id])
        current = refreshed[0] if refreshed else issue
        pr_url = self._extract_field_from_description(current.description, "PR 链接")
        if pr_url.strip():
            return None
        return "代码实施阶段未写入 PR 链接，不能进入代码评审。请创建或更新 GitLab MR/PR，并把链接写回任务主表「PR 链接」。"

    def completion_success_state_for(self, issue: Issue, source_state: str, default_state: str | None) -> tuple[str | None, str | None]:
        try:
            record = self._fetch_record(issue.id)
        except TrackerError as exc:
            return default_state, f"读取任务主表「Agent 建议下一状态」失败：{exc}"
        suggested = _stringify_lark_value(record.get(self.fields["suggested_next_state"])).strip()
        if not suggested:
            return default_state, None
        allowed = _ALLOWED_LARK_SUGGESTED_NEXT_STATES.get(source_state, set())
        if suggested not in allowed:
            allowed_text = "、".join(sorted(allowed)) or "无"
            return None, f"非法的 Agent 建议下一状态：{suggested}。来源状态「{source_state}」只允许：{allowed_text}。"
        return suggested, None

    def _fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
        wanted = {state.lower() for state in state_names}
        return [issue for issue in self._fetch_all_issues() if issue.state.lower() in wanted]

    def _fetch_all_issues(self) -> list[Issue]:
        payload = self._run_cli([
            "base",
            "+record-list",
            "--as",
            "user",
            "--base-token",
            self._base_token(),
            "--table-id",
            self._task_table_id(),
            "--limit",
            "100",
            "--format",
            "json",
        ])
        data = payload.get("data") or {}
        rows = data.get("data") or []
        field_names = data.get("fields") or []
        record_ids = data.get("record_id_list") or []
        issues: list[Issue] = []
        for index, row in enumerate(rows):
            if not isinstance(row, list):
                continue
            record = {str(field_names[pos]): row[pos] for pos in range(min(len(field_names), len(row)))}
            record_id = str(record_ids[index] if index < len(record_ids) else "")
            issue = self._record_to_issue(record_id, record)
            if issue:
                issues.append(issue)
        return issues

    def _fetch_record(self, record_id: str) -> dict[str, Any]:
        payload = self._run_cli([
            "base",
            "+record-get",
            "--as",
            "user",
            "--base-token",
            self._base_token(),
            "--table-id",
            self._task_table_id(),
            "--record-id",
            record_id,
        ])
        record = ((payload.get("data") or {}).get("record") or {})
        return record if isinstance(record, dict) else {}

    def _record_to_issue(self, record_id: str, record: dict[str, Any]) -> Issue | None:
        title = _stringify_lark_value(record.get(self.fields["title"])).strip()
        state = _stringify_lark_value(record.get(self.fields["state"])).strip()
        if not record_id or not title or not state:
            return None
        ai_doc_url = _stringify_lark_value(record.get(self.fields["ai_doc_url"])).strip()
        return Issue(
            id=record_id,
            identifier=record_id,
            title=title,
            description=self._build_description(record),
            state=state,
            url=ai_doc_url or _stringify_lark_value(record.get(self.fields["reference_url"])).strip() or None,
            created_at=_parse_lark_time(record.get(self.fields["created_at"])),
            updated_at=_parse_lark_time(record.get(self.fields["updated_at"])),
        )

    def _build_description(self, record: dict[str, Any]) -> str:
        parts = [
            "## Base 任务字段",
            f"- 任务说明：{_stringify_lark_value(record.get(self.fields['description']))}",
            f"- 参考资料链接：{_stringify_lark_value(record.get(self.fields['reference_url']))}",
            f"- AI 交互文档链接：{_stringify_lark_value(record.get(self.fields['ai_doc_url']))}",
            f"- PR 链接：{_stringify_lark_value(record.get(self.fields['pr_url']))}",
            f"- Agent 交接说明：{_stringify_lark_value(record.get(self.fields['handoff']))}",
            f"- Agent 建议下一状态：{_stringify_lark_value(record.get(self.fields['suggested_next_state']))}",
            f"- 最近错误：{_stringify_lark_value(record.get(self.fields['latest_error']))}",
            f"- 阻塞原因：{_stringify_lark_value(record.get(self.fields['blocker_reason']))}",
        ]
        ai_doc_url = _stringify_lark_value(record.get(self.fields["ai_doc_url"])).strip()
        if ai_doc_url:
            parts.extend(
                [
                    "",
                    "## 上下文读取方式",
                    "AI 交互文档正文和评论不由调度器预读进 prompt。Agent 每轮必须使用 lark-cli 自行读取该文档正文、划词评论和全文评论。",
                ]
            )
        return "\n".join(parts)

    def _fetch_doc_context(self, doc_url: str) -> tuple[str, list[dict[str, Any]]]:
        doc_url = self._normalize_doc_url(doc_url)
        if not doc_url:
            return "", []
        markdown = ""
        try:
            payload = self._run_cli(["docs", "+fetch", "--as", "user", "--doc", doc_url, "--format", "json", "--limit", "20000"])
            markdown = str(((payload.get("data") or {}).get("markdown")) or "")
        except TrackerError:
            markdown = ""
        return markdown, self._fetch_doc_comments(doc_url)

    def _append_system_handoff_to_ai_doc(self, issue_id: str, body: str) -> None:
        try:
            record = self._fetch_record(issue_id)
            doc_url = self._normalize_doc_url(_stringify_lark_value(record.get(self.fields["ai_doc_url"])))
            if not doc_url:
                return
            markdown = self._render_system_handoff(body)
            self._insert_round_record(doc_url, markdown)
        except TrackerError:
            return

    def _append_state_transition_to_ai_doc(self, record: dict[str, Any], previous_state: str, next_state: str) -> None:
        doc_url = self._normalize_doc_url(_stringify_lark_value(record.get(self.fields["ai_doc_url"])))
        if not doc_url:
            return
        try:
            title = _stringify_lark_value(record.get(self.fields["title"])).strip()
            self._append_state_transition_row(doc_url, previous_state, next_state, title)
        except TrackerError:
            return

    def _append_state_transition_row(self, doc_url: str, previous_state: str, next_state: str, title: str) -> None:
        checked_at = now_beijing().strftime("%Y-%m-%d %H:%M:%S UTC+08:00")
        from_state = previous_state or "未知"
        row = (
            f"| {self._md_cell(checked_at)} | Symphony 调度器 | {self._md_cell(from_state)} | "
            f"{self._md_cell(next_state)} | {self._md_cell(title or '未知')} |\n"
        )
        header = "| 时间 | 触发方 | 来源状态 | 目标状态 | 原因 |\n| --- | --- | --- | --- | --- |\n"
        try:
            section = self._section_with_appended_table_row(doc_url, _STATE_TRANSITION_TITLE, header, row)
            self._run_cli(
                [
                    "docs",
                    "+update",
                    "--as",
                    "user",
                    "--doc",
                    doc_url,
                    "--mode",
                    "replace_range",
                    "--selection-by-title",
                    _STATE_TRANSITION_TITLE,
                    "--markdown",
                    section,
                ]
            )
        except TrackerError:
            self._insert_doc_record(doc_url, _LEGACY_HANDOFF_DETAIL_TITLE, self._render_state_transition(previous_state, next_state, title))

    def _insert_round_record(self, doc_url: str, markdown: str) -> None:
        self._insert_doc_record(doc_url, _ROUND_RECORD_TITLE, markdown, fallback_title=_LEGACY_HANDOFF_DETAIL_TITLE)

    def _section_with_appended_table_row(self, doc_url: str, title: str, header: str, row: str) -> str:
        markdown, _ = self._fetch_doc_context(doc_url)
        section = self._extract_markdown_section(markdown, title)
        if not section:
            raise TrackerError(f"missing document section: {title}", code="missing_doc_section")
        lines = section.rstrip().splitlines()
        if row.strip() in {line.strip() for line in lines}:
            return section
        separator_index = next((idx for idx, line in enumerate(lines) if line.strip().startswith("| ---")), None)
        if separator_index is None:
            return f"{title}\n\n{header}{row}"
        insert_at = separator_index + 1
        while insert_at < len(lines) and lines[insert_at].strip().startswith("|"):
            insert_at += 1
        lines.insert(insert_at, row.rstrip())
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _extract_markdown_section(markdown: str, title: str) -> str:
        if not markdown:
            return ""
        lines = markdown.splitlines()
        start = next((idx for idx, line in enumerate(lines) if line.strip() == title), None)
        if start is None:
            return ""
        level = len(title) - len(title.lstrip("#"))
        end = len(lines)
        for idx in range(start + 1, len(lines)):
            stripped = lines[idx].lstrip()
            if not stripped.startswith("#"):
                continue
            heading_level = len(stripped) - len(stripped.lstrip("#"))
            if heading_level <= level and stripped[heading_level:heading_level + 1] == " ":
                end = idx
                break
        return "\n".join(lines[start:end]).strip() + "\n"

    @staticmethod
    def _md_cell(value: str) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ").strip()

    def _insert_doc_record(self, doc_url: str, title: str, markdown: str, *, fallback_title: str | None = None) -> None:
        try:
            self._run_cli(self._doc_insert_after_command(doc_url, title, markdown))
        except TrackerError:
            if not fallback_title:
                raise
            self._run_cli(self._doc_insert_after_command(doc_url, fallback_title, markdown))

    @staticmethod
    def _doc_insert_after_command(doc_url: str, title: str, markdown: str) -> list[str]:
        return [
            "docs",
            "+update",
            "--as",
            "user",
            "--doc",
            doc_url,
            "--mode",
            "insert_after",
            "--selection-by-title",
            title,
            "--markdown",
            markdown,
        ]

    @staticmethod
    def _render_state_transition(previous_state: str, next_state: str, title: str) -> str:
        checked_at = now_beijing().strftime("%Y-%m-%d %H:%M:%S UTC+08:00")
        from_state = previous_state or "未知"
        return (
            f"\n\n#### Symphony 状态流转记录（调度器填写，{checked_at}）\n\n"
            "| 时间 | 类型 | 任务 | 状态变化 | 触发方 | 下一步 |\n"
            "| --- | --- | --- | --- | --- | --- |\n"
            f"| {checked_at} | 状态流转记录 | {title or '未知'} | {from_state} -> {next_state} | Symphony 调度器 | 按新状态等待下一轮调度或用户处理 |\n"
        )

    @staticmethod
    def _render_system_handoff(body: str) -> str:
        checked_at = now_beijing().strftime("%Y-%m-%d %H:%M:%S UTC+08:00")
        return (
            f"\n\n#### Symphony 系统失败记录（调度器填写，{checked_at}）\n\n"
            "| 时间 | 类型 | 结果 | 失败原因 | 下一步 |\n"
            "| --- | --- | --- | --- | --- |\n"
            f"| {checked_at} | 系统失败记录 | Agent 执行失败，任务已由调度器转入阻塞状态 | {body} | 请根据失败原因修正环境、登录态、任务信息或执行配置，然后把任务状态改回对应待处理状态 |\n"
        )

    def _fetch_doc_comments(self, doc_url: str) -> list[dict[str, Any]]:
        doc_url = self._normalize_doc_url(doc_url)
        token = self._docx_token(doc_url)
        if not token:
            return []
        comments: list[dict[str, Any]] = []
        for is_whole in (False, True):
            params = {"file_token": token, "file_type": "docx", "is_whole": is_whole, "page_size": 100}
            try:
                payload = self._run_cli(["drive", "file.comments", "list", "--as", "user", "--params", json.dumps(params, ensure_ascii=False)])
            except TrackerError:
                continue
            data = payload.get("data") or {}
            for item in data.get("items") or []:
                if not isinstance(item, dict):
                    continue
                bodies: list[str] = []
                reply_list = item.get("reply_list") or {}
                for reply in reply_list.get("replies") or []:
                    if isinstance(reply, dict):
                        content = reply.get("content") or reply.get("text") or reply.get("content_text")
                        if content:
                            bodies.append(_stringify_lark_value(content))
                body = "\n".join(bodies) or _stringify_lark_value(item.get("quote"))
                comments.append(
                    {
                        "id": str(item.get("comment_id") or ""),
                        "body": body,
                        "created_at": _parse_lark_time(item.get("create_time")),
                        "user_name": str(item.get("user_id") or ""),
                    }
                )
        return comments

    def _fetch_template_markdown(self) -> str:
        url = self.config.tracker.doc_template_url
        if not url:
            return ""
        payload = self._run_cli(["docs", "+fetch", "--as", "user", "--doc", url, "--format", "json", "--limit", "20000"])
        return str(((payload.get("data") or {}).get("markdown")) or "")

    def _ensure_planning_doc_initialized(self, issue: Issue, doc_url: str) -> None:
        try:
            markdown, _ = self._fetch_doc_context(doc_url)
            if f"任务 ID：{issue.identifier}" in markdown and "任务信息" in markdown:
                return
            if markdown.strip():
                if "### 1.1 任务摘要（调度器填写）" in markdown:
                    self._run_cli(
                        [
                            "docs",
                            "+update",
                            "--as",
                            "user",
                            "--doc",
                            doc_url,
                            "--mode",
                            "replace_range",
                            "--selection-by-title",
                            "### 1.1 任务摘要（调度器填写）",
                            "--markdown",
                            self._render_task_summary_section(issue),
                        ]
                    )
                else:
                    insert_anchor = self._planning_doc_header_anchor(markdown)
                    if not insert_anchor:
                        return
                    self._run_cli(
                        [
                            "docs",
                            "+update",
                            "--as",
                            "user",
                            "--doc",
                            doc_url,
                            "--mode",
                            "insert_before",
                            "--selection-by-title",
                            insert_anchor,
                            "--markdown",
                            self._render_ai_doc_header(issue),
                        ]
                    )
                return
            template = self._fetch_template_markdown()
            if not template:
                return
            reference = self._extract_field_from_description(issue.description, "参考资料链接")
            content = self._render_ai_doc_template(template, issue, reference)
            self._run_cli(["docs", "+update", "--as", "user", "--doc", doc_url, "--mode", "overwrite", "--markdown", content])
        except TrackerError:
            return

    @staticmethod
    def _render_ai_doc_header(issue: Issue) -> str:
        return f"## 任务信息（调度器填写）\n\n- 任务 ID：{issue.identifier}\n- 标题：{issue.title}\n\n"

    @staticmethod
    def _render_task_summary_section(issue: Issue) -> str:
        return (
            "### 1.1 任务摘要（调度器填写）\n\n"
            "| 字段 | 内容 |\n"
            "| --- | --- |\n"
            f"| 任务 ID | {issue.identifier} |\n"
            f"| 任务标题 | {issue.title} |\n"
            f"| 当前状态 | {issue.state} |\n"
        )

    @staticmethod
    def _planning_doc_header_anchor(markdown: str) -> str | None:
        for title in (
            "## 1. 背景信息",
            "### 1.1 任务摘要（调度器填写）",
            "## 任务概览（用户填写）",
        ):
            if title in markdown:
                return title
        return None

    @staticmethod
    def _render_ai_doc_template(template: str, issue: Issue, reference: str) -> str:
        task_description = LarkBaseClient._extract_field_from_description(issue.description, "任务说明") or issue.description or ""
        body = template.replace("后台从任务主表「任务说明」复制到这里。用户可以补充或修正。", task_description)
        body = body.replace("参考资料链接：待填充", f"参考资料链接：{reference or '无'}")
        body = body.replace("agent/{任务ID}", f"agent/{issue.identifier}")
        body = body.replace("任务 ID：待填充", f"任务 ID：{issue.identifier}")
        body = body.replace("任务标题：待填充", f"任务标题：{issue.title}")
        body = body.replace("当前状态：待填充", f"当前状态：{issue.state}")
        if "### 1.1 任务摘要（调度器填写）" in body:
            return body
        return LarkBaseClient._render_ai_doc_header(issue) + body

    @staticmethod
    def _extract_field_from_description(description: str | None, field_name: str) -> str:
        if not description:
            return ""
        prefix = f"- {field_name}："
        for line in description.splitlines():
            if line.startswith(prefix):
                return line.removeprefix(prefix).strip()
        return ""

    @staticmethod
    def _docx_token(doc_url: str) -> str | None:
        doc_url = LarkBaseClient._normalize_doc_url(doc_url)
        match = _DOCX_TOKEN_RE.search(doc_url)
        if match:
            return match.group(1)
        parsed = urlparse(doc_url)
        if parsed.path.startswith("/docx/"):
            return parsed.path.removeprefix("/docx/").split("/", 1)[0]
        return None

    @staticmethod
    def _normalize_doc_url(value: str | None) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        match = _MARKDOWN_LINK_RE.search(raw)
        if match:
            return match.group(1)
        return raw

    def _update_record(self, record_id: str, values: dict[str, Any]) -> None:
        self._run_cli([
            "base",
            "+record-upsert",
            "--as",
            "user",
            "--base-token",
            self._base_token(),
            "--table-id",
            self._task_table_id(),
            "--record-id",
            record_id,
            "--json",
            json.dumps(values, ensure_ascii=False),
        ])

    @staticmethod
    def _normalize_failure_reason(value: str | None) -> str:
        if not value:
            return ""
        return " ".join(str(value).split())[:1000]

    def _run_cli(self, args: list[str]) -> dict[str, Any]:
        command = [self.config.tracker.cli_command, *args]
        try:
            completed = subprocess.run(command, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
        except Exception as exc:
            raise TrackerError(f"lark-cli request failed: {exc}", code="lark_cli_request") from exc
        output = (completed.stdout or "").strip()
        if completed.returncode != 0:
            detail = (completed.stderr or output or f"exit {completed.returncode}").strip()
            raise TrackerError(f"lark-cli failed: {detail[:1000]}", code="lark_cli_status")
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:
            raise TrackerError(f"lark-cli returned non-json output: {output[:500]}", code="lark_cli_payload") from exc
        if payload.get("ok") is False:
            raise TrackerError(f"lark-cli error: {payload.get('error')}", code="lark_cli_error")
        return payload

    def _base_token(self) -> str:
        if not self.config.tracker.base_token:
            raise TrackerError("missing Base token", code="missing_base_token")
        return self.config.tracker.base_token

    def _task_table_id(self) -> str:
        if not self.config.tracker.task_table_id:
            raise TrackerError("missing task table id", code="missing_task_table_id")
        return self.config.tracker.task_table_id


class LinearClient:
    def __init__(self, config: ServiceConfig):
        self.config = config

    def fetch_candidate_issues(self) -> list[Issue]:
        return self.fetch_dispatch_issues()

    def fetch_dispatch_issues(self) -> list[Issue]:
        return self._fetch_issues_by_states(self.config.tracker.dispatch_states)

    def fetch_recovery_issues(self) -> list[Issue]:
        return self._fetch_issues_by_states(self.config.tracker.running_states)

    def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
        if not state_names:
            return []
        return self._fetch_issues_by_states(state_names)

    def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        if not issue_ids:
            return []
        query = """
        query SymphonyIssueStates($ids: [ID!]) {
          issues(filter: { id: { in: $ids } }) {
            nodes {
              id identifier title description priority branchName url createdAt updatedAt
              state { name }
              labels { nodes { name } }
              inverseRelations { nodes { type issue { id identifier state { name } } } }
              comments(first: 10) { nodes { id body createdAt user { name } } }
            }
          }
        }
        """
        data = self._graphql(query, {"ids": issue_ids})
        nodes = (((data.get("data") or {}).get("issues") or {}).get("nodes") or [])
        return [normalize_issue(node) for node in nodes if isinstance(node, dict)]

    def _fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
        query = """
        query SymphonyIssues($projectSlug: String!, $stateNames: [String!], $after: String) {
          issues(
            first: 50,
            after: $after,
            filter: {
              project: { slugId: { eq: $projectSlug } }
              state: { name: { in: $stateNames } }
            }
          ) {
            pageInfo { hasNextPage endCursor }
            nodes {
              id identifier title description priority branchName url createdAt updatedAt
              state { name }
              labels { nodes { name } }
              inverseRelations { nodes { type issue { id identifier state { name } } } }
              comments(first: 10) { nodes { id body createdAt user { name } } }
            }
          }
        }
        """
        issues: list[Issue] = []
        after = None
        while True:
            data = self._graphql(query, {
                "projectSlug": self.config.tracker.project_slug,
                "stateNames": state_names,
                "after": after,
            })
            connection = ((data.get("data") or {}).get("issues") or {})
            nodes = connection.get("nodes")
            page_info = connection.get("pageInfo") or {}
            if not isinstance(nodes, list):
                raise TrackerError("Linear payload missing issues.nodes", code="linear_unknown_payload")
            issues.extend(normalize_issue(node) for node in nodes if isinstance(node, dict))
            if not page_info.get("hasNextPage"):
                return issues
            after = page_info.get("endCursor")
            if not after:
                raise TrackerError("Linear pagination missing endCursor", code="linear_missing_end_cursor")

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if not self.config.tracker.api_key:
            raise TrackerError("missing Linear API key", code="missing_tracker_api_key")
        body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        request = urllib.request.Request(
            self.config.tracker.endpoint,
            data=body,
            headers={
                "Authorization": self.config.tracker.api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = _read_http_error_body(exc)
            raise TrackerError(f"Linear HTTP status {exc.code}: {detail}", code="linear_api_status") from exc
        except Exception as exc:
            raise TrackerError(f"Linear request failed: {exc}", code="linear_api_request") from exc
        if payload.get("errors"):
            raise TrackerError("Linear GraphQL errors", code="linear_graphql_errors")
        return payload

    def move_issue_to_state(self, issue_id: str, state_name: str) -> bool:
        state_id = self._find_issue_team_state_id(issue_id, state_name)
        if not state_id:
            self.add_comment(issue_id, f"Symphony could not move this issue to `{state_name}` because that state does not exist.")
            return False
        mutation = """
        mutation SymphonyMoveIssueState($id: String!, $stateId: String!) {
          issueUpdate(id: $id, input: { stateId: $stateId }) {
            success
            issue { id identifier state { name } }
          }
        }
        """
        data = self._graphql(mutation, {"id": issue_id, "stateId": state_id})
        return bool((((data.get("data") or {}).get("issueUpdate") or {}).get("success")))

    def add_comment(self, issue_id: str, body: str) -> bool:
        mutation = """
        mutation SymphonyAddComment($input: CommentCreateInput!) {
          commentCreate(input: $input) {
            success
            comment { id url }
          }
        }
        """
        data = self._graphql(mutation, {"input": {"issueId": issue_id, "body": body}})
        return bool((((data.get("data") or {}).get("commentCreate") or {}).get("success")))

    def _find_issue_team_state_id(self, issue_id: str, state_name: str) -> str | None:
        query = """
        query SymphonyIssueWorkflowStates($issueId: String!) {
          issue(id: $issueId) {
            id
            team {
              states {
                nodes { id name type }
              }
            }
          }
        }
        """
        data = self._graphql(query, {"issueId": issue_id})
        states = (((((data.get("data") or {}).get("issue") or {}).get("team") or {}).get("states") or {}).get("nodes") or [])
        for state in states:
            if isinstance(state, dict) and str(state.get("name") or "").lower() == state_name.lower():
                return str(state.get("id") or "")
        return None


def execute_linear_graphql(config: ServiceConfig, arguments: Any) -> dict[str, Any]:
    if config.tracker.kind != "linear" or not config.tracker.api_key:
        return {"success": False, "error": {"code": "missing_auth", "message": "Linear auth is not configured"}}
    if isinstance(arguments, str):
        query = arguments
        variables: dict[str, Any] = {}
    elif isinstance(arguments, dict):
        query = arguments.get("query")
        variables = arguments.get("variables") or {}
    else:
        return {"success": False, "error": {"code": "invalid_input", "message": "arguments must be an object or query string"}}
    if not isinstance(query, str) or not query.strip():
        return {"success": False, "error": {"code": "invalid_input", "message": "query must be a non-empty string"}}
    if not isinstance(variables, dict):
        return {"success": False, "error": {"code": "invalid_input", "message": "variables must be an object"}}
    if len(_GRAPHQL_OPERATION_RE.findall(_strip_graphql_comments(query))) != 1:
        return {"success": False, "error": {"code": "invalid_input", "message": "query must contain exactly one GraphQL operation"}}
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = urllib.request.Request(
        config.tracker.endpoint,
        data=body,
        headers={"Authorization": config.tracker.api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"success": False, "error": {"code": "linear_api_status", "message": f"HTTP {exc.code}: {_read_http_error_body(exc)}"}}
    except Exception as exc:
        return {"success": False, "error": {"code": "linear_api_request", "message": str(exc)}}
    return {"success": not bool(payload.get("errors")), "response": payload}


def _strip_graphql_comments(query: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in query.splitlines())


def _read_http_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", errors="replace").strip()
    except Exception:
        body = ""
    if not body:
        return "empty response body"
    return body[:1000]
