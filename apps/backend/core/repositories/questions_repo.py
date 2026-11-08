"""Question + `NEEDS#` rows of the `tasks` table (contracts-v2 §3, §7, §14.2; spec 05 §5.4).

A Question opens with one `NEEDS#` row per answerer in the same transaction; answering or cancelling closes it and
deletes those rows in the same transaction. Rows are never deleted. Only `TaskWorkflow` Activities call the writers
(contracts-v2 §7). The row itself refuses an answer from anyone not in its stored `answerers` (defence in depth: the
route and the workflow validator check first).
"""

from __future__ import annotations

import base64
import json
from typing import Any

from botocore.exceptions import ClientError

from core import tasks_schema as ts
from core.dynamodb import ddb_safe, plain, run_in_thread, utc_now_iso
from core.repositories import tasks_repo
from core.repositories.tasks_repo import TasksRepoError, VersionConflict, _get, _is_condition_failure, _row, _table

__all__ = ["InvalidOption", "NotAnswerer", "NotFound", "NotOpen", "VersionConflict"]


class NotFound(TasksRepoError):
    pass


class NotAnswerer(TasksRepoError):
    pass


class InvalidOption(TasksRepoError):
    """The answer names an option the question does not offer."""


class NotOpen(TasksRepoError):
    """The question is already answered or cancelled: who closed it and when (409 `not_open`)."""

    def __init__(self, qid: str, state: str, by: str | None, at: str | None):
        super().__init__(f"question {qid} is {state}")
        self.qid, self.state, self.by, self.at = qid, state, by, at


def _closed(q: dict) -> NotOpen:
    if q["state"] == "cancelled":
        return NotOpen(q["qid"], "cancelled", q.get("cancelledBy"), q.get("cancelledAt"))
    return NotOpen(q["qid"], q["state"], q.get("answeredBy"), q.get("answeredAt"))


def _needs_deletes(table, ws: str, q: dict) -> list[dict]:
    return [
        {"Delete": {"TableName": table.name, "Key": {"pk": ts.ws_pk(ws), "sk": ts.needs_sk(u, q["taskId"], q["qid"])}}}
        for u in q["answerers"]
    ]


async def _query(ws: str, prefix: str, *, limit: int | None = None, cursor: str | None = None):
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": "pk = :pk AND begins_with(sk, :p)",
        "ExpressionAttributeValues": {":pk": ts.ws_pk(ws), ":p": prefix},
        "ConsistentRead": True,
    }
    if limit:
        kwargs["Limit"] = limit
    if cursor:
        kwargs["ExclusiveStartKey"] = json.loads(base64.urlsafe_b64decode(cursor))
    items: list[dict] = []
    while True:
        page = await run_in_thread(_table().query, **kwargs)
        items += [plain(i) for i in page.get("Items", [])]
        last = page.get("LastEvaluatedKey")
        if not last or limit:
            nxt = base64.urlsafe_b64encode(json.dumps(plain(last)).encode()).decode() if last else None
            return items, nxt
        kwargs["ExclusiveStartKey"] = last


async def open_question(ws: str, q: dict) -> tuple[dict, bool]:
    """Q row + one `NEEDS#` per answerer, atomically. A retry (same qid) returns the stored row unchanged."""
    row = await tasks_repo.stamp_channel(ws, _row(ts.Question, ws, {**q, "state": "open"}))
    if await tasks_repo.get_task(ws, row["taskId"]) is None:
        raise TasksRepoError(f"task {row['taskId']} not found")  # checked first: no orphan question rows
    table = _table()
    needs = [
        _row(ts.Needs, ws, {"userId": u, "taskId": row["taskId"], "qid": row["qid"], "kind": row["kind"]})
        | ({"subkind": row["subkind"]} if row.get("subkind") else {})
        for u in row["answerers"]
    ]
    try:
        await run_in_thread(
            table.meta.client.transact_write_items,
            TransactItems=[
                {
                    "Put": {
                        "TableName": table.name,
                        "Item": ddb_safe(row),
                        "ConditionExpression": "attribute_not_exists(sk)",
                    }
                },
                *({"Put": {"TableName": table.name, "Item": ddb_safe(n)}} for n in needs),
            ],
        )
        await _add_to_audience(ws, row["taskId"], row["answerers"])
        return row, True
    except ClientError as exc:
        if not _is_condition_failure(exc):
            raise
    existing = await get_question(ws, row["taskId"], row["qid"])
    if existing is None:
        raise TasksRepoError(f"question {row['qid']} create failed")
    await _add_to_audience(ws, row["taskId"], existing["answerers"])  # a retry finishes what a crash left
    return existing, False


async def _add_to_audience(ws: str, task_id: str, user_ids: list[str]) -> None:
    """contracts-v2 §5: a question's answerers join `Task.audience.userIds` (`can_view` trusts only that list).
    Idempotent; a version race re-reads and retries."""
    for _ in range(5):
        task = await tasks_repo.get_task(ws, task_id)
        if task is None:
            raise TasksRepoError(f"task {task_id} not found")
        audience = dict(task.get("audience") or {})
        current = list(audience.get("userIds") or [])
        missing = [u for u in user_ids if u not in current]
        if not missing:
            return
        audience["userIds"] = current + missing
        try:
            await tasks_repo.update_task_fields(ws, task_id, {"audience": audience}, expected_version=task["version"])
            return
        except VersionConflict:
            continue
    raise VersionConflict(f"task {task_id}: audience update kept racing")


async def get_question(ws: str, task_id: str, qid: str) -> dict | None:
    return await _get(ws, ts.question_sk(task_id, qid))


async def _close(ws: str, q: dict, sets: dict[str, Any], *, extra_cond: str = "", extra_vals: dict | None = None):
    table = _table()
    names = {"#st": "state", "#v": "version", "#u": "updatedAt"}
    vals: dict[str, Any] = {":open": "open", ":one": 1, ":now": utc_now_iso(), **(extra_vals or {})}
    parts = ["#v = #v + :one", "#u = :now"]
    for i, (attr, value) in enumerate(sets.items()):
        names[f"#f{i}"], vals[f":f{i}"] = attr, value
        parts.append(f"#f{i} = :f{i}")
    if "#ans" in extra_cond:
        names["#ans"] = "answerers"
    await run_in_thread(
        table.meta.client.transact_write_items,
        TransactItems=[
            {
                "Update": {
                    "TableName": table.name,
                    "Key": {"pk": ts.ws_pk(ws), "sk": ts.question_sk(q["taskId"], q["qid"])},
                    "UpdateExpression": "SET " + ", ".join(parts),
                    "ConditionExpression": "#st = :open" + extra_cond,
                    "ExpressionAttributeNames": names,
                    "ExpressionAttributeValues": ddb_safe(vals),
                }
            },
            *_needs_deletes(table, ws, q),
        ],
    )


async def record_answer(
    ws: str,
    task_id: str,
    qid: str,
    *,
    option_id: str,
    trigger_id: str | None,
    by: str,
    client_answer_id: str,
    expected_version: int | None,
) -> dict:
    """`open → answered` (a deny is an answer). One winner under concurrent presses; the same `client_answer_id`
    pressed again returns the stored row. Raises `NotFound`, `NotAnswerer`, `NotOpen`, `VersionConflict`."""
    q = await get_question(ws, task_id, qid)
    if q is None:
        raise NotFound(qid)
    if by not in q["answerers"]:
        raise NotAnswerer(f"{by} may not answer {qid}")
    offered = {o["id"] for o in q.get("options") or []} or {"answer"}  # no options: a free-text answer
    if option_id not in offered:
        raise InvalidOption(f"{option_id!r} is not an option of {qid}")
    if q["state"] != "open":
        if q["state"] == "answered" and q.get("clientAnswerId") == client_answer_id:
            return q
        raise _closed(q)
    cond, vals = " AND contains(#ans, :by)", {":by": by}
    if expected_version is not None:
        cond += " AND #v = :ev"
        vals[":ev"] = expected_version
    answer = {"optionId": option_id} | ({"triggerId": trigger_id} if trigger_id else {})
    sets = {
        "state": "answered",
        "answer": answer,
        "answeredBy": by,
        "answeredAt": utc_now_iso(),
        "clientAnswerId": client_answer_id,
    }
    try:
        await _close(ws, q, sets, extra_cond=cond, extra_vals=vals)
    except ClientError as exc:
        if not _is_condition_failure(exc):
            raise
        now = await get_question(ws, task_id, qid)
        if now["state"] == "answered" and now.get("clientAnswerId") == client_answer_id:
            return now
        if now["state"] != "open":
            raise _closed(now) from exc
        raise VersionConflict(f"question {qid}: not at version {expected_version}") from exc
    return await get_question(ws, task_id, qid)


async def set_pending_confirm(ws: str, task_id: str, qid: str, pending: dict) -> None:
    """A voice "yes" waiting for the member's tap (`voice_approve`). Open cards only; the version is left alone so a
    tap with the version the card showed still lands."""
    await run_in_thread(
        _table().update_item,
        Key={"pk": ts.ws_pk(ws), "sk": ts.question_sk(task_id, qid)},
        UpdateExpression="SET pendingConfirm = :p, updatedAt = :now",
        ConditionExpression="#st = :open",
        ExpressionAttributeNames={"#st": "state"},
        ExpressionAttributeValues=ddb_safe({":p": pending, ":now": utc_now_iso(), ":open": "open"}),
    )


async def cancel_open(
    ws: str, task_id: str, *, by: str, reason: str, kinds: tuple[str, ...] | None = None, except_qid: str | None = None
) -> list[str]:
    """Cancel the task's open questions (all, or `kinds` only; stop, supersede). Returns the qids it cancelled."""
    done = []
    for q in await list_open(ws, task_id):
        if (kinds and q["kind"] not in kinds) or q["qid"] == except_qid:
            continue
        try:
            await _close(
                ws, q, {"state": "cancelled", "reason": reason, "cancelledBy": by, "cancelledAt": utc_now_iso()}
            )
            done.append(q["qid"])
        except ClientError as exc:
            if not _is_condition_failure(exc):
                raise  # answered in the meantime: the answer stands
    return done


async def list_for_task(ws: str, task_id: str) -> list[dict]:
    return (await _query(ws, ts.question_sk(task_id, "")))[0]


async def list_open(ws: str, task_id: str) -> list[dict]:
    return [q for q in await list_for_task(ws, task_id) if q["state"] == "open"]


async def needs_for_user(
    ws: str, user_id: str, *, cursor: str | None = None, limit: int = 50
) -> tuple[list[dict], str | None]:
    """The user's `NEEDS#` rows (Waiting on you, Activity, away email). The cursor is opaque."""
    return await _query(ws, ts.needs_sk(user_id, "", "")[:-1], limit=limit, cursor=cursor)
