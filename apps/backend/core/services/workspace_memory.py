"""Workspace memory: the per-env AgentCore Memory every agent of a workspace shares.

Runtimes read and write it themselves (the image's ``agentcore`` memory provider, under the
per-owner ``owner-memory`` inline policy -- ``bedrock_aip.ensure_owner_memory_policy``). The
backend only ever DELETES from it. Two scopes per workspace:

- team: records in ``/owners/<owner>/``, events under ``actorId = <owner>``;
- private to one member (their 1:1 DMs with agents): records in ``/owners/<owner>:<user>/``,
  events under ``actorId = <owner>:<user>``.

Owner ids never contain ``:`` (``owner_namespace`` refuses one), so ``<owner>:`` can only name
this owner's members. Records are listed by ``namespace``, never ``namespacePath``.

A wipe deletes the RECORDS in the caller's request and raises if any fail -- they never expire,
so a silent miss would outlive the wipe. The events (one per turn, possibly thousands) and a late
re-sweep of records whose extraction was still in flight (96-133 s in the 2026-09-22 dev spike)
run as a background tail: a lost tail is bounded by the event expiry, a lost records pass is not.
Deletes are eventually consistent (~22 s). Empty session/actor shells stay listed until their
events would have expired; AgentCore has no API to delete them and they carry no content.

The owner wipe finds member scopes two ways, because neither alone is complete: a
``namespace`` prefix list of ``/owners/<owner>:user_`` (the API documents ``namespace`` as a
prefix; it reaches a member whose events already expired, which ``ListActors`` cannot), and
``ListActors`` (the events, plus each listed member's exact namespace in case the prefix is
not honoured).
"""

import asyncio
import logging
import re

import boto3
from botocore.config import Config

from core.config import settings
from core.dynamodb import run_in_thread

logger = logging.getLogger(__name__)

_BATCH = 100  # BatchDeleteMemoryRecords' per-call cap
RESWEEP_AFTER_S = 180.0
_MEMBER_ID = re.compile(r"user_[A-Za-z0-9]+")  # the runtime plugin's private-scope speaker
_tails: set[asyncio.Task] = set()


def owner_namespace(owner_id: str) -> str:
    # The strategy's template is /owners/{actorId}/ -- a "/" in the id would widen or escape it,
    # and a ":" would let it name another owner's member scope (<owner>:<user>).
    if not owner_id or "/" in owner_id or ".." in owner_id or ":" in owner_id:
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


def _delete_records(client, label: str, memory_id: str, namespaces: list[str]) -> int:
    # dict.fromkeys: the owner wipe's namespaces overlap (prefix + exact), so ids repeat.
    record_ids = list(
        dict.fromkeys(
            r["memoryRecordId"]
            for ns in namespaces
            for r in _pages(client.list_memory_records, "memoryRecordSummaries", memoryId=memory_id, namespace=ns)
        )
    )
    failed = 0
    for i in range(0, len(record_ids), _BATCH):
        resp = client.batch_delete_memory_records(
            memoryId=memory_id, records=[{"memoryRecordId": r} for r in record_ids[i : i + _BATCH]]
        )
        failed += len(resp.get("failedRecords") or [])
    if failed:
        raise RuntimeError(f"memory wipe: {failed} of {len(record_ids)} records failed to delete for {label}")
    return len(record_ids)


def _delete_events(client, actor_id: str, memory_id: str) -> int:
    count = 0
    for session in _pages(client.list_sessions, "sessionSummaries", memoryId=memory_id, actorId=actor_id):
        sid = session["sessionId"]
        events = _pages(
            client.list_events, "events", memoryId=memory_id, sessionId=sid, actorId=actor_id, includePayloads=False
        )
        for event in events:
            client.delete_event(memoryId=memory_id, sessionId=sid, eventId=event["eventId"], actorId=actor_id)
        count += len(events)
    return count


def _member_actors(client, owner_id: str, memory_id: str) -> list[str]:
    """This owner's member actors (``<owner>:<user>``) that still hold events. ListActors is
    env-wide, so it is filtered here; ``<owner>:`` cannot match another owner (no ``:`` in ids)."""
    prefix = f"{owner_id}:"
    return [
        a["actorId"]
        for a in _pages(client.list_actors, "actorSummaries", memoryId=memory_id)
        if a["actorId"].startswith(prefix)
    ]


async def _tail(label: str, memory_id: str, namespaces: list[str], actors: list[str], resweep_after_s: float) -> None:
    client = _client()
    for actor in actors:
        try:
            events = await run_in_thread(_delete_events, client, actor, memory_id)
            logger.info("memory wipe: deleted %d events for actor=%s", events, actor)
        except Exception:  # noqa: BLE001 — a background tail; bounded by the event expiry
            logger.warning("memory wipe: event deletion failed for actor=%s", actor, exc_info=True)
    await asyncio.sleep(resweep_after_s)
    try:
        late = await run_in_thread(_delete_records, client, label, memory_id, namespaces)
        if late:
            logger.info("memory wipe: re-sweep removed %d late records for %s", late, label)
    except Exception:  # noqa: BLE001 — nothing awaits the tail
        logger.error("memory wipe: re-sweep failed for %s", label, exc_info=True)


async def _wipe(
    client, label: str, memory_id: str, namespaces: list[str], actors: list[str], resweep_after_s: float
) -> None:
    records = await run_in_thread(_delete_records, client, label, memory_id, namespaces)
    logger.info("memory wipe: deleted %d records for %s", records, label)
    # ponytail: in-process tail, lost if this backend task stops within ~RESWEEP_AFTER_S; events
    # then just expire, and a late record needs a retried wipe. Durable follow-up (SFN wait) if a
    # wipe audit ever demands it.
    task = asyncio.create_task(_tail(label, memory_id, namespaces, actors, resweep_after_s))
    _tails.add(task)
    task.add_done_callback(_tails.discard)


async def delete_owner_memory(owner_id: str, *, resweep_after_s: float = RESWEEP_AFTER_S) -> None:
    """Delete this owner's long-term records -- team AND every member's private scope -- now
    (raising if any fail), then start the background tail: their events, and one late re-sweep
    of records. No-op when the env has no memory."""
    team = owner_namespace(owner_id)
    memory_id = settings.AGENTCORE_MEMORY_ID
    if not memory_id:
        return
    client = _client()
    members = await run_in_thread(_member_actors, client, owner_id, memory_id)
    namespaces = [team, f"/owners/{owner_id}:user_", *(f"/owners/{a}/" for a in members)]
    await _wipe(client, f"owner={owner_id}", memory_id, namespaces, [owner_id, *members], resweep_after_s)


async def delete_member_memory(owner_id: str, user_id: str, *, resweep_after_s: float = RESWEEP_AFTER_S) -> None:
    """A member left the workspace: delete their private scope at once (no grace period), the
    same way as the owner wipe. Team memory is the workspace's and stays. No-op for an id that
    can never have a private scope, and when the env has no memory."""
    owner_namespace(owner_id)
    memory_id = settings.AGENTCORE_MEMORY_ID
    if not memory_id or not _MEMBER_ID.fullmatch(user_id or ""):
        return
    actor = f"{owner_id}:{user_id}"
    await _wipe(_client(), f"actor={actor}", memory_id, [f"/owners/{actor}/"], [actor], resweep_after_s)
