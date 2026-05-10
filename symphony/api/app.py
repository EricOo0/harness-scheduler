from __future__ import annotations

import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from symphony.agents.runtime import AgentRuntime
from symphony.artifacts.comments import ArtifactCommentManager
from symphony.artifacts.file_manager import ArtifactFileManager
from symphony.scheduler.prompt_builder import PromptBuilder
from symphony.scheduler.runtime import SchedulerRuntime
from symphony.storage.db import HarnessPaths, beijing_now_display, utc_now
from symphony.storage.task_store import LocalTaskStore


def make_task_id() -> str:
    return f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


class HarnessApp:
    def __init__(self, paths: HarnessPaths):
        self.paths = paths
        self.store = LocalTaskStore(paths.db_path)
        self.artifacts = ArtifactFileManager(paths)
        self.comments = ArtifactCommentManager(self.artifacts)
        self.prompt_builder = PromptBuilder(self.store)
        self.agent_runtime = AgentRuntime(store=self.store, artifacts=self.artifacts)
        self.scheduler = SchedulerRuntime(
            store=self.store,
            artifacts=self.artifacts,
            prompt_builder=self.prompt_builder,
            agent_runtime=self.agent_runtime,
        )

    def create_task(self, data: dict[str, Any]) -> dict[str, Any]:
        title = str(data.get("title") or "未命名任务").strip() or "未命名任务"
        description = str(data.get("description") or "")
        task_id = make_task_id()
        workspace, artifact = self.artifacts.create_for_task(task_id, title, description)
        return self.store.create_task(
            task_id=task_id,
            title=title,
            description=description,
            priority=str(data.get("priority") or "P1"),
            repository_path=str(data.get("repositoryPath") or data.get("repository_path") or self.paths.root),
            target_branch=str(data.get("targetBranch") or data.get("target_branch") or ""),
            workspace_path=str(workspace),
            artifact_path=str(artifact),
        )

    def task_payload(self, task_id: str) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if not task:
            raise KeyError(task_id)
        return {"task": task, "runs": self.store.recent_runs(task_id), "prompt": self.prompt_builder.build(task)}

    def reset_artifact(self, task_id: str) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if not task:
            raise KeyError(task_id)
        self.artifacts.reset_to_template(task["id"], task["title"], task["description"], task["artifact_path"])
        with self.store.connection() as conn:
            self.store.append_event(conn, task_id, "artifact_reset", {"artifact_path": task["artifact_path"]})
        return {"taskId": task_id, "artifactPath": task["artifact_path"], "resetAt": utc_now()}

    def export_learning_markdown(self, task_id: str) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if not task:
            raise KeyError(task_id)
        content = self.artifacts.read(task["artifact_path"])
        learning_html = self._extract_section(content, "learning") or "<p>(暂无沉淀)</p>"
        markdown = self._html_section_to_markdown(learning_html)
        export_dir = self.paths.data_dir / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        export_path = export_dir / f"{task['id']}.learning.md"
        body = (
            f"# {task['title']}\n\n"
            f"- 任务 ID: {task['id']}\n"
            f"- 状态: {task['status']}\n"
            f"- 导出时间: {beijing_now_display()}\n\n"
            "## 经验沉淀\n\n"
            f"{markdown}\n"
        )
        export_path.write_text(body, encoding="utf-8")
        with self.store.connection() as conn:
            self.store.append_event(conn, task_id, "learning_exported", {"export_path": str(export_path)})
        return {"taskId": task_id, "exportPath": str(export_path), "markdown": body}

    def process_dispatchable_once(self) -> int:
        return self.scheduler.process_dispatchable_once()

    @staticmethod
    def _extract_section(content: str, section_id: str) -> str | None:
        marker = f'id="{section_id}"'
        start = content.find(marker)
        if start < 0:
            return None
        body_start = content.find(">", start)
        if body_start < 0:
            return None
        end = content.find("</section>", body_start)
        if end < 0:
            return None
        return content[body_start + 1 : end]

    @staticmethod
    def _html_section_to_markdown(snippet: str) -> str:
        text = re.sub(r"<h2[^>]*>(.*?)</h2>", lambda m: f"### {m.group(1).strip()}\n", snippet, flags=re.S)
        text = re.sub(r"<h3[^>]*>(.*?)</h3>", lambda m: f"#### {m.group(1).strip()}\n", text, flags=re.S)
        text = re.sub(r"<li[^>]*>(.*?)</li>", lambda m: f"- {m.group(1).strip()}\n", text, flags=re.S)
        text = re.sub(r"</?(ul|ol)[^>]*>", "", text)
        text = re.sub(r"<p[^>]*>(.*?)</p>", lambda m: f"{m.group(1).strip()}\n\n", text, flags=re.S)
        text = re.sub(r"<strong[^>]*>(.*?)</strong>", lambda m: f"**{m.group(1).strip()}**", text, flags=re.S)
        text = re.sub(r"<em[^>]*>(.*?)</em>", lambda m: f"*{m.group(1).strip()}*", text, flags=re.S)
        text = re.sub(r"<br\s*/?>", "\n", text)
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
