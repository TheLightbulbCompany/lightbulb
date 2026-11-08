"""Retire the automatic Ideas/Feed schedules removed by the voice pivot.

User-created schedules and webhook automations keep their normal lifecycle.
This idempotent cleanup uses the existing workspace reconciliation doors; it
never starts an agent or creates an idle per-business schedule.
"""

RETIRED_SCHEDULE_IDS = frozenset({"builtin:look_around", "builtin:feed"})


async def ensure(ws: str) -> None:
    from core.repositories import automations_repo
    from core.services import automation_schedule

    for sid in RETIRED_SCHEDULE_IDS:
        if await automations_repo.get_schedule(ws, sid) is not None:
            await automation_schedule.delete(ws, sid)
            await automations_repo.delete_schedule(ws, sid)
