"""The super-agent switch, retired (Phase 1 task 4): every workspace is on the super-agent path.

`flags_for` stays for the trigger fallbacks that still ask it (they go once triggers move to `HOOK#`);
`list_on` is the watchdog's and the automations reconcile's "every workspace" scan.
"""

from core.repositories import container_repo


async def flags_for(ws: str) -> bool:
    """Always True, with no read."""
    return True


async def list_on() -> list[str]:
    """Every workspace's owner id: the watchdog's scan scope."""
    # ponytail: full scan of containers, cached 1 h by the caller; add a sparse GSI when workspaces > ~1k.
    return [row["owner_id"] for row in await container_repo.scan_all()]
