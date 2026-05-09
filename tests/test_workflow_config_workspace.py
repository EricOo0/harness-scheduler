from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from symphony.config import build_config, validate_dispatch_config
from symphony.errors import ConfigError, WorkflowError, WorkspaceError
from symphony.models import Issue
from symphony.workflow import StrictTemplate, load_workflow, select_workflow_path
from symphony.workspace import WorkspaceManager, sanitize_workspace_key


class WorkflowConfigWorkspaceTests(unittest.TestCase):
    def test_workflow_front_matter_and_prompt_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "WORKFLOW.md"
            path.write_text(
                """---
tracker:
  kind: linear
  api_key: $LINEAR_API_KEY
  project_slug: PRJ
workspace:
  root: ./ws
---
Issue {{ issue.identifier }} attempt {{ attempt }}: {{ issue.title }}
""",
                encoding="utf-8",
            )
            os.environ["LINEAR_API_KEY"] = "secret"
            workflow = load_workflow(path)
            config = build_config(workflow)
            validate_dispatch_config(config)

            rendered = StrictTemplate(workflow.prompt_template).render(
                issue=Issue(id="1", identifier="ABC-1", title="Fix it", state="Todo"),
                attempt=2,
            )
            self.assertIn("ABC-1", rendered)
            self.assertIn("attempt 2", rendered)
            self.assertEqual(config.workspace_root, str((Path(tmp) / "ws").resolve()))

    def test_repository_workflow_tracks_user_input_ledger(self):
        workflow_path = Path(__file__).resolve().parents[1] / "WORKFLOW.md"
        workflow = load_workflow(workflow_path)

        rendered = StrictTemplate(workflow.prompt_template).render(
            issue=Issue(
                id="rec1",
                identifier="TASK-1",
                title="测试任务",
                state="实施中",
                dispatch_state="代码需修改",
                description="- AI 交互文档链接：https://lark.example.com/docx/docToken",
            ),
            attempt=None,
        )

        self.assertIn("调度来源状态：代码需修改", rendered)
        self.assertIn("运行状态：实施中", rendered)
        self.assertIn("### 2.5 用户输入处理台账（Agent 填写）", rendered)
        self.assertIn("comment_id", rendered)
        self.assertIn("已采纳", rendered)
        self.assertIn("评论已解决", rendered)
        self.assertIn("Codebase MR 评论", rendered)
        self.assertIn("codebase mr comment list -R <repo> -N <mr> --unresolved", rendered)
        self.assertIn("codebase mr comment reply", rendered)
        self.assertIn("codebase mr comment resolve", rendered)
        self.assertIn("repo + mr number + thread id", rendered)

    def test_missing_workflow_and_non_map_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(WorkflowError) as missing:
                load_workflow(Path(tmp) / "WORKFLOW.md")
            self.assertEqual(missing.exception.code, "missing_workflow_file")

            path = Path(tmp) / "WORKFLOW.md"
            path.write_text("---\n- nope\n---\nbody\n", encoding="utf-8")
            with self.assertRaises(WorkflowError) as non_map:
                load_workflow(path)
            self.assertEqual(non_map.exception.code, "workflow_front_matter_not_a_map")

    def test_default_path_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(select_workflow_path(cwd=tmp), (Path(tmp) / "WORKFLOW.md").resolve())

    def test_strict_template_rejects_unknown_variable(self):
        with self.assertRaises(WorkflowError):
            StrictTemplate("{{ issue.missing }}").render(
                issue=Issue(id="1", identifier="A-1", title="T", state="Todo"),
                attempt=None,
            )

    def test_strict_template_iterates_nested_issue_arrays(self):
        rendered = StrictTemplate("Labels:{{#issue.labels}} {{.}}{{/issue.labels}} Blockers:{{#issue.blocked_by}} {{identifier}}={{state}}{{/issue.blocked_by}}").render(
            issue=Issue(
                id="1",
                identifier="A-1",
                title="T",
                state="Todo",
                labels=["bug", "p0"],
                blocked_by=[],
            ),
            attempt=None,
        )
        self.assertEqual(rendered, "Labels: bug p0 Blockers:")

    def test_config_validation_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "WORKFLOW.md"
            path.write_text("---\ntracker:\n  kind: linear\n  project_slug: P\n---\nbody", encoding="utf-8")
            os.environ.pop("LINEAR_API_KEY", None)
            config = build_config(load_workflow(path))
            with self.assertRaises(ConfigError) as err:
                validate_dispatch_config(config)
            self.assertEqual(err.exception.code, "missing_tracker_api_key")

    def test_workspace_sanitization_hooks_and_root_safety(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "WORKFLOW.md"
            marker = Path(tmp) / "marker"
            path.write_text(
                f"""---
tracker:
  kind: linear
  api_key: token
  project_slug: P
workspace:
  root: ./ws
hooks:
  after_create: "printf created > {marker}"
---
body
""",
                encoding="utf-8",
            )
            config = build_config(load_workflow(path))
            manager = WorkspaceManager(config)
            self.assertEqual(sanitize_workspace_key("ABC/1:two"), "ABC_1_two")
            workspace = manager.create_for_issue("ABC/1:two")
            self.assertTrue(Path(workspace.path).is_dir())
            self.assertEqual(marker.read_text(encoding="utf-8"), "created")

            reused = manager.create_for_issue("ABC/1:two")
            self.assertFalse(reused.created_now)

            with self.assertRaises(WorkspaceError):
                manager.validate_agent_cwd(workspace.path, tmp)

    def test_workspace_existing_file_and_nonfatal_remove_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "WORKFLOW.md"
            path.write_text(
                """---
tracker:
  kind: linear
  api_key: token
  project_slug: P
workspace:
  root: ./ws
hooks:
  before_remove: "exit 1"
---
body
""",
                encoding="utf-8",
            )
            config = build_config(load_workflow(path))
            manager = WorkspaceManager(config)
            target = Path(config.workspace_root) / "P-1"
            target.parent.mkdir(parents=True)
            target.write_text("not a dir", encoding="utf-8")
            with self.assertRaises(WorkspaceError):
                manager.create_for_issue("P-1")

            target.unlink()
            workspace = manager.create_for_issue("P-1")
            manager.remove_for_issue("P-1")
            self.assertFalse(Path(workspace.path).exists())


if __name__ == "__main__":
    unittest.main()
