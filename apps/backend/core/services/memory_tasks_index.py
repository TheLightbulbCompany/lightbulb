"""Task cards in memory (spec 07 §3.4, wave2-M Task 5): one strategy-less record per task in
``/tasks/<o>/`` (or ``/tasks/<o>:<owner>/`` for a private task) so recall's task arm can find it.

The card is a pointer, never the truth: recall re-reads the live task row before it shows a link.
A ``MEMTASK#{taskId}`` row in the messaging table names the current record and the task version it
holds, so an older index never replaces a newer one. No Task-row write (contracts-v2 §7). Best
effort: a failure logs its type and never raises.
"""

import logging
from datetime import datetime, timezone

from botocore.exceptions import ClientError

from core.config import settings
from core.dynamodb import run_in_thread
from core.services import memory_sync, secret_shaped, workspace_memory
from core.services.super_agent_flags import flags_for

logger = logging.getLogger(__name__)


def _sk(task_id: str) -> str:
    return f"MEMTASK#{task_id}"


def _namespace(ws: str, task: dict) -> str:
    if task.get("lane") == "private":
        owner = task.get("ownerUserId")
        if not owner:
            raise ValueError("a private task without its owner has no lane")
        return workspace_memory.namespaces_for(ws, owner)["private_tasks"]
    return workspace_memory.namespaces_for(ws, None)["tasks"]


def card_text(task: dict) -> str:
    """Title and description are redacted before anything reaches memory (description before the cut)."""
    title = secret_shaped.redact(task["title"])
    description = secret_shaped.redact(task.get("description") or "")[:200]
    return f"{task['identifier']} {title} — {task['status']} · {task.get('specialist') or ''}\n{description}"


def _claim(ws: str, task_id: str, version: int, record_id: str | None) -> bool:
    """Point MEMTASK# at ``record_id`` unless a version at least as new is already there."""
    try:
        memory_sync._t().put_item(
            Item={**memory_sync._key(ws, _sk(task_id)), "recordId": record_id or "", "version": version},
            ConditionExpression="attribute_not_exists(sk) OR version < :v",
            ExpressionAttributeValues={":v": version},
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def _delete(client, memory_id: str, record_id: str | None) -> None:
    if record_id:
        resp = client.batch_delete_memory_records(memoryId=memory_id, records=[{"memoryRecordId": record_id}])
        memory_sync._write_ok(resp, "task card", gone_ok=True)


def _index(ws: str, task: dict, memory_id: str) -> None:
    version = int(task["version"])
    prev = memory_sync._get(ws, _sk(task["taskId"]))
    if prev and int(prev.get("version", 0)) >= version:
        return
    if task.get("hidden"):
        if _claim(ws, task["taskId"], version, None):
            _delete(workspace_memory._client(), memory_id, prev.get("recordId"))
        return
    ns = _namespace(ws, task)
    request_id = f"{task['taskId']}-v{version}"
    client = workspace_memory._client()
    resp = client.batch_create_memory_records(
        memoryId=memory_id,
        clientToken=memory_sync._token([request_id]),
        records=[
            {
                "requestIdentifier": request_id,
                "namespaces": [ns],
                "content": {"text": card_text(task)},
                "timestamp": datetime.fromisoformat(task["updatedAt"])
                if task.get("updatedAt")
                else datetime.now(timezone.utc),
                "metadata": memory_sync._strings(
                    taskId=task["taskId"],
                    status=task.get("status") or "",
                    specialist=task.get("specialist") or "",
                    lane=task.get("lane") or "",
                ),
            }
        ],
    )
    memory_sync._write_ok(resp, "task card")
    record_id = resp["successfulRecords"][0]["memoryRecordId"]
    if _claim(ws, task["taskId"], version, record_id):
        _delete(client, memory_id, prev.get("recordId"))  # the old card only after the new one is in
    elif (memory_sync._get(ws, _sk(task["taskId"])) or {}).get("recordId") != record_id:
        # a newer version won the race; a duplicate event got the live record back from clientToken
        _delete(client, memory_id, record_id)


async def _run(ws: str, task: dict) -> None:
    memory_id = settings.AGENTCORE_MEMORY_ID
    if not memory_id:
        return
    try:
        if not await flags_for(ws):
            return
        await run_in_thread(_index, ws, task, memory_id)
    except Exception as exc:  # noqa: BLE001 -- recall falls back to turns; the next change re-indexes
        logger.warning("task card index failed ws=%s task=%s error=%s", ws, task.get("taskId"), type(exc).__name__)


async def index_task(ws: str, task: dict) -> None:
    """Write (or replace) ``task``'s card; a hidden task is unindexed."""
    await _run(ws, task)


async def unindex_task(ws: str, task: dict) -> None:
    """Remove ``task``'s card; its version stays on the pointer so an older index cannot restore it."""
    await _run(ws, {**task, "hidden": True})
