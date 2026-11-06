"""An automation's Temporal Schedule (spec 06 §3.4, contracts-v2 §11).

One Schedule per `SCHED#` row, id `ts.schedule_workflow_id(ws, sid)`: cron in the row's own zone, SKIP overlap,
1-minute catch-up, paused unless the row is `active`, `remainingRuns` → limited actions. The row is the truth;
`upsert` makes Temporal match it and `ScheduleReconcileWorkflow` repairs drift daily.

Drift check: Temporal compiles cron into calendars, so `describe()` cannot return the cron we sent. A
fingerprint of (cron, tz, remainingRuns) rides in the start action's memo; paused is read from the state.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from typing import Literal

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleDescription,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleState,
    ScheduleUpdate,
)
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.service import RPCError, RPCStatusCode

from core import tasks_schema as ts
from core.config import settings
from core.repositories.user_repo import valid_timezone
from core.temporal.automations import FireInput
from core.temporal.client import get_client

MIN_INTERVAL_MIN = 10
MAX_ENTRIES = 4
PLATFORM_SCHEDULE_ID = "platform:reconcile-schedules"
RECONCILE_WORKFLOW_ID = "reconcile:schedules"

_FIELD = re.compile(r"^[A-Za-z0-9*,/-]+$")


_MONTHS = {m: i for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split(), 1)}
_DAYS = {d: i for i, d in enumerate("SUN MON TUE WED THU FRI SAT".split())}
# (low, high, names) for minute, hour, day of month, month, day of week (7 = Sunday too)
_RANGES = ((0, 59, {}), (0, 23, {}), (1, 31, {}), (1, 12, _MONTHS), (0, 7, _DAYS))


def _expand(field: str, lo: int, hi: int, names: dict[str, int] | None = None) -> set[int]:
    """Values of one cron field (`*`, `a`, `a-b`, `*/n`, `a-b/n`, comma lists, JAN/MON names)."""
    if names:
        field = re.sub(r"[A-Za-z]+", lambda m: str(names[m.group().upper()]), field)
    out: set[int] = set()
    for part in field.split(","):
        rng, _, step = part.partition("/")
        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            a, b = (int(x) for x in rng.split("-", 1))
        else:
            a = b = int(rng)
        n = int(step) if step else 1
        if not (lo <= a <= b <= hi) or n < 1:
            raise ValueError(f"cron field {field!r} out of range")
        out.update(range(a, b + 1, n))
    return out


def validate_cron(cron: list[str], tz: str) -> None:
    """1–4 five-field crons, no `@` macros, at least 10 minutes between fires, an IANA zone. Raises ValueError."""
    if not valid_timezone(tz):
        raise ValueError(f"unknown time zone {tz!r}")
    if not 1 <= len(cron) <= MAX_ENTRIES:
        raise ValueError(f"1 to {MAX_ENTRIES} schedules")
    minutes: set[int] = set()
    for expr in cron:
        fields = expr.split()
        if len(fields) != 5 or not all(_FIELD.match(f) for f in fields):
            raise ValueError(f"not a 5-field cron: {expr!r}")
        try:
            mins, hours, *_ = (_expand(f, lo, hi, names) for f, (lo, hi, names) in zip(fields, _RANGES))
        except (ValueError, KeyError) as exc:
            raise ValueError(f"bad cron {expr!r}: {exc}") from None
        minutes |= {h * 60 + m for h in hours for m in mins}
    # ponytail: minute-of-day union ignores day fields, so two entries on different days that are close in
    # clock time are refused too; compare per weekday if anyone needs that.
    times = sorted(minutes)
    gaps = [b - a for a, b in zip(times, times[1:])] + [times[0] + 1440 - times[-1]]
    if min(gaps) < MIN_INTERVAL_MIN:
        raise ValueError(f"runs must be at least {MIN_INTERVAL_MIN} minutes apart")


def remaining_runs(cron: list[str], remaining: int | None) -> int | None:
    """The `remainingRuns` to store. A cron that pins one minute of one date (`0 9 6 11 *`) is a one-off: the
    Schedule would fire it again every year, so it gets one run whatever the caller sent. Every writer (the
    create/patch routes, the Router's propose) stores this. ponytail: an every-year-on-this-date automation
    reads as a one-off too; give it `remainingRuns` > 1 or a weekday field if anyone ever wants one."""
    if remaining is None and len(cron) == 1:
        fields = cron[0].split()
        if len(fields) == 5 and fields[4] == "*" and all(f.isdigit() for f in fields[:4]):
            return 1
    return remaining


def _ws(row: dict) -> str:
    return row["pk"].removeprefix("WS#")


def schedule_id(row: dict) -> str:
    return ts.schedule_workflow_id(_ws(row), row["scheduleId"])


def fingerprint(row: dict) -> str:
    data = json.dumps([row["cron"], row["tz"], row.get("remainingRuns")], separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()[:16]


def fingerprint_of(desc: ScheduleDescription) -> str | None:
    memo = desc.schedule.action.memo or {}  # type: ignore[union-attr]
    payload = memo.get("fp")
    if payload is None:
        return None
    from temporalio.converter import DataConverter

    return DataConverter.default.payload_converter.from_payload(payload, str)


def _paused(row: dict) -> bool:
    return row["status"] != "active"


def build(row: dict) -> Schedule:
    ws, sid = _ws(row), row["scheduleId"]
    remaining = row.get("remainingRuns")
    return Schedule(
        action=ScheduleActionStartWorkflow(
            "TriggerFireWorkflow",
            FireInput(ws=ws, schedule_id=sid, source="schedule"),
            id=f"fire:{ws}:{sid}",
            task_queue=settings.TEMPORAL_TASK_QUEUE,
            memo={"fp": fingerprint(row)},
        ),
        spec=ScheduleSpec(cron_expressions=list(row["cron"]), time_zone_name=row["tz"]),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP, catchup_window=timedelta(minutes=1)),
        state=ScheduleState(
            paused=_paused(row), limited_actions=remaining is not None, remaining_actions=remaining or 0
        ),
    )


async def upsert(row: dict) -> Literal["created", "updated"] | None:
    """Make the Temporal Schedule match the row. None = it already did. An event-only automation (no cron)
    has no Schedule; one left from an earlier cron is deleted ("updated")."""
    handle = get_client().get_schedule_handle(schedule_id(row))
    try:
        desc = await handle.describe()
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
        if not row["cron"]:
            return None
        await get_client().create_schedule(schedule_id(row), build(row))
        return "created"
    if not row["cron"]:
        await delete(_ws(row), row["scheduleId"])
        return "updated"
    if fingerprint_of(desc) != fingerprint(row) or desc.schedule.spec.time_zone_name != row["tz"]:
        new = build(row)
        await handle.update(lambda _: ScheduleUpdate(schedule=new))
        return "updated"
    if desc.schedule.state.paused != _paused(row):
        await (handle.pause if _paused(row) else handle.unpause)(note=f"reconcile: row is {row['status']}")
        return "updated"
    return None


async def pause(ws: str, sid: str, note: str) -> None:
    await get_client().get_schedule_handle(ts.schedule_workflow_id(ws, sid)).pause(note=note)


async def unpause(ws: str, sid: str, note: str) -> None:
    await get_client().get_schedule_handle(ts.schedule_workflow_id(ws, sid)).unpause(note=note)


async def delete(ws: str, sid: str) -> None:
    """Delete the Schedule; already gone is done."""
    try:
        await get_client().get_schedule_handle(ts.schedule_workflow_id(ws, sid)).delete()
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise


async def next_runs(ws: str, sid: str, n: int = 3) -> list[datetime]:
    """The next `n` fire times in UTC (also for a paused Schedule: the confirm card shows them)."""
    desc = await get_client().get_schedule_handle(ts.schedule_workflow_id(ws, sid)).describe()
    return list(desc.info.next_action_times[:n])


async def ensure_platform(client: Client, *, task_queue: str | None = None) -> None:
    """Worker boot: start one reconcile now and keep the daily 04:00 UTC platform Schedule. Idempotent."""
    queue = task_queue or settings.TEMPORAL_TASK_QUEUE
    await client.start_workflow(
        "ScheduleReconcileWorkflow",
        id=RECONCILE_WORKFLOW_ID,
        task_queue=queue,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )
    try:
        await client.create_schedule(
            PLATFORM_SCHEDULE_ID,
            Schedule(
                action=ScheduleActionStartWorkflow(
                    "ScheduleReconcileWorkflow", id=RECONCILE_WORKFLOW_ID, task_queue=queue
                ),
                spec=ScheduleSpec(cron_expressions=["0 4 * * *"]),
                policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
            ),
        )
    except ScheduleAlreadyRunningError:  # kept from an earlier boot
        pass
