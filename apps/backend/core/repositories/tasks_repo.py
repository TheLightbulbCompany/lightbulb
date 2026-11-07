"""The `tasks` table's Task, Attempt, Subscription + `THREAD#` and Trigger rows (contracts-v2 §3, §4, §7).

Write rules: every put is `attribute_not_exists(sk)` (a retried create returns the existing row); every update
is conditional on `version` or an allowed-status set and bumps `version` and `updatedAt`. Reads are strongly
consistent except GSI queries. Keys come from `core.tasks_schema`; this module never formats one.

Not here (owners, C29): Question → `questions_repo` (05), Effect → `effects_repo` (02), `AGENT#`/META → Lane R,
Schedule → 06, slots/identifier → wave 2.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from botocore.exceptions import ClientError

from core import tasks_schema as ts
from core.dynamodb import ddb_safe, get_table, plain, run_in_thread, utc_now_iso
from core.services import super_agent_faults


class TasksRepoError(Exception):
    pass


class VersionConflict(TasksRepoError):
    pass


class IllegalTransition(TasksRepoError):
    def __init__(self, task_id: str, current: str | None, to: str | None):
        super().__init__(f"task {task_id}: {current} -> {to} not allowed")
        self.task_id, self.current, self.to = task_id, current, to


# to-status -> allowed from-statuses; None = a new row (§4).
TRANSITIONS: dict[str, frozenset[str | None]] = {
    "queued": frozenset({None, "idle", "failed", "resolved"}),
    "working": frozenset({"queued", "waiting"}),
    # queued/idle → waiting: the per-run budget is checked before an attempt starts (§14.8).
    "waiting": frozenset({"working", "queued", "idle"}),
    # resolved/failed → idle: Reopen (Done, open for follow-up; nothing runs until a steer).
    "idle": frozenset({"working", "waiting", "resolved", "failed"}),
    "failed": frozenset({"working"}),
    # `proposed`: rows from before 5 Nov 2026 (proposals removed) can still close (quiet, dismissed).
    "resolved": frozenset({"proposed", "queued", "working", "waiting", "idle", "failed"}),
}

# Attributes a field update may never touch: keys, the status machine and the writer-owned counters.
_PROTECTED = frozenset(
    {"pk", "sk", "type", "taskId", "n", "threadKey", "status", "statusKey", "statusReason", "version", "createdAt"}
)


def _table():
    return get_table("tasks")


def _is_condition_failure(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") in (
        "ConditionalCheckFailedException",
        "TransactionCanceledException",
    )


async def _get(ws: str, sk: str) -> dict | None:
    resp = await run_in_thread(_table().get_item, Key={"pk": ts.ws_pk(ws), "sk": sk}, ConsistentRead=True)
    item = resp.get("Item")
    return plain(item) if item else None


_LIVE_PREFIXES = ("TASK#", "EFFECT#")


async def stamp_channel(ws: str, item: dict) -> dict:
    """A task, attempt, question or effect row carries the workspace's live channel segment (`/ws/{wsChannel}/tasks`,
    O9): the projector publishes `task.changed` only for rows that carry it. Updates are update_item, so the stamp
    survives them."""
    if item.get("wsChannel") or not str(item.get("sk", "")).startswith(_LIVE_PREFIXES):
        return item
    from core.repositories import messaging_repo  # lazy: messaging_repo is the heavier module

    return {**item, "wsChannel": await messaging_repo.ensure_workspace_id(ws)}


async def _put_new(ws: str, item: dict) -> tuple[dict, bool]:
    """Put unless the row exists; on a retry return the stored row unchanged."""
    item = await stamp_channel(ws, item)
    try:
        super_agent_faults.maybe_conflict()  # dev fault hook: conditional_conflict:<pct>
        await run_in_thread(_table().put_item, Item=ddb_safe(item), ConditionExpression="attribute_not_exists(sk)")
        return item, True
    except ClientError as exc:
        if not _is_condition_failure(exc):
            raise
    existing = await _get(ws, item["sk"])
    if existing is None:  # deleted between the put and the read: not something wave 1 does
        raise TasksRepoError(f"row {item['sk']} vanished after a failed create")
    return existing, False


def _row(model: type[ts._Row], ws: str, data: dict) -> dict:
    now = utc_now_iso()
    if "ws_channel" not in model.model_fields:  # writer-owned (`stamp_channel`): a copied stored row may carry it
        data = {k: v for k, v in data.items() if k != "wsChannel"}
    return plain(model.model_validate({**data, "ws": ws, "version": 1, "createdAt": now, "updatedAt": now}).to_item())


async def _update(
    ws: str,
    sk: str,
    sets: dict[str, Any],
    *,
    condition: str,
    values: dict[str, Any],
    remove: tuple[str, ...] = (),
    bump: bool = True,
) -> dict:
    """SET `sets`, `version = version + 1`, `updatedAt`; REMOVE `remove`; under `condition`. Returns the new row.
    `bump=False` leaves `version` alone: the projector publishes no `task.changed` for the write."""
    names: dict[str, str] = {"#u": "updatedAt", "#sk": "sk"}
    vals: dict[str, Any] = {":now": utc_now_iso(), **values}
    parts = ["#u = :now"]
    if bump:
        names["#v"], vals[":one"] = "version", 1
        parts.insert(0, "#v = #v + :one")
    for i, (attr, value) in enumerate(sets.items()):
        names[f"#f{i}"] = attr
        vals[f":f{i}"] = value
        parts.append(f"#f{i} = :f{i}")
    expr = "SET " + ", ".join(parts)
    if remove:
        for i, attr in enumerate(remove):
            names[f"#r{i}"] = attr
        expr += " REMOVE " + ", ".join(f"#r{i}" for i in range(len(remove)))
    if "#s" in condition:
        names["#s"] = "status"
    if "#v" in condition:
        names["#v"] = "version"
    super_agent_faults.maybe_conflict()  # dev fault hook: conditional_conflict:<pct>
    resp = await run_in_thread(
        _table().update_item,
        Key={"pk": ts.ws_pk(ws), "sk": sk},
        UpdateExpression=expr,
        ConditionExpression=f"attribute_exists(#sk) AND {condition}",
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=ddb_safe(vals),
        ReturnValues="ALL_NEW",
    )
    return plain(resp["Attributes"])


def _status_in(allowed: frozenset[str | None]) -> tuple[str, dict[str, str]]:
    values = {f":a{i}": s for i, s in enumerate(sorted(s for s in allowed if s is not None))}
    return f"#s IN ({', '.join(values)})", values


def _check_fields(fields: dict) -> None:
    bad = _PROTECTED & set(fields)
    if bad:
        raise ValueError(f"field update may not set {sorted(bad)}")


# --- Task ------------------------------------------------------------------------------------------


async def create_task(ws: str, task: dict) -> tuple[dict, bool]:
    if task.get("status") != "queued":
        raise ValueError("a task is created queued")
    return await _put_new(ws, _row(ts.Task, ws, task))


async def get_task(ws: str, task_id: str) -> dict | None:
    return await _get(ws, ts.task_sk(task_id))


async def get_task_tree(ws: str, task_id: str) -> dict:
    """The task row with its attempts, subscriptions and Question rows (Lane Q writes them under the task's
    prefix), from one consistent `begins_with` query."""
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": "pk = :pk AND begins_with(sk, :prefix)",
        "ExpressionAttributeValues": {":pk": ts.ws_pk(ws), ":prefix": ts.task_sk(task_id)},
        "ConsistentRead": True,
    }
    tree: dict[str, Any] = {"task": None, "attempts": [], "subscriptions": [], "questions": []}
    while True:
        page = await run_in_thread(_table().query, **kwargs)
        for item in map(plain, page.get("Items", [])):
            if item.get("type") == "task":
                tree["task"] = item
            elif item.get("type") == "attempt":
                tree["attempts"].append(item)
            elif item.get("type") == "subscription":
                tree["subscriptions"].append(item)
            elif item.get("type") == "question":
                tree["questions"].append(item)
        if "LastEvaluatedKey" not in page:
            return tree
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


async def transition_task(
    ws: str,
    task_id: str,
    *,
    to: str,
    reason: str | None = None,
    fields: dict | None = None,
    expected_version: int | None = None,
) -> dict:
    if to not in TRANSITIONS:
        raise ValueError(f"unknown status {to!r}")
    fields = fields or {}
    _check_fields(fields)
    if TRANSITIONS[to] == {None}:  # only a new row may enter it (proposed)
        current = await get_task(ws, task_id)
        raise IllegalTransition(task_id, current and current["status"], to)
    condition, values = _status_in(TRANSITIONS[to])
    if expected_version is not None:
        condition += " AND #v = :ev"
        values[":ev"] = expected_version
    sets = {**fields, "status": to, "statusKey": ts.status_key(ws, to)}
    if reason is not None:
        sets["statusReason"] = reason
    try:
        return await _update(
            ws,
            ts.task_sk(task_id),
            sets,
            condition=condition,
            values=values,
            remove=() if reason is not None else ("statusReason",),
        )
    except ClientError as exc:
        if not _is_condition_failure(exc):
            raise
    current = await get_task(ws, task_id)
    if current is not None and expected_version is not None and current["version"] != expected_version:
        raise VersionConflict(f"task {task_id}: version {current['version']} != {expected_version}")
    raise IllegalTransition(task_id, current and current["status"], to)


async def update_task_fields(ws: str, task_id: str, fields: dict, *, expected_version: int) -> dict:
    _check_fields(fields)
    try:
        return await _update(ws, ts.task_sk(task_id), fields, condition="#v = :ev", values={":ev": expected_version})
    except ClientError as exc:
        if not _is_condition_failure(exc):
            raise
    raise VersionConflict(f"task {task_id}: not at version {expected_version} (or missing)")


MAX_OUTPUTS = 50  # ponytail: the newest 50 per task on its row; an OUT# row per output if a task makes more


async def add_task_output(ws: str, task_id: str, output: dict) -> bool:
    """Append one `TaskOutput` to the task's `outputs` unless its `id` is there (a retried write). Bumps the version,
    so the projector tells clients. False when it was there already or the task is gone."""
    item = ts.TaskOutput.model_validate({"createdAt": utc_now_iso(), **output}).model_dump(by_alias=True)
    for _ in range(5):
        task = await get_task(ws, task_id)
        if task is None:
            return False
        outputs = task.get("outputs") or []
        if any(o.get("id") == item["id"] for o in outputs):
            return False
        try:
            await update_task_fields(
                ws, task_id, {"outputs": [*outputs, item][-MAX_OUTPUTS:]}, expected_version=task["version"]
            )
            return True
        except VersionConflict:
            continue  # the workflow moved the row under us: read it again
    raise VersionConflict(f"task {task_id}: outputs write kept losing the race")


async def list_tasks_by_status(
    ws: str, status: str, *, limit: int = 50, cursor: str | None = None, owner_user_id: str | None = None
) -> tuple[list[dict], str | None]:
    """GSI `by-status`, newest first. The cursor is the urlsafe-b64 JSON of `LastEvaluatedKey`. `owner_user_id`
    filters server-side (a page can then come back short or empty with a cursor: `Limit` counts items read)."""
    kwargs: dict[str, Any] = {
        "IndexName": "by-status",
        "KeyConditionExpression": "statusKey = :k",
        "ExpressionAttributeValues": {":k": ts.status_key(ws, status)},
        "ScanIndexForward": False,
        "Limit": limit,
    }
    if owner_user_id:
        kwargs["FilterExpression"] = "ownerUserId = :o"
        kwargs["ExpressionAttributeValues"][":o"] = owner_user_id
    if cursor:
        kwargs["ExclusiveStartKey"] = json.loads(base64.urlsafe_b64decode(cursor))
    page = await run_in_thread(_table().query, **kwargs)
    last = page.get("LastEvaluatedKey")
    nxt = base64.urlsafe_b64encode(json.dumps(plain(last)).encode()).decode() if last else None
    return [plain(i) for i in page.get("Items", [])], nxt


# --- Attempt ---------------------------------------------------------------------------------------


async def put_attempt(ws: str, task_id: str, n: int, fields: dict) -> tuple[dict, bool]:
    data = {"runId": ts.run_id(ws, task_id, n), **fields, "taskId": task_id, "n": n}
    for owned in ("pk", "sk", "version", "createdAt", "updatedAt", "type", "noteCount"):  # noteCount: count_note
        data.pop(owned, None)
    return await _put_new(ws, _row(ts.Attempt, ws, data))


async def count_note(ws: str, task_id: str, n: int, cap: int) -> bool:
    """Count one `task_note` on the attempt; False once it has `cap` (no version bump: not a state change)."""
    try:
        await run_in_thread(
            _table().update_item,
            Key={"pk": ts.ws_pk(ws), "sk": ts.attempt_sk(task_id, n)},
            UpdateExpression="ADD #c :one",
            ConditionExpression="attribute_exists(sk) AND (attribute_not_exists(#c) OR #c < :cap)",
            ExpressionAttributeNames={"#c": "noteCount"},
            ExpressionAttributeValues={":one": 1, ":cap": cap},
        )
        return True
    except ClientError as exc:
        if not _is_condition_failure(exc):
            raise
    return False


async def get_attempt(ws: str, task_id: str, n: int) -> dict | None:
    return await _get(ws, ts.attempt_sk(task_id, n))


async def update_attempt(
    ws: str, task_id: str, n: int, fields: dict, *, allowed_status: frozenset[str], bump: bool = True
) -> dict:
    """Update an attempt only while its status is in `allowed_status`; `fields` may move `status` itself.
    `bump=False`: no version change, so no live event (the bridge's 2-minute heartbeat)."""
    _check_fields({k: v for k, v in fields.items() if k != "status"})
    condition, values = _status_in(allowed_status)
    try:
        return await _update(ws, ts.attempt_sk(task_id, n), fields, condition=condition, values=values, bump=bump)
    except ClientError as exc:
        if not _is_condition_failure(exc):
            raise
    current = await get_attempt(ws, task_id, n)
    raise IllegalTransition(task_id, current and current["status"], fields.get("status"))


# --- Subscription + THREAD# ------------------------------------------------------------------------


async def subscribe(ws: str, task_id: str, thread_key: str, *, created_by: str) -> None:
    """SUB and `THREAD#` in one transaction. A thread held (active) by another task refuses; an inactive one
    is taken over and its previous task id goes into `history`."""
    prev = await _get(ws, ts.thread_sk(thread_key))
    if prev and prev["active"] and prev["taskId"] != task_id:
        raise TasksRepoError(f"thread {thread_key} belongs to task {prev['taskId']}")
    history = list(prev["history"]) if prev else []
    if prev and prev["taskId"] != task_id:
        history.append(prev["taskId"])
    sub = _row(ts.Subscription, ws, {"taskId": task_id, "threadKey": thread_key, "createdBy": created_by})
    thread = _row(ts.Thread, ws, {"threadKey": thread_key, "taskId": task_id, "history": history})
    if prev:
        thread |= {"version": prev["version"] + 1, "createdAt": prev["createdAt"]}
        guard = {
            "ConditionExpression": "#v = :v",
            "ExpressionAttributeNames": {"#v": "version"},
            "ExpressionAttributeValues": {":v": prev["version"]},
        }
    else:
        guard = {"ConditionExpression": "attribute_not_exists(sk)"}
    table = _table()
    try:
        await run_in_thread(
            table.meta.client.transact_write_items,
            TransactItems=[
                {"Put": {"TableName": table.name, "Item": ddb_safe(sub)}},
                {"Put": {"TableName": table.name, "Item": ddb_safe(thread), **guard}},
            ],
        )
    except ClientError as exc:
        if not _is_condition_failure(exc):
            raise
        raise TasksRepoError(f"thread {thread_key} changed while subscribing; retry") from exc


async def unsubscribe(ws: str, task_id: str, thread_key: str) -> None:
    table = _table()
    pk = ts.ws_pk(ws)
    now = utc_now_iso()

    def deactivate(sk: str, condition: str, values: dict) -> dict:
        return {
            "Update": {
                "TableName": table.name,
                "Key": {"pk": pk, "sk": sk},
                "UpdateExpression": "SET #a = :f, #v = #v + :one, #u = :now",
                "ConditionExpression": condition,
                "ExpressionAttributeNames": {"#a": "active", "#v": "version", "#u": "updatedAt"},
                "ExpressionAttributeValues": {":f": False, ":one": 1, ":now": now, **values},
            }
        }

    try:
        await run_in_thread(
            table.meta.client.transact_write_items,
            TransactItems=[
                deactivate(ts.subscription_sk(task_id, thread_key), "attribute_exists(sk)", {}),
                deactivate(ts.thread_sk(thread_key), "taskId = :t", {":t": task_id}),
            ],
        )
    except ClientError as exc:
        if not _is_condition_failure(exc):
            raise
        raise TasksRepoError(f"task {task_id} is not subscribed to {thread_key}") from exc


async def unsubscribe_all(ws: str, task_id: str) -> None:
    """Every active subscription of the task, deactivated (a thread another task took over is skipped)."""
    for sub in (await get_task_tree(ws, task_id))["subscriptions"]:
        if sub.get("active", True):
            try:
                await unsubscribe(ws, task_id, sub["threadKey"])
            except TasksRepoError:
                pass


async def resubscribe_router_threads(ws: str, task_id: str) -> None:
    """The task's Router-made threads it let go of when it ended, back (one another task took meanwhile stays)."""
    for sub in (await get_task_tree(ws, task_id))["subscriptions"]:
        if not sub.get("active", True) and sub.get("createdBy") == "router":
            try:
                await subscribe(ws, task_id, sub["threadKey"], created_by="router")
            except TasksRepoError:
                pass


async def task_for_thread(ws: str, thread_key: str) -> str | None:
    row = await _get(ws, ts.thread_sk(thread_key))
    return row["taskId"] if row and row["active"] else None


# --- Trigger ---------------------------------------------------------------------------------------


async def put_trigger(ws: str, trigger: dict) -> tuple[dict, bool]:
    return await _put_new(ws, _row(ts.Trigger, ws, trigger))


async def set_trigger_routed_to(ws: str, trigger_id: str, routed: dict[str, str]) -> dict:
    """Where the Router sent a trigger (`routedTo`, append-only row: this is the one field that changes)."""
    return await _update(
        ws, ts.trigger_sk(trigger_id), {"routedTo": routed}, condition="#v >= :zero", values={":zero": 0}
    )


async def next_identifier(ws: str) -> str:
    """`LB-{n}`: monotonic per workspace (one atomic ADD on the `TASKSEQ` row; gaps are fine)."""
    resp = await run_in_thread(
        _table().update_item,
        Key={"pk": ts.ws_pk(ws), "sk": ts.task_seq_sk()},
        UpdateExpression="ADD #n :one",
        ExpressionAttributeNames={"#n": "n"},
        ExpressionAttributeValues={":one": 1},
        ReturnValues="UPDATED_NEW",
    )
    return f"LB-{int(resp['Attributes']['n'])}"


async def get_trigger(ws: str, trigger_id: str) -> dict | None:
    return await _get(ws, ts.trigger_sk(trigger_id))


async def from_contact(ws: str, task: dict) -> bool:
    """Whether the task was started for an outside contact (a phone caller): its trigger's `ref.origin`, which only the
    backend writes (routers/voice_phone.py). ponytail: the task's own trigger only; a sub-task it starts inherits
    nothing yet."""
    if not task.get("triggerId"):
        return False
    return (((await get_trigger(ws, task["triggerId"])) or {}).get("ref") or {}).get("origin") in (
        "phone_contact",
        "meeting",
    )


# --- Creation (`CAP#`) -----------------------------------------------------------------------------


async def put_creation(ws: str, creation: dict) -> tuple[dict, bool]:
    """First writer wins: the request that made it stays the one that made it."""
    return await _put_new(ws, _row(ts.Creation, ws, creation))


async def list_creations(ws: str) -> list[dict]:
    """The workspace's Creation rows (the watchdog's `CAP#act#…` caps share the prefix: filtered by type)."""
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": "pk = :pk AND begins_with(sk, :prefix)",
        "ExpressionAttributeValues": {":pk": ts.ws_pk(ws), ":prefix": "CAP#"},
    }
    out: list[dict] = []
    while True:
        page = await run_in_thread(_table().query, **kwargs)
        out += [i for i in map(plain, page.get("Items", [])) if i.get("type") == "creation"]
        if "LastEvaluatedKey" not in page:
            return out
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


# --- Generic (C29) ---------------------------------------------------------------------------------


async def batch_get(ws: str, sks: list[str]) -> list[dict]:
    """Strongly read many rows of one workspace, 100 per request; missing rows are absent."""
    table = _table()
    pk = ts.ws_pk(ws)
    unique = list(dict.fromkeys(sks))
    out: list[dict] = []
    for start in range(0, len(unique), 100):
        request: dict | None = {
            table.name: {"Keys": [{"pk": pk, "sk": sk} for sk in unique[start : start + 100]], "ConsistentRead": True}
        }
        while request:
            resp = await run_in_thread(table.meta.client.batch_get_item, RequestItems=request)
            out.extend(plain(resp.get("Responses", {}).get(table.name, [])))
            request = resp.get("UnprocessedKeys") or None
    return out


async def claim_attempt_terminal(ws: str, task_id: str, n: int, fields: dict) -> bool:
    """Record the bridge's terminal event once: False when one was already recorded (a replayed POST)."""
    try:
        await _update(
            ws,
            ts.attempt_sk(task_id, n),
            fields,
            # created rows carry terminalStatus = null
            condition="(attribute_not_exists(terminalStatus) OR attribute_type(terminalStatus, :nul))",
            values={":nul": "NULL"},
        )
        return True
    except ClientError as exc:
        if not _is_condition_failure(exc):
            raise
    return False


async def unclaim_attempt_terminal(ws: str, task_id: str, n: int) -> None:
    await _update(ws, ts.attempt_sk(task_id, n), {"terminalStatus": None}, condition="attribute_exists(#sk)", values={})


# --- Slots (contracts §9: Worker leases per workspace) ----------------------------------------------


async def get_slots(ws: str) -> dict:
    return await _get(ws, ts.slots_sk()) or {"leases": {}, "waiters": [], "version": 0}


async def put_slots(ws: str, leases: dict[str, int], waiters: list[str], *, expected_version: int) -> bool:
    """Write the SLOTS row if nobody else did since `expected_version` (0 = absent). False on a lost race."""
    item = {
        "pk": ts.ws_pk(ws),
        "sk": ts.slots_sk(),
        "type": "slots",
        "leases": leases,
        "waiters": waiters,
        "version": expected_version + 1,
        "updatedAt": utc_now_iso(),
    }
    cond = "attribute_not_exists(sk)" if expected_version == 0 else "version = :ev"
    kwargs: dict[str, Any] = {"Item": ddb_safe(item), "ConditionExpression": cond}
    if expected_version:
        kwargs["ExpressionAttributeValues"] = {":ev": expected_version}
    try:
        await run_in_thread(_table().put_item, **kwargs)
        return True
    except ClientError as exc:
        if not _is_condition_failure(exc):
            raise
    return False
