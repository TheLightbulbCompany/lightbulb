"""The one switch (contracts-v2 §1). The only reader of the env master and of `containers.super_agent`;
`tests/unit/super_agent/test_flags.py::test_no_other_reader` fails if anything else reads either.

Unfinished lanes are off in code, not behind flags of their own. The backend writes the run mode onto each run
request from this, so the runtime never reads flags.
"""

from core.config import settings
from core.repositories import container_repo


async def flags_for(ws: str) -> bool:
    """True when the super-agent path is on for workspace owner `ws`. The env master off (prod) means off
    without reading the row."""
    if not settings.SUPER_AGENT_ENV_ENABLED:
        return False
    row = await container_repo.get_by_owner_id(ws)
    # ponytail: one eventually consistent GetItem per call; add a 30 s cache when a per-message caller lands.
    return (row or {}).get("super_agent") is True
