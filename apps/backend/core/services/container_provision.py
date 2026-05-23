"""Container provisioning helper.

Lives in core/services/ so both routers/container.py (auto-retry path)
and core/services/user_provision.py (ensure_user_ready) can dispatch
container provisioning the same way.
"""

from __future__ import annotations

import logging

from core.containers import get_ecs_manager
from core.repositories import billing_repo

logger = logging.getLogger(__name__)


async def resolve_provider_choice(owner_id: str) -> tuple[str, str | None]:
    """Look up the owner's provider_choice (+ byo_provider) from billing_repo.

    Mirrors routers/container.py:_resolve_provider_choice. Falls back
    to bedrock_claude when no row or no choice is persisted, matching
    the legacy default and keeping recovery working for owners
    onboarded before per-owner provider_choice storage.
    """
    row = await billing_repo.get_by_owner_id(owner_id)
    provider_choice = (row or {}).get("provider_choice") or "bedrock_claude"
    byo_provider = (row or {}).get("byo_provider") if provider_choice == "byo_key" else None
    return provider_choice, byo_provider


async def background_provision(owner_id: str, clerk_user_id: str) -> None:
    """Run container provisioning in the background.

    ``owner_id`` is the container scope (org_id in org context, user_id
    in personal). ``clerk_user_id`` is kept for caller compatibility;
    the function reads provider_choice from billing_repo by owner_id
    and does not consult clerk_user_id directly.

    Catches and logs all exceptions — never raises. This function is an
    asyncio.create_task target.
    """
    try:
        provider_choice, byo_provider = await resolve_provider_choice(owner_id)
        await get_ecs_manager().provision_user_container(
            owner_id,
            provider_choice=provider_choice,
            byo_provider=byo_provider,
        )
    except Exception:
        logger.exception("Background provisioning failed for owner %s", owner_id)
