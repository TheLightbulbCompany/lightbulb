"""Pre-stage Friday's IDENTITY / SOUL / USER / BOOTSTRAP files on EFS.

Friday is the seeded CEO agent in Paperclip (`role="ceo"`). When her
container provisions, we write four customized markdown files to her
EFS workspace dir BEFORE the container boots. OpenClaw's lazy
``ensureAgentWorkspace`` runs on first chat as today, but
``writeFileIfMissing`` honors any file we wrote first — so OpenClaw
fills only the gaps we don't seed (``AGENTS.md``, ``MEMORY.md``,
``TOOLS.md``, ``HEARTBEAT.md``).

Best-effort throughout: failures log + emit metrics, never raise. The
caller (``ecs_manager.write_user_configs``) wraps this in try/except
so a Clerk outage or EFS hiccup never blocks container provision.

See the design spec at
``docs/superpowers/specs/2026-05-08-friday-identity-seeding-design.md``.
"""

from __future__ import annotations

import logging

from core.containers.workspace import Workspace
from core.observability.metrics import put_metric

logger = logging.getLogger(__name__)


async def pre_stage_friday_identity(
    *,
    owner_id: str,
    paperclip_agents: list[dict],
    workspace: Workspace,
) -> None:
    """Seed Friday's IDENTITY/SOUL/USER/BOOTSTRAP files on EFS.

    Resolves Friday's UUID from `paperclip_agents` (the entry with
    role == "ceo"). Writes four files using write-if-missing semantics
    so subsequent (re)provisions don't clobber Friday's evolved persona.

    Best-effort: emits metrics + logs on failure but never raises.
    """
    friday = next((a for a in paperclip_agents if a.get("role") == "ceo"), None)
    if friday is None:
        if not paperclip_agents:
            put_metric("friday_identity.seed.skipped")
            logger.info(
                "friday_identity.skipped: no paperclip agents for owner=%s — deferring to next provision",
                owner_id,
            )
        else:
            put_metric("friday_identity.seed.skipped_no_ceo")
            logger.info(
                "friday_identity.skipped: no CEO in paperclip agents for owner=%s",
                owner_id,
            )
        return

    # Content-rendering + write-loop land in Task 3.
    # For now, the function returns silently when a CEO IS present.
    return
