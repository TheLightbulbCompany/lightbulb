"""Limit one-off automations whose time has passed, so their Temporal Schedule never fires again.

Before the fix, a one-off (a cron that pins one date, `21 6 5 11 *`) was stored with `remainingRuns: null`, so its
Schedule had unlimited actions and would fire again on the same date next year. For every such row whose first
fire after `createdAt` is in the past (or that already ran) this:

- sets `remainingRuns: 1` when it is null (what the create/patch/propose paths now store),
- turns an `active` one off as the fire path does after a one-off's run ("paused", "ran once"),
- makes the Temporal Schedule match the row (`automation_schedule.upsert`: paused, limited actions).

Idempotent: a row already limited and off, with a Schedule that matches, is reported "ok" and left alone.

    AWS_PROFILE=isol8-admin AWS_REGION=us-east-1 TEMPORAL_ADDRESS=... TEMPORAL_NAMESPACE=... TEMPORAL_API_KEY=... \\
        .venv/bin/python -m scripts.limit_past_one_off_automations --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from temporalio.service import RPCError

from core.repositories import automations_repo
from core.services import automation_schedule
from core.temporal import client as temporal_client


def first_fire(cron: str, tz: str, after: datetime) -> datetime:
    """The first fire of a dated cron (`M H D Mo *`) after `after`: this year's date, else next year's."""
    minute, hour, day, month = (int(f) for f in cron.split()[:4])
    for year in (after.year, after.year + 1):
        at = datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(tz))
        if at > after:
            return at
    raise AssertionError("unreachable")


def is_past(row: dict, now: datetime) -> bool:
    if row.get("runTaskIds"):
        return True
    created = datetime.fromisoformat(row["createdAt"])
    try:
        return first_fire(row["cron"][0], row["tz"], created) <= now
    except ValueError:  # an undated `remainingRuns: 1` cron: Temporal already limits it; only a run counts
        return False


async def _temporal(row: dict) -> str:
    try:
        desc = await temporal_client.get_client().get_schedule_handle(automation_schedule.schedule_id(row)).describe()
    except RPCError as exc:
        return f"temporal={exc.status.name}"
    st = desc.schedule.state
    nxt = desc.info.next_action_times[0].isoformat() if desc.info.next_action_times else None
    left = st.remaining_actions if st.limited_actions else "unlimited"
    return f"temporal paused={st.paused} remaining={left} next={nxt}"


async def run(apply: bool) -> None:
    temporal_client.set_client(await temporal_client.connect())
    now = datetime.now(timezone.utc)
    for row in await automations_repo.scan_schedules():
        cron = row.get("cron") or []
        if automation_schedule.remaining_runs(cron, row.get("remainingRuns")) != 1 or not is_past(row, now):
            continue
        ws, sid = row["pk"].removeprefix("WS#"), row["scheduleId"]
        changes = []
        if row.get("remainingRuns") is None:
            changes.append("remainingRuns null->1")
        if row["status"] == "active":
            changes.append("status active->paused (ran once)")
        before = await _temporal(row)
        if apply:
            if row.get("remainingRuns") is None:
                row = await automations_repo.update_schedule(
                    ws, sid, {"remainingRuns": 1}, expected_version=row["version"]
                )
            if row["status"] == "active":
                row = await automations_repo.set_status(
                    ws, sid, "paused", reason="ran once", allowed_from=frozenset({"active"})
                )
            synced = await automation_schedule.upsert(row)
            if synced:
                changes.append(f"schedule {synced}")
        elif changes or row["status"] == "active":
            changes.append("schedule would be synced")
        verb = "" if apply else "would: "
        print(f"{ws} {sid} cron={cron} status={row['status']} {before} -> {verb}{', '.join(changes) or 'ok'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print what would change, write nothing")
    asyncio.run(run(apply=not parser.parse_args().dry_run))
