from __future__ import annotations

from symphony.storage.task_store import LocalTaskStore


class WorkflowStore(LocalTaskStore):
    """Compatibility store focused on workflow prompt operations."""
