from __future__ import annotations

import json
import tempfile
import unittest
import urllib.request
from pathlib import Path

from symphony.agents.base import AgentRunResult
from symphony.harness import ArtifactFileManager, HarnessApp, HarnessPaths, HarnessServer


class HarnessTests(unittest.IsolatedAsyncioTestCase):
    def test_create_task_generates_artifact_and_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = HarnessApp(HarnessPaths.from_project(tmp))
            task = app.create_task({"title": "HTML 产物", "description": "实现评论", "priority": "P0"})

            self.assertEqual(task["status"], "待规划")
            artifact_path = Path(task["artifact_path"])
            self.assertTrue(artifact_path.exists())
            html = artifact_path.read_text(encoding="utf-8")
            self.assertNotIn('id="harness-artifact-runtime"', html)
            self.assertNotIn('id="harness-artifact-style"', html)
            self.assertIn('id="harness-comments"', html)
            self.assertIn('id="solution"', html)

            prompt = app.prompt_builder.build(task)
            self.assertIn(str(artifact_path), prompt)
            self.assertIn("section#background", prompt)
            self.assertIn("浏览器运行时由服务端渲染时注入", prompt)
            self.assertIn("## BaseSystemPrompt", prompt)
            self.assertIn("## StagePrompt", prompt)
            self.assertIn("信息不足时先写清楚缺口", prompt)

    def test_prompt_includes_pending_artifact_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = HarnessApp(HarnessPaths.from_project(tmp))
            task = app.create_task({"title": "评论反馈", "description": "D"})
            result = app.comments.add_comment(
                task["artifact_path"],
                {
                    "section": "background",
                    "quote": "等待需求规划阶段补充。",
                    "content": "这里需要补充用户评论里的约束",
                },
            )

            prompt = app.prompt_builder.build(task)
            self.assertIn("## PendingComments", prompt)
            self.assertIn(f"评论 ID：{result['comment']['id']}", prompt)
            self.assertIn("这里需要补充用户评论里的约束", prompt)
            self.assertIn("必须优先处理 status=pending 的评论", prompt)

    def test_artifact_save_validates_template_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = HarnessApp(HarnessPaths.from_project(tmp))
            task = app.create_task({"title": "T", "description": "D"})
            manager = ArtifactFileManager(app.paths)
            original = manager.read(task["artifact_path"])
            manager.save(task["artifact_path"], original.replace("等待方案设计阶段生成方案", "方案已更新"))
            self.assertIn("方案已更新", manager.read(task["artifact_path"]))
            self.assertNotIn("harness-artifact-runtime", manager.read(task["artifact_path"]))
            with self.assertRaises(ValueError):
                manager.save(task["artifact_path"], "<html></html>")

    def test_artifact_runtime_v2_is_rendered_not_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = HarnessApp(HarnessPaths.from_project(tmp))
            task = app.create_task({"title": "Runtime", "description": "D"})

            persisted = app.artifacts.read(task["artifact_path"])
            rendered = app.artifacts.read_for_browser(task["artifact_path"])

            self.assertNotIn("artifact-runtime-v2", persisted)
            self.assertIn("artifact-runtime-v2", rendered)
            self.assertIn("data-comment-selection", rendered)
            self.assertIn("data-selection-status", rendered)

            app.artifacts.save(task["artifact_path"], rendered)
            saved = app.artifacts.read(task["artifact_path"])
            self.assertNotIn("artifact-runtime-v2", saved)
            self.assertNotIn("data-comment-selection", saved)
            self.assertIn('id="harness-comments"', saved)

    def test_browser_snapshot_save_strips_injected_scripts(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = HarnessApp(HarnessPaths.from_project(tmp))
            task = app.create_task({"title": "评论保存", "description": "D"})
            html = app.artifacts.read(task["artifact_path"])
            html = html.replace('{"comments":[],"anchors":[]}', '{"comments":[{"id":"c_verify","section":"background","quote":"Q","content":"保存验证","status":"pending"}],"anchors":[]}')
            html = html.replace("</body>", '<script id="browser-extension-script">window.x=1</script></body>')

            app.artifacts.save_browser_snapshot(task["artifact_path"], html)

            saved = app.artifacts.read(task["artifact_path"])
            self.assertIn("保存验证", saved)
            self.assertNotIn("browser-extension-script", saved)
            self.assertNotIn("harness-artifact-runtime", saved)

    def test_state_transition_only_from_review_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = HarnessApp(HarnessPaths.from_project(tmp))
            task = app.create_task({"title": "T", "description": "D"})
            with self.assertRaises(ValueError):
                app.store.transition_task(task["id"], "confirm")
            app.store.update_task_status(task["id"], "需求确认")
            updated = app.store.transition_task(task["id"], "confirm")
            self.assertEqual(updated["status"], "待方案设计")
            self.assertEqual(updated["phase"], "solution")

    def test_force_task_status_can_mark_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = HarnessApp(HarnessPaths.from_project(tmp))
            task = app.create_task({"title": "T", "description": "D"})

            updated = app.store.force_task_status(task["id"], "已失败", note="用户取消任务")

            self.assertEqual(updated["status"], "已失败")
            self.assertEqual(updated["phase"], "failed")
            self.assertEqual(updated["blocked_reason"], "用户取消任务")

    async def test_harness_server_task_api_and_artifact_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = HarnessApp(HarnessPaths.from_project(tmp))
            server = HarnessServer(app, "127.0.0.1", 0)
            await server.start()
            host, port = server.server_address
            base = f"http://{host}:{port}"
            try:
                body = json.dumps({"title": "API 任务", "description": "需求"}).encode("utf-8")
                req = urllib.request.Request(f"{base}/api/tasks", data=body, method="POST", headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                task = payload["task"]

                with urllib.request.urlopen(f"{base}/api/tasks", timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(payload["metrics"]["pending"], 1)
                self.assertEqual(len(payload["items"]), 1)
                self.assertIn("已失败", payload["items"][0]["status_options"])

                force_body = json.dumps({"status": "已失败", "note": "用户手动取消任务"}).encode("utf-8")
                force_req = urllib.request.Request(f"{base}/api/tasks/{task['id']}/status", data=force_body, method="POST", headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(force_req, timeout=5) as response:
                    force_payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(force_payload["task"]["status"], "已失败")

                with urllib.request.urlopen(f"{base}/tasks/{task['id']}/artifact", timeout=5) as response:
                    artifact_html = response.read().decode("utf-8")
                self.assertIn("API 任务", artifact_html)
                self.assertIn("harness-artifact-runtime", artifact_html)

                comment_body = json.dumps({"section": "background", "quote": "原始任务", "content": "第一条结构化评论"}).encode("utf-8")
                comment_req = urllib.request.Request(f"{base}/api/tasks/{task['id']}/comments", data=comment_body, method="POST", headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(comment_req, timeout=5) as response:
                    comment_payload = json.loads(response.read().decode("utf-8"))
                anchor_id = comment_payload["comment"]["anchorId"]
                self.assertTrue(comment_payload["comment"]["anchored"])

                second_body = json.dumps({"section": "background", "quote": "原始任务", "content": "同一位置第二条评论", "anchorId": anchor_id}).encode("utf-8")
                second_req = urllib.request.Request(f"{base}/api/tasks/{task['id']}/comments", data=second_body, method="POST", headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(second_req, timeout=5) as response:
                    second_payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(second_payload["comment"]["anchorId"], anchor_id)

                resolve_body = json.dumps({"status": "done"}).encode("utf-8")
                resolve_req = urllib.request.Request(f"{base}/api/tasks/{task['id']}/comments/{comment_payload['comment']['id']}/resolve", data=resolve_body, method="POST", headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(resolve_req, timeout=5) as response:
                    resolve_payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(resolve_payload["comment"]["status"], "done")

                with urllib.request.urlopen(f"{base}/tasks/{task['id']}/artifact", timeout=5) as response:
                    commented_html = response.read().decode("utf-8")
                self.assertIn('data-comment-anchor-id', commented_html)
                self.assertIn("第一条结构化评论", commented_html)
                self.assertIn("同一位置第二条评论", commented_html)
                self.assertIn('data-comment-status="pending"', commented_html)

                dirty_html = commented_html.replace("第一条结构化评论", "API 保存验证")
                dirty_html = dirty_html.replace("</body>", '<script id="extension-script">window.y=1</script></body>')
                save_body = json.dumps({"html": dirty_html}).encode("utf-8")
                save_req = urllib.request.Request(f"{base}/api/tasks/{task['id']}/artifact/save", data=save_body, method="POST", headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(save_req, timeout=5) as response:
                    save_payload = json.loads(response.read().decode("utf-8"))
                self.assertTrue(save_payload["saved"])

                with urllib.request.urlopen(f"{base}/tasks/{task['id']}/artifact", timeout=5) as response:
                    saved_artifact_html = response.read().decode("utf-8")
                self.assertIn("API 保存验证", saved_artifact_html)
                self.assertNotIn("extension-script", saved_artifact_html)
            finally:
                await server.stop()


class HarnessMockSchedulerTests(unittest.TestCase):
    def test_mock_scheduler_processes_dispatchable_task_without_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = HarnessApp(HarnessPaths.from_project(tmp))
            task = app.create_task({"title": "Mock 调度", "description": "不调用真实 Agent"})
            processed = app.process_dispatchable_once()
            updated = app.store.get_task(task["id"])
            self.assertEqual(processed, 1)
            self.assertEqual(updated["status"], "需求确认")
            artifact = Path(updated["artifact_path"]).read_text(encoding="utf-8")
            self.assertIn("Mock Agent", artifact)
            self.assertNotIn('id="harness-artifact-runtime"', artifact)

            run = app.store.list_runs(task_id=task["id"])[0]
            self.assertEqual(run["agent_profile_id"], "agent_mock")
            self.assertTrue(Path(run["event_log_path"]).exists())
            agent_events = app.store.list_agent_run_events(run_id=run["id"])
            self.assertTrue(any(event["event_type"] == "agent_selected" for event in agent_events))

    def test_mock_scheduler_marks_pending_comments_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = HarnessApp(HarnessPaths.from_project(tmp))
            task = app.create_task({"title": "评论处理", "description": "D"})
            result = app.comments.add_comment(
                task["artifact_path"],
                {"section": "background", "quote": "等待需求规划阶段补充。", "content": "请根据评论更新"},
            )
            comment_id = result["comment"]["id"]

            app.process_dispatchable_once()

            updated = app.store.get_task(task["id"])
            artifact = Path(updated["artifact_path"]).read_text(encoding="utf-8")
            self.assertIn('"status": "done"', artifact)
            self.assertIn(f"Mock Agent 处理评论 {comment_id}", artifact)
            self.assertIn("评论处理记录：需求规划", artifact)
            self.assertIn("本轮已解决 1 条评论", artifact)
            self.assertIn(f"<td>{comment_id}</td>", artifact)
            self.assertIn("未解决评论继续保留", artifact)

    def test_only_resolved_comments_are_archived_by_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = HarnessApp(HarnessPaths.from_project(tmp))
            task = app.create_task({"title": "评论部分处理", "description": "D"})
            done = app.comments.add_comment(
                task["artifact_path"],
                {"section": "background", "quote": "等待需求规划阶段补充。", "content": "已解决反馈"},
            )
            pending = app.comments.add_comment(
                task["artifact_path"],
                {
                    "section": "background",
                    "quote": "等待需求规划阶段补充。",
                    "content": "未解决反馈",
                    "anchorId": done["comment"]["anchorId"],
                },
            )
            app.comments.resolve_comment(task["artifact_path"], done["comment"]["id"], {"status": "done"})

            app.process_dispatchable_once()

            updated = app.store.get_task(task["id"])
            artifact = Path(updated["artifact_path"]).read_text(encoding="utf-8")
            self.assertIn(f"<td>{pending['comment']['id']}</td>", artifact)
            self.assertNotIn(f"<td>{done['comment']['id']}</td>", artifact)


class HarnessAgentConfigTests(unittest.TestCase):
    def test_default_agents_and_stage_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = HarnessApp(HarnessPaths.from_project(tmp))
            profiles = app.store.list_agent_profiles()
            kinds = {profile["kind"] for profile in profiles}
            self.assertIn("codex_app_server", kinds)
            self.assertIn("claude_cli", kinds)

            claude = next(profile for profile in profiles if profile["kind"] == "claude_cli")
            self.assertTrue(claude["dangerously_skip_permissions"])
            binding = app.store.update_stage_agent_binding("planning", claude["id"])
            self.assertEqual(binding["agent_profile_id"], claude["id"])

            task = app.create_task({"title": "Agent 选择", "description": "D"})
            selected = app.store.resolve_agent_profile(task, "planning")
            self.assertEqual(selected["id"], claude["id"])


class HarnessResetAndExportTests(unittest.TestCase):
    def test_reset_artifact_restores_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = HarnessApp(HarnessPaths.from_project(tmp))
            task = app.create_task({"title": "Reset 测试", "description": "D"})
            content = app.artifacts.read(task["artifact_path"])
            app.artifacts.save(task["artifact_path"], content.replace("等待方案设计阶段生成方案", "脏方案痕迹"))
            self.assertIn("脏方案痕迹", app.artifacts.read(task["artifact_path"]))

            result = app.reset_artifact(task["id"])
            restored = app.artifacts.read(task["artifact_path"])
            self.assertEqual(result["taskId"], task["id"])
            self.assertNotIn("脏方案痕迹", restored)
            self.assertIn("等待方案设计阶段生成方案", restored)

    def test_export_learning_writes_markdown_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = HarnessApp(HarnessPaths.from_project(tmp))
            task = app.create_task({"title": "导出测试", "description": "D"})
            content = app.artifacts.read(task["artifact_path"])
            content = content.replace(
                "<p>等待经验沉淀阶段生成 Markdown 草稿和导出预览。</p>",
                "<h2>关键经验</h2><ul><li>经验一</li><li>经验二</li></ul>",
            )
            app.artifacts.save(task["artifact_path"], content)

            result = app.export_learning_markdown(task["id"])
            export_path = Path(result["exportPath"])
            self.assertTrue(export_path.exists())
            md = export_path.read_text(encoding="utf-8")
            self.assertIn("# 导出测试", md)
            self.assertIn("- 经验一", md)
            self.assertIn("- 经验二", md)


class HarnessSkillTests(unittest.TestCase):
    def test_skill_crud_and_prompt_includes_skill_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = HarnessApp(HarnessPaths.from_project(tmp))
            skill = app.store.upsert_skill({
                "name": "codex",
                "path": "/usr/local/bin/codex",
                "description": "Codex CLI",
                "prompt_hint": "可调用 codex 完成方案补全",
                "enabled": True,
            })
            self.assertTrue(skill["id"])
            skills = app.store.list_skills()
            self.assertEqual(len(skills), 1)

            updated = app.store.upsert_skill({**skill, "description": "Codex CLI v2"})
            self.assertEqual(updated["description"], "Codex CLI v2")

            app.store.update_stage_prompt("solution", {"enabled_skill_names": ["codex"]})
            task = app.create_task({"title": "Prompt 测试", "description": "D"})
            app.store.update_task_status(task["id"], "待方案设计")
            updated_task = app.store.get_task(task["id"])
            prompt = app.prompt_builder.build(updated_task)
            self.assertIn("本阶段可用 Skill/MCP", prompt)
            self.assertIn("codex", prompt)
            self.assertIn("可调用 codex", prompt)

            app.store.delete_skill(skill["id"])
            self.assertEqual(app.store.list_skills(), [])


class HarnessWorkflowPromptTests(unittest.IsolatedAsyncioTestCase):
    def test_default_prompt_sections_are_structured(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = HarnessApp(HarnessPaths.from_project(tmp))
            solution = app.store.get_stage_prompt("solution")
            implementation = app.store.get_stage_prompt("implementation")

            self.assertIn("生成可评审的 HTML 技术方案", solution["stage_goal"])
            self.assertIn("质量门禁：用户输入处理台账", solution["prompt"])
            self.assertIn("质量门禁：方案评审完整性", solution["prompt"])
            self.assertIn("背景区", solution["required_reads"])
            self.assertIn("更新方案区", solution["allowed_actions"])
            self.assertIn("不要执行代码修改", solution["forbidden_actions"])
            self.assertTrue(solution["output_requirements"])
            self.assertIn("质量门禁：代码实施与修改闭环", implementation["prompt"])
            self.assertIn("质量门禁：TDD 与验证", implementation["prompt"])
            self.assertIn("Go 远端 CI 记录 result_dir/status_file", implementation["output_requirements"])

    def test_existing_stage_prompt_merges_quality_gates_without_overwriting_custom_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = HarnessApp(HarnessPaths.from_project(tmp))
            app.store.update_stage_prompt("implementation", {"prompt": "用户自定义实施 Prompt", "output_requirements": ["用户自定义输出"]})

            reopened = HarnessApp(HarnessPaths.from_project(tmp))
            implementation = reopened.store.get_stage_prompt("implementation")

            self.assertIn("用户自定义实施 Prompt", implementation["prompt"])
            self.assertIn("质量门禁：TDD 与验证", implementation["prompt"])
            self.assertIn("用户自定义输出", implementation["output_requirements"])
            self.assertIn("代码需修改时记录 MR 评论处理和回复/resolve 结果", implementation["output_requirements"])

    def test_prompt_builder_supports_wildcard_skills_and_precise_next_statuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = HarnessApp(HarnessPaths.from_project(tmp))
            app.store.upsert_skill({"name": "lark-doc", "path": "", "description": "读写飞书文档", "enabled": True})
            app.store.upsert_skill({"name": "lark-base", "path": "", "description": "读写多维表格", "enabled": True})
            app.store.update_stage_prompt("solution", {"enabled_skill_names": ["lark-*"]})
            task = app.create_task({"title": "通配符 Skill", "description": "D"})
            app.store.update_task_status(task["id"], "待方案设计")
            prompt = app.prompt_builder.build(app.store.get_task(task["id"]))

            self.assertIn("lark-base", prompt)
            self.assertIn("lark-doc", prompt)
            self.assertIn("建议下一状态必须是当前阶段允许的精确状态名之一：方案评审 / 需求确认 / 已阻塞", prompt)

    async def test_workflow_preview_uses_draft_without_persisting(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = HarnessApp(HarnessPaths.from_project(tmp))
            task = app.create_task({"title": "Prompt 预览", "description": "D"})
            before = app.store.get_stage_prompt("planning")
            server = HarnessServer(app, "127.0.0.1", 0)
            await server.start()
            host, port = server.server_address
            base = f"http://{host}:{port}"
            try:
                body = json.dumps({
                    "task_id": task["id"],
                    "stage": {
                        "stage_goal": "草稿目标",
                        "required_reads": ["草稿读取"],
                        "allowed_actions": ["草稿允许"],
                        "forbidden_actions": ["草稿禁止"],
                        "output_requirements": ["草稿输出"],
                        "prompt": "草稿补充",
                        "editable_sections": ["background"],
                        "enabled_skill_names": [],
                    },
                }).encode("utf-8")
                req = urllib.request.Request(f"{base}/api/workflow/stages/planning/preview", data=body, method="POST", headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))

                self.assertIn("草稿目标", payload["prompt"])
                self.assertIn("草稿读取", payload["prompt"])
                after = app.store.get_stage_prompt("planning")
                self.assertEqual(after["stage_goal"], before["stage_goal"])
                self.assertNotIn("草稿目标", after["stage_goal"])
            finally:
                await server.stop()

    async def test_workflow_save_persists_prompt_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = HarnessApp(HarnessPaths.from_project(tmp))
            server = HarnessServer(app, "127.0.0.1", 0)
            await server.start()
            host, port = server.server_address
            base = f"http://{host}:{port}"
            try:
                body = json.dumps({"prompt": "保存后的补充 Prompt", "stage_goal": "保存后的阶段目标"}).encode("utf-8")
                req = urllib.request.Request(f"{base}/api/workflow/stages/breakdown/prompt", data=body, method="PUT", headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))

                self.assertEqual(payload["stage"]["prompt"], "保存后的补充 Prompt")
                saved = app.store.get_stage_prompt("breakdown")
                self.assertEqual(saved["prompt"], "保存后的补充 Prompt")
            finally:
                await server.stop()

    def test_workflow_page_avoids_prompt_global_collision(self):
        from symphony.api.pages import WORKFLOW_PAGE

        self.assertNotIn("prompt.value", WORKFLOW_PAGE)
        self.assertIn("byId('prompt').value", WORKFLOW_PAGE)

    def test_artifact_comment_save_reports_persistence(self):
        template = Path("symphony/artifacts/templates/artifact_template.html").read_text(encoding="utf-8")

        self.assertIn("评论已保存，Agent 下次运行可读取", template)
        self.assertIn("评论保存失败", template)
        self.assertIn("clone.querySelectorAll('script')", template)
        self.assertIn("artifact-runtime-v2", template)
        self.assertIn("function rangeSection(range)", template)
        self.assertIn("event?.target?.closest?.('.harness-comment-popover')", template)
        self.assertIn("document.addEventListener('selectionchange'", template)
        self.assertIn("function readCommentTarget", template)
        self.assertIn("lastCommentTarget", template)
        self.assertIn("评论选区", template)
        self.assertIn("请先选中文本，再添加评论", template)
        self.assertNotIn("range.commonAncestorContainer.parentElement?.closest?.('section[data-section]')", template)


class HarnessExtendedHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_skill_endpoints_and_task_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = HarnessApp(HarnessPaths.from_project(tmp))
            server = HarnessServer(app, "127.0.0.1", 0)
            await server.start()
            host, port = server.server_address
            base = f"http://{host}:{port}"
            try:
                # create a skill
                body = json.dumps({"name": "file-mcp", "path": "/tmp/file-mcp", "description": "fs ops", "prompt_hint": "读写文件", "enabled": True}).encode("utf-8")
                req = urllib.request.Request(f"{base}/api/skills", data=body, method="POST", headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    skill_payload = json.loads(resp.read().decode("utf-8"))
                skill_id = skill_payload["skill"]["id"]

                with urllib.request.urlopen(f"{base}/api/skills", timeout=5) as resp:
                    listing = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(any(s["id"] == skill_id for s in listing["items"]))

                # create a task and exercise reset + learning export
                body = json.dumps({"title": "HTTP 扩展", "description": "D"}).encode("utf-8")
                req = urllib.request.Request(f"{base}/api/tasks", data=body, method="POST", headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    task = json.loads(resp.read().decode("utf-8"))["task"]

                req = urllib.request.Request(f"{base}/api/tasks/{task['id']}/artifact/reset", method="POST")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    reset = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(reset["taskId"], task["id"])

                req = urllib.request.Request(f"{base}/api/tasks/{task['id']}/learning/export", method="POST")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    exp = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(Path(exp["exportPath"]).exists())

                # delete skill
                req = urllib.request.Request(f"{base}/api/skills/{skill_id}", method="DELETE")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    deleted = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(deleted["deleted"])
            finally:
                await server.stop()

    async def test_agent_endpoints_and_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = HarnessApp(HarnessPaths.from_project(tmp))
            server = HarnessServer(app, "127.0.0.1", 0)
            await server.start()
            host, port = server.server_address
            base = f"http://{host}:{port}"
            try:
                with urllib.request.urlopen(f"{base}/agents", timeout=5) as resp:
                    page = resp.read().decode("utf-8")
                self.assertIn("--dangerously-skip-permissions", page)
                self.assertIn("Codex App Server", page)

                with urllib.request.urlopen(f"{base}/api/agents", timeout=5) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(any(agent["kind"] == "claude_cli" for agent in payload["items"]))
                codex = next(agent for agent in payload["items"] if agent["kind"] == "codex_app_server")

                body = json.dumps({"agent_profile_id": codex["id"]}).encode("utf-8")
                req = urllib.request.Request(f"{base}/api/agents/bindings/implementation", data=body, method="PUT", headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    binding = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(binding["binding"]["agent_profile_id"], codex["id"])
            finally:
                await server.stop()


class HarnessDesignContractTests(unittest.TestCase):
    def test_artifact_rejects_unexpected_script_and_inline_handlers(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = HarnessApp(HarnessPaths.from_project(tmp))
            task = app.create_task({"title": "安全校验", "description": "D"})
            original = app.artifacts.read(task["artifact_path"])

            with self.assertRaises(ValueError):
                app.artifacts.save(task["artifact_path"], original.replace("</body>", '<script src="http://example.com/a.js"></script></body>'))

            with self.assertRaises(ValueError):
                app.artifacts.save(task["artifact_path"], original.replace("<p>等待方案设计阶段生成方案。</p>", '<p onclick="alert(1)">等待方案设计阶段生成方案。</p>'))

    def test_scheduler_records_prompt_and_log_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = HarnessApp(HarnessPaths.from_project(tmp))
            task = app.create_task({"title": "Run 记录", "description": "D"})

            app.process_dispatchable_once()

            updated = app.store.get_task(task["id"])
            runs = app.store.list_runs(task_id=task["id"])
            self.assertEqual(updated["latest_run_id"], runs[0]["id"])
            self.assertEqual(runs[0]["status"], "completed")
            self.assertTrue(Path(runs[0]["prompt_path"]).exists())
            self.assertTrue(Path(runs[0]["log_path"]).exists())
            self.assertIn("HTML 产物路径", Path(runs[0]["prompt_path"]).read_text(encoding="utf-8"))

    def test_scheduler_applies_allowed_agent_suggested_status(self):
        class SuggestBackAdapter:
            def run(self, ctx):
                return AgentRunResult(status="completed", summary="需要回需求确认", suggested_status="需求确认")

        with tempfile.TemporaryDirectory() as tmp:
            app = HarnessApp(HarnessPaths.from_project(tmp))
            app.agent_runtime.adapters["mock"] = SuggestBackAdapter()
            task = app.create_task({"title": "方案信息不足", "description": "D"})
            app.store.update_task_status(task["id"], "待方案设计")

            app.process_dispatchable_once()

            updated = app.store.get_task(task["id"])
            self.assertEqual(updated["status"], "需求确认")
            events = app.store.list_events(task_id=task["id"])
            handoff = next(event for event in events if event["event_type"] == "scheduler_handoff")
            self.assertEqual(handoff["payload"]["to_status"], "需求确认")
            self.assertEqual(handoff["payload"]["reason"], "agent_suggested_status_allowed")

    def test_scheduler_rejects_illegal_agent_suggested_status(self):
        class IllegalSuggestionAdapter:
            def run(self, ctx):
                return AgentRunResult(status="completed", summary="非法建议", suggested_status="已完成")

        with tempfile.TemporaryDirectory() as tmp:
            app = HarnessApp(HarnessPaths.from_project(tmp))
            app.agent_runtime.adapters["mock"] = IllegalSuggestionAdapter()
            task = app.create_task({"title": "非法状态", "description": "D"})
            app.store.update_task_status(task["id"], "待方案设计")

            app.process_dispatchable_once()

            updated = app.store.get_task(task["id"])
            self.assertEqual(updated["status"], "方案评审")
            events = app.store.list_events(task_id=task["id"])
            handoff = next(event for event in events if event["event_type"] == "scheduler_handoff")
            self.assertEqual(handoff["payload"]["to_status"], "方案评审")
            self.assertIn("agent_suggested_status_rejected", handoff["payload"]["reason"])

    def test_scheduler_uses_harness_result_json_from_agent_output(self):
        class HarnessJsonAdapter:
            def run(self, ctx):
                return AgentRunResult(
                    status="completed",
                    summary='HARNESS_RESULT_JSON: {"status":"completed","summary":"方案信息不足","suggested_status":"需求确认","reason":"缺少仓库路径"}',
                )

        with tempfile.TemporaryDirectory() as tmp:
            app = HarnessApp(HarnessPaths.from_project(tmp))
            app.agent_runtime.adapters["mock"] = HarnessJsonAdapter()
            task = app.create_task({"title": "解析结果 JSON", "description": "D"})
            app.store.update_task_status(task["id"], "待方案设计")

            app.process_dispatchable_once()

            updated = app.store.get_task(task["id"])
            self.assertEqual(updated["status"], "需求确认")


class HarnessMonitorHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_monitor_health_and_runs_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = HarnessApp(HarnessPaths.from_project(tmp))
            task = app.create_task({"title": "监控 API", "description": "D"})
            app.process_dispatchable_once()
            server = HarnessServer(app, "127.0.0.1", 0)
            await server.start()
            host, port = server.server_address
            base = f"http://{host}:{port}"
            try:
                with urllib.request.urlopen(f"{base}/monitor", timeout=5) as resp:
                    page = resp.read().decode("utf-8")
                self.assertIn("调度监控", page)

                with urllib.request.urlopen(f"{base}/api/scheduler/health", timeout=5) as resp:
                    health = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(health["status"], "ok")
                self.assertIn("metrics", health)

                with urllib.request.urlopen(f"{base}/api/tasks/{task['id']}/runs", timeout=5) as resp:
                    runs = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(len(runs["items"]), 1)
                self.assertTrue(runs["items"][0]["prompt_path"])
            finally:
                await server.stop()


class HarnessLogPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_task_logs_page_and_run_log_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = HarnessApp(HarnessPaths.from_project(tmp))
            task = app.create_task({"title": "日志任务", "description": "D"})
            app.process_dispatchable_once()
            run = app.store.list_runs(task_id=task["id"])[0]
            server = HarnessServer(app, "127.0.0.1", 0)
            await server.start()
            host, port = server.server_address
            base = f"http://{host}:{port}"
            try:
                with urllib.request.urlopen(f"{base}/logs", timeout=5) as resp:
                    page = resp.read().decode("utf-8")
                self.assertIn("任务日志", page)
                self.assertIn("调度过程日志", page)
                self.assertIn("Agent 操作日志", page)

                with urllib.request.urlopen(f"{base}/tasks", timeout=5) as resp:
                    tasks_page = resp.read().decode("utf-8")
                self.assertIn("查看日志", tasks_page)

                with urllib.request.urlopen(f"{base}/api/tasks/{task['id']}/logs", timeout=5) as resp:
                    logs = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(logs["task"]["id"], task["id"])
                self.assertTrue(any(event["event_type"] == "scheduler_picked" for event in logs["events"]))
                self.assertTrue(any(event["event_type"] == "run_finished" for event in logs["events"]))
                self.assertEqual(logs["runs"][0]["id"], run["id"])

                with urllib.request.urlopen(f"{base}/api/runs/{run['id']}/logs", timeout=5) as resp:
                    run_logs = json.loads(resp.read().decode("utf-8"))
                self.assertIn("# Harness Scheduler Agent Prompt", run_logs["prompt"])
                self.assertIn("result=completed", run_logs["log"])
            finally:
                await server.stop()


class HarnessTaskPageRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_metric_filter_includes_all_matching_statuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = HarnessApp(HarnessPaths.from_project(tmp))
            pending = app.create_task({"title": "待规划任务", "description": "D"})
            waiting = app.create_task({"title": "确认任务", "description": "D"})
            app.store.update_task_status(waiting["id"], "需求确认")
            running = app.create_task({"title": "运行任务", "description": "D"})
            app.store.update_task_status(running["id"], "规划中")
            server = HarnessServer(app, "127.0.0.1", 0)
            await server.start()
            host, port = server.server_address
            base = f"http://{host}:{port}"
            try:
                with urllib.request.urlopen(f"{base}/api/tasks?metric=pending", timeout=5) as resp:
                    pending_payload = json.loads(resp.read().decode("utf-8"))
                self.assertEqual([t["title"] for t in pending_payload["items"]], ["待规划任务"])

                with urllib.request.urlopen(f"{base}/api/tasks?metric=waiting_user", timeout=5) as resp:
                    waiting_payload = json.loads(resp.read().decode("utf-8"))
                self.assertEqual([t["title"] for t in waiting_payload["items"]], ["确认任务"])

                with urllib.request.urlopen(f"{base}/api/tasks?metric=running", timeout=5) as resp:
                    running_payload = json.loads(resp.read().decode("utf-8"))
                self.assertEqual([t["title"] for t in running_payload["items"]], ["运行任务"])
            finally:
                await server.stop()

    async def test_tasks_page_contains_create_display_regression_guards(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = HarnessApp(HarnessPaths.from_project(tmp))
            server = HarnessServer(app, "127.0.0.1", 0)
            await server.start()
            host, port = server.server_address
            try:
                with urllib.request.urlopen(f"http://{host}:{port}/tasks", timeout=5) as resp:
                    page = resp.read().decode("utf-8")
                self.assertIn("clearFilters();", page)
                self.assertIn("暂无任务", page)
                self.assertIn("activeMetric", page)
                self.assertIn("showError", page)
                self.assertIn("需求规划", page)
                self.assertIn("方案设计", page)
                self.assertIn("任务拆解", page)
                self.assertIn("代码实施", page)
                self.assertIn("经验沉淀", page)
                self.assertIn("stageOrder.map", page)
                self.assertIn("minmax(420px,480px)", page)
                self.assertIn("取消任务", page)
                self.assertIn("manualStatuses", page)
                self.assertIn("applyManualStatus", page)
                self.assertNotIn(">${s}</div>", page)
            finally:
                await server.stop()


if __name__ == "__main__":
    unittest.main()
