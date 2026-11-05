"""Effect rows of the `tasks` table (contracts-v2 §3, §7, §8).

Effects are append-only puts on a deterministic key (`EFFECT#{taskId}:{attempt}:{toolCallId}`) and move forward
only: proposed → approved | denied → intent → done | unknown (a fresh `proceed` is put straight in `intent`).
`set_effect_state` is for `TaskWorkflow` Activities; `mark_effect_done` is the bridge's `…/effects/{idemKey}/done`.
An approval is single use: `consume_approval` puts `EFFECT#{approvedIdemKey}:use`, which `questions.gate` reads.
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
    "intent": frozenset({"approved"}),
    "done": frozenset({"intent"}),
    "unknown": frozenset({"intent"}),
}
_SET_FORBIDDEN = frozenset({"pk", "sk", "type", "idemKey", "taskId", "state", "version", "createdAt"})


async def put_effect(ws: str, effect: dict) -> tuple[dict, bool]:
    """Append-only; a retried put (same idem key) returns the stored row unchanged."""
    return await _put_new(ws, _row(ts.Effect, ws, effect))


async def get_effect(ws: str, idem_key: str) -> dict | None:
    return await _get(ws, ts.effect_sk(idem_key))


async def effects_for(ws: str, task_id: str, operands_hash: str) -> list[dict]:
    """The task's Effect (and `:use`) rows with this operands hash, strongly read, oldest first.

    ponytail: one `begins_with EFFECT#{taskId}:` query per call; a task makes a handful of external effects."""
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": "pk = :pk AND begins_with(sk, :p)",
        "ExpressionAttributeValues": {":pk": ts.ws_pk(ws), ":p": ts.effect_sk(f"{task_id}:")},
        "ConsistentRead": True,
    }
    out: list[dict] = []
    while True:
        page = await run_in_thread(_table().query, **kwargs)
        out += [plain(i) for i in page.get("Items", []) if i.get("operandsHash") == operands_hash]
        if "LastEvaluatedKey" not in page:
            return sorted(out, key=lambda e: e["createdAt"])
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


async def set_effect_state(ws: str, idem_key: str, *, to: str, fields: dict | None = None) -> dict:
    """Move one Effect forward (`TRANSITIONS`); raises `IllegalTransition` otherwise."""
    fields = fields or {}
    if _SET_FORBIDDEN & set(fields):
        raise ValueError(f"may not set {sorted(_SET_FORBIDDEN & set(fields))}")
    allowed = TRANSITIONS[to]
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


async def consume_approval(ws: str, approved_idem_key: str, new_idem_key: str, operands_hash: str) -> bool:
    """Use an approval once: a conditional put of `EFFECT#{approved}:use`. False if it was already used."""
    now = utc_now_iso()
    item = {
        "pk": ts.ws_pk(ws),
        "sk": ts.effect_sk(f"{approved_idem_key}:use"),
        "type": "effect_use",
        "idemKey": f"{approved_idem_key}:use",
        "usedBy": new_idem_key,
        "operandsHash": operands_hash,
        "createdAt": now,
        "updatedAt": now,
    }
    try:
        await run_in_thread(_table().put_item, Item=item, ConditionExpression="attribute_not_exists(sk)")
        return True
    except ClientError as exc:
        if not _is_condition_failure(exc):
            raise
    return False
