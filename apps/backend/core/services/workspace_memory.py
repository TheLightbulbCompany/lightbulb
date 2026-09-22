"""Workspace memory: the per-env AgentCore Memory every agent of a workspace shares.

Runtimes read and write it themselves (the image's ``agentcore`` memory provider, under the
per-owner ``owner-memory`` inline policy -- ``bedrock_aip.ensure_owner_memory_policy``). The
backend only ever DELETES from it, when an owner's data is wiped. An owner's data is:

- the long-term records AgentCore extracted into ``/owners/<owner>/`` (exact namespace -- never
  ``namespacePath``, whose prefix match would also reach ``/owners/<owner>x/``), and
- the raw events under ``actorId = <owner>``, one set per session.

Deletes are eventually consistent (gone from reads within ~22 s in the 2026-09-22 dev spike).
Empty session/actor shells stay listed until their events would have expired; AgentCore has no
API to delete them, and they carry no content.
"""

import asyncio
import logging

import boto3

from core.config import settings
from core.dynamodb import run_in_thread

logger = logging.getLogger(__name__)

_BATCH = 100  # BatchDeleteMemoryRecords' per-call cap
#: Extraction lands records 96-133 s after an event (dev spike), so events written just before a
#: wipe can still produce records after its sweep. One late re-sweep catches them.
RESWEEP_AFTER_S = 180.0
_resweeps: set[asyncio.Task] = set()


def owner_namespace(owner_id: str) -> str:
    # The strategy's template is /owners/{actorId}/ -- a "/" in the id would widen or escape it.
    if not owner_id or "/" in owner_id or ".." in owner_id:
        raise ValueError(f"refusing unsafe owner id for a memory namespace: {owner_id!r}")
    return f"/owners/{owner_id}/"


def _client():
    return boto3.client("bedrock-agentcore", region_name=settings.AWS_REGION)


def _pages(call, key: str, **kwargs):
    token = None
    while True:
        resp = call(**kwargs, **({"nextToken": token} if token else {}))
        yield from resp.get(key, [])
        token = resp.get("nextToken")
        if not token:
            return


def _delete_records(client, owner_id: str, namespace: str, memory_id: str) -> int:
    record_ids = [
        r["memoryRecordId"]
        for r in _pages(client.list_memory_records, "memoryRecordSummaries", memoryId=memory_id, namespace=namespace)
    ]
    for i in range(0, len(record_ids), _BATCH):
        resp = client.batch_delete_memory_records(
            memoryId=memory_id, records=[{"memoryRecordId": r} for r in record_ids[i : i + _BATCH]]
        )
        if resp.get("failedRecords"):
            logger.warning("memory wipe: %d records failed for owner=%s", len(resp["failedRecords"]), owner_id)
    return len(record_ids)


def _delete_owner_memory_sync(owner_id: str, namespace: str, memory_id: str) -> None:
    client = _client()
    # Events FIRST: they are what extraction reads, so nothing new is queued behind this wipe.
    sessions = [
        s["sessionId"] for s in _pages(client.list_sessions, "sessionSummaries", memoryId=memory_id, actorId=owner_id)
    ]
    for session_id in sessions:
        for event in _pages(
            client.list_events,
            "events",
            memoryId=memory_id,
            sessionId=session_id,
            actorId=owner_id,
            includePayloads=False,
        ):
            client.delete_event(memoryId=memory_id, sessionId=session_id, eventId=event["eventId"], actorId=owner_id)
    records = _delete_records(client, owner_id, namespace, memory_id)
    logger.info("memory wipe: owner=%s records=%d sessions=%d", owner_id, records, len(sessions))


async def _resweep(owner_id: str, namespace: str, memory_id: str, delay_s: float) -> None:
    await asyncio.sleep(delay_s)
    try:
        late = await run_in_thread(_delete_records, _client(), owner_id, namespace, memory_id)
        if late:
            logger.info("memory wipe: re-sweep removed %d late records for owner=%s", late, owner_id)
    except Exception:  # noqa: BLE001 — a background tail; nothing awaits it
        logger.warning("memory wipe: re-sweep failed for owner=%s", owner_id, exc_info=True)


async def delete_owner_memory(owner_id: str, *, resweep_after_s: float = RESWEEP_AFTER_S) -> None:
    """Delete every event and record this owner has in workspace memory, then schedule one late
    re-sweep of records for extraction that was still in flight. No-op when the env has no memory
    configured. Raises on AWS errors -- the teardown seam decides whether that is fatal."""
    namespace = owner_namespace(owner_id)
    memory_id = settings.AGENTCORE_MEMORY_ID
    if not memory_id:
        return
    await run_in_thread(_delete_owner_memory_sync, owner_id, namespace, memory_id)
    # ponytail: in-process re-sweep, lost if this backend task stops within RESWEEP_AFTER_S; a
    # durable follow-up (SFN wait / DDB TTL job) if a wipe audit ever demands it.
    task = asyncio.create_task(_resweep(owner_id, namespace, memory_id, resweep_after_s))
    _resweeps.add(task)
    task.add_done_callback(_resweeps.discard)
