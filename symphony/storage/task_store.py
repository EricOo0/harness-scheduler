from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from symphony.domain.workflow import (
    DEFAULT_QUALITY_GATES,
    ALL_TASK_STATUSES,
    DEFAULT_PROMPT_SECTIONS,
    DEFAULT_STAGE_PROMPTS,
    DISPATCHABLE_STATUSES,
    RUNNING_STATUSES,
    STAGES,
    STATUS_TO_STAGE,
    WAITING_USER_STATUSES,
)
from symphony.storage.db import SQLiteDatabase, utc_now
from symphony.storage.models import SCHEMA_SQL


class LocalTaskStore(SQLiteDatabase):
    """SQLite-backed local store for Harness Scheduler.

    The detailed design intentionally keeps artifacts and comments out of the
    database. This store only tracks metadata, workflow prompt config, run
    records, Skill/MCP declarations, and system events.
    """

    def __init__(self, db_path: Path):
        super().__init__(db_path)
        self.init_db()

    def init_db(self) -> None:
        with self.connection() as conn:
            conn.executescript(SCHEMA_SQL)
            self._ensure_workflow_prompt_columns(conn)
            self._ensure_agent_columns(conn)
            for stage, spec in STAGES.items():
                defaults = DEFAULT_PROMPT_SECTIONS[stage]
                conn.execute(
                    """
                    INSERT OR IGNORE INTO workflow_stage_prompts
                    (stage, objective, prompt, stage_goal, required_reads_json, allowed_actions_json, forbidden_actions_json, output_requirements_json, editable_sections_json, required_context_json, enabled_skill_names_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        stage,
                        spec["title"],
                        DEFAULT_STAGE_PROMPTS[stage],
                        defaults["stage_goal"],
                        json.dumps(defaults["required_reads"], ensure_ascii=False),
                        json.dumps(defaults["allowed_actions"], ensure_ascii=False),
                        json.dumps(defaults["forbidden_actions"], ensure_ascii=False),
                        json.dumps(defaults["output_requirements"], ensure_ascii=False),
                        json.dumps(spec["editable_sections"], ensure_ascii=False),
                        "[]",
                        "[]",
                        utc_now(),
                    ),
                )
                self._merge_default_quality_gates(conn, stage)
            self._ensure_default_agents(conn)

    def _merge_default_quality_gates(self, conn: sqlite3.Connection, stage: str) -> None:
        gates = DEFAULT_QUALITY_GATES.get(stage) or []
        defaults = DEFAULT_PROMPT_SECTIONS[stage]
        row = conn.execute("SELECT prompt, output_requirements_json FROM workflow_stage_prompts WHERE stage = ?", (stage,)).fetchone()
        if not row:
            return
        prompt = str(row["prompt"] or "")
        changed = False
        for gate in gates:
            marker = gate.splitlines()[0]
            if marker not in prompt:
                prompt = prompt.rstrip() + "\n\n" + gate
                changed = True
        output_requirements = self._json_list(row["output_requirements_json"])
        superseded_output_items = {
            "breakdown": {"建议下一状态为拆解评审或已阻塞"},
        }
        old_items = superseded_output_items.get(stage, set())
        if old_items:
            output_requirements = [item for item in output_requirements if item not in old_items]
            changed = True
        for item in defaults["output_requirements"]:
            if item not in output_requirements:
                output_requirements.append(item)
                changed = True
        if changed:
            conn.execute(
                "UPDATE workflow_stage_prompts SET prompt = ?, output_requirements_json = ?, updated_at = ? WHERE stage = ?",
                (prompt, json.dumps(output_requirements, ensure_ascii=False), utc_now(), stage),
            )

    def _ensure_workflow_prompt_columns(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(workflow_stage_prompts)").fetchall()}
        additions = {
            "stage_goal": "TEXT NOT NULL DEFAULT ''",
            "required_reads_json": "TEXT NOT NULL DEFAULT '[]'",
            "allowed_actions_json": "TEXT NOT NULL DEFAULT '[]'",
            "forbidden_actions_json": "TEXT NOT NULL DEFAULT '[]'",
            "output_requirements_json": "TEXT NOT NULL DEFAULT '[]'",
        }
        for name, definition in additions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE workflow_stage_prompts ADD COLUMN {name} {definition}")

    def _ensure_agent_columns(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(task_runs)").fetchall()}
        additions = {
            "agent_profile_id": "TEXT",
            "agent_session_id": "TEXT",
            "attempt": "INTEGER NOT NULL DEFAULT 1",
            "command_json": "TEXT NOT NULL DEFAULT '{}'",
            "event_log_path": "TEXT",
            "usage_json": "TEXT NOT NULL DEFAULT '{}'",
            "external_run_id": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE task_runs ADD COLUMN {name} {definition}")

    def _ensure_default_agents(self, conn: sqlite3.Connection) -> None:
        now = utc_now()
        defaults = [
            ("agent_mock", "Mock Agent", "mock", "mock", [], {}, None, 1, 1, 1800, 1),
            ("agent_codex", "Codex App Server", "codex_app_server", "codex", ["app-server"], {}, None, 1, 1, 3600, 1),
            ("agent_claude", "Claude Code", "claude_cli", "claude", [], {}, None, 1, 1, 3600, 1),
        ]
        for row in defaults:
            conn.execute(
                """
                INSERT OR IGNORE INTO agent_profiles
                (id, name, kind, command, args_json, env_json, model, enabled, max_concurrency, timeout_seconds, dangerously_skip_permissions, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*row[:4], json.dumps(row[4], ensure_ascii=False), json.dumps(row[5], ensure_ascii=False), *row[6:], now, now),
            )
        for stage in STAGES:
            conn.execute(
                "INSERT OR IGNORE INTO stage_agent_bindings (stage, agent_profile_id, fallback_profile_id, updated_at) VALUES (?, ?, ?, ?)",
                (stage, "agent_mock", None, now),
            )

    def append_event(self, conn: sqlite3.Connection, task_id: str | None, event_type: str, payload: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO event_logs (id, task_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (f"evt_{uuid.uuid4().hex}", task_id, event_type, json.dumps(payload, ensure_ascii=False), utc_now()),
        )

    def record_event(self, task_id: str | None, event_type: str, payload: dict[str, Any]) -> None:
        with self.connection() as conn:
            self.append_event(conn, task_id, event_type, payload)

    def list_events(self, *, task_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        sql = "SELECT * FROM event_logs"
        args: list[Any] = []
        if task_id:
            sql += " WHERE task_id = ?"
            args.append(task_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self.connection() as conn:
            rows = [dict(row) for row in conn.execute(sql, args).fetchall()]
        for row in rows:
            row["payload"] = json.loads(row.pop("payload_json") or "{}")
        return rows

    def create_task(
        self,
        *,
        task_id: str,
        title: str,
        description: str,
        priority: str | None,
        repository_path: str | None,
        target_branch: str | None,
        workspace_path: str,
        artifact_path: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO tasks
                (id, title, description, status, phase, priority, repository_path, target_branch, workspace_path, artifact_path, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, title, description, "待规划", "planning", priority, repository_path, target_branch, workspace_path, artifact_path, now, now),
            )
            self.append_event(conn, task_id, "task_created", {"title": title, "artifact_path": artifact_path})
        task = self.get_task(task_id)
        assert task is not None
        return task

    def list_tasks(self, *, status: str | None = None, statuses: list[str] | None = None, phase: str | None = None, q: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM tasks WHERE 1=1"
        args: list[Any] = []
        if status:
            sql += " AND status = ?"
            args.append(status)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            sql += f" AND status IN ({placeholders})"
            args.extend(statuses)
        if phase:
            sql += " AND phase = ?"
            args.append(phase)
        if q:
            sql += " AND (title LIKE ? OR description LIKE ? OR repository_path LIKE ?)"
            like = f"%{q}%"
            args.extend([like, like, like])
        sql += " ORDER BY created_at DESC"
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(sql, args).fetchall()]

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return dict(row) if row else None

    def transition_task(self, task_id: str, action: str, note: str | None = None) -> dict[str, Any]:
        task = self.get_task(task_id)
        if not task:
            raise KeyError(task_id)
        status = task["status"]
        stage = STATUS_TO_STAGE.get(status)
        if stage not in STAGES:
            raise ValueError(f"当前状态不支持状态动作：{status}")
        spec = STAGES[stage]
        if status != spec["review"]:
            raise ValueError(f"当前状态不是用户确认态：{status}")
        if action == "confirm":
            next_status = spec["confirm"]
        elif action == "request_changes" and spec["request_changes"]:
            next_status = spec["request_changes"]
        else:
            raise ValueError(f"当前阶段不支持动作：{action}")
        next_phase = STATUS_TO_STAGE.get(next_status, stage)
        with self.connection() as conn:
            conn.execute("UPDATE tasks SET status = ?, phase = ?, updated_at = ? WHERE id = ?", (next_status, next_phase, utc_now(), task_id))
            self.append_event(conn, task_id, "task_status_changed", {"from": status, "to": next_status, "action": action, "note": note})
        updated = self.get_task(task_id)
        assert updated is not None
        return updated

    def update_task_status(self, task_id: str, status: str, *, blocked_reason: str | None = None) -> dict[str, Any]:
        phase = STATUS_TO_STAGE.get(status, "blocked" if status == "已阻塞" else "done" if status == "已完成" else "planning")
        with self.connection() as conn:
            conn.execute("UPDATE tasks SET status = ?, phase = ?, blocked_reason = ?, updated_at = ? WHERE id = ?", (status, phase, blocked_reason, utc_now(), task_id))
            self.append_event(conn, task_id, "task_status_set", {"to": status, "blocked_reason": blocked_reason})
        updated = self.get_task(task_id)
        assert updated is not None
        return updated

    def force_task_status(self, task_id: str, status: str, *, note: str | None = None) -> dict[str, Any]:
        if status not in ALL_TASK_STATUSES:
            raise ValueError(f"不支持的任务状态：{status}")
        task = self.get_task(task_id)
        if not task:
            raise KeyError(task_id)
        blocked_reason = note if status in {"已阻塞", "已失败"} else None
        with self.connection() as conn:
            conn.execute(
                "UPDATE tasks SET status = ?, phase = ?, blocked_reason = ?, updated_at = ? WHERE id = ?",
                (status, STATUS_TO_STAGE[status], blocked_reason, utc_now(), task_id),
            )
            self.append_event(conn, task_id, "task_status_forced", {"from": task["status"], "to": status, "note": note})
        updated = self.get_task(task_id)
        assert updated is not None
        return updated

    def metrics(self) -> dict[str, int]:
        tasks = self.list_tasks()
        today = datetime.now(timezone.utc).date().isoformat()
        return {
            "pending": sum(1 for t in tasks if t["status"] in DISPATCHABLE_STATUSES),
            "running": sum(1 for t in tasks if t["status"] in RUNNING_STATUSES),
            "waiting_user": sum(1 for t in tasks if t["status"] in WAITING_USER_STATUSES),
            "blocked": sum(1 for t in tasks if t["status"] == "已阻塞"),
            "completed_today": sum(1 for t in tasks if t["status"] == "已完成" and str(t["updated_at"]).startswith(today)),
        }

    @staticmethod
    def _json_list(value: str | None) -> list[Any]:
        try:
            decoded = json.loads(value or "[]")
        except json.JSONDecodeError:
            return []
        return decoded if isinstance(decoded, list) else []

    def get_stage_prompt(self, stage: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM workflow_stage_prompts WHERE stage = ?", (stage,)).fetchone()
        if not row:
            return None
        data = dict(row)
        defaults = DEFAULT_PROMPT_SECTIONS[data["stage"]]
        if not data.get("stage_goal"):
            data["stage_goal"] = defaults["stage_goal"]
        data["required_reads"] = json.loads(data.pop("required_reads_json") or json.dumps(defaults["required_reads"], ensure_ascii=False))
        data["allowed_actions"] = json.loads(data.pop("allowed_actions_json") or json.dumps(defaults["allowed_actions"], ensure_ascii=False))
        data["forbidden_actions"] = json.loads(data.pop("forbidden_actions_json") or json.dumps(defaults["forbidden_actions"], ensure_ascii=False))
        data["output_requirements"] = json.loads(data.pop("output_requirements_json") or json.dumps(defaults["output_requirements"], ensure_ascii=False))
        data["editable_sections"] = json.loads(data.pop("editable_sections_json"))
        data["required_context"] = json.loads(data.pop("required_context_json", "[]"))
        data["enabled_skill_names"] = json.loads(data.pop("enabled_skill_names_json"))
        return data

    def list_stage_prompts(self) -> list[dict[str, Any]]:
        return [prompt for stage in STAGES if (prompt := self.get_stage_prompt(stage))]

    def update_stage_prompt(self, stage: str, data: dict[str, Any]) -> dict[str, Any]:
        if stage not in STAGES:
            raise KeyError(stage)
        current = self.get_stage_prompt(stage) or {}
        prompt = str(data.get("prompt", current.get("prompt") or DEFAULT_STAGE_PROMPTS[stage]))
        objective = str(data.get("objective", current.get("objective") or STAGES[stage]["title"]))
        defaults = DEFAULT_PROMPT_SECTIONS[stage]
        stage_goal = str(data.get("stage_goal", current.get("stage_goal") or defaults["stage_goal"]))
        required_reads = data.get("required_reads", current.get("required_reads") or defaults["required_reads"])
        allowed_actions = data.get("allowed_actions", current.get("allowed_actions") or defaults["allowed_actions"])
        forbidden_actions = data.get("forbidden_actions", current.get("forbidden_actions") or defaults["forbidden_actions"])
        output_requirements = data.get("output_requirements", current.get("output_requirements") or defaults["output_requirements"])
        editable_sections = data.get("editable_sections", current.get("editable_sections") or STAGES[stage]["editable_sections"])
        required_context = data.get("required_context", current.get("required_context") or [])
        skills = data.get("enabled_skill_names", current.get("enabled_skill_names") or [])
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO workflow_stage_prompts
                (stage, objective, prompt, stage_goal, required_reads_json, allowed_actions_json, forbidden_actions_json, output_requirements_json, editable_sections_json, required_context_json, enabled_skill_names_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(stage) DO UPDATE SET
                  objective=excluded.objective,
                  prompt=excluded.prompt,
                  stage_goal=excluded.stage_goal,
                  required_reads_json=excluded.required_reads_json,
                  allowed_actions_json=excluded.allowed_actions_json,
                  forbidden_actions_json=excluded.forbidden_actions_json,
                  output_requirements_json=excluded.output_requirements_json,
                  editable_sections_json=excluded.editable_sections_json,
                  required_context_json=excluded.required_context_json,
                  enabled_skill_names_json=excluded.enabled_skill_names_json,
                  updated_at=excluded.updated_at
                """,
                (
                    stage,
                    objective,
                    prompt,
                    stage_goal,
                    json.dumps(required_reads, ensure_ascii=False),
                    json.dumps(allowed_actions, ensure_ascii=False),
                    json.dumps(forbidden_actions, ensure_ascii=False),
                    json.dumps(output_requirements, ensure_ascii=False),
                    json.dumps(editable_sections, ensure_ascii=False),
                    json.dumps(required_context, ensure_ascii=False),
                    json.dumps(skills, ensure_ascii=False),
                    utc_now(),
                ),
            )
        updated = self.get_stage_prompt(stage)
        assert updated is not None
        return updated

    def recent_runs(self, task_id: str, limit: int = 5) -> list[dict[str, Any]]:
        with self.connection() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM task_runs WHERE task_id = ? ORDER BY started_at DESC LIMIT ?", (task_id, limit)).fetchall()]

    def list_runs(self, *, task_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM task_runs"
        args: list[Any] = []
        if task_id:
            sql += " WHERE task_id = ?"
            args.append(task_id)
        sql += " ORDER BY started_at DESC LIMIT ?"
        args.append(limit)
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(sql, args).fetchall()]

    def create_run(self, task_id: str, stage: str, status: str, prompt_path: str | None = None) -> str:
        run_id = f"run_{uuid.uuid4().hex}"
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO task_runs (id, task_id, stage, status, prompt_path, started_at) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, task_id, stage, status, prompt_path, utc_now()),
            )
            conn.execute("UPDATE tasks SET latest_run_id = ?, updated_at = ? WHERE id = ?", (run_id, utc_now(), task_id))
            self.append_event(conn, task_id, "run_created", {"run_id": run_id, "stage": stage, "status": status})
        return run_id

    def update_run_paths(self, run_id: str, *, prompt_path: str | None = None, log_path: str | None = None) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE task_runs SET prompt_path = COALESCE(?, prompt_path), log_path = COALESCE(?, log_path) WHERE id = ?",
                (prompt_path, log_path, run_id),
            )

    def finish_run(self, run_id: str, status: str, error: str | None = None) -> None:
        with self.connection() as conn:
            conn.execute("UPDATE task_runs SET status = ?, error = ?, finished_at = ? WHERE id = ?", (status, error, utc_now(), run_id))
            row = conn.execute("SELECT task_id, stage FROM task_runs WHERE id = ?", (run_id,)).fetchone()
            if row:
                self.append_event(conn, row["task_id"], "run_finished", {"run_id": run_id, "stage": row["stage"], "status": status, "error": error})

    def list_skills(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM skill_refs ORDER BY name").fetchall()]

    def upsert_skill(self, data: dict[str, Any]) -> dict[str, Any]:
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("skill name required")
        skill_id = str(data.get("id") or f"skill_{uuid.uuid4().hex[:8]}")
        enabled = 1 if data.get("enabled", True) else 0
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO skill_refs (id, name, path, description, enabled, prompt_hint)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  name=excluded.name, path=excluded.path, description=excluded.description,
                  enabled=excluded.enabled, prompt_hint=excluded.prompt_hint
                """,
                (skill_id, name, data.get("path"), data.get("description"), enabled, data.get("prompt_hint") or data.get("promptHint")),
            )
            row = conn.execute("SELECT * FROM skill_refs WHERE id = ?", (skill_id,)).fetchone()
        return dict(row) if row else {}

    def delete_skill(self, skill_id: str) -> None:
        with self.connection() as conn:
            conn.execute("DELETE FROM skill_refs WHERE id = ?", (skill_id,))

    def list_agent_profiles(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = [dict(row) for row in conn.execute("SELECT * FROM agent_profiles ORDER BY name").fetchall()]
        return [self._decode_agent_profile(row) for row in rows]

    def get_agent_profile(self, profile_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM agent_profiles WHERE id = ?", (profile_id,)).fetchone()
        return self._decode_agent_profile(dict(row)) if row else None

    def upsert_agent_profile(self, data: dict[str, Any]) -> dict[str, Any]:
        name = str(data.get("name") or "").strip()
        kind = str(data.get("kind") or "").strip()
        command = str(data.get("command") or "").strip()
        if not name or not kind or not command:
            raise ValueError("agent name, kind and command are required")
        profile_id = str(data.get("id") or f"agent_{uuid.uuid4().hex[:8]}")
        now = utc_now()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO agent_profiles
                (id, name, kind, command, args_json, env_json, model, enabled, max_concurrency, timeout_seconds, dangerously_skip_permissions, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  name=excluded.name, kind=excluded.kind, command=excluded.command,
                  args_json=excluded.args_json, env_json=excluded.env_json, model=excluded.model,
                  enabled=excluded.enabled, max_concurrency=excluded.max_concurrency,
                  timeout_seconds=excluded.timeout_seconds,
                  dangerously_skip_permissions=excluded.dangerously_skip_permissions,
                  updated_at=excluded.updated_at
                """,
                (
                    profile_id,
                    name,
                    kind,
                    command,
                    json.dumps(data.get("args") or [], ensure_ascii=False),
                    json.dumps(data.get("env") or {}, ensure_ascii=False),
                    data.get("model"),
                    1 if data.get("enabled", True) else 0,
                    int(data.get("max_concurrency") or 1),
                    int(data.get("timeout_seconds") or 1800),
                    1 if data.get("dangerously_skip_permissions", True) else 0,
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM agent_profiles WHERE id = ?", (profile_id,)).fetchone()
        return self._decode_agent_profile(dict(row)) if row else {}

    def list_stage_agent_bindings(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = [dict(row) for row in conn.execute("SELECT * FROM stage_agent_bindings ORDER BY stage").fetchall()]
        return rows

    def update_stage_agent_binding(self, stage: str, agent_profile_id: str, fallback_profile_id: str | None = None) -> dict[str, Any]:
        if stage not in STAGES:
            raise KeyError(stage)
        if not self.get_agent_profile(agent_profile_id):
            raise KeyError(agent_profile_id)
        if fallback_profile_id and not self.get_agent_profile(fallback_profile_id):
            raise KeyError(fallback_profile_id)
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO stage_agent_bindings (stage, agent_profile_id, fallback_profile_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(stage) DO UPDATE SET
                  agent_profile_id=excluded.agent_profile_id,
                  fallback_profile_id=excluded.fallback_profile_id,
                  updated_at=excluded.updated_at
                """,
                (stage, agent_profile_id, fallback_profile_id, utc_now()),
            )
            row = conn.execute("SELECT * FROM stage_agent_bindings WHERE stage = ?", (stage,)).fetchone()
        return dict(row) if row else {}

    def resolve_agent_profile(self, task: dict[str, Any], stage: str) -> dict[str, Any] | None:
        task_profile_id = task.get("agent_profile_id")
        if task_profile_id:
            profile = self.get_agent_profile(str(task_profile_id))
            if profile and profile.get("enabled"):
                return profile
        with self.connection() as conn:
            binding = conn.execute("SELECT * FROM stage_agent_bindings WHERE stage = ?", (stage,)).fetchone()
        if binding:
            profile = self.get_agent_profile(binding["agent_profile_id"])
            if profile and profile.get("enabled"):
                return profile
        profile = self.get_agent_profile("agent_mock")
        return profile if profile and profile.get("enabled") else None

    def create_agent_session(self, *, agent_profile_id: str, task_id: str, stage: str) -> str:
        session_id = f"ags_{uuid.uuid4().hex}"
        now = utc_now()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO agent_sessions
                (id, agent_profile_id, task_id, stage, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, agent_profile_id, task_id, stage, "running", now, now),
            )
        return session_id

    def update_agent_session(self, session_id: str, *, external_session_id: str | None = None, status: str | None = None) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE agent_sessions
                SET external_session_id = COALESCE(?, external_session_id),
                    status = COALESCE(?, status),
                    updated_at = ?
                WHERE id = ?
                """,
                (external_session_id, status, utc_now(), session_id),
            )

    def latest_external_session_id(self, task_id: str, stage: str, agent_profile_id: str) -> str | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT external_session_id FROM agent_sessions
                WHERE task_id = ? AND stage = ? AND agent_profile_id = ? AND external_session_id IS NOT NULL
                ORDER BY updated_at DESC LIMIT 1
                """,
                (task_id, stage, agent_profile_id),
            ).fetchone()
        return row["external_session_id"] if row else None

    def append_agent_run_event(self, *, run_id: str, task_id: str, agent_profile_id: str, event_type: str, payload: dict[str, Any]) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO agent_run_events
                (id, run_id, task_id, agent_profile_id, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (f"are_{uuid.uuid4().hex}", run_id, task_id, agent_profile_id, event_type, json.dumps(payload, ensure_ascii=False), utc_now()),
            )

    def list_agent_run_events(self, *, run_id: str | None = None, task_id: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        sql = "SELECT * FROM agent_run_events WHERE 1=1"
        args: list[Any] = []
        if run_id:
            sql += " AND run_id = ?"
            args.append(run_id)
        if task_id:
            sql += " AND task_id = ?"
            args.append(task_id)
        sql += " ORDER BY created_at ASC LIMIT ?"
        args.append(limit)
        with self.connection() as conn:
            rows = [dict(row) for row in conn.execute(sql, args).fetchall()]
        for row in rows:
            row["payload"] = json.loads(row.pop("payload_json") or "{}")
        return rows

    def update_run_agent(self, run_id: str, *, agent_profile_id: str, event_log_path: str, command: dict[str, Any]) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE task_runs SET agent_profile_id = ?, event_log_path = ?, command_json = ? WHERE id = ?",
                (agent_profile_id, event_log_path, json.dumps(command, ensure_ascii=False), run_id),
            )

    def update_run_result(self, run_id: str, *, usage: dict[str, Any] | None = None, external_run_id: str | None = None, agent_session_id: str | None = None) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE task_runs
                SET usage_json = COALESCE(?, usage_json),
                    external_run_id = COALESCE(?, external_run_id),
                    agent_session_id = COALESCE(?, agent_session_id)
                WHERE id = ?
                """,
                (json.dumps(usage, ensure_ascii=False) if usage is not None else None, external_run_id, agent_session_id, run_id),
            )

    @staticmethod
    def _decode_agent_profile(row: dict[str, Any]) -> dict[str, Any]:
        row["args"] = json.loads(row.pop("args_json") or "[]")
        row["env"] = json.loads(row.pop("env_json") or "{}")
        row["enabled"] = bool(row.get("enabled"))
        row["dangerously_skip_permissions"] = bool(row.get("dangerously_skip_permissions"))
        return row


HarnessStore = LocalTaskStore
