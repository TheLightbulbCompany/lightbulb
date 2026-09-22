"""Workspace memory: the per-env AgentCore Memory every agent of a workspace shares.

Runtimes read and write it themselves (the image's ``agentcore`` memory provider, under the
per-owner ``owner-memory`` inline policy -- ``bedrock_aip.ensure_owner_memory_policy``). The
backend only ever DELETES from it, when an owner's data is wiped. An owner's data is:

- the long-term records AgentCore extracted into ``/owners/<owner>/`` (exact namespace -- never
  ``namespacePath``, whose prefix match would also reach ``/owners/<owner>x/``), and
- the raw events under ``actorId = <owner>``, which expire on their own (``eventExpiryDuration``).

The wipe deletes the RECORDS in the caller's request and raises if any fail -- they never expire,
so a silent miss would outlive the wipe. The events (one per turn, possibly thousands) and a late
re-sweep of records whose extraction was still in flight (96-133 s in the 2026-09-22 dev spike)
run as a background tail: a lost tail is bounded by the event expiry, a lost records pass is not.
Deletes are eventually consistent (~22 s). Empty session/actor shells stay listed until their
events would have expired; AgentCore has no API to delete them and they carry no content.
"""

import asyncio
import logging

import boto3
from botocore.config import Config

from core.config import settings
from core.dynamodb import run_in_thread

logger = logging.getLogger(__name__)

_BATCH = 100  # BatchDeleteMemoryRecords' per-call cap
RESWEEP_AFTER_S = 180.0
_tails: set[asyncio.Task] = set()


def owner_namespace(owner_id: str) -> str:
    # The strategy's template is /owners/{actorId}/ -- a "/" in the id would widen or escape it.
    if not owner_id or "/" in owner_id or ".." in owner_id:
        raise ValueError(f"refusing unsafe owner id for a memory namespace: {owner_id!r}")
    return f"/owners/{owner_id}/"


def _client():
    return boto3.client(
        "bedrock-agentcore",
        region_name=settings.AWS_REGION,
        config=Config(retries={"mode": "adaptive", "max_attempts": 8}),
    )


def _pages(call, key: str, **kwargs) -> list:
    """Every item across all pages, collected BEFORE the caller deletes anything -- deleting while
    paging could shift an offset-style token and skip items."""
    items, token = [], None
    while True:
        resp = call(**kwargs, **({"nextToken": token} if token else {}))
        items.extend(resp.get(key, []))
        token = resp.get("nextToken")
        if not token:
            return items


def _delete_records(client, owner_id: str, memory_id: str) -> int:
    record_ids = [
        r["memoryRecordId"]
        for r in _pages(
            client.list_memory_records,
            "memoryRecordSummaries",
            memoryId=memory_id,
            namespace=owner_namespace(owner_id),
        )
    ]
    failed = 0
    for i in range(0, len(record_ids), _BATCH):
        resp = client.batch_delete_memory_records(
            memoryId=memory_id, records=[{"memoryRecordId": r} for r in record_ids[i : i + _BATCH]]
        )
        failed += len(resp.get("failedRecords") or [])
    if failed:
        raise RuntimeError(f"memory wipe: {failed} of {len(record_ids)} records failed to delete for {owner_id}")
    return len(record_ids)


def _delete_events(client, owner_id: str, memory_id: str) -> int:
    count = 0
    for session in _pages(client.list_sessions, "sessionSummaries", memoryId=memory_id, actorId=owner_id):
        sid = session["sessionId"]
        events = _pages(
            client.list_events, "events", memoryId=memory_id, sessionId=sid, actorId=owner_id, includePayloads=False
        )
        for event in events:
            client.delete_event(memoryId=memory_id, sessionId=sid, eventId=event["eventId"], actorId=owner_id)
        count += len(events)
    return count


async def _tail(owner_id: str, memory_id: str, resweep_after_s: float) -> None:
    client = _client()
    try:
        events = await run_in_thread(_delete_events, client, owner_id, memory_id)
        logger.info("memory wipe: deleted %d events for owner=%s", events, owner_id)
    except Exception:  # noqa: BLE001 — a background tail; bounded by the event expiry
        logger.warning("memory wipe: event deletion failed for owner=%s", owner_id, exc_info=True)
    await asyncio.sleep(resweep_after_s)
    try:
        late = await run_in_thread(_delete_records, client, owner_id, memory_id)
        if late:
            logger.info("memory wipe: re-sweep removed %d late records for owner=%s", late, owner_id)
    except Exception:  # noqa: BLE001 — nothing awaits the tail
        logger.error("memory wipe: re-sweep failed for owner=%s", owner_id, exc_info=True)


async def delete_owner_memory(owner_id: str, *, resweep_after_s: float = RESWEEP_AFTER_S) -> None:
    """Delete this owner's long-term records now (raising if any fail), then start the background
    tail: their events, and one late re-sweep of records. No-op when the env has no memory."""
    owner_namespace(owner_id)
    memory_id = settings.AGENTCORE_MEMORY_ID
    if not memory_id:
        return
    records = await run_in_thread(_delete_records, _client(), owner_id, memory_id)
    logger.info("memory wipe: deleted %d records for owner=%s", records, owner_id)
    # ponytail: in-process tail, lost if this backend task stops within ~RESWEEP_AFTER_S; events
    # then just expire, and a late record needs a retried wipe. Durable follow-up (SFN wait) if a
    # wipe audit ever demands it.
    task = asyncio.create_task(_tail(owner_id, memory_id, resweep_after_s))
    _tails.add(task)
    task.add_done_callback(_tails.discard)
