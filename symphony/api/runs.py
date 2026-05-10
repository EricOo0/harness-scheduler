from __future__ import annotations

from pathlib import Path

from symphony.api.app import HarnessApp
from symphony.domain.workflow import DISPATCHABLE_STATUSES, RUNNING_STATUSES, WAITING_USER_STATUSES


def runs_payload(app: HarnessApp, *, task_id: str | None = None, limit: int = 50) -> dict:
    return {"items": app.store.list_runs(task_id=task_id, limit=limit)}


def task_logs_payload(app: HarnessApp, task_id: str) -> dict:
    task = app.store.get_task(task_id)
    if not task:
        raise KeyError(task_id)
    runs = app.store.list_runs(task_id=task_id, limit=100)
    events = app.store.list_events(task_id=task_id, limit=300)
    agent_events = app.store.list_agent_run_events(task_id=task_id, limit=500)
    return {"task": task, "runs": runs, "events": events, "agentEvents": agent_events}


def run_log_payload(app: HarnessApp, run_id: str) -> dict:
    runs = [run for run in app.store.list_runs(limit=500) if run["id"] == run_id]
    if not runs:
        raise KeyError(run_id)
    run = runs[0]
    prompt = _read_text_if_safe(app, run.get("prompt_path"))
    log = _read_text_if_safe(app, run.get("log_path"))
    event_log = _read_text_if_safe(app, run.get("event_log_path"))
    events = app.store.list_agent_run_events(run_id=run_id, limit=500)
    return {"run": run, "prompt": prompt, "log": log, "eventLog": event_log, "events": events}


def scheduler_health(app: HarnessApp) -> dict:
    metrics = app.store.metrics()
    return {
        "status": "ok",
        "dispatchableStatuses": sorted(DISPATCHABLE_STATUSES),
        "runningStatuses": sorted(RUNNING_STATUSES),
        "waitingUserStatuses": sorted(WAITING_USER_STATUSES),
        "metrics": metrics,
        "agents": app.store.list_agent_profiles(),
    }


def _read_text_if_safe(app: HarnessApp, path: str | None) -> str:
    if not path:
        return ""
    candidate = Path(path).resolve()
    root = app.paths.workspace_root.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return "[日志路径不在任务工作区内，已拒绝读取]"
    if not candidate.exists() or not candidate.is_file():
        return ""
    return candidate.read_text(encoding="utf-8", errors="replace")
