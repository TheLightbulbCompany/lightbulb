"""The watchdog's own rows in the `tasks` table (spec 11 §3.7, §5.3): Finding, Action, Cap.

Append-only: every put is `attribute_not_exists(sk)` and nothing here deletes. Rows expire by TTL (`expiresAt`):
Finding/Action after 400 days, Cap after 2. Action rows are the per-task counters: written (`outcome="pending"`)
before the action is tried, under `WS#{ws}` with a `WD#` sort key, so no Task status change, reopen or
Continue-As-New resets them. This module never writes Task, Attempt, Question or Effect rows.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any, Literal

from botocore.exceptions import ClientError

from core import tasks_schema as ts
from core.dynamodb import ddb_safe, get_table, plain, run_in_thread, utc_now_iso
from core.observability.metrics import put_metric

DAY_S = 86_400
FINDING_TTL_S = 400 * DAY_S
CAP_TTL_S = 2 * DAY_S


def _table():
    return get_table("tasks")


def _condition_failed(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _wrote() -> None:
    put_metric("watchdog.write")


def finding_id(pk: str, sk: str) -> str:
    """`{yyyymmdd}#{sk}`: what the digest prints and `--label` takes."""
    return f"{pk.rsplit('#', 1)[1]}#{sk}"


def finding_key(fid: str) -> tuple[str, str]:
    day, sk = fid.split("#", 1)
    return ts.wd_finding_pk(day), sk


async def _put_new(item: dict) -> bool:
    try:
        await run_in_thread(_table().put_item, Item=ddb_safe(item), ConditionExpression="attribute_not_exists(sk)")
    except ClientError as exc:
        if _condition_failed(exc):
            return False
        raise
    _wrote()
    return True


async def get_finding(pk: str, sk: str) -> dict | None:
    item = (await run_in_thread(_table().get_item, Key={"pk": pk, "sk": sk}, ConsistentRead=True)).get("Item")
    return plain(item) if item else None


async def put_finding(item: dict) -> tuple[dict, bool]:
    """Write a Finding under its deterministic key; a re-scan gets `(existing, False)` and writes nothing."""
    item = {
        "type": "wd_finding",
        "createdAt": utc_now_iso(),
        "expiresAt": int(time.time()) + FINDING_TTL_S,
        **item,
        "findingId": finding_id(item["pk"], item["sk"]),
    }
    if await _put_new(item):
        return item, True
    return (await get_finding(item["pk"], item["sk"])) or item, False


def _action_prefix(task_id: str, kind: str) -> str:
    return ts.wd_action_sk(task_id, kind, 0)[:-2]


async def count_actions(ws: str, task_id: str, kind: str) -> int:
    resp = await run_in_thread(
        _table().query,
        KeyConditionExpression="pk = :pk AND begins_with(sk, :p)",
        ExpressionAttributeValues={":pk": ts.ws_pk(ws), ":p": _action_prefix(task_id, kind)},
        ConsistentRead=True,
        Select="COUNT",
    )
    return int(resp["Count"])


async def spend_action(ws: str, task_id: str, kind: str, *, limit: int, finding_id: str, by: str) -> int | None:
    """Spend the next attempt for `kind` on this task: write row `n = count + 1` with `outcome="pending"`.
    `None` when `n > limit`. A lost race (another writer took `n`) re-counts once."""
    for _ in range(2):
        n = await count_actions(ws, task_id, kind) + 1
        if n > limit:
            return None
        row = {
            "pk": ts.ws_pk(ws),
            "sk": ts.wd_action_sk(task_id, kind, n),
            "type": "wd_action",
            "taskId": task_id,
            "kind": kind,
            "n": n,
            "outcome": "pending",
            "findingId": finding_id,
            "by": by,
            "createdAt": utc_now_iso(),
            "expiresAt": int(time.time()) + FINDING_TTL_S,
        }
        if await _put_new(row):
            return n
    return None


async def finish_action(
    ws: str,
    task_id: str,
    kind: str,
    n: int,
    outcome: Literal["done", "refused", "failed"],
    reason: str | None = None,
) -> None:
    await run_in_thread(
        _table().update_item,
        Key={"pk": ts.ws_pk(ws), "sk": ts.wd_action_sk(task_id, kind, n)},
        UpdateExpression="SET outcome = :o, reason = :r, finishedAt = :t",
        ConditionExpression="attribute_exists(sk)",
        ExpressionAttributeValues={":o": outcome, ":r": reason, ":t": utc_now_iso()},
    )
    _wrote()


async def take_cap(kind: Literal["act", "judge"], bucket: str, cap: int) -> bool:
    """One unit of the fleet-wide budget for `bucket` (hour for act, day for judge). False once `cap` is spent."""
    if cap <= 0:
        return False
    try:
        await run_in_thread(
            _table().update_item,
            Key={"pk": ts.WD_PLATFORM_PK, "sk": ts.wd_cap_sk(kind, bucket)},
            UpdateExpression="ADD n :one SET expiresAt = if_not_exists(expiresAt, :exp)",
            ConditionExpression="attribute_not_exists(n) OR n < :cap",
            ExpressionAttributeValues={":one": 1, ":cap": cap, ":exp": int(time.time()) + CAP_TTL_S},
        )
    except ClientError as exc:
        if _condition_failed(exc):
            return False
        raise
    _wrote()
    return True


async def _set(pk: str, sk: str, fields: dict[str, Any]) -> dict:
    names = {f"#f{i}": k for i, k in enumerate(fields)}
    values = {f":f{i}": v for i, v in enumerate(fields.values())}
    resp = await run_in_thread(
        _table().update_item,
        Key={"pk": pk, "sk": sk},
        UpdateExpression="SET " + ", ".join(f"#f{i} = :f{i}" for i in range(len(fields))),
        ConditionExpression="attribute_exists(sk)",
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=ddb_safe(values),
        ReturnValues="ALL_NEW",
    )
    _wrote()
    return plain(resp["Attributes"])


async def label(finding_pk: str, finding_sk: str, label: ts.WatchdogLabel, by: str, note: str | None = None) -> dict:
    """A platform admin's label (spec 11 §3.10). Overwrites an earlier label; `labelledBy` names who."""
    return await _set(
        finding_pk, finding_sk, {"label": label, "labelledBy": by, "labelledAt": utc_now_iso(), "labelNote": note}
    )


async def set_hint(finding_pk: str, finding_sk: str, hint: str) -> None:
    """What happened next (`resolved_without_help`, `person_stopped`, `person_retried`): a labelling aid."""
    await _set(finding_pk, finding_sk, {"hint": hint})


async def list_findings(
    day: str, *, unlabelled: bool = False, limit: int = 100, cursor: str | None = None
) -> tuple[list[dict], str | None]:
    """One day's findings in key order. `unlabelled` filters server-side; pages fill up to `limit`."""
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": "pk = :pk",
        "ExpressionAttributeValues": {":pk": ts.wd_finding_pk(day)},
    }
    if unlabelled:
        kwargs["FilterExpression"] = "attribute_not_exists(#l)"
        kwargs["ExpressionAttributeNames"] = {"#l": "label"}
    last = json.loads(base64.urlsafe_b64decode(cursor)) if cursor else None
    items: list[dict] = []
    while len(items) < limit:
        if last:
            kwargs["ExclusiveStartKey"] = last
        page = await run_in_thread(_table().query, Limit=limit - len(items), **kwargs)
        items.extend(plain(i) for i in page.get("Items", []))
        last = page.get("LastEvaluatedKey")
        if not last:
            break
    nxt = base64.urlsafe_b64encode(json.dumps(plain(last)).encode()).decode() if last else None
    return items, nxt
