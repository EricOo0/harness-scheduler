from __future__ import annotations

from pathlib import Path
from typing import Any

from symphony.api.app import HarnessApp
from symphony.domain.workflow import STAGES


def task_trace_payload(app: HarnessApp, task_id: str) -> dict[str, Any]:
    task = app.store.get_task(task_id)
    if not task:
        raise KeyError(task_id)
    runs_by_stage: dict[str, list[dict[str, Any]]] = {stage: [] for stage in STAGES}
    for run in reversed(app.store.list_trace_runs(task_id=task_id, limit=500)):
        steps = app.store.list_trace_steps(run["id"])
        artifacts = app.store.list_trace_artifacts(run_id=run["id"])
        runs_by_stage.setdefault(run["stage"], []).append(
            {
                **run,
                "timeline": [_step_summary(step) for step in steps],
                "artifact_count": len(artifacts),
            }
        )
    stages = [
        {
            "stage": stage,
            "title": spec["title"],
            "dispatch": spec["dispatch"],
            "running": spec["running"],
            "review": spec["review"],
            "runs": runs_by_stage.get(stage, []),
        }
        for stage, spec in STAGES.items()
    ]
    return {"task": task, "stages": stages}


def trace_step_payload(app: HarnessApp, step_id: str) -> dict[str, Any]:
    step = app.store.get_trace_step(step_id)
    if not step:
        raise KeyError(step_id)
    artifacts = []
    for artifact in step.get("artifacts") or []:
        path = artifact.get("path")
        artifacts.append({**artifact, "file": _file_payload(app, path) if path else _empty_file_payload()})
    return {"step": {**step, "artifacts": artifacts}}


def _step_summary(step: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": step["id"],
        "run_id": step["run_id"],
        "seq": step["seq"],
        "step_type": step["step_type"],
        "title": step["title"],
        "status": step["status"],
        "source": step["source"],
        "summary": step.get("summary"),
        "started_at": step.get("started_at"),
        "finished_at": step.get("finished_at"),
    }


def _read_text_if_safe(app: HarnessApp, path: str | None, *, limit: int) -> str:
    if not path:
        return ""
    candidate = Path(path).resolve()
    root = app.paths.workspace_root.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return "[文件路径不在任务工作区内，已拒绝读取]"
    if not candidate.exists() or not candidate.is_file():
        return ""
    content = candidate.read_text(encoding="utf-8", errors="replace")
    return content if len(content) <= limit else content[:limit] + "\n...[truncated]"


def _file_payload(app: HarnessApp, path: str | None) -> dict[str, Any]:
    if not path:
        return _empty_file_payload()
    candidate = Path(path).resolve()
    root = app.paths.workspace_root.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return {**_empty_file_payload(), "path": str(candidate), "error": "文件路径不在任务工作区内，已拒绝读取"}
    if not candidate.exists() or not candidate.is_file():
        return {**_empty_file_payload(), "path": str(candidate), "error": "文件不存在"}
    content = candidate.read_text(encoding="utf-8", errors="replace")
    size = candidate.stat().st_size
    preview = content[:500]
    max_content_chars = 200000
    truncated = len(content) > max_content_chars
    return {
        "path": str(candidate),
        "name": candidate.name,
        "size_bytes": size,
        "size_label": _format_size(size),
        "preview": preview,
        "content": content[:max_content_chars],
        "content_truncated": truncated,
    }


def _empty_file_payload() -> dict[str, Any]:
    return {"path": "", "name": "", "size_bytes": 0, "size_label": "0 B", "preview": "", "content": "", "content_truncated": False}


def _format_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"
