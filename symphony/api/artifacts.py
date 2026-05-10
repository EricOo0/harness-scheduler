from __future__ import annotations

from pathlib import Path

from symphony.api.app import HarnessApp


def artifact_metadata(app: HarnessApp, task_id: str) -> dict:
    task = app.store.get_task(task_id)
    if not task:
        raise KeyError(task_id)
    return {
        "taskId": task["id"],
        "artifactPath": task["artifact_path"],
        "renderUrl": app.artifacts.render_url(task["id"]),
        "exists": Path(task["artifact_path"]).exists(),
        "updatedAt": task["updated_at"],
    }


def render_artifact(app: HarnessApp, task_id: str) -> str:
    task = app.store.get_task(task_id)
    if not task:
        raise KeyError(task_id)
    return app.artifacts.read_for_browser(task["artifact_path"])


def save_artifact(app: HarnessApp, task_id: str, html: str) -> dict:
    task = app.store.get_task(task_id)
    if not task:
        raise KeyError(task_id)
    app.artifacts.save(task["artifact_path"], html)
    return {"saved": True}
