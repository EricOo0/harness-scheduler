from __future__ import annotations

from symphony.agents.runtime import AgentRuntime
from symphony.artifacts.file_manager import ArtifactFileManager
from symphony.domain.workflow import ALLOWED_NEXT_STATUSES, DISPATCHABLE_STATUSES, STAGES
from symphony.scheduler.prompt_builder import PromptBuilder
from symphony.storage.task_store import LocalTaskStore


class SchedulerRuntime:
    LOG_SCHEMA_VERSION = "task-run-log-v2"

    def __init__(self, *, store: LocalTaskStore, artifacts: ArtifactFileManager, prompt_builder: PromptBuilder, agent_runtime: AgentRuntime):
        self.store = store
        self.artifacts = artifacts
        self.prompt_builder = prompt_builder
        self.agent_runtime = agent_runtime

    def process_dispatchable_once(self) -> int:
        processed = 0
        for task in self.store.list_tasks():
            if task["status"] not in DISPATCHABLE_STATUSES:
                continue
            stage = task["phase"]
            spec = STAGES.get(stage)
            if not spec:
                continue
            run_id = self.store.create_run(task["id"], stage, "running")
            try:
                self.store.append_trace_step(
                    run_id=run_id,
                    task_id=task["id"],
                    stage=stage,
                    step_type="run.start",
                    title="Run started",
                    summary=f"Scheduler picked {spec['title']} for execution.",
                    detail={"from_status": task["status"], "stage": stage, "stage_title": spec["title"]},
                )
                self.store.record_event(task["id"], "scheduler_picked", {"stage": stage, "from_status": task["status"], "run_id": run_id})
                self.store.update_task_status(task["id"], spec["running"])
                prompt = self.prompt_builder.build(task)
                prompt_path = self._write_prompt_snapshot(task, run_id, prompt)
                prompt_step_id = self.store.append_trace_step(
                    run_id=run_id,
                    task_id=task["id"],
                    stage=stage,
                    step_type="prompt.build",
                    title="Start prompt",
                    summary=f"Generated stage prompt snapshot: {prompt_path.name}",
                    detail={"prompt_path": str(prompt_path), "prompt_chars": len(prompt), "prompt_preview": prompt[:2000]},
                )
                self.store.append_trace_artifact(
                    run_id=run_id,
                    step_id=prompt_step_id,
                    artifact_type="prompt",
                    title="Stage Prompt Snapshot",
                    path=str(prompt_path),
                    content_type="text/plain",
                    metadata={"chars": len(prompt)},
                )
                self.store.record_event(task["id"], "prompt_written", {"run_id": run_id, "prompt_path": str(prompt_path)})
                self.store.record_event(task["id"], "agent_started", {"run_id": run_id})
                result, event_log_path = self.agent_runtime.run_stage(run_id=run_id, task=task, stage=stage, stage_spec=spec, prompt=prompt)
                self.artifacts.validate_basic(task["artifact_path"])
                if result.status != "completed":
                    raise RuntimeError(result.error or result.summary)
                self.store.update_run_result(run_id, usage=result.usage, external_run_id=result.external_session_id)
                handoff_status, handoff_reason = self._resolve_handoff_status(stage, spec, result.suggested_status)
                log_path = self._write_run_log(
                    task,
                    run_id,
                    "\n".join(
                        [
                            f"run_id={run_id}",
                            f"task_id={task['id']}",
                            f"stage={stage}",
                            f"agent_status={result.status}",
                            f"event_log_path={event_log_path}",
                            f"log_schema={self.LOG_SCHEMA_VERSION}",
                            "result=completed",
                            f"suggested_status={result.suggested_status or ''}",
                            f"effective_status={handoff_status}",
                            f"handoff_reason={handoff_reason}",
                            f"message={result.summary}",
                        ]
                    ),
                )
                self.store.update_run_paths(run_id, prompt_path=str(prompt_path), log_path=str(log_path))
                self.store.update_trace_run(run_id, status="completed", summary=result.summary, suggested_status=result.suggested_status, external_run_id=result.external_session_id, error=result.error)
                latest = self.store.get_task(task["id"]) or task
                if latest["status"] == "已失败":
                    self.store.append_trace_step(
                        run_id=run_id,
                        task_id=task["id"],
                        stage=stage,
                        step_type="scheduler.handoff",
                        title="Scheduler handoff skipped",
                        source="harness",
                        summary="Task was manually marked as failed before handoff.",
                        detail={"reason": "task already failed by user", "log_path": str(log_path)},
                    )
                    self.store.record_event(task["id"], "scheduler_handoff_skipped", {"run_id": run_id, "reason": "task already failed by user", "log_path": str(log_path)})
                else:
                    self.store.update_task_status(task["id"], handoff_status, blocked_reason=result.error if handoff_status == "已阻塞" else None)
                    self.store.append_trace_step(
                        run_id=run_id,
                        task_id=task["id"],
                        stage=stage,
                        step_type="scheduler.handoff",
                        title="Scheduler handoff",
                        source="harness",
                        summary=f"Moved task to {handoff_status}.",
                        detail={
                            "to_status": handoff_status,
                            "suggested_status": result.suggested_status,
                            "reason": handoff_reason,
                            "log_path": str(log_path),
                        },
                    )
                    self.store.record_event(
                        task["id"],
                        "scheduler_handoff",
                        {
                            "run_id": run_id,
                            "to_status": handoff_status,
                            "suggested_status": result.suggested_status,
                            "reason": handoff_reason,
                            "log_path": str(log_path),
                        },
                    )
                self.store.append_trace_artifact(run_id=run_id, artifact_type="log", title="Run Log", path=str(log_path), content_type="text/plain")
                self.store.append_trace_step(
                    run_id=run_id,
                    task_id=task["id"],
                    stage=stage,
                    step_type="run.end",
                    title="Run completed",
                    summary=result.summary,
                    detail={"status": "completed", "log_path": str(log_path), "event_log_path": str(event_log_path)},
                )
                self.store.finish_run(run_id, "completed")
                processed += 1
            except Exception as exc:
                log_path = self._write_run_log(task, run_id, f"run_id={run_id}\nstage={stage}\nresult=failed\nerror={exc}")
                self.store.update_run_paths(run_id, log_path=str(log_path))
                self.store.append_trace_artifact(run_id=run_id, artifact_type="log", title="Failed Run Log", path=str(log_path), content_type="text/plain")
                self.store.append_trace_step(
                    run_id=run_id,
                    task_id=task["id"],
                    stage=stage,
                    step_type="run.error",
                    title="Run failed",
                    status="failed",
                    summary=str(exc),
                    detail={"error": str(exc), "log_path": str(log_path)},
                )
                self.store.finish_run(run_id, "failed", str(exc))
                self.store.update_task_status(task["id"], "已阻塞", blocked_reason=str(exc))
                self.store.record_event(task["id"], "scheduler_blocked", {"run_id": run_id, "error": str(exc), "log_path": str(log_path)})
        return processed

    def _resolve_handoff_status(self, stage: str, spec: dict, suggested_status: str | None) -> tuple[str, str]:
        allowed = ALLOWED_NEXT_STATUSES.get(stage, [spec["review"], "已阻塞"])
        if suggested_status and suggested_status in allowed:
            return suggested_status, "agent_suggested_status_allowed"
        if suggested_status:
            return spec["review"], f"agent_suggested_status_rejected:{suggested_status}"
        return spec["review"], "default_stage_review_status"

    def _write_prompt_snapshot(self, task: dict, run_id: str, prompt: str):
        runs_dir = self.artifacts.ensure_artifact_path(task["artifact_path"]).parent / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = runs_dir / f"{run_id}.prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        return prompt_path

    def _write_run_log(self, task: dict, run_id: str, message: str):
        runs_dir = self.artifacts.ensure_artifact_path(task["artifact_path"]).parent / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        log_path = runs_dir / f"{run_id}.log.txt"
        log_path.write_text(message, encoding="utf-8")
        return log_path
