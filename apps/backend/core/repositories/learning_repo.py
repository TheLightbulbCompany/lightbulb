"""`LEARN#` intent rows on the tasks table (spec 07 §3.5 crash safety 2-3; C29: lane V's own module).

One row per `(specialist, task, attempt)` merge: `planned` ({rel: sha256} of every put the merge
intends) is written BEFORE the S3 writes and again before every replan, `applied` + the report
after. A replay of an `applied` row is a no-op; a replay of a `planned` row hands its shas back
to the planner so a write that already landed is never planned twice (usage deltas would double).
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError

from core import tasks_schema as ts
from core.dynamodb import ddb_safe, get_table, plain, run_in_thread, utc_now_iso


def _table():
    return get_table("tasks")


def _key(ws: str, specialist_id: str, task_id: str, attempt: int) -> dict:
    return {"pk": ts.ws_pk(ws), "sk": ts.learn_sk(specialist_id, task_id, attempt)}


def _conditional_miss(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def get_sync(ws: str, specialist_id: str, task_id: str, attempt: int) -> dict | None:
    item = _table().get_item(Key=_key(ws, specialist_id, task_id, attempt), ConsistentRead=True).get("Item")
    return plain(item) if item else None


def put_planned_sync(
    ws: str, specialist_id: str, task_id: str, attempt: int, planned: dict[str, str]
) -> tuple[dict, bool]:
    """The row in state ``planned`` carrying ``planned``. Creates it, or re-plans an existing
    ``planned`` row (a 412 retry, a crashed earlier run). An ``applied`` row is returned as is
    (created False) and never reopened."""
    now = utc_now_iso()
    row = ts.LearnIntent(
        ws=ws,
        specialist_id=specialist_id,
        task_id=task_id,
        attempt=attempt,
        state="planned",
        planned=dict(planned),
        created_at=now,
        updated_at=now,
    ).to_item()
    try:
        _table().put_item(Item=ddb_safe(row), ConditionExpression="attribute_not_exists(sk)")
        return row, True
    except ClientError as exc:
        if not _conditional_miss(exc):
            raise
    existing = get_sync(ws, specialist_id, task_id, attempt)
    if existing is None or existing.get("state") == "applied" or existing.get("planned") == dict(planned):
        return existing or row, False  # applied rows are never reopened; an identical replan is a no-op
    try:
        resp = _table().update_item(
            Key=_key(ws, specialist_id, task_id, attempt),
            UpdateExpression="SET planned = :p, #v = #v + :one, updatedAt = :now",
            ConditionExpression="#s = :planned",
            ExpressionAttributeNames={"#v": "version", "#s": "state"},
            ExpressionAttributeValues=ddb_safe({":p": dict(planned), ":one": 1, ":now": now, ":planned": "planned"}),
            ReturnValues="ALL_NEW",
        )
        return plain(resp["Attributes"]), False
    except ClientError as exc:
        if not _conditional_miss(exc):
            raise
        return get_sync(ws, specialist_id, task_id, attempt) or row, False


def mark_applied_sync(ws: str, specialist_id: str, task_id: str, attempt: int, report: dict[str, Any]) -> None:
    _table().update_item(
        Key=_key(ws, specialist_id, task_id, attempt),
        UpdateExpression="SET #s = :applied, report = :r, #v = #v + :one, updatedAt = :now",
        ConditionExpression="attribute_exists(sk)",
        ExpressionAttributeNames={"#v": "version", "#s": "state"},
        ExpressionAttributeValues=ddb_safe({":applied": "applied", ":r": report, ":one": 1, ":now": utc_now_iso()}),
    )


async def get(ws: str, specialist_id: str, task_id: str, attempt: int) -> dict | None:
    return await run_in_thread(get_sync, ws, specialist_id, task_id, attempt)


async def put_planned(
    ws: str, specialist_id: str, task_id: str, attempt: int, planned: dict[str, str]
) -> tuple[dict, bool]:
    return await run_in_thread(put_planned_sync, ws, specialist_id, task_id, attempt, planned)


async def mark_applied(ws: str, specialist_id: str, task_id: str, attempt: int, report: dict[str, Any]) -> None:
    await run_in_thread(mark_applied_sync, ws, specialist_id, task_id, attempt, report)
