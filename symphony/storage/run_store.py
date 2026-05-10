from __future__ import annotations

from symphony.storage.task_store import LocalTaskStore


class RunStore(LocalTaskStore):
    """Compatibility store focused on task run operations."""
