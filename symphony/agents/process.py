from __future__ import annotations

import os
import subprocess
from pathlib import Path


def merged_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    for key, value in (extra or {}).items():
        if key:
            env[key] = value
    return env


def safe_cwd(*, repository_path: Path | None, workspace_path: Path) -> Path:
    candidate = repository_path or workspace_path
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def run_command(args: list[str], *, cwd: Path, env: dict[str, str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd),
        env=env,
        timeout=timeout_seconds,
        text=True,
        capture_output=True,
        check=False,
    )
