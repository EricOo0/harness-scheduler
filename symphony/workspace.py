from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from .config import ServiceConfig
from .errors import WorkspaceError
from .models import Workspace


def sanitize_workspace_key(identifier: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", identifier)


def ensure_inside_root(root: str, path: str) -> None:
    root_path = Path(root).resolve()
    child_path = Path(path).resolve()
    try:
        child_path.relative_to(root_path)
    except ValueError as exc:
        raise WorkspaceError(f"workspace path escapes root: {child_path}", code="workspace_path_outside_root") from exc


class WorkspaceManager:
    def __init__(self, config: ServiceConfig):
        self.config = config

    @property
    def root(self) -> Path:
        return Path(self.config.workspace_root).resolve()

    def path_for_issue(self, identifier: str) -> Path:
        key = sanitize_workspace_key(identifier)
        path = self.root / key
        ensure_inside_root(str(self.root), str(path))
        return path

    def create_for_issue(self, identifier: str) -> Workspace:
        key = sanitize_workspace_key(identifier)
        path = self.root / key
        ensure_inside_root(str(self.root), str(path))
        created_now = False
        if path.exists() and not path.is_dir():
            raise WorkspaceError(f"workspace path exists and is not a directory: {path}", code="workspace_not_directory")
        if not path.exists():
            path.mkdir(parents=True, exist_ok=False)
            created_now = True
        workspace = Workspace(path=str(path), workspace_key=key, created_now=created_now)
        if created_now and self.config.hooks.after_create:
            self.run_hook("after_create", workspace.path, fatal=True)
        return workspace

    def remove_for_issue(self, identifier: str) -> None:
        path = self.path_for_issue(identifier)
        if path.exists():
            if self.config.hooks.before_remove:
                self.run_hook("before_remove", str(path), fatal=False)
            shutil.rmtree(path)

    def run_hook(self, hook_name: str, cwd: str, *, fatal: bool) -> bool:
        script = getattr(self.config.hooks, hook_name)
        if not script:
            return True
        ensure_inside_root(str(self.root), cwd)
        timeout = max(self.config.hooks.timeout_ms / 1000.0, 0.001)
        try:
            subprocess.run(
                ["sh", "-lc", script],
                cwd=cwd,
                timeout=timeout,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            return True
        except Exception as exc:
            if fatal:
                raise WorkspaceError(f"{hook_name} hook failed: {exc}", code=f"{hook_name}_hook_failed") from exc
            return False

    def validate_agent_cwd(self, workspace_path: str, cwd: str) -> None:
        if os.path.realpath(workspace_path) != os.path.realpath(cwd):
            raise WorkspaceError("agent cwd must equal workspace path", code="invalid_workspace_cwd")
        ensure_inside_root(str(self.root), workspace_path)
