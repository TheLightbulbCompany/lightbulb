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
from datetime import UTC, datetime
from functools import partial

from botocore.exceptions import BotoCoreError, ClientError

from core.config import settings
from core.dynamodb import run_in_thread
from core.repositories import container_repo
from core.services import evaluator, memory_recall, memory_stages, memory_sync, network_member, workspace_memory

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
) -> dict:
    """The evidence page. ``user_id`` adds that person's private scope; ``None`` is the workspace."""
    question = _question(question)
    if as_of and as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=UTC)
    memory_id = settings.AGENTCORE_MEMORY_ID
    if not memory_id:
        return {"asOf": (as_of or datetime.now().astimezone()).isoformat(), "usedEvidenceTokens": 0, "results": []}
    ns = workspace_memory.namespaces_for(owner_id, user_id)
    _sync_in_background(owner_id, memory_id)
    return await memory_recall.recall(
        memory_id=memory_id,
        namespaces=ns,
        question=question,
        as_of=as_of,
        budget_tokens=budget_tokens,
        limit=limit,
        include_private=user_id is not None,
        use_expand=use_expand,
    )


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
    memory_id = settings.AGENTCORE_MEMORY_ID
    ran = 0
    if not memory_id:
        return await _collect_due(ran)
    # ponytail: one containers-table scan per pass; keep an index of memory owners if the fleet grows.
    for row in await container_repo.scan_all():
        owner_id = row.get("owner_id") or ""
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
    Raises ``Refused`` when the member refuses it, or (parked) when it looks like a secret: a queued
    row is plaintext at rest until the member can refuse it itself."""
    params = {"subject": subject, "fact": fact, "kind": kind, "scope": scope}
    if replaces:
        params["replaces"] = replaces
    if on_behalf_of:
        params["on_behalf_of"] = on_behalf_of
    near = await neighbours(owner_id, subject)
    answer = await network_member.call_op(owner_id, "memory.remember", params)
    if answer is None:
        if evaluator._SECRET.search(f"{subject}\n{fact}"):
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
    """Forget every entry the workspace wrote on ``user_id``'s behalf (the member wipe), found by
    paging ``memory.list``: each row carries its source. Raises when the member is parked: the list
    cannot be read, and a wipe that skipped it would keep the person's notes."""
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


async def forget_author(owner_id: str, author: str) -> None:
    """Tombstone every entry this engine member key authored."""
    await _send_or_queue(owner_id, "memory.forget_author", {"author": author})
