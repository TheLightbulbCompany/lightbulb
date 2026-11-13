"""Workspace memory: the per-env AgentCore Memory every agent of a workspace shares.

Runtimes read and write it themselves (the image's ``agentcore`` memory provider, under the
per-owner ``owner-memory`` inline policy -- ``bedrock_aip.ensure_owner_memory_policy``). The
backend never searches it: it wipes, and it lets a person review (list, delete, share, confirm)
their own private scope and the team scope (``routers/memory.py``). Two scopes per workspace:

- team: records in ``/owners/<owner>/``, events under ``actorId = <owner>``;
- private to one member (their 1:1 DMs with agents): records in ``/owners/<owner>:<user>/``,
  events under ``actorId = <owner>:<user>``.

Owner ids never contain ``:`` (``owner_namespace`` refuses one), so ``<owner>:`` can only name
this owner's members. Records are listed by exact ``namespace``; only the owner wipe's member
scan uses ``namespacePath``.

A wipe deletes the RECORDS in the caller's request and raises if any fail -- they never expire,
so a silent miss would outlive the wipe. The events (one per turn, possibly thousands) and a late
re-sweep of records whose extraction was still in flight (96-133 s in the 2026-09-22 dev spike)
run as a background tail: a lost tail is bounded by the event expiry, a lost records pass is not.
Deletes are eventually consistent (~22 s). Empty session/actor shells stay listed until their
events would have expired; AgentCore has no API to delete them and they carry no content.

The owner wipe finds member scopes two ways, because neither alone is complete: a
``namespacePath="/owners/"`` scan (it reaches a member whose events already expired, which
``ListActors`` cannot), and ``ListActors`` (the events, plus each listed member's namespace).
``namespace`` is NOT a prefix in practice, whatever the API docs say: on dev (2026-10-11)
``/owners/<owner>:user_`` and ``/owners/`` both listed nothing while private records existed.
"""

import asyncio
import functools
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

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


def namespaces_for(owner_id: str, user_id: str | None) -> dict[str, str]:
    """The namespaces recall reads for ``owner_id``; ``private`` only when ``user_id`` is given."""
    # turns and entries are siblings of the facts namespace, never children: a prefix read of
    # /owners/<o>/ must not pull them in.
    # tasks holds the super-agent task cards; the private_* pair is a person's private lane
    # (their DM with @lightbulb, their private tasks), read only with a backend-computed member.
    team = owner_namespace(owner_id)
    out = {
        "facts": team,
        "turns": f"/turns/{owner_id}/",
        "entries": f"/entries/{owner_id}/",
        "tasks": f"/tasks/{owner_id}/",
    }
    if user_id:
        member = f"{owner_id}:{user_id}"
        out |= {
            "private": f"/owners/{member}/",
            "private_turns": f"/turns/{member}/",
            "private_tasks": f"/tasks/{member}/",
        }
    return out


_ROOM_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def room_actor(owner_id: str, room_id: str) -> str:
    """A group chat's own memory scope ``<owner>:room_<roomId>`` (founder ruling, 6 Nov 2026): recalled only in that
    chat, so only its members ever see it. An owner wipe covers it (``<owner>:`` prefix); a member wipe does not."""
    owner_namespace(owner_id)  # the same owner-id guard
    if not _ROOM_ID.fullmatch(room_id):
        raise ValueError(f"refusing unsafe room id for a memory namespace: {room_id!r}")
    return f"{owner_id}:room_{room_id}"


def room_namespaces(owner_id: str, room_id: str) -> dict[str, str]:
    """The group chat's facts and turns, added to recall only when the audience is that chat."""
    actor = room_actor(owner_id, room_id)
    return {"room": f"/owners/{actor}/", "room_turns": f"/turns/{actor}/"}


# The families that hold an ``<owner>:<user>`` member scope; a wipe covers each.
_MEMBER_FAMILIES = ("/owners/", "/turns/", "/tasks/")


def _member_scopes(actor: str) -> list[str]:
    return [f"{family}{actor}/" for family in _MEMBER_FAMILIES]


@functools.cache
def _client():
    """One client per process (boto3 clients are thread-safe): a fresh one per call paid client
    construction and a TLS handshake per recall arm. The pool covers recall's MAX_CALLS at once."""
    return boto3.client(
        "bedrock-agentcore",
        region_name=settings.AWS_REGION,
        config=Config(retries={"mode": "adaptive", "max_attempts": 8}, max_pool_connections=32),
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
    # dict.fromkeys: the owner wipe's namespaces overlap (scan + ListActors), so ids repeat.
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


#: Retention (Phase 2): a mirrored room turn is rent forever once its 7-day event is gone. Older
#: than this, it goes. Facts and log entries are not swept: memory_sync links them to the log.
TURN_RETENTION_DAYS = 90


def expire_old_turns(client, memory_id: str, now: datetime) -> int:
    """Delete every mirrored turn record (team and private, every owner) created more than
    ``TURN_RETENTION_DAYS`` ago; returns how many. One env-wide ``/turns/`` listing.
    ponytail: by age, not by last recall -- AgentCore keeps no recall time and runtimes search
    Memory directly; stamp recall hits in the runtime plugin if a recall-based rule is wanted."""
    cutoff = now - timedelta(days=TURN_RETENTION_DAYS)
    old = [
        r["memoryRecordId"]
        for r in _pages(
            client.list_memory_records, "memoryRecordSummaries", memoryId=memory_id, namespacePath="/turns/"
        )
        if r["createdAt"] < cutoff
    ]
    for i in range(0, len(old), _BATCH):
        client.batch_delete_memory_records(
            memoryId=memory_id, records=[{"memoryRecordId": r} for r in old[i : i + _BATCH]]
        )
    return len(old)


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


def _member_namespaces(client, owner_id: str, memory_id: str) -> list[str]:
    """Every ``<family><owner>:<user>/`` that holds a record, found by scanning each member family.
    ponytail: env-wide scans per owner wipe; page by owner if the env grows past a few 10k records."""
    found = {
        ns
        for family in _MEMBER_FAMILIES
        for r in _pages(client.list_memory_records, "memoryRecordSummaries", memoryId=memory_id, namespacePath=family)
        for ns in r.get("namespaces") or []
        if ns.startswith(f"{family}{owner_id}:")
    }
    return sorted(found)


async def _still_gone(label: str, check: Callable[[], Awaitable[bool]] | None) -> bool:
    """False once the member has rejoined; a failing check also says False, so the tail keeps data."""
    if check is None:
        return True
    try:
        return await check()
    except Exception:  # noqa: BLE001 — records never come back; fail safe
        logger.warning("memory wipe: rejoin check failed for %s; tail skipped", label, exc_info=True)
        return False


async def _tail(
    label: str,
    memory_id: str,
    namespaces: list[str],
    actors: list[str],
    resweep_after_s: float,
    still_gone: Callable[[], Awaitable[bool]] | None = None,
) -> None:
    client = _client()
    if not await _still_gone(label, still_gone):
        return
    for actor in actors:
        try:
            events = await run_in_thread(_delete_events, client, actor, memory_id)
            logger.info("memory wipe: deleted %d events for actor=%s", events, actor)
        except Exception:  # noqa: BLE001 — a background tail; bounded by the event expiry
            logger.warning("memory wipe: event deletion failed for actor=%s", actor, exc_info=True)
    await asyncio.sleep(resweep_after_s)
    if not await _still_gone(label, still_gone):
        return
    try:
        late = await run_in_thread(_delete_records, client, label, memory_id, namespaces)
        if late:
            logger.info("memory wipe: re-sweep removed %d late records for %s", late, label)
    except Exception:  # noqa: BLE001 — nothing awaits the tail
        logger.error("memory wipe: re-sweep failed for %s", label, exc_info=True)


async def _wipe(
    client,
    label: str,
    memory_id: str,
    namespaces: list[str],
    actors: list[str],
    resweep_after_s: float,
    still_gone: Callable[[], Awaitable[bool]] | None = None,
) -> None:
    records = await run_in_thread(_delete_records, client, label, memory_id, namespaces)
    logger.info("memory wipe: deleted %d records for %s", records, label)
    # ponytail: in-process tail, lost if this backend task stops within ~RESWEEP_AFTER_S; events
    # then just expire, and a late record needs a retried wipe. Durable follow-up (SFN wait) if a
    # wipe audit ever demands it.
    task = asyncio.create_task(_tail(label, memory_id, namespaces, actors, resweep_after_s, still_gone))
    _tails.add(task)
    task.add_done_callback(_tails.discard)


async def _run_all(label: str, steps: list[Callable[[], Awaitable]]) -> None:
    """Every step runs; one failure must not leave the rest of the data behind. Raises after."""
    errors = []
    for step in steps:
        try:
            await step()
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    if errors:
        raise RuntimeError(f"{label} failed: {', '.join(type(e).__name__ for e in errors)}") from errors[0]


async def delete_owner_memory(owner_id: str, *, resweep_after_s: float = RESWEEP_AFTER_S) -> None:
    """Delete this owner's long-term records -- team AND every member's private scope -- now
    (raising if any fail), then start the background tail: their events, and one late re-sweep
    of records. Also every session and draft members uploaded or collected in the workspace,
    directly from the workspace's rows (no per-member webhook is relied on). The records are a
    no-op when the env has no memory."""
    owned = namespaces_for(owner_id, None)  # facts, turns, entries, tasks
    from core.services import evaluator, session_store  # lazy: import cycles

    async def records() -> None:
        memory_id = settings.AGENTCORE_MEMORY_ID
        if not memory_id:
            return
        client = _client()
        members = await run_in_thread(_member_actors, client, owner_id, memory_id)
        scanned = await run_in_thread(_member_namespaces, client, owner_id, memory_id)
        namespaces = list(
            dict.fromkeys([*owned.values(), *scanned, *(ns for a in members for ns in _member_scopes(a))])
        )
        await _wipe(client, f"owner={owner_id}", memory_id, namespaces, [owner_id, *members], resweep_after_s)

    await _run_all(
        f"owner wipe for {owner_id}",
        [
            lambda: evaluator.delete_workspace_drafts(owner_id),  # first: it finds people by their session rows
            lambda: session_store.delete_workspace_sessions(owner_id),
            records,
        ],
    )


async def forget_workspace_authored(owner_id: str) -> str | None:
    """The owner wipe's half that needs the runtime: tombstone, through the cloud member, every
    entry it signed (imported facts, workspace remembers), so the tombstones reach members'
    computers. ``delete_workspace_runtime`` awaits this BEFORE it deletes the runtime and the boot
    secret holding the member key. Never raises: parked, it is owed and the runtime woken, and the
    member key is returned so the caller can keep the runtime until the forget lands. None when
    it was sent, or there was nothing to send."""
    from core.services import agentcore_runtime, memory_api  # lazy: import cycles

    try:
        key = await agentcore_runtime.network_member_key(owner_id)
    except Exception as exc:  # noqa: BLE001 -- a teardown must not stop here
        logger.warning("owner wipe: member key unreadable owner=%s error=%s", owner_id, type(exc).__name__)
        return None
    if key and not await memory_api.forward_forget_author(owner_id, key):  # no key: it signed nothing
        return key
    return None


async def _forget_authored(owner_id: str, user_id: str, memory_api, network_login) -> None:
    """Tombstone, in the network log, every entry written from this person's signed-in computers,
    and every entry the workspace wrote on their behalf (their own notes from DMs, hosted tools,
    the member route). The key registry stays: it is the person's, for their other workspaces."""
    for key in await network_login.computer_keys(user_id):
        await memory_api.forget_author(owner_id, key)
    await memory_api.forward_on_behalf_of(owner_id, user_id)  # never raises: a parked member is owed it


async def delete_member_memory(
    owner_id: str,
    user_id: str,
    *,
    resweep_after_s: float = RESWEEP_AFTER_S,
    still_gone: Callable[[], Awaitable[bool]] | None = None,
) -> None:
    """A member left the workspace: delete their private scope at once (no grace period), the
    same way as the owner wipe, and what they uploaded or were drafted in THIS workspace. Team
    memory is the workspace's and stays; so do their episodes and computer keys, which are theirs,
    not this workspace's (``delete_person_memory`` takes those with the account). No-op for an id
    that can never have a private scope. The tail (events, late re-sweep) runs only while
    ``still_gone`` says so: a re-add must keep its new memories."""
    owner_namespace(owner_id)
    if not _MEMBER_ID.fullmatch(user_id or ""):
        return
    from core.services import evaluator, memory_api, network_login, session_store  # lazy: import cycles

    memory_id = settings.AGENTCORE_MEMORY_ID
    actor = f"{owner_id}:{user_id}"
    steps = [
        lambda: session_store.delete_sessions(user_id, workspace_id=owner_id),
        lambda: evaluator.delete_drafts(user_id, workspace_id=owner_id),
        lambda: _forget_authored(owner_id, user_id, memory_api, network_login),
    ]
    if memory_id:
        steps.insert(
            0,
            lambda: _wipe(
                _client(), f"actor={actor}", memory_id, _member_scopes(actor), [actor], resweep_after_s, still_gone
            ),
        )
    await _run_all(f"member wipe for {actor}", steps)


async def delete_person_memory(user_id: str, *, resweep_after_s: float = RESWEEP_AFTER_S) -> None:
    """The account is deleted: everything of the person's own, in no workspace's keeping -- every
    session they uploaded, every draft and evaluator row, their episodes and reflections, and the
    registry of their computers' keys."""
    if not user_id:
        return
    from core.services import evaluator, network_login, session_store  # lazy: import cycles

    await _run_all(
        f"person wipe for {user_id}",
        [
            lambda: session_store.delete_sessions(user_id),
            lambda: evaluator.delete_drafts(user_id),
            lambda: evaluator.delete_episodes(user_id, resweep_after_s=resweep_after_s),
            lambda: network_login.forget_keys(user_id),
        ],
    )


_MAX_TEXT = 16000  # AgentCore's MemoryContentTextString max length
