"""Automation rows of the `tasks` table: `SCHED#{id}` (an automation) and `HOOK#{publicId}` (one of its webhook
triggers). Spec 06 §3, contracts-v2 §3/§7/§11.

Same write rules as `tasks_repo` (whose helpers this reuses): conditional puts on deterministic keys, every
update conditional and bumping `version` + `updatedAt`, strongly consistent reads. Runs are tasks: History is
`runTaskIds` on the row (no `SCHED#…#RUN#` rows); `activeRunTaskId` is the SKIP-overlap claim.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from botocore.exceptions import ClientError
from pydantic.alias_generators import to_camel

from core import tasks_schema as ts
from core.dynamodb import ddb_safe, plain, run_in_thread
from core.repositories.tasks_repo import (
    IllegalTransition,
    VersionConflict,
    _get,
    _is_condition_failure,
    _put_new,
    _row,
    _table,
    _update,
)

RUN_HISTORY = 50

# Rows store unset optional fields as NULL (as tasks_repo does), so "free" conditions also accept NULL.
# Writer-owned: changed only by set_status / claim_run / close_run / note_sig_failure.
_PROTECTED = frozenset(
    {
        "pk",
        "sk",
        "type",
        "scheduleId",
        "publicId",
        "status",
        "pausedReason",
        "activeRunTaskId",
        "runTaskIds",
        "consecutiveFailures",
        "sigFailCount",
        "lastSigFailAt",
        "version",
        "createdAt",
    }
)


def _check_fields(fields: dict) -> dict:
    """The update with camelCase attribute names (rows are stored camel); refuses writer-owned fields in
    either spelling."""
    camel = {to_camel(k): v for k, v in fields.items()}
    bad = _PROTECTED & set(camel)
    if bad:
        raise ValueError(f"field update may not set {sorted(bad)}")
    return camel


async def _conditional(ws: str, sk: str, sets: dict, *, condition: str, values: dict, remove=()) -> dict | None:
    """`_update`, or None when the condition (or row existence) fails."""
    try:
        return await _update(ws, sk, sets, condition=condition, values=values, remove=remove)
    except ClientError as exc:
        if not _is_condition_failure(exc):
            raise
    return None


# --- Schedule --------------------------------------------------------------------------------------


async def create_schedule(ws: str, row: dict) -> tuple[dict, bool]:
    return await _put_new(ws, _row(ts.Schedule, ws, {**row, "status": "draft"}))


async def get_schedule(ws: str, sid: str) -> dict | None:
    return await _get(ws, ts.schedule_sk(sid))


async def list_schedules(ws: str) -> list[dict]:
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": "pk = :pk AND begins_with(sk, :p)",
        "ExpressionAttributeValues": {":pk": ts.ws_pk(ws), ":p": ts.schedule_sk("")},
        "ConsistentRead": True,
    }
    out: list[dict] = []
    while True:
        page = await run_in_thread(_table().query, **kwargs)
        out.extend(plain(page.get("Items", [])))
        if "LastEvaluatedKey" not in page:
            return out
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


async def update_schedule(ws: str, sid: str, fields: dict, *, expected_version: int) -> dict:
    fields = _check_fields(fields)
    row = await _conditional(ws, ts.schedule_sk(sid), fields, condition="#v = :ev", values={":ev": expected_version})
    if row is None:
        raise VersionConflict(f"schedule {sid}: not at version {expected_version} (or missing)")
    return row


async def set_status(
    ws: str, sid: str, to: ts.ScheduleStatus, *, reason: str | None, allowed_from: frozenset[str]
) -> dict:
    values = {f":a{i}": s for i, s in enumerate(sorted(allowed_from))}
    sets: dict[str, Any] = {"status": to}
    if reason is not None:
        sets["pausedReason"] = reason
    row = await _conditional(
        ws,
        ts.schedule_sk(sid),
        sets,
        condition=f"#s IN ({', '.join(values)})",
        values=values,
        remove=() if reason is not None else ("pausedReason",),
    )
    if row is None:
        current = await get_schedule(ws, sid)
        raise IllegalTransition(sid, current and current["status"], to)
    return row


async def claim_run(ws: str, sid: str, task_id: str) -> bool:
    """Claim the run slot for `task_id`: only an active automation with no run in flight (SKIP overlap).
    Claiming again with the same task id (an Activity retry) is a success, unless that run already closed:
    re-arming a finished run would hold the slot forever."""
    row = await _conditional(
        ws,
        ts.schedule_sk(sid),
        {"activeRunTaskId": task_id},
        condition="#s = :active AND (attribute_not_exists(activeRunTaskId) "
        "OR attribute_type(activeRunTaskId, :nul) OR activeRunTaskId = :task) "
        "AND NOT contains(runTaskIds, :task)",
        values={":active": "active", ":task": task_id, ":nul": "NULL"},
    )
    return row is not None


async def close_run(ws: str, sid: str, task_id: str, *, failed: bool) -> dict:
    """Free the claim, prepend the run to History (capped), count consecutive failures. A retry after success
    returns the row unchanged; closing someone else's run is a `VersionConflict`."""
    current = await get_schedule(ws, sid)
    if current is None:
        raise VersionConflict(f"schedule {sid}: missing")
    if current.get("activeRunTaskId") != task_id:
        if task_id in current.get("runTaskIds", []):
            return current
        raise VersionConflict(f"schedule {sid}: run {task_id} is not the active run")
    history = [task_id, *[t for t in current.get("runTaskIds", []) if t != task_id]][:RUN_HISTORY]
    row = await _conditional(
        ws,
        ts.schedule_sk(sid),
        {
            "runTaskIds": history,
            "consecutiveFailures": current.get("consecutiveFailures", 0) + 1 if failed else 0,
        },
        condition="activeRunTaskId = :task AND #v = :ev",
        values={":task": task_id, ":ev": current["version"]},
        remove=("activeRunTaskId",),
    )
    if row is None:
        raise VersionConflict(f"schedule {sid}: changed while closing run {task_id}")
    return row


async def delete_schedule(ws: str, sid: str) -> None:
    await run_in_thread(_table().delete_item, Key={"pk": ts.ws_pk(ws), "sk": ts.schedule_sk(sid)})


# --- Hook ------------------------------------------------------------------------------------------


async def put_hook(ws: str, hook: dict) -> tuple[dict, bool]:
    if hook.get("signingMode") == "none":
        raise ValueError("new webhooks must be signed (signing mode 'none' is refused)")
    return await _put_new(ws, _row(ts.Hook, ws, hook))


async def get_hook(ws: str, public_id: str) -> dict | None:
    return await _get(ws, ts.hook_sk(public_id))


async def update_hook(ws: str, public_id: str, fields: dict, *, expected_version: int) -> dict:
    """The routes and the Google watch keeper both update a hook: version-checked so neither loses a write."""
    fields = _check_fields(fields)
    row = await _conditional(ws, ts.hook_sk(public_id), fields, condition="#v = :ev", values={":ev": expected_version})
    if row is None:
        raise VersionConflict(f"hook {public_id}: not at version {expected_version} (or missing)")
    return row


async def delete_hook(ws: str, public_id: str) -> None:
    await run_in_thread(_table().delete_item, Key={"pk": ts.ws_pk(ws), "sk": ts.hook_sk(public_id)})


async def note_sig_failure(ws: str, public_id: str, *, now: datetime) -> bool:
    """Count a bad signature, at most one write a minute (a flood of bad deliveries costs one write/min)."""
    try:
        await run_in_thread(
            _table().update_item,
            Key={"pk": ts.ws_pk(ws), "sk": ts.hook_sk(public_id)},
            UpdateExpression="SET sigFailCount = if_not_exists(sigFailCount, :zero) + :one, lastSigFailAt = :now",
            ConditionExpression="attribute_exists(sk) AND (attribute_not_exists(lastSigFailAt) "
            "OR attribute_type(lastSigFailAt, :nul) OR lastSigFailAt < :cutoff)",
            ExpressionAttributeValues=ddb_safe(
                {
                    ":zero": 0,
                    ":one": 1,
                    ":nul": "NULL",
                    ":now": now.isoformat(),
                    ":cutoff": (now - timedelta(seconds=60)).isoformat(),
                }
            ),
        )
        return True
    except ClientError as exc:
        if not _is_condition_failure(exc):
            raise
    return False
