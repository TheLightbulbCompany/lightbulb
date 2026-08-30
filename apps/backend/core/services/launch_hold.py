"""Pre-drop hold: a signed-up account is kept container-less (and trial-clock-less)
until the Launch Competition's drop instant. Existing owners (container row present)
are never held. Evaluated BEFORE ensure_personal_trial_default in every door so the
3-day free trial starts on the first sign-in after the drop, not at sign-up."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from core.repositories import container_repo
from core.services import launch_phase
from core.services.provision_gate import Gate

logger = logging.getLogger(__name__)
CODE = "launch_pending"
_ET = ZoneInfo("America/New_York")


def _doors_open_copy(drop_at: datetime) -> str:
    local = drop_at.astimezone(_ET)
    return f"Doors open {local.strftime('%A, %B %-d')} at {local.strftime('%-I:%M %p')} ET. Sign back in then and Friday runs your idea."


async def pre_drop_gate(owner_id: str, *, owner_role: str = "admin", now: datetime | None = None) -> Gate | None:
    # Deliberate fail-loud: a malformed settings row (start <= drop, which
    # launch_settings.validate() should already have refused at write time)
    # raises here and 500s the poll rather than silently un-holding every
    # signup. Only the DDB read below fails open — a launch-settings read
    # failure itself is swallowed inside launch_phase's own cache. (The
    # webhook path is the opposite — see launch_referral.on_invoice_paid.)
    s = launch_phase.schedule()
    if s is None:
        return None
    now = now or datetime.now(timezone.utc)
    if now >= s.drop_at:
        return None
    try:
        if await container_repo.get_by_owner_id(owner_id) is not None:
            return None
    except Exception:  # noqa: BLE001 — a read failure must not block provisioning
        logger.warning("launch_hold: container read failed for %s; not holding", owner_id, exc_info=True)
        return None
    return Gate(
        code=CODE,
        title="You're on the list",
        message=_doors_open_copy(s.drop_at),
        action_label="See your spot",
        action_href="/launch",
        action_admin_only=False,
        owner_role=owner_role,
    )
