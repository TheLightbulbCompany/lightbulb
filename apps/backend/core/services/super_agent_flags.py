"""The one switch (contracts-v2 §1). The only reader of the env master and of `containers.super_agent`;
`tests/unit/super_agent/test_flags.py::test_no_other_reader` fails if anything else reads either.

Unfinished lanes are off in code, not behind flags of their own. The backend writes the run mode onto each run
request from this, so the runtime never reads flags.
"""

from core.config import settings
from core.repositories import container_repo


async def flags_for(ws: str) -> bool:
    """True when the super-agent path is on for workspace owner `ws`. The env master off (prod) means off
    without reading the row. With it on (dev), every workspace is on unless its row says `super_agent: false`
    (founder ruling 5 Nov 2026: on in dev; the opt-out keeps a flag-off parity workspace possible)."""
    if not settings.SUPER_AGENT_ENV_ENABLED:
        return False
    row = await container_repo.get_by_owner_id(ws)
    # ponytail: one eventually consistent GetItem per call; add a 30 s cache when a per-message caller lands.
    return (row or {}).get("super_agent") is not False


def env_enabled() -> bool:
    """The env master alone, for process-wide wiring with no workspace (the Temporal client in the
    lifespan, the worker entrypoint). Per-workspace behaviour still asks `flags_for`."""
    return settings.SUPER_AGENT_ENV_ENABLED


async def list_on() -> list[str]:
    """Owner ids with the switch on (every workspace row not opted out): the watchdog's scan scope. Empty without a read when the env master is off."""
    if not settings.SUPER_AGENT_ENV_ENABLED:
        return []
    # ponytail: full scan of containers, cached 1 h by the caller; add a sparse GSI when workspaces > ~1k.
    return [row["owner_id"] for row in await container_repo.scan_all() if row.get("super_agent") is not False]
