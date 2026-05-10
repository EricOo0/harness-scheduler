from __future__ import annotations

import html
import re
from pathlib import Path

from symphony.storage.db import HarnessPaths, beijing_now_display
from symphony.workspace import ensure_inside_root, sanitize_workspace_key


class ArtifactFileManager:
    RUNTIME_VERSION = "artifact-runtime-v2"

    def __init__(self, paths: HarnessPaths):
        self.paths = paths
        self.template_path = Path(__file__).resolve().parent / "templates" / "artifact_template.html"
        self.paths.workspace_root.mkdir(parents=True, exist_ok=True)

    def create_for_task(self, task_id: str, title: str, description: str) -> tuple[Path, Path]:
        key = sanitize_workspace_key(task_id)
        workspace = self.paths.workspace_root / key
        ensure_inside_root(str(self.paths.workspace_root), str(workspace))
        workspace.mkdir(parents=True, exist_ok=True)
        artifact = workspace / "artifact.html"
        artifact.write_text(self.strip_runtime_shell(self._render_template(task_id, title, description)), encoding="utf-8")
        return workspace, artifact

    def get_path(self, task: dict) -> Path:
        return self.ensure_artifact_path(task["artifact_path"])

    def ensure_artifact_path(self, path: str) -> Path:
        artifact = Path(path).resolve()
        ensure_inside_root(str(self.paths.workspace_root), str(artifact))
        if artifact.name != "artifact.html":
            raise ValueError("artifact path must end with artifact.html")
        return artifact

    def read(self, artifact_path: str) -> str:
        return self.ensure_artifact_path(artifact_path).read_text(encoding="utf-8")

    def read_for_browser(self, artifact_path: str) -> str:
        """Render persisted artifact content with the latest protected browser shell."""
        return self.with_latest_runtime(self.read(artifact_path))

    def save(self, artifact_path: str, content: str) -> None:
        path = self.ensure_artifact_path(artifact_path)
        content = self.strip_runtime_shell(content)
        self.validate_basic_content(content)
        path.write_text(content, encoding="utf-8")

    def save_browser_snapshot(self, artifact_path: str, content: str) -> None:
        """Save HTML captured from a browser, removing extension-injected scripts."""
        path = self.ensure_artifact_path(artifact_path)
        sanitized = self.strip_runtime_shell(self.sanitize_browser_snapshot(content))
        self.validate_basic_content(sanitized)
        path.write_text(sanitized, encoding="utf-8")

    def validate_basic(self, artifact_path: str) -> None:
        self.validate_basic_content(self.read(artifact_path))

    def render_url(self, task_id: str) -> str:
        return f"/tasks/{task_id}/artifact"

    def reset_to_template(self, task_id: str, title: str, description: str, artifact_path: str) -> None:
        path = self.ensure_artifact_path(artifact_path)
        path.write_text(self.strip_runtime_shell(self._render_template(task_id, title, description)), encoding="utf-8")

    def _render_template(self, task_id: str, title: str, description: str) -> str:
        template = self.template_path.read_text(encoding="utf-8")
        return (
            template.replace("{{TASK_ID}}", html.escape(task_id, quote=True))
            .replace("{{TASK_TITLE}}", html.escape(title))
            .replace("{{TASK_DESCRIPTION}}", html.escape(description).replace("\n", "<br>"))
            .replace("{{CREATED_AT}}", html.escape(beijing_now_display()))
        )

    def with_latest_runtime(self, content: str) -> str:
        template = self.template_path.read_text(encoding="utf-8")
        content = self.strip_runtime_shell(content)
        if 'id="harness-render-assets"' not in content:
            content = self._inject_before(
                content,
                '<script type="application/json" id="harness-comments">',
                self._render_assets_block(),
            )
        content = self._inject_head_block(content, self._protected_block(template, "harness-artifact-style"))
        content = self._inject_before(
            content,
            '<script type="application/json" id="harness-comments">',
            self._runtime_toolbar(template),
        )
        content = self._inject_before(content, "</body>", self._protected_block(template, "harness-artifact-runtime"))
        return content

    def strip_runtime_shell(self, content: str) -> str:
        """Persist only artifact content plus comments; browser runtime is injected on render."""
        for marker in ("harness-artifact-style", "harness-artifact-runtime"):
            pattern = rf"\s*<(?P<tag>style|script)\b(?=[^>]*\bid=['\"]{re.escape(marker)}['\"])[^>]*>.*?</(?P=tag)>\s*"
            content = re.sub(pattern, "\n", content, flags=re.I | re.S)
        toolbar_pattern = r"\s*<div\b(?=[^>]*\bclass=['\"]harness-toolbar['\"])(?=[^>]*\bdata-harness-runtime-ui=['\"]true['\"])[^>]*>.*?</div>\s*"
        content = re.sub(toolbar_pattern, "\n", content, flags=re.I | re.S)
        return content

    @staticmethod
    def _protected_block(content: str, marker: str) -> str | None:
        match = re.search(
            rf"<(?P<tag>style|script)\b(?=[^>]*\bid=['\"]{re.escape(marker)}['\"])[^>]*>.*?</(?P=tag)>",
            content,
            flags=re.I | re.S,
        )
        return match.group(0) if match else None

    @staticmethod
    def _runtime_toolbar(template: str) -> str | None:
        toolbar_pattern = r"<div\b(?=[^>]*\bclass=['\"]harness-toolbar['\"])(?=[^>]*\bdata-harness-runtime-ui=['\"]true['\"])[^>]*>.*?</div>"
        match = re.search(
            toolbar_pattern,
            template,
            flags=re.I | re.S,
        )
        return match.group(0) if match else None

    @staticmethod
    def _render_assets_block() -> str:
        return '<script type="application/json" id="harness-render-assets">\n    {"version":1,"mermaid":[]}\n  </script>'

    @staticmethod
    def _inject_head_block(content: str, block: str | None) -> str:
        if not block:
            return content
        return ArtifactFileManager._inject_before(content, "</head>", block)

    @staticmethod
    def _inject_before(content: str, needle: str, block: str | None) -> str:
        if not block or needle not in content:
            return content
        return content.replace(needle, block + "\n" + needle, 1)

    @staticmethod
    def sanitize_browser_snapshot(content: str) -> str:
        def keep_only_harness_scripts(match: re.Match[str]) -> str:
            attrs = match.group(1)
            script_id = re.search(r"\bid\s*=\s*(['\"])(.*?)\1", attrs, flags=re.I)
            if script_id and script_id.group(2) in {"harness-comments", "harness-render-assets", "harness-artifact-runtime"}:
                return match.group(0)
            return ""

        return re.sub(r"<script\b([^>]*)>.*?</script>", keep_only_harness_scripts, content, flags=re.I | re.S)

    @staticmethod
    def validate_basic_content(content: str) -> None:
        required = [
            'data-harness-artifact-version="1"',
            'id="harness-comments"',
            'id="harness-render-assets"',
            'id="background"',
            'id="interaction"',
            'id="solution"',
            'id="task-breakdown"',
            'id="validation"',
            'id="history"',
            'id="learning"',
        ]
        missing = [item for item in required if item not in content]
        if missing:
            raise ValueError("artifact html missing required markers: " + ", ".join(missing))
        if re.search(r"<script\b(?![^>]*\bid=\"harness-(?:comments|render-assets)\")", content, flags=re.I):
            raise ValueError("artifact html contains unexpected script tag")
        if re.search(r"<[^>]+\son[a-z]+\s*=", content, flags=re.I):
            raise ValueError("artifact html contains inline event handler")
        protected_markers = [
            'id="harness-section-nav" data-agent-editable="false"',
            'id="harness-content"',
        ]
        missing_protected = [item for item in protected_markers if item not in content]
        if missing_protected:
            raise ValueError("artifact html missing protected markers: " + ", ".join(missing_protected))
