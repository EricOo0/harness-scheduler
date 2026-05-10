from __future__ import annotations

from symphony.api.app import HarnessApp
from symphony.domain.workflow import STAGES


def agents_payload(app: HarnessApp) -> dict:
    return {
        "items": app.store.list_agent_profiles(),
        "bindings": app.store.list_stage_agent_bindings(),
        "stageOrder": list(STAGES.keys()),
        "stageNames": {stage: spec["title"] for stage, spec in STAGES.items()},
    }


def upsert_agent(app: HarnessApp, data: dict) -> dict:
    return {"agent": app.store.upsert_agent_profile(data)}


def update_agent_binding(app: HarnessApp, stage: str, data: dict) -> dict:
    return {
        "binding": app.store.update_stage_agent_binding(
            stage,
            str(data.get("agent_profile_id") or data.get("agentProfileId") or ""),
            data.get("fallback_profile_id") or data.get("fallbackProfileId"),
        )
    }


def agent_health_payload(app: HarnessApp, profile_id: str) -> dict:
    profile = app.store.get_agent_profile(profile_id)
    if not profile:
        raise KeyError(profile_id)
    # Health here is intentionally non-invasive: it verifies configuration is
    # present without launching a real coding agent from a GET request.
    return {
        "id": profile["id"],
        "name": profile["name"],
        "kind": profile["kind"],
        "enabled": profile["enabled"],
        "command": profile["command"],
        "dangerously_skip_permissions": profile["dangerously_skip_permissions"],
        "status": "configured" if profile["enabled"] else "disabled",
    }
