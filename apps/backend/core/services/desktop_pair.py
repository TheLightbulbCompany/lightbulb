"""A removed member's paired devices: the ``paired_devices`` entries on the owner's containers row,
which the local-session doors check (``local_sessions._paired``)."""

import asyncio
import logging

from core.repositories import container_repo

logger = logging.getLogger(__name__)

_revocations: set = set()


def revoke_member_soon(owner: str, member_id: str, still_current=None) -> None:
    """A removed member's paired devices, in the background (the webhook never waits): mark every
    device they paired to this workspace REVOKING (refused from then on), then unpair them.

    Every device write is conditional on the entry still being this member's (and, for the
    removal, still REVOKING), so a re-add and re-pair meanwhile survives. ``still_current``
    (async, True while this delete is still the member's lifecycle epoch) is checked before
    touching anything: a superseded delete does nothing."""

    async def current() -> bool:
        if still_current is None:
            return True
        try:
            if await still_current():
                return True
            logger.info("device revocation superseded owner=%s", owner)
        except Exception as exc:  # noqa: BLE001 -- unknown: stop; REVOKING is not set, nothing changed
            logger.warning("device revocation lifecycle unreadable owner=%s: %s", owner, type(exc).__name__)
        return False

    async def run() -> None:
        if not await current():
            return
        try:
            row = await container_repo.get_by_owner_id(owner, consistent=True) or {}
            devices = [
                device_id
                for device_id, entry in (row.get("paired_devices") or {}).items()
                if isinstance(entry, dict)
                and entry.get("userId") == member_id
                and await container_repo.mark_paired_device_revoking(owner, device_id, user_id=member_id)
            ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("device revocation could not list devices owner=%s: %s", owner, type(exc).__name__)
            return
        for device_id in devices:
            try:
                await container_repo.remove_revoked_paired_device(owner, device_id, member_id)
            except Exception as exc:  # noqa: BLE001 -- REVOKING already refuses it
                logger.warning("device pairing not removed owner=%s: %s", owner, type(exc).__name__)

    task = asyncio.create_task(run())
    _revocations.add(task)
    task.add_done_callback(_revocations.discard)
