"""Agent create and delete shared by every door (Teams, runtime_rpc ``agents.*``, admin).

The registry (``agent_registry``) is the record; the agent's S3 Hermes profile is what makes it run.
"""

from __future__ import annotations

from typing import Any

from core.services import agent_registry

FINAL_ROOT_CEO_CONFLICT = "Lightbulb's coordinator cannot be deleted or demoted."


class AgentLifecycleConflict(RuntimeError):
    """A requested mutation would leave the workspace without a root CEO."""


def _is_root_ceo(agent: dict) -> bool:
    return agent.get("role") == "ceo" and not agent.get("reportsTo")


def ensure_root_ceo_survives(*, target: dict, agents: list[dict]) -> None:
    if _is_root_ceo(target) and not any(
        agent.get("id") != target.get("id") and _is_root_ceo(agent) for agent in agents
    ):
        raise AgentLifecycleConflict(FINAL_ROOT_CEO_CONFLICT)


async def delete_agent_everywhere(*, owner_id: str, agent_id: str) -> dict[str, Any]:
    """Delete an agent's Hermes profile, then its registry row. Idempotent.

    The profile goes first: if it fails, the row stays visible and a retry finishes the job.
    """
    from core.dynamodb import run_in_thread
    from core.repositories.paperclip_repo import PaperclipRepo
    from core.services import hermes_profiles, network_member
    from core.services.owner_facts import write_owner_facts

    roster = await agent_registry.roster(owner_id)
    target = next((a for a in roster if a["id"] == agent_id), None)
    if target is not None:
        ensure_root_ceo_survives(target=target, agents=roster)
    await run_in_thread(hermes_profiles.delete_profile, owner_id, agent_id)
    await agent_registry.delete(owner_id, agent_id)
    await PaperclipRepo(table_name="paperclip-companies").remove_starter_agent_id(owner_id, agent_id)
    await write_owner_facts(owner_id)  # never raises; no survivor keeps a ghost teammate
    await network_member.publish_sessions(owner_id)  # and no network address for a deleted agent
    return {"deleted": True, "agent_id": agent_id}
