from __future__ import annotations

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  phase TEXT NOT NULL,
  priority TEXT,
  repository_path TEXT,
  target_branch TEXT,
  workspace_path TEXT NOT NULL,
  artifact_path TEXT NOT NULL,
  latest_run_id TEXT,
  blocked_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workflow_stage_prompts (
  stage TEXT PRIMARY KEY,
  objective TEXT NOT NULL,
  prompt TEXT NOT NULL,
  stage_goal TEXT NOT NULL DEFAULT '',
  required_reads_json TEXT NOT NULL DEFAULT '[]',
  allowed_actions_json TEXT NOT NULL DEFAULT '[]',
  forbidden_actions_json TEXT NOT NULL DEFAULT '[]',
  output_requirements_json TEXT NOT NULL DEFAULT '[]',
  editable_sections_json TEXT NOT NULL,
  required_context_json TEXT NOT NULL DEFAULT '[]',
  enabled_skill_names_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS skill_refs (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  path TEXT,
  description TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  prompt_hint TEXT
);
CREATE TABLE IF NOT EXISTS task_runs (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  stage TEXT NOT NULL,
  status TEXT NOT NULL,
  agent_profile_id TEXT,
  agent_session_id TEXT,
  attempt INTEGER NOT NULL DEFAULT 1,
  command_json TEXT NOT NULL DEFAULT '{}',
  prompt_path TEXT,
  log_path TEXT,
  event_log_path TEXT,
  usage_json TEXT NOT NULL DEFAULT '{}',
  external_run_id TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  error TEXT
);
CREATE TABLE IF NOT EXISTS event_logs (
  id TEXT PRIMARY KEY,
  task_id TEXT,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_profiles (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  command TEXT NOT NULL,
  args_json TEXT NOT NULL DEFAULT '[]',
  env_json TEXT NOT NULL DEFAULT '{}',
  model TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  max_concurrency INTEGER NOT NULL DEFAULT 1,
  timeout_seconds INTEGER NOT NULL DEFAULT 1800,
  dangerously_skip_permissions INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stage_agent_bindings (
  stage TEXT PRIMARY KEY,
  agent_profile_id TEXT NOT NULL,
  fallback_profile_id TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_sessions (
  id TEXT PRIMARY KEY,
  agent_profile_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  stage TEXT NOT NULL,
  external_session_id TEXT,
  external_conversation_id TEXT,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_run_events (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  agent_profile_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""
