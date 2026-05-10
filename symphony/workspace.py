from __future__ import annotations

import re
from pathlib import Path


def sanitize_workspace_key(identifier: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", identifier)


def ensure_inside_root(root: str, path: str) -> None:
    root_path = Path(root).resolve()
    child_path = Path(path).resolve()
    try:
        child_path.relative_to(root_path)
    except ValueError as exc:
        raise ValueError(f"workspace path escapes root: {child_path}") from exc
