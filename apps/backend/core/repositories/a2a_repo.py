"""A2A state in the messaging table (``pk=A2A#{org}``)."""

import time
import uuid

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from core.dynamodb import run_in_thread
from core.repositories.messaging_repo import _table

AUTHORITY_ROW_TTL_S = 86_400


async def claim_authority_id(org: str, authority_id: str, now: int) -> bool:
    """Record an authority object's id once per org; False when it was already claimed (a replay)."""
    try:
        await run_in_thread(
            _table().put_item,
            Item={"pk": f"A2A#{org}", "sk": f"AUTH#{authority_id}", "expiresAt": now + AUTHORITY_ROW_TTL_S},
            ConditionExpression="attribute_not_exists(sk)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
    return True


async def get_rules(org: str) -> dict | None:
    resp = await run_in_thread(_table().get_item, Key={"pk": f"A2A#{org}", "sk": "RULES"})
    item = resp.get("Item")
    return {k: v for k, v in item.items() if k not in ("pk", "sk", "expiresAt")} if item else None


async def put_rules(org: str, rules: dict) -> None:
    await run_in_thread(_table().put_item, Item={**rules, "pk": f"A2A#{org}", "sk": "RULES"})


async def put_exception(
    org: str,
    resource: str,
    skill: str,
    requester_person: str,
    operands_hash: str,
    *,
    holder: str,
    task_id: str,
    now: int,
) -> str:
    """Record a holder's one-day approval for this requester, resource, skill and operands; returns the
    exception id."""
    exc_id = str(uuid.uuid4())
    await run_in_thread(
        _table().put_item,
        Item={
            "pk": f"A2A#{org}",
            "sk": f"EXC#{resource}#{skill}#{requester_person}#{operands_hash}",
            "id": exc_id,
            "holder": holder,
            "taskId": task_id,
            "expiresAt": now + AUTHORITY_ROW_TTL_S,
        },
    )
    return exc_id


async def get_exception(
    org: str, resource: str, skill: str, requester_person: str, operands_hash: str, now: int
) -> dict | None:
    key = {"pk": f"A2A#{org}", "sk": f"EXC#{resource}#{skill}#{requester_person}#{operands_hash}"}
    resp = await run_in_thread(_table().get_item, Key=key)
    item = resp.get("Item")
    return item if item and int(item["expiresAt"]) > now else None


def _is_conflict(exc: ClientError) -> bool:
    return exc.response["Error"]["Code"] == "ConditionalCheckFailedException"


def _strip(item: dict) -> dict:
    return {k: v for k, v in item.items() if k not in ("pk", "sk")}


async def put_task(org: str, task: dict) -> None:
    await run_in_thread(
        _table().put_item,
        Item={**task, "pk": f"A2A#{org}", "sk": f"TASK#{task['id']}"},
        ConditionExpression="attribute_not_exists(sk)",
    )


async def get_task(org: str, task_id: str) -> dict | None:
    resp = await run_in_thread(_table().get_item, Key={"pk": f"A2A#{org}", "sk": f"TASK#{task_id}"})
    item = resp.get("Item")
    return _strip(item) if item else None


async def update_task(
    org: str, task_id: str, *, state: str, extra: dict | None = None, expect: str | None = None
) -> bool:
    """Set the state and fields; with ``expect``, only from that state (False when the task left it)."""
    fields = {**(extra or {}), "state": state, "updatedAt": int(time.time())}
    names = {f"#f{i}": k for i, k in enumerate(fields)}
    values = {f":v{i}": v for i, v in enumerate(fields.values())}
    cond = "attribute_exists(sk)"
    if expect is not None:
        names["#s"], values[":expect"] = "state", expect
        cond += " AND #s = :expect"
    try:
        await run_in_thread(
            _table().update_item,
            Key={"pk": f"A2A#{org}", "sk": f"TASK#{task_id}"},
            UpdateExpression="SET " + ", ".join(f"#f{i} = :v{i}" for i in range(len(fields))),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ConditionExpression=cond,
        )
    except ClientError as exc:
        if expect is not None and _is_conflict(exc):
            return False
        raise
    return True


async def put_notification(org: str, message_id: str, task_id: str, holder: str, room_id: str) -> None:
    """Index a hold notification posted to ``holder`` in ``room_id``, so a press on its card finds the task."""
    item = {"pk": f"A2A#{org}", "sk": f"NOTIF#{message_id}", "taskId": task_id, "holder": holder, "roomId": room_id}
    await run_in_thread(_table().put_item, Item=item)


async def get_task_by_notification(org: str, message_id: str) -> tuple[str, str, str] | None:
    """``(task_id, holder, room_id)`` for a hold notification's message id; None for any other message."""
    resp = await run_in_thread(_table().get_item, Key={"pk": f"A2A#{org}", "sk": f"NOTIF#{message_id}"})
    item = resp.get("Item")
    return (item["taskId"], item["holder"], item["roomId"]) if item else None


async def _query_all(key_cond, **kw) -> list[dict]:
    items: list[dict] = []
    while True:
        resp = await run_in_thread(_table().query, KeyConditionExpression=key_cond, **kw)
        items += resp.get("Items", [])
        if "LastEvaluatedKey" not in resp:
            return items
        kw["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


async def list_records(org: str, task_id: str) -> list[dict]:
    items = await _query_all(Key("pk").eq(f"A2A#{org}") & Key("sk").begins_with(f"RECORD#{task_id}#"))
    return [{k: v for k, v in i.items() if k != "pk"} for i in items]


async def append_record(org: str, task_id: str, record: dict) -> int:
    """Append a record numbered after the existing ones; retries once if another writer took the number."""
    for attempt in (1, 2):
        n = len(await list_records(org, task_id)) + 1
        try:
            await run_in_thread(
                _table().put_item,
                Item={**record, "pk": f"A2A#{org}", "sk": f"RECORD#{task_id}#{n:06d}"},
                ConditionExpression="attribute_not_exists(sk)",
            )
            return n
        except ClientError as exc:
            if not _is_conflict(exc) or attempt == 2:
                raise
    raise AssertionError("unreachable")


async def list_held_tasks(org: str) -> list[dict]:
    # ponytail: scan of the org's tasks; add a state GSI when an org holds thousands
    items = await _query_all(
        Key("pk").eq(f"A2A#{org}") & Key("sk").begins_with("TASK#"), FilterExpression=Attr("state").eq("held")
    )
    return [_strip(i) for i in items]


async def list_room_tasks(org: str, room_id: str, agent_id: str) -> list[dict]:
    """Every task dispatched to this agent in this room, any state."""
    # ponytail: filtered scan of the org's tasks on every agent reply; add a room GSI when an org holds thousands
    items = await _query_all(
        Key("pk").eq(f"A2A#{org}") & Key("sk").begins_with("TASK#"),
        FilterExpression=Attr("roomId").eq(room_id) & Attr("agentId").eq(agent_id),
    )
    return [_strip(i) for i in items]


async def complete_task(org: str, task_id: str, *, artifact: dict, record: str) -> bool:
    """working -> completed once; False when the task already left working (a retry or a racing reply)."""
    try:
        await run_in_thread(
            _table().update_item,
            Key={"pk": f"A2A#{org}", "sk": f"TASK#{task_id}"},
            UpdateExpression="SET #s = :done, artifact = :a, #r = :r, updatedAt = :t",
            ConditionExpression="#s = :working",
            ExpressionAttributeNames={"#s": "state", "#r": "record"},
            ExpressionAttributeValues={
                ":done": "completed",
                ":working": "working",
                ":a": artifact,
                ":r": record,
                ":t": int(time.time()),
            },
        )
    except ClientError as exc:
        if _is_conflict(exc):
            return False
        raise
    return True
