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
import hashlib
import logging
import re
from collections.abc import Awaitable, Callable

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, ParamValidationError

from core.config import settings
from core.dynamodb import run_in_thread
from core.services import memory_stages

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


# ponytail: per-scope cap, newest-first after the cap; add a cursor when a workspace hits it.
_LIST_CAP = 200

# The confirmation mark, in the text so agents see it in recall. Written only by the backend's
# vouched copy. (AWS: a record created WITHOUT memoryStrategyId keeps its custom metadata as-is, and
# indexed keys are filterable -- the entries mirror relies on it; confirmed only on a deployed run,
# docs/research/2026-10-28-inventory-workspace-code.md "Open at deploy time" item 1.)
CONFIRMED_PREFIX = "Confirmed by a person: "
_MAX_TEXT = 16000  # AgentCore's MemoryContentTextString max length


def _scopes(owner_id: str, user_id: str) -> tuple[str, str]:
    """(private, team) for this viewer. Both derive from auth, never from the client."""
    team = owner_namespace(owner_id)
    if not _MEMBER_ID.fullmatch(user_id or ""):
        raise LookupError("no private memory scope for this caller")
    return f"/owners/{owner_id}:{user_id}/", team


def _entry_id(r: dict) -> str:
    """The log entry a mirrored record stands for (``memory_sync`` writes ``entryId``); "" for a fact."""
    return ((r.get("metadata") or {}).get("entryId") or {}).get("stringValue") or ""


def _view(r: dict, mirrored: bool = False) -> dict:
    text = (r.get("content") or {}).get("text", "")
    confirmed = text.startswith(CONFIRMED_PREFIX)
    # status, history and speaker are the recall page's, parsed by memory_stages.
    shown = memory_stages._result(memory_stages.from_record(r, "claim"))
    return {
        "id": r["memoryRecordId"],
        "text": text.removeprefix(CONFIRMED_PREFIX),
        "createdAt": r["createdAt"].isoformat(),
        "confirmed": confirmed,
        "status": shown["status"],
        "history": shown["history"],
        "speaker": shown["sources"][0]["speaker"],
        # network: a mirrored log entry (team remembers, approved lessons, a person's own notes);
        # room: what AgentCore extracted from the workspace's conversations. ``mirrored``: listed from
        # the entries namespace, which only the sync writes (record metadata is not always returned).
        "source": "network" if mirrored or _entry_id(r) else "room",
    }


def _not_imported(owner_id: str, mirrored: list[dict], facts_ids: set[str]) -> list[dict]:
    """Mirrored entries, less those whose facts record is already listed: one fact shows once.
    The sync never mirrors an imported fact, but a mirror can precede its import's link row.
    ponytail: one Dynamo read per mirrored record (<= _LIST_CAP); batch-get if listing gets slow."""
    from core.services import memory_sync  # lazy: it imports this module

    def imported(r: dict) -> bool:
        eid = _entry_id(r)  # no entry id: no link row to read, so nothing says it is a duplicate
        return bool(eid) and memory_sync._get(owner_id, f"MEMENTRY#{eid}").get("facts_rec") in facts_ids

    return [r for r in mirrored if not imported(r)]


def _list_scope(client, memory_id: str, ns: str) -> tuple[list[dict], bool]:
    # namespace is a PREFIX: keep only records whose namespace IS ns.
    rows, token = [], None
    while True:
        try:
            resp = client.list_memory_records(
                memoryId=memory_id, namespace=ns, maxResults=100, **({"nextToken": token} if token else {})
            )
        except ClientError as exc:
            raise RuntimeError("memory list failed") from exc
        rows += [r for r in resp.get("memoryRecordSummaries", []) if r.get("namespaces") == [ns]]
        token = resp.get("nextToken")
        if not token or len(rows) >= _LIST_CAP:
            return rows[:_LIST_CAP], bool(token) or len(rows) > _LIST_CAP


def _owned(client, memory_id: str, record_id: str, allowed: tuple[str, ...]) -> dict:
    """The record, only if it sits in exactly one of ``allowed``. Batch writes take bare ids and
    this role reaches every tenant, so the id alone is never trusted."""
    try:
        rec = client.get_memory_record(memoryId=memory_id, memoryRecordId=record_id)["memoryRecord"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("ResourceNotFoundException", "ValidationException"):
            raise LookupError(record_id) from exc
        raise RuntimeError("memory lookup failed") from exc
    if len(rec.get("namespaces") or []) != 1 or rec["namespaces"][0] not in allowed:
        raise LookupError(record_id)
    return rec


def _delete_one(client, memory_id: str, record_id: str) -> None:
    try:
        resp = client.batch_delete_memory_records(memoryId=memory_id, records=[{"memoryRecordId": record_id}])
    except ClientError as exc:
        raise RuntimeError(f"memory delete failed for {record_id}") from exc
    if resp.get("failedRecords"):
        raise RuntimeError(f"memory delete failed for {record_id}")


async def list_memories(owner_id: str, user_id: str) -> dict:
    """The viewer's private scope, and the team: extracted facts plus the mirrored log entries."""
    private, team = _scopes(owner_id, user_id)
    entries = namespaces_for(owner_id, None)["entries"]
    memory_id = settings.AGENTCORE_MEMORY_ID
    if not memory_id:
        return {"private": [], "team": [], "truncated": False}

    def run() -> dict:
        client, rows, truncated = _client(), {}, False
        for key, ns in (("private", private), ("team", team), ("entries", entries)):
            rows[key], more = _list_scope(client, memory_id, ns)
            truncated |= more
        facts_ids = {r["memoryRecordId"] for r in rows["team"]}
        mirrors = _not_imported(owner_id, rows.pop("entries"), facts_ids)
        rows["team"] += mirrors
        net = {r["memoryRecordId"] for r in mirrors}
        out = {
            k: [_view(r, r["memoryRecordId"] in net) for r in sorted(v, key=lambda r: r["createdAt"], reverse=True)]
            for k, v in rows.items()
        }
        return {**out, "truncated": truncated}

    return await run_in_thread(run)


def _deletable(owner_id: str, user_id: str, record_id: str):
    """(client, memory id) once ``record_id`` is in a scope this viewer may delete from: their own
    private scope or the team scope (any member: D2). LookupError otherwise."""
    scopes = (*_scopes(owner_id, user_id), namespaces_for(owner_id, None)["entries"])
    memory_id = settings.AGENTCORE_MEMORY_ID
    if not memory_id:
        raise LookupError(record_id)
    client = _client()
    _owned(client, memory_id, record_id, scopes)
    return client, memory_id


async def check_deletable(owner_id: str, user_id: str, record_id: str) -> None:
    """The scope check alone, for a caller that acts (the log forget) before the delete."""
    await run_in_thread(_deletable, owner_id, user_id, record_id)


async def delete_memory(owner_id: str, user_id: str, record_id: str) -> None:
    """Delete one fact from the viewer's private scope or the team scope -- extracted facts and
    mirrored log entries alike (any member: D2)."""

    def run() -> None:
        client, memory_id = _deletable(owner_id, user_id, record_id)
        _delete_one(client, memory_id, record_id)

    await run_in_thread(run)


def _vouched_copy(client, memory_id: str, rec: dict, team: str) -> str:
    """Copy ``rec`` into the team scope with ``CONFIRMED_PREFIX`` on its text, then delete the
    original. Share and Confirm are this one act (decisions D4-D6). The token is derived from the
    source record, so a retry after a failed delete replays the same create instead of a new one.
    No ``metadata`` key: AgentCore drops custom record metadata (spike S2)."""
    token = hashlib.sha256(f"{rec['memoryRecordId']}>{team}".encode()).hexdigest()
    text = (rec.get("content") or {}).get("text", "")
    if not text.startswith(CONFIRMED_PREFIX):
        text = CONFIRMED_PREFIX + text
    if len(text) > _MAX_TEXT:
        raise ValueError("memory too long to share or confirm")
    try:
        resp = client.batch_create_memory_records(
            memoryId=memory_id,
            clientToken=token,
            records=[
                {
                    "requestIdentifier": token,
                    "namespaces": [team],
                    "content": {"text": text},
                    "timestamp": rec["createdAt"],
                    "memoryStrategyId": rec["memoryStrategyId"],
                }
            ],
        )
    except (ClientError, ParamValidationError) as exc:
        raise RuntimeError(f"memory copy failed for {rec['memoryRecordId']}") from exc
    done = resp.get("successfulRecords") or []
    if resp.get("failedRecords") or not done or done[0].get("status") != "SUCCEEDED":
        raise RuntimeError(f"memory copy failed for {rec['memoryRecordId']}")
    _delete_one(client, memory_id, rec["memoryRecordId"])
    return done[0]["memoryRecordId"]


async def _vouch(owner_id: str, user_id: str, record_id: str, *, from_private: bool) -> str:
    private, team = _scopes(owner_id, user_id)
    memory_id = settings.AGENTCORE_MEMORY_ID
    if not memory_id:
        raise LookupError(record_id)

    def run() -> str:
        client = _client()
        rec = _owned(client, memory_id, record_id, (private,) if from_private else (team,))
        if not from_private and (rec.get("content") or {}).get("text", "").startswith(CONFIRMED_PREFIX):
            return record_id
        return _vouched_copy(client, memory_id, rec, team)

    return await run_in_thread(run)


async def share_memory(owner_id: str, user_id: str, record_id: str) -> str:
    """Phase 3: a person moves one of THEIR private facts to the team. Agents never call this."""
    return await _vouch(owner_id, user_id, record_id, from_private=True)


async def confirm_memory(owner_id: str, user_id: str, record_id: str) -> str:
    """Phase 4: a person confirms a team fact; agents then recall it as confirmed."""
    return await _vouch(owner_id, user_id, record_id, from_private=False)
