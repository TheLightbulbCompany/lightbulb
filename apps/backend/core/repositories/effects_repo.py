"""Effect rows of the `tasks` table (contracts-v2 §3, §7, §8).

Effects are append-only puts on a deterministic key (`EFFECT#{taskId}:{attempt}:{toolCallId}`) and move forward
only: proposed → approved | denied → intent → done | unknown (a fresh `proceed` is put straight in `intent`).
`set_effect_state` is for `TaskWorkflow` Activities; `mark_effect_done` is the bridge's `…/effects/{idemKey}/done`.
A `done` row whose `resultRef` starts with `refused:` did NOT happen (a Landing executor refused it, e.g.
`refused:rate_limited`): every surface renders it as "Didn't happen: <reason>", and `questions.gate` treats it like
any `done` row, so the same draft can be asked again. `unknown` = it may have happened; never retried.

An approval is single use: `use_approval` puts `EFFECT#{approvedIdemKey}:use` in the same transaction as the intent it
lets through; `questions.gate` reads the `:use` row.
"""

from __future__ import annotations

from typing import Any, Literal

from botocore.exceptions import ClientError

from core import tasks_schema as ts
from core.dynamodb import ddb_safe, plain, run_in_thread, utc_now_iso
from core.repositories.tasks_repo import IllegalTransition, _get, _is_condition_failure, _put_new, _row, _table

# to-state -> allowed from-states
TRANSITIONS: dict[str, frozenset[str]] = {
    "approved": frozenset({"proposed"}),
    "denied": frozenset({"proposed"}),
    "intent": frozenset(),  # only `use_approval` enters intent from an approval (single use, one transaction)
    "done": frozenset({"intent"}),
    "unknown": frozenset({"intent"}),
}
_SET_FORBIDDEN = frozenset(
    {"pk", "sk", "type", "idemKey", "taskId", "state", "version", "createdAt", "kind", "operands", "operandsHash"}
)


async def put_effect(ws: str, effect: dict) -> tuple[dict, bool]:
    """Append-only; a retried put (same idem key) returns the stored row unchanged."""
    return await _put_new(ws, _row(ts.Effect, ws, effect))


async def get_effect(ws: str, idem_key: str) -> dict | None:
    return await _get(ws, ts.effect_sk(idem_key))


async def _effects(ws: str, prefix: str) -> list[dict]:
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": "pk = :pk AND begins_with(sk, :p)",
        "ExpressionAttributeValues": {":pk": ts.ws_pk(ws), ":p": ts.effect_sk(prefix)},
        "ConsistentRead": True,
    }
    out: list[dict] = []
    while True:
        page = await run_in_thread(_table().query, **kwargs)
        out += [plain(i) for i in page.get("Items", [])]
        if "LastEvaluatedKey" not in page:
            return sorted(out, key=lambda e: e["createdAt"])
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


async def effects_for(ws: str, task_id: str, operands_hash: str) -> list[dict]:
    """The task's Effect (and `:use`) rows with this operands hash, strongly read, oldest first.

    ponytail: one `begins_with EFFECT#{taskId}:` query per call; a task makes a handful of external effects."""
    return [e for e in await _effects(ws, f"{task_id}:") if e.get("operandsHash") == operands_hash]


async def effects_of_task(ws: str, task_id: str) -> list[dict]:
    """The task's Effect rows, oldest first; no `:use` rows."""
    return [e for e in await _effects(ws, f"{task_id}:") if e.get("type") == "effect"]


async def set_effect_state(ws: str, idem_key: str, *, to: str, fields: dict | None = None) -> dict:
    """Move one Effect forward (`TRANSITIONS`); raises `IllegalTransition` otherwise."""
    fields = fields or {}
    if _SET_FORBIDDEN & set(fields):
        raise ValueError(f"may not set {sorted(_SET_FORBIDDEN & set(fields))}")
    allowed = TRANSITIONS[to]
    if not allowed:
        current = await get_effect(ws, idem_key)
        raise IllegalTransition(idem_key, current and current["state"], to)
    names = {"#st": "state", "#v": "version", "#u": "updatedAt", "#sk": "sk"}
    vals: dict[str, Any] = {":to": to, ":one": 1, ":now": utc_now_iso()}
    parts = ["#st = :to", "#v = #v + :one", "#u = :now"]
    for i, (attr, value) in enumerate(fields.items()):
        names[f"#f{i}"], vals[f":f{i}"] = attr, value
        parts.append(f"#f{i} = :f{i}")
    froms = {f":a{i}": s for i, s in enumerate(sorted(allowed))}
    try:
        resp = await run_in_thread(
            _table().update_item,
            Key={"pk": ts.ws_pk(ws), "sk": ts.effect_sk(idem_key)},
            UpdateExpression="SET " + ", ".join(parts),
            ConditionExpression=f"attribute_exists(#sk) AND #st IN ({', '.join(froms)})",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=ddb_safe(vals | froms),
            ReturnValues="ALL_NEW",
        )
        return plain(resp["Attributes"])
    except ClientError as exc:
        if not _is_condition_failure(exc):
            raise
    current = await get_effect(ws, idem_key)
    raise IllegalTransition(idem_key, current and current["state"], to)


async def mark_effect_done(
    ws: str, idem_key: str, *, disposition: Literal["done", "unknown"], result_ref: str | None
) -> dict:
    """`intent → done | unknown`. The same disposition again (a retried callback) returns the row."""
    try:
        return await set_effect_state(ws, idem_key, to=disposition, fields={"resultRef": result_ref})
    except IllegalTransition as exc:
        current = await get_effect(ws, idem_key)
        if current and current["state"] == disposition:
            return current
        raise exc


async def use_approval(ws: str, approved_idem_key: str, operands_hash: str, *, new_effect: dict | None = None) -> bool:
    """Use one approval exactly once, in ONE transaction with the effect it lets through: the `:use` row
    (`attribute_not_exists`) plus either
    - Landing (`new_effect=None`): the approved row itself `approved → intent`, or
    - a Worker's retry: a check that the approved row is still `approved` and the retry's own row put in `intent`.
    So the backend's Landing and a Worker retry racing on one approval can't both proceed. False when it was
    already used (or is no longer `approved`)."""
    table = _table()
    pk = ts.ws_pk(ws)
    now = utc_now_iso()
    use = {
        "pk": pk,
        "sk": ts.effect_sk(f"{approved_idem_key}:use"),
        "type": "effect_use",
        "idemKey": f"{approved_idem_key}:use",
        "usedBy": (new_effect or {}).get("idemKey") or approved_idem_key,
        "operandsHash": operands_hash,
        "createdAt": now,
        "updatedAt": now,
    }
    approved_key = {"pk": pk, "sk": ts.effect_sk(approved_idem_key)}
    names = {"#st": "state"}
    if new_effect is None:
        second = {
            "Update": {
                "TableName": table.name,
                "Key": approved_key,
                "UpdateExpression": "SET #st = :intent, #v = #v + :one, #u = :now",
                "ConditionExpression": "#st = :approved",
                "ExpressionAttributeNames": {**names, "#v": "version", "#u": "updatedAt"},
                "ExpressionAttributeValues": {":intent": "intent", ":approved": "approved", ":one": 1, ":now": now},
            }
        }
        extra: list[dict] = []
    else:
        second = {
            "ConditionCheck": {
                "TableName": table.name,
                "Key": approved_key,
                "ConditionExpression": "#st = :approved",
                "ExpressionAttributeNames": names,
                "ExpressionAttributeValues": {":approved": "approved"},
            }
        }
        row = _row(ts.Effect, ws, {**new_effect, "state": "intent"})
        extra = [
            {
                "Put": {
                    "TableName": table.name,
                    "Item": ddb_safe(row),
                    "ConditionExpression": "attribute_not_exists(sk)",
                }
            }
        ]
    try:
        await run_in_thread(
            table.meta.client.transact_write_items,
            TransactItems=[
                {"Put": {"TableName": table.name, "Item": use, "ConditionExpression": "attribute_not_exists(sk)"}},
                second,
                *extra,
            ],
        )
        return True
    except ClientError as exc:
        if not _is_condition_failure(exc):
            raise
    return False


async def mark_door_used(ws: str, idem_key: str) -> bool:
    """A backend door passes an `intent` row exactly once: `SET doorUsedAt` only while it is unset and the row is
    still `intent`. False when it was used already (or moved on)."""
    try:
        await run_in_thread(
            _table().update_item,
            Key={"pk": ts.ws_pk(ws), "sk": ts.effect_sk(idem_key)},
            UpdateExpression="SET doorUsedAt = :now",
            # The row model writes every field, so an unused marker is stored as NULL, not absent.
            ConditionExpression=(
                "attribute_exists(sk) AND (attribute_not_exists(doorUsedAt) OR attribute_type(doorUsedAt, :null))"
                " AND #st = :intent"
            ),
            ExpressionAttributeNames={"#st": "state"},
            ExpressionAttributeValues={":now": utc_now_iso(), ":intent": "intent", ":null": "NULL"},
        )
        return True
    except ClientError as exc:
        if not _is_condition_failure(exc):
            raise
    return False
