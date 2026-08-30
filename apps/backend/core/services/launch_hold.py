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


# The settings row is unreadable, so we cannot name the drop instant — the
# hold still says the true thing, just without the date.
_DOORS_OPEN_SOON_COPY = "Doors open soon. Sign back in in a minute and Friday runs your idea."


def _gate(message: str, owner_role: str) -> Gate:
    return Gate(
        code=CODE,
        title="You're on the list",
        message=message,
        action_label="See your spot",
        action_href="/launch",
        action_admin_only=False,
        owner_role=owner_role,
    )


async def pre_drop_gate(owner_id: str, *, owner_role: str = "admin", now: datetime | None = None) -> Gate | None:
    # launch_phase.schedule() never raises: a DynamoDB read failure and a
    # malformed row both resolve to None (feature off). For a container-less
    # owner those two are NOT the same answer — un-holding is irreversible
    # (the next step stamps the 3-day trial clock) — so settings_unavailable()
    # tells them apart. Both reads are cached; neither hits the poll path.
    s = launch_phase.schedule()
    unavailable = launch_phase.settings_unavailable()

    # A schedule we actually read can answer on its own: nobody is held while
    # the competition is off, and nobody is held after the drop. Return before
    # the container read — this is the hot path on every /container/status poll.
    if not unavailable:
        if s is None:
            return None
        now = now or datetime.now(timezone.utc)
        if now >= s.drop_at:
            return None

    # Two cases left, and both hold UNLESS the owner already has a container:
    # the settings row is unreadable (fail closed; the next poll retries), or
    # we are knowingly pre-drop. Hence the container read, which fails open —
    # it is the one read here that must never block provisioning.
    try:
        if await container_repo.get_by_owner_id(owner_id) is not None:
            return None
    except Exception:  # noqa: BLE001 — a read failure must not block provisioning
        logger.warning("launch_hold: container read failed for %s; not holding", owner_id, exc_info=True)
        return None
    return _gate(_DOORS_OPEN_SOON_COPY if unavailable else _doors_open_copy(s.drop_at), owner_role)
