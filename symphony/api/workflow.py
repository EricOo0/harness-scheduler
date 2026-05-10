from __future__ import annotations

from symphony.api.app import HarnessApp
from symphony.domain.workflow import STAGES


def workflow_payload(app: HarnessApp) -> dict:
    return {"stages": app.store.list_stage_prompts(), "stageOrder": list(STAGES.keys())}


def update_stage_prompt(app: HarnessApp, stage: str, data: dict) -> dict:
    return {"stage": app.store.update_stage_prompt(stage, data)}


def preview_stage_prompt(app: HarnessApp, stage: str, data: dict) -> dict:
    task_id = str(data.get("task_id") or "")
    task = app.store.get_task(task_id)
    if not task:
        raise KeyError(task_id)
    current = app.store.get_stage_prompt(stage)
    if not current:
        raise KeyError(stage)
    draft = {**current, **(data.get("stage") or {})}
    return {"prompt": app.prompt_builder.build_for_stage(task, stage, draft)}
