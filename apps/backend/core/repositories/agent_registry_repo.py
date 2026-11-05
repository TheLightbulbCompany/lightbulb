"""The `tasks` table's `AGENT#` specialist rows and their `AGENTID#` reverse pointers (spec 03 §5.1).

Write rules (contracts-v2 §7): a new agent is one `TransactWriteItems` of the `AGENT#` row and the reverse row,
both `attribute_not_exists(pk)`, so an agent id belongs to exactly one workspace; a field update is conditional on
the row existing and bumps `version` and `updatedAt`. Reads are strongly consistent. Keys come from
`core.tasks_schema`. Only written while the super-agent env master is on (callers check; prod has no table).
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError

from core import tasks_schema as ts
from core.dynamodb import ddb_safe, get_table, plain, run_in_thread, utc_now_iso


class AgentConflict(Exception):
    """The agent id is already registered under another workspace."""


# Attributes `upsert_fields` never sets: keys and writer-owned counters.
_PROTECTED = frozenset({"pk", "sk", "type", "agentId", "version", "createdAt", "updatedAt"})


def _table():
    return get_table("tasks")


async def get(ws: str, agent_id: str) -> dict | None:
    resp = await run_in_thread(
        _table().get_item, Key={"pk": ts.ws_pk(ws), "sk": ts.agent_sk(agent_id)}, ConsistentRead=True
    )
    item = resp.get("Item")
    return plain(item) if item else None


async def list_agents(ws: str) -> list[dict]:
    """Every `AGENT#` row of the workspace, `deleted` ones included."""
    rows: list[dict] = []
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": "pk = :pk AND begins_with(sk, :p)",
        "ExpressionAttributeValues": {":pk": ts.ws_pk(ws), ":p": ts.agent_sk("")},
        "ConsistentRead": True,
    }
    while True:
        resp = await run_in_thread(_table().query, **kwargs)
        rows.extend(plain(i) for i in resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            return rows
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


async def owner_of(agent_id: str) -> str | None:
    resp = await run_in_thread(
        _table().get_item, Key={"pk": ts.agent_owner_pk(agent_id), "sk": ts.AGENT_OWNER_SK}, ConsistentRead=True
    )
    return (resp.get("Item") or {}).get("ws")


async def put_new(ws: str, row: dict) -> tuple[dict, bool]:
    """Create `row` (camelCase `Agent` fields, no keys) and its reverse pointer. Same id already in `ws` →
    `(existing, False)`; in another workspace → `AgentConflict`."""
    now = utc_now_iso()
    item = plain(ts.Agent.model_validate({**row, "ws": ws, "version": 1, "createdAt": now, "updatedAt": now}).to_item())
    agent_id = item["agentId"]
    table_name = _table().name
    try:
        await run_in_thread(
            _table().meta.client.transact_write_items,
            TransactItems=[
                {
                    "Put": {
                        "TableName": table_name,
                        "Item": ddb_safe(item),
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                },
                {
                    "Put": {
                        "TableName": table_name,
                        "Item": {"pk": ts.agent_owner_pk(agent_id), "sk": ts.AGENT_OWNER_SK, "ws": ws},
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                },
            ],
        )
        return item, True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            raise
    owner = await owner_of(agent_id)
    existing = await get(ws, agent_id) if owner == ws else None
    if existing is None:
        raise AgentConflict(f"agent {agent_id} is registered under another workspace")
    return existing, False


async def upsert_fields(ws: str, agent_id: str, fields: dict[str, Any]) -> dict:
    """SET `fields` (camelCase), `version + 1`, `updatedAt` on an existing row. Returns the new row."""
    bad = _PROTECTED & set(fields)
    if bad:
        raise ValueError(f"field update may not set {sorted(bad)}")
    names = {"#v": "version", "#u": "updatedAt", "#sk": "sk"}
    values: dict[str, Any] = {":one": 1, ":now": utc_now_iso()}
    parts = ["#v = #v + :one", "#u = :now"]
    for i, (attr, value) in enumerate(fields.items()):
        names[f"#f{i}"], values[f":f{i}"] = attr, value
        parts.append(f"#f{i} = :f{i}")
    resp = await run_in_thread(
        _table().update_item,
        Key={"pk": ts.ws_pk(ws), "sk": ts.agent_sk(agent_id)},
        UpdateExpression="SET " + ", ".join(parts),
        ConditionExpression="attribute_exists(#sk)",
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=ddb_safe(values),
        ReturnValues="ALL_NEW",
    )
    return plain(resp["Attributes"])


async def mark_deleted(ws: str, agent_id: str) -> dict:
    """Soft delete: the id stays reserved (reverse row kept)."""
    return await upsert_fields(ws, agent_id, {"status": "deleted"})
