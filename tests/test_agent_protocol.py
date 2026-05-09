from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import unittest
from pathlib import Path

from symphony.agent import CodexAppServerClient
from symphony.config import build_config
from symphony.models import Issue
from symphony.workflow import load_workflow
from symphony.workspace import WorkspaceManager


class AgentProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_jsonrpc_session_initializes_thread_runs_turn_and_handles_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "fake_app_server.py"
            seen = Path(tmp) / "seen.jsonl"
            app.write_text(
                f"""
import json
import sys

seen = open({str(seen)!r}, "w", encoding="utf-8")

for line in sys.stdin:
    msg = json.loads(line)
    seen.write(json.dumps(msg, sort_keys=True) + "\\n")
    seen.flush()
    method = msg.get("method")
    if method == "initialize":
        print(json.dumps({{"id": msg["id"], "result": {{"userAgent": "fake", "codexHome": "/tmp", "platformFamily": "unix", "platformOs": "macos"}}}}), flush=True)
    elif method == "thread/start":
        thread = {{"id": "thread-1", "preview": "", "turns": [], "cwd": msg["params"]["cwd"]}}
        print(json.dumps({{"id": msg["id"], "result": {{"thread": thread, "model": "fake", "modelProvider": "fake", "serviceTier": None, "cwd": msg["params"]["cwd"], "instructionSources": [], "approvalPolicy": "never", "approvalsReviewer": "user", "sandbox": {{"type": "dangerFullAccess"}}, "permissionProfile": None, "reasoningEffort": None}}}}), flush=True)
        print(json.dumps({{"method": "thread/started", "params": {{"thread": thread}}}}), flush=True)
    elif method == "turn/start":
        turn = {{"id": "turn-1", "items": [], "status": "inProgress", "error": None, "startedAt": 1, "completedAt": None, "durationMs": None}}
        print(json.dumps({{"id": msg["id"], "result": {{"turn": turn}}}}), flush=True)
        print(json.dumps({{"method": "turn/started", "params": {{"threadId": "thread-1", "turn": turn}}}}), flush=True)
        print(json.dumps({{"id": "approval-1", "method": "item/commandExecution/requestApproval", "params": {{"threadId": "thread-1", "turnId": "turn-1", "itemId": "item-1", "command": "echo ok", "cwd": msg["params"]["cwd"]}}}}), flush=True)
    elif "result" in msg and msg.get("id") == "approval-1":
        print(json.dumps({{"method": "thread/tokenUsage/updated", "params": {{"threadId": "thread-1", "turnId": "turn-1", "tokenUsage": {{"inputTokens": 3, "outputTokens": 4, "totalTokens": 7}}}}}}), flush=True)
        turn = {{"id": "turn-1", "items": [], "status": "completed", "error": None, "startedAt": 1, "completedAt": 2, "durationMs": 1000}}
        print(json.dumps({{"method": "turn/completed", "params": {{"threadId": "thread-1", "turn": turn}}}}), flush=True)
""",
                encoding="utf-8",
            )
            workflow_path = Path(tmp) / "WORKFLOW.md"
            workflow_path.write_text(
                f"""---
tracker:
  kind: linear
  api_key: token
  project_slug: P
workspace:
  root: ./ws
codex:
  command: python3 {app}
---
body
""",
                encoding="utf-8",
            )
            workflow = load_workflow(workflow_path)
            config = build_config(workflow)
            manager = WorkspaceManager(config)
            workspace = manager.create_for_issue("P-1")
            issue = Issue(id="1", identifier="P-1", title="Title", state="Todo")
            events = []
            session = CodexAppServerClient(config, manager).session(
                workspace_path=workspace.path,
                issue=issue,
                on_event=events.append,
            )
            await session.start()
            result = await session.run_turn("Do work", 1)
            await session.close()

            self.assertTrue(result.ok)
            self.assertIn("session_started", [event.event for event in events])
            self.assertIn("approval_auto_approved", [event.event for event in events])
            usage_event = next(event for event in events if event.event == "thread/tokenUsage/updated")
            self.assertEqual(usage_event.usage, {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7})

            sent = [json.loads(line) for line in seen.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(sent[0]["method"], "initialize")
            self.assertEqual(sent[1]["method"], "thread/start")
            self.assertEqual(sent[2]["method"], "turn/start")
            self.assertEqual(sent[3]["result"]["decision"], "acceptForSession")

    async def test_jsonrpc_session_handles_large_stdout_json_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "large_app_server.py"
            large_text = "x" * (1024 * 1024)
            app.write_text(
                f"""
import json
import sys

large_text = {large_text!r}

for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        print(json.dumps({{"id": msg["id"], "result": {{"userAgent": "fake", "codexHome": "/tmp", "platformFamily": "unix", "platformOs": "macos"}}}}), flush=True)
    elif method == "thread/start":
        thread = {{"id": "thread-1", "preview": "", "turns": [], "cwd": msg["params"]["cwd"]}}
        print(json.dumps({{"id": msg["id"], "result": {{"thread": thread}}}}), flush=True)
    elif method == "turn/start":
        turn = {{"id": "turn-1", "items": [], "status": "inProgress", "error": None}}
        print(json.dumps({{"id": msg["id"], "result": {{"turn": turn}}}}), flush=True)
        print(json.dumps({{"method": "thread/tokenUsage/updated", "params": {{"threadId": "thread-1", "turnId": "turn-1", "usage": {{"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}, "large": large_text}}}}), flush=True)
        completed = {{"id": "turn-1", "items": [], "status": "completed", "error": None}}
        print(json.dumps({{"method": "turn/completed", "params": {{"threadId": "thread-1", "turn": completed}}}}), flush=True)
""",
                encoding="utf-8",
            )
            workflow_path = Path(tmp) / "WORKFLOW.md"
            workflow_path.write_text(
                f"""---
tracker:
  kind: linear
  api_key: token
  project_slug: P
workspace:
  root: ./ws
codex:
  command: python3 {app}
---
body
""",
                encoding="utf-8",
            )
            workflow = load_workflow(workflow_path)
            config = build_config(workflow)
            manager = WorkspaceManager(config)
            workspace = manager.create_for_issue("P-1")
            issue = Issue(id="1", identifier="P-1", title="Title", state="Todo")
            events = []
            session = CodexAppServerClient(config, manager).session(
                workspace_path=workspace.path,
                issue=issue,
                on_event=events.append,
            )
            await session.start()
            result = await session.run_turn("Do work", 1)
            await session.close()

            self.assertTrue(result.ok)
            self.assertIn("thread/tokenUsage/updated", [event.event for event in events])


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()
