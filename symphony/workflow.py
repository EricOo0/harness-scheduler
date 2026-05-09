from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from .errors import WorkflowError
from .models import Issue, WorkflowDefinition


def select_workflow_path(path: str | None = None, cwd: str | None = None) -> Path:
    if path:
        return Path(path).expanduser().resolve()
    return (Path(cwd or os.getcwd()) / "WORKFLOW.md").resolve()


def load_workflow(path: str | Path) -> WorkflowDefinition:
    workflow_path = Path(path)
    try:
        raw = workflow_path.read_text(encoding="utf-8")
        stat = workflow_path.stat()
    except FileNotFoundError as exc:
        raise WorkflowError(f"missing workflow file: {workflow_path}", code="missing_workflow_file") from exc
    except OSError as exc:
        raise WorkflowError(f"cannot read workflow file: {exc}", code="missing_workflow_file") from exc

    config: dict[str, Any] = {}
    body = raw
    if raw.startswith("---"):
        lines = raw.splitlines()
        end = None
        for idx, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                end = idx
                break
        if end is None:
            raise WorkflowError("unterminated YAML front matter", code="workflow_parse_error")
        front_matter = "\n".join(lines[1:end])
        body = "\n".join(lines[end + 1 :])
        try:
            loaded = yaml.safe_load(front_matter) if front_matter.strip() else {}
        except yaml.YAMLError as exc:
            raise WorkflowError(f"invalid YAML front matter: {exc}", code="workflow_parse_error") from exc
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise WorkflowError("workflow front matter must be a map", code="workflow_front_matter_not_a_map")
        config = loaded

    return WorkflowDefinition(config=config, prompt_template=body.strip(), path=str(workflow_path), mtime_ns=stat.st_mtime_ns)


_VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_SECTION_RE = re.compile(r"\{\{\#\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}(.*?)\{\{/\s*\1\s*\}\}", re.DOTALL)
_INTERPOLATION_RE = re.compile(r"\{\{\s*(.*?)\s*\}\}")


class StrictTemplate:
    def __init__(self, template: str):
        self.template = template or "You are working on an issue from Linear."

    def render(self, *, issue: Issue, attempt: int | None = None) -> str:
        context = {"issue": issue.to_template_dict(), "attempt": attempt}
        try:
            return self._render_block(self.template, context, context)
        except WorkflowError:
            raise
        except Exception as exc:
            raise WorkflowError(f"template render failed: {exc}", code="template_render_error") from exc

    def _render_block(self, template: str, context: dict[str, Any], current: Any) -> str:
        rendered = template
        while True:
            match = _SECTION_RE.search(rendered)
            if not match:
                break
            key = match.group(1).strip()
            value = self._resolve(context, current, key)
            body = match.group(2)
            if isinstance(value, list):
                replacement = "".join(self._render_block(body, context, item) for item in value)
            elif value:
                replacement = self._render_block(body, context, value)
            else:
                replacement = ""
            rendered = rendered[: match.start()] + replacement + rendered[match.end() :]
        return _INTERPOLATION_RE.sub(lambda m: self._render_value(m, context, current), rendered)

    def _render_value(self, match: re.Match[str], context: dict[str, Any], current: Any) -> str:
        expr = match.group(1).strip()
        if expr.startswith("/") or expr.startswith("#"):
            raise WorkflowError(f"unmatched template section: {expr}", code="template_parse_error")
        value = self._resolve(context, current, expr)
        return "" if value is None else str(value)

    def _resolve(self, context: dict[str, Any], current: Any, expr: str) -> Any:
        if expr == ".":
            return current
        if not _VAR_RE.match(expr):
            raise WorkflowError(f"unsupported template expression: {expr}", code="template_parse_error")
        parts = expr.split(".")
        if isinstance(current, dict) and parts[0] in current:
            value: Any = current
        else:
            value = context
        for part in parts:
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                raise WorkflowError(f"unknown template variable: {expr}", code="template_render_error")
        return value
