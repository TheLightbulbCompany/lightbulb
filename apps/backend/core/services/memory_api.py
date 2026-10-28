"""What the memory routes, the hosted MCP tools and the runtime plugin share: recall a page, remember a fact.

Recall syncs first (``sync_if_stale``) so the page sees the network log; a remember is forwarded to the
workspace's cloud member as ``memory.remember`` and queued when its runtime is parked. Nothing here
logs a question or a fact.
"""

import logging
from datetime import UTC, datetime
from functools import partial

from botocore.exceptions import BotoCoreError, ClientError

from core.config import settings
from core.dynamodb import run_in_thread
from core.services import memory_recall, memory_stages, memory_sync, network_member, workspace_memory

logger = logging.getLogger(__name__)

MAX_QUESTION = 2000  # the Hermes plugin truncates to this (_QUERY_MAX in plugins/memory/agentcore)
NEIGHBOURS = 5


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
) -> dict:
    """The evidence page. ``user_id`` adds that person's private scope; ``None`` is the workspace."""
    question = _question(question)
    if as_of and as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=UTC)
    memory_id = settings.AGENTCORE_MEMORY_ID
    if not memory_id:
        return {"asOf": (as_of or datetime.now().astimezone()).isoformat(), "usedEvidenceTokens": 0, "results": []}
    ns = workspace_memory.namespaces_for(owner_id, user_id)
    try:
        await memory_sync.sync_if_stale(owner_id, memory_id=memory_id, namespaces=ns)
    except Exception as exc:  # noqa: BLE001 -- a stale mirror still answers; the next recall retries
        logger.warning("memory sync before recall failed owner=%s error=%s", owner_id, type(exc).__name__)
    return await memory_recall.recall(
        memory_id=memory_id,
        namespaces=ns,
        question=question,
        as_of=as_of,
        budget_tokens=budget_tokens,
        limit=limit,
        include_private=user_id is not None,
    )


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
    """``{"queued": bool, "neighbours": [...]}``; the write is ``memory.remember`` on the member."""
    params = {"subject": subject, "fact": fact, "kind": kind, "scope": scope}
    if replaces:
        params["replaces"] = replaces
    if on_behalf_of:
        params["on_behalf_of"] = on_behalf_of
    near = await neighbours(owner_id, subject)
    queued = await network_member.call_op(owner_id, "memory.remember", params) is None
    if queued:
        await network_member.queue_op(owner_id, "memory.remember", params)
    return {"queued": queued, "neighbours": near}


async def forget(owner_id: str, entry_id: str) -> None:
    """Never raises out of the network call: parked or failing, the forget is queued."""
    params = {"entry": entry_id}
    sent = None
    try:
        sent = await network_member.call_op(owner_id, "memory.forget", params)
    except Exception as exc:  # noqa: BLE001 -- the queue is the retry
        logger.warning("memory.forget failed owner=%s error=%s", owner_id, type(exc).__name__)
    if sent is None:
        await network_member.queue_op(owner_id, "memory.forget", params)
