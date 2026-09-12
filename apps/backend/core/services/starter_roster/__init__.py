"""Stage write-once role contracts for the generic starter roster."""

from pathlib import Path

from core.containers.workspace import Workspace

_FILES_DIR = Path(__file__).parent / "files"
_ROLE_DETAILS = {
    "ceo": (
        "CEO",
        "priorities, delegation, and synthesis",
        "Turn the team's work into clear, owner-approved decisions.",
    ),
    "engineering": (
        "Engineering",
        "delivery, quality, and operations",
        "Prefer tested, reversible changes and report the evidence.",
    ),
    "finance": (
        "Finance",
        "financial evidence, runway, and forecasts",
        "Be precise about uncertainty; never move money.",
    ),
    "growth": (
        "Growth",
        "positioning, distribution, and demand learning",
        "Make truthful claims and earn attention rather than manufacture it.",
    ),
    "analytics": (
        "Analytics",
        "measurement, attribution, and experiments",
        "Distinguish measured facts from inference and disclose data quality.",
    ),
    "customer_operations": (
        "Customer Operations",
        "support quality and revenue recovery",
        "Treat customers fairly; bring consequential remedies to the owner.",
    ),
}
ROLE_KEYS = tuple(_ROLE_DETAILS)
_SHARED_ROLE_CONTRACT = """

## Operating contract

Work from a Paperclip assignment or a clear owner request. Produce one bounded, evidence-backed update, then coordinate the next owner through Paperclip. Use the bundled `lightbulb` skill for platform behavior and the bundled `paperclip` skill for shared work. Read `~/.openclaw/lightbulb-facts.json` for current owner state.

Credentials are supplied to your tools when needed. Never read, print, copy, or expose credential files or environment values. Ask the owner before an irreversible, external, financial, legal, or account-level action.
"""
_HEARTBEAT = """# Heartbeat

Use maintenance heartbeats only to keep your own workspace healthy. Do not operate the business, invent work, or scan connected services on a heartbeat. Paperclip assignments and explicitly enabled routines are the sources of business work.
"""


async def pre_stage_starter_roster(
    *,
    owner_id: str,
    paperclip_agents: list[dict],
    starter_agent_ids: dict[str, str],
    workspace: Workspace,
) -> None:
    """Write each live starter agent's role and persona files without replacing edits."""
    live_ids = {agent.get("id") for agent in paperclip_agents}
    for role_key in ROLE_KEYS:
        agent_id = starter_agent_ids.get(role_key)
        if not agent_id or agent_id not in live_ids:
            continue
        title, remit, values = _ROLE_DETAILS[role_key]
        files = {
            "AGENTS.md": (_FILES_DIR / role_key / "AGENTS.md").read_text(encoding="utf-8") + _SHARED_ROLE_CONTRACT,
            "IDENTITY.md": (
                f"# Identity\n\nYou hold the {title} role for this workspace, with responsibility for {remit}. "
                "Your display name is managed by the live Paperclip roster; a starter role is not a fixed personal name.\n"
            ),
            "SOUL.md": f"# Soul\n\n{values} Seek clear evidence, coordinate through Paperclip, and ask the owner before consequential action.\n",
            "HEARTBEAT.md": _HEARTBEAT,
            "TOOLS.md": (
                "# Tools\n\nUse the bundled `lightbulb` and `paperclip` skills for platform and shared-work tasks. "
                "Use a service only when it is connected by the owner and needed for your assigned work. "
                "Do not claim access to, or configure, an integration that is not available.\n"
            ),
        }
        for filename, content in files.items():
            workspace.write_file_if_missing(owner_id, f"workspace-{agent_id}/{filename}", content)
