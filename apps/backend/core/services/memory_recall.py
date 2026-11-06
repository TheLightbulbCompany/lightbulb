"""Recall over AgentCore: expand the question, search each part several ways, fuse, rerank, compose.

The one function the workspace's room agents, hosted tools and Memory tab read memory through.
Logs stage timings only -- never the question or record text.
"""

import asyncio
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial

from botocore.exceptions import BotoCoreError, ClientError, ParamValidationError

from core.services import memory_models, workspace_memory
from core.services.memory_stages import (
    _CURRENT_KINDS,
    PAGE_OTHERS,
    PAGE_TURNS,
    Candidate,
    compose,
    excerpt,
    from_record,
    rrf,
    time_window,
)

logger = logging.getLogger(__name__)

MAX_PARTS = 5
MAX_ENTITIES = 5
MIN_POSITIVE = 5
ENTITY_TTL_S = 60.0
MAX_PAGES = 10
MAX_CALLS = 16
TASK_TOP_K = 10
# Recall's own threads: on the default executor (6 threads on a dev task) its arms queued behind
# the background sync and every other DynamoDB call, so one recall took two or three rounds.
_POOL = ThreadPoolExecutor(MAX_CALLS, thread_name_prefix="memory-recall")
_entity_cache: dict[tuple[str, str], tuple[float, set[str]]] = {}
_entity_refresh: dict[tuple[str, str], asyncio.Task] = {}


def _flt(key: str, op: str, value: str | float | None = None) -> dict:
    out: dict = {"left": {"metadataKey": key}, "operator": op}
    if value is not None:
        field = "stringValue" if isinstance(value, str) else "numberValue"
        if key.startswith("x-amz-agentcore-memory-"):  # dateTimeValue is reserved for the system fields
            field = "dateTimeValue"
        out["right"] = {"metadataValue": {field: value}}
    return out


def _iso_utc(at: datetime) -> str:
    """ISO-8601 in UTC (``2026-01-01T00:00:00Z``); a naive time is UTC."""
    return at.replace(tzinfo=at.tzinfo or UTC).astimezone(UTC).isoformat().replace("+00:00", "Z")


def _as_of_variants(as_of: datetime | None) -> list[list[dict]]:
    if as_of is None:
        return [[]]
    e = as_of.timestamp()
    start = _flt("validFrom", "LESS_THAN_OR_EQUALS", e)
    return [
        [start, _flt("validTo", "NOT_EXISTS")],
        [start, _flt("validTo", "GREATER_THAN", e)],
        # 0 is how the extraction model writes "empty" (validTo is a NUMBER): still current.
        [start, _flt("validTo", "EQUALS_TO", 0.0)],
    ]


async def _call(label: str, fn, **kwargs) -> list[dict] | None:
    """One AgentCore read on recall's pool, at most MAX_CALLS at once. None when it failed: that loses
    one arm only. A malformed request is a bug and propagates."""
    try:
        return await asyncio.get_running_loop().run_in_executor(_POOL, partial(fn, **kwargs))
    except ParamValidationError:
        raise
    except (ClientError, BotoCoreError) as exc:
        logger.warning("memory recall %s failed: %s", label, type(exc).__name__)
        return None


def _retrieve(client, memory_id: str, namespace: str, query: str, limit: int, filters: list[dict]) -> list[dict]:
    criteria = {"searchQuery": query, "topK": limit}  # the plugin comment: counted by topK, not maxResults
    if filters:
        criteria["metadataFilters"] = filters
    resp = client.retrieve_memory_records(memoryId=memory_id, namespace=namespace, searchCriteria=criteria)
    return resp.get("memoryRecordSummaries", [])


def _list(client, memory_id: str, namespace: str, filters: list[dict], cap: int) -> list[dict]:
    """At most ``cap`` records and MAX_PAGES pages."""
    extra = {"metadataFilters": filters} if filters else {}
    items, token = [], None
    for _ in range(MAX_PAGES):
        resp = client.list_memory_records(
            memoryId=memory_id, namespace=namespace, **extra, **({"nextToken": token} if token else {})
        )
        items.extend(resp.get("memoryRecordSummaries", []))
        token = resp.get("nextToken")
        if not token or len(items) >= cap:
            return items[:cap]
    logger.info("memory recall listing hit the %d page cap", MAX_PAGES)
    return items[:cap]


async def _entities(client, memory_id: str, facts_ns: str) -> set[str]:
    """The workspace's entity names. Only the first read per process waits; a stale list answers
    while a background read refreshes it: the listing is up to MAX_PAGES serial pages (1-2 s on dev,
    6 Nov 2026), and with a 60 s TTL it landed on most turns of a quiet room."""
    key = (memory_id, facts_ns)
    hit = _entity_cache.get(key)
    if hit is None:
        return await _load_entities(client, memory_id, facts_ns)
    if time.monotonic() - hit[0] >= ENTITY_TTL_S and key not in _entity_refresh:
        task = asyncio.create_task(_load_entities(client, memory_id, facts_ns))
        _entity_refresh[key] = task
        task.add_done_callback(lambda t: (_entity_refresh.pop(key, None), t.cancelled() or t.exception()))
    return hit[1]


async def _load_entities(client, memory_id: str, facts_ns: str) -> set[str]:
    key = (memory_id, facts_ns)
    rows = await _call("entities", partial(_list, client, memory_id, facts_ns, [], MAX_PAGES * 100))
    if rows is None:  # a failed listing is not an empty one: do not cache it
        return set()
    names = {e for r in rows if (e := ((r.get("metadata") or {}).get("entity") or {}).get("stringValue"))}
    _entity_cache[key] = (time.monotonic(), names)
    return names


def _named(part: str, entities: set[str]) -> list[str]:
    hits = [e for e in sorted(entities) if re.search(rf"(?<!\w){re.escape(e)}(?!\w)", part, re.I)]
    return hits[:MAX_ENTITIES]


async def _search_part(
    client, memory_id, namespaces, part, limit, as_of, include_private, entities, tasks=False
) -> list:
    """Every arm for one part: (kind_default, summaries) per arm, in a fixed order. ``tasks`` (a
    super-agent workspace) adds the task-card arm and, with a member, their private turns."""
    variants = _as_of_variants(as_of)
    jobs: list[tuple[str, object]] = []

    def retrieve(ns: str, filters: list[dict]):
        return partial(_retrieve, client, memory_id, ns, part, limit, filters)

    for status in ("current", "contested"):
        for extra in variants:
            jobs.append(("claim", retrieve(namespaces["facts"], [_flt("status", "EQUALS_TO", status), *extra])))
    # Turns and entries have no validity range: as of a moment is what existed by then.
    # The system field: a bare ``createdAt`` is not indexed and AgentCore refuses it.
    created = [_flt("x-amz-agentcore-memory-createdAt", "BEFORE", _iso_utc(as_of))] if as_of else []
    jobs.append(("turn", retrieve(namespaces["turns"], created)))
    jobs.append(("entry", retrieve(namespaces["entries"], created)))
    if include_private and "private" in namespaces:
        for extra in variants:
            jobs.append(("claim", retrieve(namespaces["private"], extra)))
    if tasks:
        if include_private and "private_turns" in namespaces:
            jobs.append(("turn", retrieve(namespaces["private_turns"], created)))
        task_spaces = [namespaces["tasks"], namespaces.get("private_tasks") if include_private else None]
        for ns in task_spaces:
            if ns:
                jobs.append(("task", partial(_retrieve, client, memory_id, ns, part, TASK_TOP_K, [])))
    for entity in _named(part, entities):
        for extra in variants:
            jobs.append(
                (
                    "claim",
                    partial(
                        _list,
                        client,
                        memory_id,
                        namespaces["facts"],
                        [_flt("entity", "EQUALS_TO", entity), *extra],
                        limit,
                    ),
                )
            )
    results = await asyncio.gather(*(_call("search", fn) for _, fn in jobs))
    return [(kind, rows or []) for (kind, _), rows in zip(jobs, results, strict=True)]  # failed arms are None


def _window(c: Candidate, question: str) -> str:
    """A turn's part that answers ``question`` (its speaker kept in front); anything else whole."""
    if c.kind != "turn":
        return c.content
    lead = f"{c.speaker}: " if c.speaker and c.content.startswith(f"{c.speaker}: ") else ""
    return lead + excerpt(c.content[len(lead) :], question)


def _as_current(c: Candidate, as_of: datetime | None) -> Candidate:
    # A fact replaced after ``as_of`` was still current at ``as_of``.
    if as_of and c.status == "replaced" and c.valid_to and c.valid_to > as_of:
        return replace(c, status="current", valid_to=None)
    return c


async def recall(
    *,
    memory_id: str,
    namespaces: dict[str, str],
    question: str,
    as_of: datetime | None = None,
    budget_tokens: int = 8000,
    limit: int = 100,
    include_private: bool = False,
    use_rerank: bool = True,
    use_expand: bool = True,
    tasks: bool = False,
) -> dict:
    """``tasks``: also search task cards; their ids go to ``page["taskHits"]`` (for links), never to results."""
    now = datetime.now(UTC)
    when = as_of or now
    t0 = time.monotonic()
    parts = ((await memory_models.expand(question))[:MAX_PARTS] if use_expand else []) or [question]
    t1 = time.monotonic()

    client = workspace_memory._client()
    entities = await _entities(client, memory_id, namespaces["facts"])
    per_part = await asyncio.gather(
        *(_search_part(client, memory_id, namespaces, p, limit, as_of, include_private, entities, tasks) for p in parts)
    )
    t2 = time.monotonic()

    start, end = time_window(question, when)
    by_id: dict[str, Candidate] = {}
    rankings: list[list[str]] = []
    task_hits: list[str] = []
    for arms in per_part:
        for kind_default, rows in arms:
            if kind_default == "task":  # a card is a pointer: it becomes a link, never evidence
                task_hits += [t for r in rows if (t := from_record(r, "task").task_id)]
                continue
            ranking = []
            for row in rows:
                c = _as_current(from_record(row, kind_default), as_of)
                # The window ("today", "in May") dates what was said, so it narrows turns and entries;
                # a current claim's creation date says nothing about the time asked about.
                dated = c.kind not in _CURRENT_KINDS
                # The service filtered validFrom <= as_of on the stored value; from_record may have
                # moved a guessed year to the day the claim was said, after as_of.
                late = not dated and as_of and c.valid_from and c.valid_from > as_of
                if (
                    late
                    or c.status == "replaced"
                    or (dated and ((start and c.occurred_at < start) or (end and c.occurred_at > end)))
                ):
                    continue
                by_id.setdefault(c.id, c)
                ranking.append(c.id)
            if ranking:
                rankings.append(ranking)
    # Claims come from many arms and turns from one, so each keeps its own share of the rerank's 100.
    room = {True: PAGE_TURNS, False: PAGE_OTHERS}
    ordered = []
    for i, score in rrf(rankings):
        c = by_id[i]
        if room[c.kind == "turn"]:
            room[c.kind == "turn"] -= 1
            ordered.append(replace(c, fused=score, content=_window(c, question)))
    t3 = time.monotonic()

    ranked = (
        await memory_models.rerank(question, [c.content for c in ordered], len(ordered))
        if use_rerank and ordered
        else None
    )
    if ranked is not None:
        ordered = [replace(ordered[i], fused=score) for i, score in ranked]
        positive = [c for c in ordered if c.fused > 0]
        if len(positive) < MIN_POSITIVE:
            ordered = positive
    t4 = time.monotonic()

    page = compose(ordered, budget_tokens, when)
    if tasks:
        page["taskHits"] = list(dict.fromkeys(task_hits))
    t5 = time.monotonic()
    logger.info(
        "memory recall expansionMs=%d searchMs=%d fuseMs=%d rerankMs=%d composeMs=%d",
        (t1 - t0) * 1000,
        (t2 - t1) * 1000,
        (t3 - t2) * 1000,
        (t4 - t3) * 1000,
        (t5 - t4) * 1000,
    )
    return page
