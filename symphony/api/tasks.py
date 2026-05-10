from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from symphony.api.app import HarnessApp
from symphony.api.agents import agent_health_payload, agents_payload, update_agent_binding, upsert_agent
from symphony.api.artifacts import artifact_metadata, render_artifact
from symphony.api.pages import AGENTS_PAGE, LOGS_PAGE, MONITOR_PAGE, SKILLS_PAGE, TASKS_PAGE, WORKFLOW_PAGE
from symphony.api.runs import run_log_payload, runs_payload, scheduler_health, task_logs_payload
from symphony.api.trace import task_trace_payload, trace_step_payload
from symphony.api.workflow import preview_stage_prompt, update_stage_prompt, workflow_payload
from symphony.domain.workflow import ALL_TASK_STATUSES, DISPATCHABLE_STATUSES, RUNNING_STATUSES, STAGES, WAITING_USER_STATUSES
from symphony.storage.db import utc_now


class HarnessRequestHandler(BaseHTTPRequestHandler):
    app: HarnessApp

    def log_message(self, format: str, *args):  # noqa: A002
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
                return
            if path in {"/", "/tasks"}:
                self._send_html(TASKS_PAGE)
                return
            if path == "/workflow":
                self._send_html(WORKFLOW_PAGE)
                return
            if path == "/agents":
                self._send_html(AGENTS_PAGE)
                return
            if path == "/skills":
                self._send_html(SKILLS_PAGE)
                return
            if path == "/monitor":
                self._send_html(MONITOR_PAGE)
                return
            if path == "/logs":
                self._send_html(LOGS_PAGE)
                return
            if path == "/api/skills":
                self._send_json({"items": self.app.store.list_skills()})
                return
            if path == "/api/agents":
                self._send_json(agents_payload(self.app))
                return
            if path.startswith("/api/agents/") and path.endswith("/health"):
                profile_id = unquote(path.strip("/").split("/")[2])
                self._send_json(agent_health_payload(self.app, profile_id))
                return
            if path == "/api/tasks":
                qs = parse_qs(parsed.query)
                metric = self._one(qs, "metric")
                statuses = self._metric_statuses(metric) if metric else None
                tasks = self.app.store.list_tasks(status=self._one(qs, "status"), statuses=statuses, phase=self._one(qs, "phase"), q=self._one(qs, "q"))
                self._send_json({"metrics": self.app.store.metrics(), "items": [self._task_view(t) for t in tasks]})
                return
            if path == "/api/workflow":
                self._send_json(workflow_payload(self.app))
                return
            if path == "/api/runs":
                qs = parse_qs(parsed.query)
                limit = int(self._one(qs, "limit") or 50)
                self._send_json(runs_payload(self.app, task_id=self._one(qs, "taskId"), limit=limit))
                return
            if path.startswith("/api/runs/") and path.endswith("/logs"):
                run_id = unquote(path.strip("/").split("/")[2])
                self._send_json(run_log_payload(self.app, run_id))
                return
            if path.startswith("/api/trace/steps/"):
                step_id = unquote(path.strip("/").split("/")[3])
                self._send_json(trace_step_payload(self.app, step_id))
                return
            if path == "/api/scheduler/health":
                self._send_json(scheduler_health(self.app))
                return
            if path.startswith("/api/tasks/"):
                parts = path.strip("/").split("/")
                task_id = unquote(parts[2])
                if len(parts) == 3:
                    self._send_json(self.app.task_payload(task_id))
                    return
                if len(parts) == 4 and parts[3] == "artifact":
                    self._send_json(artifact_metadata(self.app, task_id))
                    return
                if len(parts) == 4 and parts[3] == "prompt":
                    task = self._get_task_or_404(task_id)
                    self._send_json({"prompt": self.app.prompt_builder.build(task)})
                    return
                if len(parts) == 4 and parts[3] == "runs":
                    self._send_json(runs_payload(self.app, task_id=task_id))
                    return
                if len(parts) == 4 and parts[3] == "logs":
                    self._send_json(task_logs_payload(self.app, task_id))
                    return
                if len(parts) == 4 and parts[3] == "trace":
                    self._send_json(task_trace_payload(self.app, task_id))
                    return
            if path.startswith("/tasks/") and path.endswith("/artifact"):
                task_id = unquote(path.split("/")[2])
                self._send_html(render_artifact(self.app, task_id))
                return
            self._send_json({"error": {"code": "not_found", "message": "not found"}}, HTTPStatus.NOT_FOUND)
        except KeyError:
            self._send_json({"error": {"code": "not_found", "message": "task not found"}}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send_json({"error": {"code": "server_error", "message": str(exc)}}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/tasks":
                task = self.app.create_task(self._read_json())
                self._send_json({"task": self._task_view(task)}, HTTPStatus.CREATED)
                return
            if path == "/api/skills":
                skill = self.app.store.upsert_skill(self._read_json())
                self._send_json({"skill": skill}, HTTPStatus.CREATED)
                return
            if path == "/api/agents":
                self._send_json(upsert_agent(self.app, self._read_json()), HTTPStatus.CREATED)
                return
            if path.startswith("/api/workflow/stages/") and path.endswith("/preview"):
                parts = path.strip("/").split("/")
                stage = unquote(parts[3])
                self._send_json(preview_stage_prompt(self.app, stage, self._read_json()))
                return
            if path.startswith("/api/tasks/"):
                parts = path.strip("/").split("/")
                task_id = unquote(parts[2])
                if len(parts) == 4 and parts[3] == "comments":
                    task = self._get_task_or_404(task_id)
                    payload = self.app.comments.add_comment(task["artifact_path"], self._read_json())
                    self._send_json(payload, HTTPStatus.CREATED)
                    return
                if len(parts) == 6 and parts[3] == "comments" and parts[5] == "resolve":
                    task = self._get_task_or_404(task_id)
                    comment_id = unquote(parts[4])
                    payload = self.app.comments.resolve_comment(task["artifact_path"], comment_id, self._read_json())
                    self._send_json(payload)
                    return
                if len(parts) == 4 and parts[3] == "status":
                    data = self._read_json()
                    if data.get("status"):
                        task = self.app.store.force_task_status(task_id, str(data.get("status")), note=data.get("note"))
                    else:
                        task = self.app.store.transition_task(task_id, str(data.get("action") or ""), data.get("note"))
                    self._send_json({"task": self._task_view(task)})
                    return
                if len(parts) == 5 and parts[3] == "artifact" and parts[4] == "save":
                    task = self._get_task_or_404(task_id)
                    data = self._read_json(max_bytes=5 * 1024 * 1024)
                    self.app.artifacts.save_browser_snapshot(task["artifact_path"], str(data.get("html") or ""))
                    self._send_json({"saved": True, "updatedAt": utc_now()})
                    return
                if len(parts) == 5 and parts[3] == "artifact" and parts[4] in {"reset", "reset-template"}:
                    self._send_json(self.app.reset_artifact(task_id))
                    return
                if len(parts) == 5 and parts[3] == "learning" and parts[4] == "export":
                    self._send_json(self.app.export_learning_markdown(task_id))
                    return
            self._send_json({"error": {"code": "not_found", "message": "not found"}}, HTTPStatus.NOT_FOUND)
        except KeyError:
            self._send_json({"error": {"code": "not_found", "message": "task not found"}}, HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._send_json({"error": {"code": "bad_request", "message": str(exc)}}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._send_json({"error": {"code": "server_error", "message": str(exc)}}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_PUT(self) -> None:
        self._handle_workflow_update()

    def do_PATCH(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/api/tasks/") and path.endswith("/status"):
            self.do_POST()
            return
        self._handle_workflow_update()

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        try:
            if path.startswith("/api/skills/"):
                skill_id = unquote(path.strip("/").split("/")[2])
                self.app.store.delete_skill(skill_id)
                self._send_json({"deleted": True})
                return
            self._send_json({"error": {"code": "not_found", "message": "not found"}}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send_json({"error": {"code": "server_error", "message": str(exc)}}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_workflow_update(self) -> None:
        path = urlparse(self.path).path
        try:
            if path.startswith("/api/agents/bindings/"):
                stage = unquote(path.strip("/").split("/")[3])
                self._send_json(update_agent_binding(self.app, stage, self._read_json()))
                return
            if path.startswith("/api/workflow/stages/") and path.endswith("/prompt"):
                parts = path.strip("/").split("/")
                stage = unquote(parts[3])
                self._send_json(update_stage_prompt(self.app, stage, self._read_json()))
                return
            self._send_json({"error": {"code": "not_found", "message": "not found"}}, HTTPStatus.NOT_FOUND)
        except KeyError:
            self._send_json({"error": {"code": "not_found", "message": "stage not found"}}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send_json({"error": {"code": "server_error", "message": str(exc)}}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _one(self, qs: dict[str, list[str]], key: str) -> str | None:
        value = qs.get(key, [None])[0]
        return value or None

    def _metric_statuses(self, metric: str | None) -> list[str] | None:
        mapping = {
            "pending": sorted(DISPATCHABLE_STATUSES),
            "running": sorted(RUNNING_STATUSES),
            "waiting_user": sorted(WAITING_USER_STATUSES),
            "blocked": ["已阻塞"],
            "completed_today": ["已完成"],
        }
        return mapping.get(metric or "")

    def _get_task_or_404(self, task_id: str) -> dict[str, Any]:
        task = self.app.store.get_task(task_id)
        if not task:
            raise KeyError(task_id)
        return task

    def _task_view(self, task: dict[str, Any]) -> dict[str, Any]:
        return {**task, "artifact_url": f"/tasks/{task['id']}/artifact", "stage_title": STAGES.get(task.get("phase"), {}).get("title", task.get("phase")), "status_options": ALL_TASK_STATUSES}

    def _read_json(self, max_bytes: int = 1024 * 1024) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > max_bytes:
            raise ValueError("request body too large")
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw or "{}")

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
