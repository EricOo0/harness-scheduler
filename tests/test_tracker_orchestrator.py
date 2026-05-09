from __future__ import annotations

import asyncio
import logging
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from symphony.agent import AgentRunner, JsonRpcAppServerSession
from symphony.config import build_config
from symphony.models import AgentEvent, BlockerRef, Issue, RunResult, RunningEntry
from symphony.orchestrator import Orchestrator
from symphony.tracker import LarkBaseClient, LinearClient, execute_linear_graphql, normalize_issue
from symphony.workflow import load_workflow
from symphony.workspace import WorkspaceManager


def make_config(tmp: str):
    path = Path(tmp) / "WORKFLOW.md"
    path.write_text(
        """---
tracker:
  kind: linear
  api_key: token
  project_slug: P
polling:
  interval_ms: 100
workspace:
  root: ./ws
agent:
  max_concurrent_agents: 2
  max_retry_backoff_ms: 1000
codex:
  command: fake
  stall_timeout_ms: 1
---
Work on {{ issue.identifier }}
""",
        encoding="utf-8",
    )
    workflow = load_workflow(path)
    return workflow, build_config(workflow)


def make_split_config(tmp: str):
    path = Path(tmp) / "WORKFLOW.md"
    path.write_text(
        """---
tracker:
  kind: linear
  api_key: token
  project_slug: P
  dispatch_states:
    - Backlog
    - Todo
  running_states:
    - In Progress
  handoff_states:
    - Review
  terminal_states:
    - Done
polling:
  interval_ms: 100
workspace:
  root: ./ws
agent:
  max_concurrent_agents: 2
  max_retry_backoff_ms: 1000
  stale_running_recovery_ms: 1
codex:
  command: fake
  stall_timeout_ms: 100000
---
Work on {{ issue.identifier }}
""",
        encoding="utf-8",
    )
    workflow = load_workflow(path)
    return workflow, build_config(workflow)


def make_lark_config(tmp: str):
    path = Path(tmp) / "WORKFLOW.md"
    path.write_text(
        """---
tracker:
  kind: lark_base
  base_token: base_token
  task_table_id: tblTask
  doc_template_url: https://lark.example.com/docx/templateToken
codex:
  command: fake
---
Work on {{ issue.identifier }}
""",
        encoding="utf-8",
    )
    workflow = load_workflow(path)
    return workflow, build_config(workflow)


class FakeTracker:
    def __init__(self, candidates=None, states=None):
        self.candidates = candidates or []
        self.states = states or {}
        self.moves = []
        self.comments = []
        self.last_move_kwargs = {}

    def fetch_candidate_issues(self):
        return list(self.candidates)

    def fetch_dispatch_issues(self):
        return list(self.candidates)

    def fetch_recovery_issues(self):
        return [issue for issue in self.states.values() if issue.state == "In Progress"]

    def fetch_issues_by_states(self, state_names):
        return []

    def fetch_issue_states_by_ids(self, issue_ids):
        return [self.states[i] for i in issue_ids if i in self.states]

    def move_issue_to_state(self, issue_id, state_name, **kwargs):
        self.moves.append((issue_id, state_name))
        self.last_move_kwargs = kwargs
        if issue_id in self.states:
            self.states[issue_id].state = state_name
        return True

    def add_comment(self, issue_id, body):
        self.comments.append((issue_id, body))
        return True


class FakeRunner(AgentRunner):
    def __init__(self, result=None):
        self.result = result or RunResult(ok=True)
        self.calls = []

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        await asyncio.sleep(0)
        return self.result


class SuggestedStateTracker(FakeTracker):
    def __init__(self, suggested_state=None, suggested_error=None, **kwargs):
        super().__init__(**kwargs)
        self.suggested_state = suggested_state
        self.suggested_error = suggested_error

    def completion_success_state_for(self, issue, source_state, default_state):
        return self.suggested_state or default_state, self.suggested_error


class FakeLarkBaseClient(LarkBaseClient):
    def __init__(self, config):
        super().__init__(config)
        self.commands = []

    def _run_cli(self, args):
        self.commands.append(args)
        if args[:2] == ["base", "+record-get"]:
            return {
                "ok": True,
                "data": {
                    "record": {
                        "AI 交互文档链接": "https://lark.example.com/docx/docToken",
                        "标题": "测试任务",
                        "状态": ["待方案设计"],
                    }
                },
            }
        if args[:2] == ["docs", "+fetch"]:
            doc = args[args.index("--doc") + 1]
            if "templateToken" in doc:
                return {"ok": True, "data": {"markdown": "## 原始需求\n\n后台从任务主表「任务说明」复制到这里。用户可以补充或修正。\n\n参考资料链接：待填充"}}
            return {
                "ok": True,
                "data": {
                    "markdown": "\n".join(
                        [
                            "## 4. 交互记录",
                            "",
                            "### 4.1 状态流转记录（调度器填写）",
                            "",
                            "| 时间 | 触发方 | 来源状态 | 目标状态 | 原因 |",
                            "| --- | --- | --- | --- | --- |",
                            "",
                            "### 4.2 轮次记录（Agent 填写）",
                            "",
                            "无",
                        ]
                    )
                },
            }
        return {"ok": True, "data": {}}


class TrackerOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    def test_normalize_issue_labels_blockers_and_priority(self):
        issue = normalize_issue(
            {
                "id": "id1",
                "identifier": "ABC-1",
                "title": "Title",
                "description": "Desc",
                "priority": "bad",
                "state": {"name": "Todo"},
                "labels": {"nodes": [{"name": "Bug"}]},
                "comments": {"nodes": [{"id": "c1", "body": "previous work", "createdAt": "2026-01-01T01:00:00Z", "user": {"name": "Alice"}}]},
                "inverseRelations": {
                    "nodes": [
                        {"type": "blocks", "issue": {"id": "b", "identifier": "ABC-0", "state": {"name": "In Progress"}}},
                        {"type": "relates", "issue": {"id": "x"}},
                    ]
                },
                "createdAt": "2026-01-01T00:00:00Z",
            }
        )
        self.assertEqual(issue.labels, ["bug"])
        self.assertIsNone(issue.priority)
        self.assertEqual(issue.blocked_by[0].identifier, "ABC-0")
        self.assertEqual(issue.comments[0].body, "previous work")
        self.assertEqual(issue.comments[0].user_name, "Alice")
        self.assertEqual(issue.created_at, datetime(2026, 1, 1, tzinfo=timezone.utc))

    def test_linear_pagination_preserves_order_and_uses_project_slug_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, config = make_config(tmp)
            pages = [
                {
                    "data": {
                        "issues": {
                            "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                            "nodes": [
                                {"id": "1", "identifier": "P-1", "title": "A", "state": {"name": "Todo"}},
                            ],
                        }
                    }
                },
                {
                    "data": {
                        "issues": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                {"id": "2", "identifier": "P-2", "title": "B", "state": {"name": "Todo"}},
                            ],
                        }
                    }
                },
            ]
            calls = []

            def fake_graphql(query, variables):
                calls.append((query, variables))
                return pages.pop(0)

            client = LinearClient(config)
            with patch.object(client, "_graphql", side_effect=fake_graphql):
                issues = client.fetch_candidate_issues()

            self.assertEqual([issue.identifier for issue in issues], ["P-1", "P-2"])
            self.assertIn("slugId", calls[0][0])
            self.assertIn("stateNames", calls[0][0])
            self.assertNotIn("IssueFilter", calls[0][0])
            self.assertNotIn("and: [$filter]", calls[0][0])
            self.assertEqual(calls[0][1]["projectSlug"], "P")
            self.assertEqual(calls[0][1]["stateNames"], ["Todo", "In Progress"])
            self.assertEqual(calls[1][1]["after"], "cursor-1")

    def test_split_state_config_sets_dispatch_running_and_handoff_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, config = make_split_config(tmp)
            self.assertEqual(config.tracker.dispatch_states, ["Backlog", "Todo"])
            self.assertEqual(config.tracker.running_states, ["In Progress"])
            self.assertEqual(config.tracker.handoff_states, ["Review"])
            self.assertEqual(config.tracker.active_states, ["Backlog", "Todo", "In Progress"])
            self.assertIn("review", config.terminal_state_set)

    def test_linear_graphql_tool_rejects_invalid_input_before_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, config = make_config(tmp)
            result = execute_linear_graphql(config, {"query": "query A { viewer { id } } query B { viewer { id } }"})
            self.assertFalse(result["success"])
            self.assertEqual(result["error"]["code"], "invalid_input")

    def test_turn_failure_error_prefers_turn_error_then_stderr(self):
        self.assertEqual(
            JsonRpcAppServerSession._turn_failure_error({"turn": {"status": "failed", "error": "mcp auth required"}}, "stderr detail"),
            "failed: mcp auth required",
        )
        self.assertEqual(
            JsonRpcAppServerSession._turn_failure_error({"turn": {"status": "failed"}}, "stderr detail"),
            "failed: stderr detail",
        )

    def test_lark_failure_comment_updates_base_and_ai_doc_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, config = make_lark_config(tmp)
            client = FakeLarkBaseClient(config)

            client.add_comment("rec1", "Symphony system: agent run did not complete successfully. State target: 已阻塞. Error: failed")

            update_calls = [command for command in client.commands if command[:2] == ["base", "+record-upsert"]]
            doc_calls = [command for command in client.commands if command[:2] == ["docs", "+update"]]
            self.assertEqual(len(update_calls), 1)
            self.assertEqual(len(doc_calls), 1)
            markdown = doc_calls[0][doc_calls[0].index("--markdown") + 1]
            self.assertIn("--mode", doc_calls[0])
            self.assertEqual(doc_calls[0][doc_calls[0].index("--mode") + 1], "insert_after")
            self.assertIn("--selection-by-title", doc_calls[0])
            self.assertEqual(doc_calls[0][doc_calls[0].index("--selection-by-title") + 1], "### 4.2 轮次记录（Agent 填写）")
            self.assertIn("系统失败记录", markdown)
            self.assertIn("失败原因", markdown)
            self.assertNotIn("## 交接记录详情", markdown)

    def test_lark_state_transition_appends_ai_doc_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, config = make_lark_config(tmp)
            client = FakeLarkBaseClient(config)

            client.move_issue_to_state("rec1", "规划中")

            update_calls = [command for command in client.commands if command[:2] == ["base", "+record-upsert"]]
            doc_calls = [command for command in client.commands if command[:2] == ["docs", "+update"]]
            self.assertEqual(len(update_calls), 1)
            self.assertEqual(len(doc_calls), 1)
            markdown = doc_calls[0][doc_calls[0].index("--markdown") + 1]
            self.assertIn("--mode", doc_calls[0])
            self.assertEqual(doc_calls[0][doc_calls[0].index("--mode") + 1], "replace_range")
            self.assertIn("--selection-by-title", doc_calls[0])
            self.assertEqual(doc_calls[0][doc_calls[0].index("--selection-by-title") + 1], "### 4.1 状态流转记录（调度器填写）")
            self.assertIn("| 时间 | 触发方 | 来源状态 | 目标状态 | 原因 |", markdown)
            self.assertIn("| 待方案设计 | 规划中 |", markdown)
            self.assertNotIn("#### Symphony 状态流转记录", markdown)

    def test_lark_record_context_does_not_prefetch_ai_doc_into_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, config = make_lark_config(tmp)
            client = FakeLarkBaseClient(config)

            issue = client._record_to_issue(
                "rec1",
                {
                    "标题": "测试任务",
                    "状态": ["待方案设计"],
                    "任务说明": "创建 test.md",
                    "AI 交互文档链接": "https://lark.example.com/docx/docToken",
                    "Agent 交接说明": "已有交接",
                },
            )

            self.assertIsNotNone(issue)
            assert issue is not None
            self.assertEqual(issue.url, "https://lark.example.com/docx/docToken")
            self.assertEqual(client.commands, [])
            self.assertIn("## Base 任务字段", issue.description or "")
            self.assertIn("AI 交互文档链接", issue.description or "")
            self.assertIn("自行读取该文档正文", issue.description or "")
            self.assertNotIn("## AI 交互文档正文", issue.description or "")
            self.assertNotIn("## AI 交互文档评论", issue.description or "")

    def test_lark_completion_success_state_uses_valid_suggestion_without_clearing(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, config = make_lark_config(tmp)
            client = FakeLarkBaseClient(config)

            def fake_run_cli(args):
                client.commands.append(args)
                if args[:2] == ["base", "+record-get"]:
                    return {"ok": True, "data": {"record": {"Agent 建议下一状态": "需求确认"}}}
                return {"ok": True, "data": {}}

            client._run_cli = fake_run_cli

            state, error = client.completion_success_state_for(
                Issue(id="rec1", identifier="rec1", title="T", state="方案设计中"),
                "待方案设计",
                "方案评审",
            )

            self.assertEqual(state, "需求确认")
            self.assertIsNone(error)
            update_calls = [command for command in client.commands if command[:2] == ["base", "+record-upsert"]]
            self.assertEqual(update_calls, [])

    def test_lark_success_move_clears_suggestion_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, config = make_lark_config(tmp)
            client = FakeLarkBaseClient(config)

            client.move_issue_to_state("rec1", "需求确认", clear_suggested_next_state=True)

            update_calls = [command for command in client.commands if command[:2] == ["base", "+record-upsert"]]
            self.assertEqual(len(update_calls), 1)
            payload = update_calls[0][update_calls[0].index("--json") + 1]
            self.assertIn('"状态": "需求确认"', payload)
            self.assertIn('"Agent 建议下一状态": ""', payload)

    def test_lark_failure_move_writes_specific_blocker_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, config = make_lark_config(tmp)
            client = FakeLarkBaseClient(config)

            client.move_issue_to_state("rec1", "已阻塞", failure_reason="具体失败原因\n包含换行")

            update_calls = [command for command in client.commands if command[:2] == ["base", "+record-upsert"]]
            self.assertEqual(len(update_calls), 1)
            payload = update_calls[0][update_calls[0].index("--json") + 1]
            self.assertIn('"状态": "已阻塞"', payload)
            self.assertIn('"阻塞原因": "具体失败原因 包含换行"', payload)
            self.assertIn('"最近错误": "具体失败原因 包含换行"', payload)

    def test_lark_completion_success_state_rejects_invalid_suggestion(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, config = make_lark_config(tmp)
            client = FakeLarkBaseClient(config)

            def fake_run_cli(args):
                client.commands.append(args)
                if args[:2] == ["base", "+record-get"]:
                    return {"ok": True, "data": {"record": {"Agent 建议下一状态": "代码评审"}}}
                return {"ok": True, "data": {}}

            client._run_cli = fake_run_cli

            state, error = client.completion_success_state_for(
                Issue(id="rec1", identifier="rec1", title="T", state="方案设计中"),
                "待方案设计",
                "方案评审",
            )

            self.assertIsNone(state)
            self.assertIsNotNone(error)
            self.assertIn("非法的 Agent 建议下一状态", error or "")
            update_calls = [command for command in client.commands if command[:2] == ["base", "+record-upsert"]]
            self.assertEqual(update_calls, [])

    def test_lark_existing_planning_doc_is_initialized_with_lark_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, config = make_lark_config(tmp)
            client = FakeLarkBaseClient(config)

            def fake_run_cli(args):
                client.commands.append(args)
                if args[:2] == ["docs", "+fetch"]:
                    doc = args[args.index("--doc") + 1]
                    if "templateToken" in doc:
                        return {"ok": True, "data": {"markdown": "## 原始需求\n\n后台从任务主表「任务说明」复制到这里。用户可以补充或修正。\n\n参考资料链接：待填充"}}
                    return {"ok": True, "data": {"markdown": ""}}
                return {"ok": True, "data": {}}

            client._run_cli = fake_run_cli
            description = "\n".join(
                [
                    "## Base 任务字段",
                    "- 任务说明：创建 test.md",
                    "- 参考资料链接：无",
                    "- AI 交互文档链接：[https://lark.example.com/docx/docToken](https://lark.example.com/docx/docToken)",
                ]
            )
            issue = Issue(id="rec1", identifier="rec1", title="测试任务", state="待规划", description=description)

            prepared = client.prepare_issue_for_dispatch(issue)

            doc_calls = [command for command in client.commands if command[:2] == ["docs", "+update"]]
            self.assertEqual(prepared.url, "https://lark.example.com/docx/docToken")
            self.assertEqual(len(doc_calls), 1)
            self.assertEqual(doc_calls[0][doc_calls[0].index("--mode") + 1], "overwrite")
            markdown = doc_calls[0][doc_calls[0].index("--markdown") + 1]
            self.assertIn("任务 ID：rec1", markdown)
            self.assertIn("创建 test.md", markdown)
            self.assertNotIn("## Base 任务字段", markdown)

    def test_lark_existing_template_doc_inserts_task_header_without_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, config = make_lark_config(tmp)
            client = FakeLarkBaseClient(config)

            def fake_run_cli(args):
                client.commands.append(args)
                if args[:2] == ["docs", "+fetch"]:
                    doc = args[args.index("--doc") + 1]
                    if "templateToken" in doc:
                        return {"ok": True, "data": {"markdown": "## 任务概览（用户填写）\n\n后台从任务主表「任务说明」复制到这里。用户可以补充或修正。"}}
                    return {"ok": True, "data": {"markdown": "## 任务概览（用户填写）\n\n已有内容"}}
                return {"ok": True, "data": {}}

            client._run_cli = fake_run_cli
            description = "\n".join(
                [
                    "## Base 任务字段",
                    "- 任务说明：创建 test.md",
                    "- 参考资料链接：无",
                    "- AI 交互文档链接：[https://lark.example.com/docx/docToken](https://lark.example.com/docx/docToken)",
                ]
            )
            issue = Issue(id="rec1", identifier="rec1", title="测试任务", state="待规划", description=description)

            client.prepare_issue_for_dispatch(issue)

            doc_calls = [command for command in client.commands if command[:2] == ["docs", "+update"]]
            self.assertEqual(len(doc_calls), 1)
            self.assertEqual(doc_calls[0][doc_calls[0].index("--mode") + 1], "insert_before")
            self.assertEqual(doc_calls[0][doc_calls[0].index("--selection-by-title") + 1], "## 任务概览（用户填写）")
            markdown = doc_calls[0][doc_calls[0].index("--markdown") + 1]
            self.assertIn("任务 ID：rec1", markdown)

    def test_lark_existing_new_template_doc_inserts_task_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, config = make_lark_config(tmp)
            client = FakeLarkBaseClient(config)

            def fake_run_cli(args):
                client.commands.append(args)
                if args[:2] == ["docs", "+fetch"]:
                    return {"ok": True, "data": {"markdown": "## 1. 背景信息\n\n### 1.1 任务摘要（调度器填写）\n\n已有内容"}}
                return {"ok": True, "data": {}}

            client._run_cli = fake_run_cli
            description = "\n".join(
                [
                    "## Base 任务字段",
                    "- 任务说明：创建 test.md",
                    "- 参考资料链接：无",
                    "- AI 交互文档链接：[https://lark.example.com/docx/docToken](https://lark.example.com/docx/docToken)",
                ]
            )
            issue = Issue(id="rec1", identifier="rec1", title="测试任务", state="待规划", description=description)

            client.prepare_issue_for_dispatch(issue)

            doc_calls = [command for command in client.commands if command[:2] == ["docs", "+update"]]
            self.assertEqual(len(doc_calls), 1)
            self.assertEqual(doc_calls[0][doc_calls[0].index("--mode") + 1], "replace_range")
            self.assertEqual(doc_calls[0][doc_calls[0].index("--selection-by-title") + 1], "### 1.1 任务摘要（调度器填写）")
            markdown = doc_calls[0][doc_calls[0].index("--markdown") + 1]
            self.assertIn("| 任务 ID | rec1 |", markdown)
            self.assertIn("### 1.1 任务摘要", markdown)

    async def test_dispatch_sort_and_todo_blocker_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow, config = make_config(tmp)
            tracker = FakeTracker()
            orchestrator = Orchestrator(
                workflow,
                config,
                tracker=tracker,
                runner_factory=lambda c, w: FakeRunner(),
                logger=logging.getLogger("test"),
            )
            blocked = Issue(
                id="1",
                identifier="P-1",
                title="Blocked",
                state="Todo",
                priority=1,
                blocked_by=[BlockerRef(id="b", identifier="B-1", state="In Progress")],
            )
            unknown_blocker = Issue(
                id="3",
                identifier="P-3",
                title="Unknown Blocker",
                state="Todo",
                priority=1,
                blocked_by=[BlockerRef(id="b", identifier="B-1", state=None)],
            )
            unblocked = Issue(
                id="2",
                identifier="P-2",
                title="Open",
                state="Todo",
                priority=2,
                blocked_by=[BlockerRef(id="b", identifier="B-1", state="Done")],
            )
            self.assertFalse(orchestrator.should_dispatch(blocked))
            self.assertFalse(orchestrator.should_dispatch(unknown_blocker))
            self.assertTrue(orchestrator.should_dispatch(unblocked))

    async def test_split_state_dispatch_does_not_dispatch_running_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow, config = make_split_config(tmp)
            orchestrator = Orchestrator(
                workflow,
                config,
                tracker=FakeTracker(),
                runner_factory=lambda c, w: FakeRunner(),
                logger=logging.getLogger("test"),
            )
            issue = Issue(id="1", identifier="P-1", title="T", state="In Progress")
            self.assertEqual(orchestrator.dispatch_block_reason(issue), "not_dispatch_state")

    async def test_planning_stage_blocks_workspace_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow, config = make_lark_config(tmp)
            orchestrator = Orchestrator(
                workflow,
                config,
                tracker=FakeTracker(),
                runner_factory=lambda c, w: FakeRunner(),
                logger=logging.getLogger("test"),
            )
            workspace = WorkspaceManager(config).create_for_issue("rec1")
            Path(workspace.path, ".symphony_run.json").write_text("{}", encoding="utf-8")
            Path(workspace.path, "log").mkdir(exist_ok=True)
            Path(workspace.path, "log", "app.log").write_text("", encoding="utf-8")
            Path(workspace.path, "test.md").write_text("bad", encoding="utf-8")
            entry = RunningEntry(
                issue=Issue(id="rec1", identifier="rec1", title="T", state="规划中"),
                worker=object(),
                identifier="rec1",
                started_at=datetime.now(timezone.utc),
                source_state="待规划",
                workspace_path=workspace.path,
            )

            reason = orchestrator._workspace_artifact_block_reason(entry)

            self.assertIsNotNone(reason)
            self.assertIn("test.md", reason or "")

    async def test_recover_stale_running_issue_requires_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow, config = make_split_config(tmp)
            issue = Issue(id="1", identifier="P-1", title="T", state="In Progress")
            tracker = FakeTracker(states={"1": issue})
            orchestrator = Orchestrator(
                workflow,
                config,
                tracker=tracker,
                runner_factory=lambda c, w: FakeRunner(RunResult(ok=True)),
                logger=logging.getLogger("test"),
            )
            self.assertEqual(orchestrator.recovery_block_reason(issue), "no_existing_workspace")
            workspace = WorkspaceManager(config).create_for_issue("P-1")
            self.assertEqual(orchestrator.recovery_block_reason(issue), "missing_symphony_run_marker")
            marker = Path(workspace.path) / ".symphony_run.json"
            marker.write_text("{}", encoding="utf-8")
            await asyncio.sleep(0.01)
            self.assertIsNone(orchestrator.recovery_block_reason(issue))

    async def test_recovery_scan_dispatches_stale_running_issue(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow, config = make_split_config(tmp)
            issue = Issue(id="1", identifier="P-1", title="T", state="In Progress")
            tracker = FakeTracker(states={"1": issue})
            orchestrator = Orchestrator(
                workflow,
                config,
                tracker=tracker,
                runner_factory=lambda c, w: FakeRunner(RunResult(ok=True)),
                logger=logging.getLogger("test"),
            )
            workspace = WorkspaceManager(config).create_for_issue("P-1")
            (Path(workspace.path) / ".symphony_run.json").write_text("{}", encoding="utf-8")
            await asyncio.sleep(0.01)
            recovered = await orchestrator.recover_stale_running_issues()
            self.assertEqual(recovered, 1)
            self.assertIn("1", orchestrator.state.running)

    async def test_retry_releases_non_stale_running_state_instead_of_looping(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow, config = make_split_config(tmp)
            config.agent.stale_running_recovery_ms = 999999
            issue = Issue(id="1", identifier="P-1", title="T", state="In Progress")
            tracker = FakeTracker(states={"1": issue})
            orchestrator = Orchestrator(
                workflow,
                config,
                tracker=tracker,
                runner_factory=lambda c, w: FakeRunner(RunResult(ok=True)),
                logger=logging.getLogger("test"),
            )
            workspace = WorkspaceManager(config).create_for_issue("P-1")
            (Path(workspace.path) / ".symphony_run.json").write_text("{}", encoding="utf-8")
            orchestrator.schedule_retry("1", "P-1", 1, error=None, continuation=True)
            await orchestrator.handle_retry("1")
            self.assertNotIn("1", orchestrator.state.retry_attempts)
            self.assertNotIn("1", orchestrator.state.claimed)
            self.assertNotIn("1", orchestrator.state.running)

    async def test_normal_exit_schedules_continuation_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow, config = make_config(tmp)
            issue = Issue(id="1", identifier="P-1", title="T", state="Todo")
            tracker = FakeTracker(candidates=[issue], states={"1": issue})
            orchestrator = Orchestrator(
                workflow,
                config,
                tracker=tracker,
                runner_factory=lambda c, w: FakeRunner(RunResult(ok=True)),
                logger=logging.getLogger("test"),
            )
            orchestrator.dispatch_issue(issue, attempt=None)
            await asyncio.sleep(0.05)
            self.assertIn(("1", "In Progress"), tracker.moves)
            self.assertIn(("1", "Review"), tracker.moves)
            self.assertTrue(any("started work" in body for _, body in tracker.comments))
            self.assertTrue(any("completed successfully" in body for _, body in tracker.comments))
            self.assertIn("1", orchestrator.state.retry_attempts)
            self.assertEqual(orchestrator.state.retry_attempts["1"].attempt, 1)

    async def test_dispatch_preserves_source_state_for_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow, config = make_split_config(tmp)
            issue = Issue(id="1", identifier="P-1", title="T", state="Todo")
            tracker = FakeTracker(candidates=[issue], states={"1": issue})
            runner = FakeRunner(RunResult(ok=True))
            orchestrator = Orchestrator(
                workflow,
                config,
                tracker=tracker,
                runner_factory=lambda c, w: runner,
                logger=logging.getLogger("test"),
            )

            orchestrator.dispatch_issue(issue, attempt=None)
            await asyncio.sleep(0.05)

            self.assertEqual(issue.state, "Review")
            self.assertEqual(issue.dispatch_state, "Todo")
            self.assertEqual(runner.calls[0]["issue"].dispatch_state, "Todo")

    async def test_success_state_can_be_overridden_by_tracker_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow, config = make_split_config(tmp)
            issue = Issue(id="1", identifier="P-1", title="T", state="Todo")
            tracker = SuggestedStateTracker(suggested_state="Backlog", candidates=[issue], states={"1": issue})
            orchestrator = Orchestrator(
                workflow,
                config,
                tracker=tracker,
                runner_factory=lambda c, w: FakeRunner(RunResult(ok=True)),
                logger=logging.getLogger("test"),
            )
            orchestrator.dispatch_issue(issue, attempt=None)
            await asyncio.sleep(0.05)
            self.assertIn(("1", "In Progress"), tracker.moves)
            self.assertIn(("1", "Backlog"), tracker.moves)
            self.assertNotIn(("1", "Review"), tracker.moves)

    async def test_invalid_tracker_success_state_decision_fails_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow, config = make_split_config(tmp)
            issue = Issue(id="1", identifier="P-1", title="T", state="Todo")
            tracker = SuggestedStateTracker(suggested_error="非法建议", candidates=[issue], states={"1": issue})
            orchestrator = Orchestrator(
                workflow,
                config,
                tracker=tracker,
                runner_factory=lambda c, w: FakeRunner(RunResult(ok=True)),
                logger=logging.getLogger("test"),
            )
            orchestrator.dispatch_issue(issue, attempt=None)
            await asyncio.sleep(0.05)
            self.assertIn(("1", "In Progress"), tracker.moves)
            self.assertIn(("1", "Todo"), tracker.moves)
            self.assertNotIn(("1", "Review"), tracker.moves)
            self.assertTrue(any("did not complete successfully" in body and "非法建议" in body for _, body in tracker.comments))

    async def test_failed_worker_moves_issue_back_to_failure_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow, config = make_split_config(tmp)
            issue = Issue(id="1", identifier="P-1", title="T", state="Todo")
            tracker = FakeTracker(candidates=[issue], states={"1": issue})
            orchestrator = Orchestrator(
                workflow,
                config,
                tracker=tracker,
                runner_factory=lambda c, w: FakeRunner(RunResult(ok=False, error="blocked")),
                logger=logging.getLogger("test"),
            )
            orchestrator.dispatch_issue(issue, attempt=None)
            await asyncio.sleep(0.05)
            self.assertIn(("1", "In Progress"), tracker.moves)
            self.assertIn(("1", "Todo"), tracker.moves)
            self.assertEqual(tracker.last_move_kwargs.get("failure_reason"), "blocked")
            self.assertTrue(any("started work" in body for _, body in tracker.comments))
            self.assertTrue(any("did not complete successfully" in body and "blocked" in body for _, body in tracker.comments))

    async def test_agent_event_extracts_nested_thread_and_turn_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow, config = make_config(tmp)
            issue = Issue(id="1", identifier="P-1", title="T", state="Todo")
            orchestrator = Orchestrator(
                workflow,
                config,
                tracker=FakeTracker(),
                runner_factory=lambda c, w: FakeRunner(),
                logger=logging.getLogger("test"),
            )
            task = asyncio.create_task(asyncio.sleep(60))
            orchestrator.state.running["1"] = RunningEntry(
                issue=issue,
                worker=task,
                identifier="P-1",
                started_at=datetime.now(timezone.utc),
            )
            orchestrator.handle_agent_event(
                "1",
                AgentEvent(
                    event="turn_started",
                    timestamp=datetime.now(timezone.utc),
                    payload={"threadId": "thread-1", "turn": {"id": "turn-1"}},
                ),
            )
            self.assertEqual(orchestrator.state.running["1"].session_id, "thread-1-turn-1")
            task.cancel()

    async def test_terminal_reconciliation_cancels_without_retry_and_cleans_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow, config = make_config(tmp)
            active = Issue(id="1", identifier="P-1", title="T", state="In Progress")
            done = Issue(id="1", identifier="P-1", title="T", state="Done")
            tracker = FakeTracker(states={"1": done})
            orchestrator = Orchestrator(
                workflow,
                config,
                tracker=tracker,
                runner_factory=lambda c, w: FakeRunner(),
                logger=logging.getLogger("test"),
            )
            WorkspaceManager(config).create_for_issue("P-1")
            never_done = asyncio.create_task(asyncio.sleep(60))
            orchestrator.state.running["1"] = RunningEntry(
                issue=active,
                worker=never_done,
                identifier="P-1",
                started_at=datetime.now(timezone.utc),
                retry_attempt=None,
            )
            orchestrator.state.claimed.add("1")
            await orchestrator.reconcile_running_issues()
            self.assertNotIn("1", orchestrator.state.running)
            self.assertNotIn("1", orchestrator.state.retry_attempts)
            self.assertFalse((Path(config.workspace_root) / "P-1").exists())

    async def test_startup_terminal_cleanup_removes_terminal_workspaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow, config = make_config(tmp)
            terminal = Issue(id="1", identifier="P-1", title="T", state="Done")
            tracker = FakeTracker()
            tracker.fetch_issues_by_states = lambda states: [terminal]
            orchestrator = Orchestrator(
                workflow,
                config,
                tracker=tracker,
                runner_factory=lambda c, w: FakeRunner(),
                logger=logging.getLogger("test"),
            )
            WorkspaceManager(config).create_for_issue("P-1")
            await orchestrator.startup_terminal_workspace_cleanup()
            self.assertFalse((Path(config.workspace_root) / "P-1").exists())

    async def test_invalid_reload_keeps_last_good_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow, config = make_config(tmp)
            orchestrator = Orchestrator(
                workflow,
                config,
                tracker=FakeTracker(),
                runner_factory=lambda c, w: FakeRunner(),
                logger=logging.getLogger("test"),
            )
            original_interval = orchestrator.config.polling_interval_ms
            Path(config.workflow_path).write_text("---\n- invalid\n---\nbody\n", encoding="utf-8")
            orchestrator.reload_workflow_if_changed()
            self.assertEqual(orchestrator.config.polling_interval_ms, original_interval)


if __name__ == "__main__":
    unittest.main()
