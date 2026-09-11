"""Stage write-once role contracts for the generic starter roster."""

from pathlib import Path

from core.containers.workspace import Workspace

_FILES_DIR = Path(__file__).parent / "files"
_ROLE_KEYS = ("ceo", "engineering", "finance", "growth", "analytics", "customer_operations")


async def pre_stage_starter_roster(
    *,
    owner_id: str,
    paperclip_agents: list[dict],
    starter_agent_ids: dict[str, str],
    workspace: Workspace,
) -> None:
    """Write each live starter agent's role contract without replacing edits."""
    live_ids = {agent.get("id") for agent in paperclip_agents}
    for role_key in _ROLE_KEYS:
        agent_id = starter_agent_ids.get(role_key)
        if not agent_id or agent_id not in live_ids:
            continue
        content = (_FILES_DIR / role_key / "AGENTS.md").read_text(encoding="utf-8")
        workspace.write_file_if_missing(owner_id, f"workspace-{agent_id}/AGENTS.md", content)
