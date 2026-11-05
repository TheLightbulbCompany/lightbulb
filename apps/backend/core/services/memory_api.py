"""What the memory routes, the hosted MCP tools and the runtime plugin share: recall a page, remember a fact.

Recall starts a sync in the background (``sync_if_stale``, behind the owner's lease) and never waits
for it: its RPCs outlast every caller's timeout, so a page reflects the log as of the last sync.
The sweep (``run_memory_sweeper``) keeps workspaces nobody is asking fresh. A remember is forwarded
to the workspace's cloud member as ``memory.remember`` and queued when its runtime is parked; a
fact the member refuses (or, queued, would refuse as a secret) raises ``Refused``. Nothing here
logs a question or a fact.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime
from functools import partial

from botocore.exceptions import BotoCoreError, ClientError

from core.config import settings
from core.dynamodb import run_in_thread
from core.repositories import container_repo
from core.services import (
    evaluator,
    memory_links,
    memory_recall,
    memory_stages,
    memory_sync,
    network_member,
    workspace_memory,
)
from core.services.secret_shaped import secret_pattern, secret_shaped
from core.services.super_agent_flags import flags_for

logger = logging.getLogger(__name__)

MAX_QUESTION = 2000  # the Hermes plugin truncates to this (_QUERY_MAX in plugins/memory/agentcore)
NEIGHBOURS = 5
SWEEP_INTERVAL_S = 300  # spec section 5: every workspace synced at least every five minutes
SWEEP_TICK_S = 60
_syncs: set[asyncio.Task] = set()  # background syncs, held so they are not collected mid-run


class Refused(ValueError):
    """The member would not store the fact: the CLI's ``refused`` (``secret:<name>``, ``too_long``,
    ``team_off``), or ``secret`` when a queued fact would be. Routes answer 422, hosted a ToolError."""

    def __init__(self, reason: str):
        super().__init__(f"Not remembered: {reason}")
        self.reason = reason


def _question(question: str) -> str:
    """AgentCore raises ParamValidationError on a bad query: a bad question never reaches it."""
    q = (question or "").strip()
    if not q or len(q) > MAX_QUESTION:
        raise ValueError(f"question must be 1 to {MAX_QUESTION} characters")
    return q


async def recall_page(
    owner_id: str,
    user_id: str | None,
    question: str,
    *,
    as_of: datetime | None = None,
    budget_tokens: int = 8000,
    limit: int = 100,
    use_expand: bool = True,
    audience: dict | None = None,
) -> dict:
    """The evidence page. ``user_id`` adds that person's private scope; ``None`` is the workspace.
    In a super-agent workspace the page also has ``links`` to tasks, each visible to ``audience``
    (default: ``user_id`` as a person, else the workspace)."""
    question = _question(question)
    if as_of and as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=UTC)
    memory_id = settings.AGENTCORE_MEMORY_ID
    if not memory_id:
        return {"asOf": (as_of or datetime.now().astimezone()).isoformat(), "usedEvidenceTokens": 0, "results": []}
    ns = workspace_memory.namespaces_for(owner_id, user_id)
    _sync_in_background(owner_id, memory_id)
    links = await flags_for(owner_id)
    page = await memory_recall.recall(
        memory_id=memory_id,
        namespaces=ns,
        question=question,
        as_of=as_of,
        budget_tokens=budget_tokens,
        limit=limit,
        include_private=user_id is not None,
        use_expand=use_expand,
        **({"tasks": True} if links else {}),
    )
    if links:
        if audience is None:
            audience = {"kind": "person", "userId": user_id} if user_id else {"kind": "workspace"}
        page["links"] = await memory_links.resolve_links(owner_id, page["results"], page.pop("taskHits", []), audience)
    return page


def _sync_in_background(owner_id: str, memory_id: str) -> None:
    task = asyncio.create_task(
        memory_sync.sync_if_stale(
            owner_id, memory_id=memory_id, namespaces=workspace_memory.namespaces_for(owner_id, None)
        )
    )
    _syncs.add(task)
    task.add_done_callback(_synced)


def _synced(task: asyncio.Task) -> None:
    _syncs.discard(task)
    if not task.cancelled() and task.exception():  # a stale mirror still answers; the next one retries
        logger.warning("memory sync failed error=%s", type(task.exception()).__name__)


async def sweep_once() -> int:
    """Sync every workspace whose last sync is older than ``SWEEP_INTERVAL_S`` (returns how many ran),
    then collect due drafts. One at a time, each behind its owner's lease, and one failure never
    stops the rest."""
    from core.services import hermes_provisioning  # lazy: import cycle

    try:  # an owner wipe's teardown whose backend task stopped inside its window
        await hermes_provisioning.run_due_teardowns()
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory sweep: due teardowns failed error=%s", type(exc).__name__)
    memory_id = settings.AGENTCORE_MEMORY_ID
    ran = 0
    # ponytail: one containers-table scan per pass; keep an index of memory owners if the fleet grows.
    for row in await container_repo.scan_all():
        owner_id = row.get("owner_id") or ""
        await retry_member_wipes(owner_id)
        if not memory_id:
            continue
        try:
            ns = workspace_memory.namespaces_for(owner_id, None)
            ran += await memory_sync.sync_if_stale(
                owner_id, memory_id=memory_id, namespaces=ns, max_age_s=SWEEP_INTERVAL_S
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory sweep failed owner=%s error=%s", owner_id, type(exc).__name__)
    return await _collect_due(ran)


async def _collect_due(ran: int) -> int:
    """The evaluator's half of the sweep: reflections that were not ready at ingest become drafts."""
    try:
        await evaluator.collect_due()
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory sweep draft collect failed: %s", type(exc).__name__)
    return ran


async def run_memory_sweeper(stop_event: asyncio.Event) -> None:
    """The lifespan sweeper: one backend task per ``SWEEP_INTERVAL_S`` runs ``sweep_once``."""
    while not stop_event.is_set():
        try:
            if await container_repo.try_claim_pass("memory_sweep", interval_s=SWEEP_INTERVAL_S):
                await sweep_once()
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory sweep failed: %s", type(exc).__name__)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=SWEEP_TICK_S)
        except asyncio.TimeoutError:
            pass


async def neighbours(owner_id: str, subject: str) -> list[dict]:
    """What the workspace already holds on ``subject``: facts and log entries, best matches first."""
    memory_id = settings.AGENTCORE_MEMORY_ID
    if not memory_id:
        return []
    ns = workspace_memory.namespaces_for(owner_id, None)
    client = workspace_memory._client()
    out: list[dict] = []
    for key, kind in (("facts", "claim"), ("entries", "entry")):
        fn = partial(memory_recall._retrieve, client, memory_id, ns[key], subject, NEIGHBOURS, [])
        try:
            rows = await run_in_thread(fn)
        except (ClientError, BotoCoreError) as exc:
            logger.warning("memory neighbours failed: %s", type(exc).__name__)
            continue
        for row in rows:
            c = memory_stages.from_record(row, kind)
            if c.status != "replaced":
                out.append({"id": c.id, "kind": c.kind, "content": c.content, "status": c.status})
    return out


async def remember(
    owner_id: str,
    *,
    subject: str,
    fact: str,
    kind: str,
    scope: str = "me",
    replaces: list[str] | None = None,
    on_behalf_of: str | None = None,
) -> dict:
    """``{"queued": bool, "neighbours": [...]}``; the write is ``memory.remember`` on the member.
    Raises ``Refused`` for a credential-shaped value before any call or queue (``secret:<name>``,
    the CLI's own names), when the member refuses it, or (parked) when it mentions a secret at all:
    a queued row is plaintext at rest until the member can refuse it itself."""
    text = f"{subject}\n{fact}"
    if name := secret_pattern(text):  # before anything leaves: neighbours, the member, the queue
        raise Refused(f"secret:{name}")
    params = {"subject": subject, "fact": fact, "kind": kind, "scope": scope}
    if replaces:
        params["replaces"] = replaces
    if on_behalf_of:
        params["on_behalf_of"] = on_behalf_of
    near = await neighbours(owner_id, subject)
    answer = await network_member.call_op(owner_id, "memory.remember", params)
    if answer is None:
        if secret_shaped(text):  # parked: a bare keyword is enough not to hold it at rest
            raise Refused("secret")
        await network_member.queue_op(owner_id, "memory.remember", params)
    elif answer.get("stored") is False:
        raise Refused(str(answer.get("refused") or "refused"))
    return {"queued": answer is None, "neighbours": near}


async def _send_or_queue(owner_id: str, op: str, params: dict) -> None:
    """Never raises out of the network call: parked or failing, the op is queued."""
    sent = None
    try:
        sent = await network_member.call_op(owner_id, op, params)
    except Exception as exc:  # noqa: BLE001 -- the queue is the retry
        logger.warning("%s failed owner=%s error=%s", op, owner_id, type(exc).__name__)
    if sent is None:
        await network_member.queue_op(owner_id, op, params)


async def forget(owner_id: str, entry_id: str) -> None:
    await _send_or_queue(owner_id, "memory.forget", {"entry": entry_id})


async def forget_on_behalf_of(owner_id: str, user_id: str) -> int:
    """Forget every entry the workspace wrote on ``user_id``'s behalf, found by paging
    ``memory.list``: each row carries its source. Raises when the member is parked or unavailable
    (the list cannot be read); the member wipe calls it through ``forward_on_behalf_of``."""
    since, sent = 0, 0
    while True:
        answer = await network_member.call_op(owner_id, "memory.list", {"since_seq": since}, 30.0)
        if answer is None:
            raise RuntimeError("memory.list unavailable: the workspace's member is parked")
        rows = answer.get("entries") or []
        for row in rows:
            op = row.get("op") or {}
            if op.get("t") == "remember" and (op.get("source") or {}).get("on_behalf_of") == user_id:
                await forget(owner_id, row["id"])
                sent += 1
        nxt = int(answer.get("next") or since)
        if not rows or nxt <= since:
            return sent
        since = nxt


OWED_WIPE_S = 30 * 86400  # how long a forward owed to a parked member is retried


async def _owe(owner_id: str, sk: str, what: str) -> None:
    """One durable retry row (``MEMWIPE#<owner>`` / ``U#<user>`` for a member wipe's forward,
    ``A#<key>`` for an owner wipe's forget of the cloud member's own entries) and a wake. Never raises."""
    from core.repositories import messaging_repo

    try:
        item = {"pk": f"MEMWIPE#{owner_id}", "sk": sk, "expiresAt": int(time.time()) + OWED_WIPE_S}
        await run_in_thread(messaging_repo._table().put_item, Item=item)
        await network_member.wake(owner_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("%s forward could not be owed owner=%s error=%s", what, owner_id, type(exc).__name__)


async def forward_on_behalf_of(owner_id: str, user_id: str) -> None:
    """The member wipe's forward. Never raises: a parked or unavailable member gets one durable
    retry row (``MEMWIPE#<owner>`` / ``U#<user>``) and a wake, and the sweep finishes the forward
    once the runtime is warm. The rest of the wipe, and the empty-workspace teardown after it, go on."""
    try:
        await forget_on_behalf_of(owner_id, user_id)
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning("member wipe forward owed owner=%s error=%s", owner_id, type(exc).__name__)
    await _owe(owner_id, f"U#{user_id}", "member wipe")


async def _forget_author_now(owner_id: str, author: str) -> None:
    """``memory.forget_author`` on the member itself, or raise: no queue, so the caller can owe it."""
    if await network_member.call_op(owner_id, "memory.forget_author", {"author": author}) is None:
        raise RuntimeError("memory.forget_author unavailable: the workspace's member is parked")


async def forward_forget_author(owner_id: str, author: str) -> bool:
    """The owner wipe's forget of every entry the cloud member signed (imported facts, workspace
    remembers), sent through that member so its tombstones replicate to members' computers. Never
    raises: parked or failing, it is owed (``A#<key>``) and the runtime woken, like the member wipe.
    True when the member took it now."""
    try:
        await _forget_author_now(owner_id, author)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("owner wipe forget_author owed owner=%s error=%s", owner_id, type(exc).__name__)
    await _owe(owner_id, f"A#{author}", "owner wipe")
    return False


async def retry_member_wipes(owner_id: str) -> int:
    """The sweep's retry of owed forwards: each one that now goes through loses its row. Never raises."""
    from core.repositories import messaging_repo

    done = 0
    try:
        for row in await messaging_repo._query(f"MEMWIPE#{owner_id}"):
            if int(row.get("expiresAt") or 0) <= time.time():
                continue
            kind, _, who = row["sk"].partition("#")
            send = {"U": forget_on_behalf_of, "A": _forget_author_now}.get(kind)
            if send is None:
                continue
            try:
                await send(owner_id, who)
            except Exception:  # noqa: BLE001 -- still parked: the next pass tries again
                continue
            await run_in_thread(messaging_repo._table().delete_item, Key={"pk": row["pk"], "sk": row["sk"]})
            done += 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("member wipe retry failed owner=%s error=%s", owner_id, type(exc).__name__)
    return done


async def forget_author(owner_id: str, author: str) -> None:
    """Tombstone every entry this engine member key authored."""
    await _send_or_queue(owner_id, "memory.forget_author", {"author": author})
