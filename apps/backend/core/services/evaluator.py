"""The evaluator: uploaded full sessions go into the sessions memory's episodic strategy
(``session_episodes``, ``AGENTCORE_SESSIONS_MEMORY_ID``: a resource of its own, so no other
strategy reads them and no room turn becomes an episode) under the person's own namespace; its
reflections (patterns across their sessions) become drafts a person can later approve into team memory.

Namespaces are per user and never cross users: episodes in ``/users/<user>/episodes/``,
reflections in ``/users/<user>/``. ListMemoryRecords on the latter also returns the episodes
beneath it, so a reflection is told apart by the record's own ``namespaces``.
Rows live in the messaging table: DRAFTS#<user>/D#<id> (``workspace_id``: the workspace it was
collected in, absent when free), mirrored to WDRAFTS#<ws>/D#<user>#<id> only for a lesson drawn from
at least one of the person's sessions in that workspace (``mirrored``): a finding may hold a secret,
and reflections span all of a person's sessions, so the rest stay their author's. EVAL#<user>/WATERMARK
and /S#<session> (ingested), EVALDUE/U#<user>#W#<ws|free> (reflections may still come: the sweep
collects until it expires), MEMCAP#<user>#<YYYY-MM>/COUNT (free cap, expires after the month).
Nothing here logs session text, summaries or reflection text.
"""

import asyncio
import hashlib
import logging
import re
import time
from datetime import datetime

from botocore.exceptions import ClientError

from core.config import settings
from core.dynamodb import run_in_thread, utc_now_iso
from core.repositories import messaging_repo as store
from core.services import workspace_memory
from core.services.secret_shaped import secret_shaped
from core.services.workspace_memory import _BATCH, _client, _delete_events, _pages

logger = logging.getLogger(__name__)

# ponytail: unreachable since 4 Nov 2026 (routers/sessions.py evaluates workspace uploads only);
# delete the cap rows, _reserve_cap and their tests when the free path is retired for good.
FREE_MONTHLY_CAP = 50
DUE_S = 7 * 86400  # how long after an ingest the sweep keeps collecting that person's reflections
CAP_ROW_S = 62 * 86400  # a monthly cap row outlives its month, then expires
_DUE = "EVALDUE"


def _ns(user_id: str) -> str:
    if not user_id or "/" in user_id or ".." in user_id:
        raise ValueError("unsafe user id for a memory namespace")
    return f"/users/{user_id}/"


def event_session_id(sid: str) -> str:
    """The AgentCore sessionId for a CLI session id (``claude:<uuid>``): CreateEvent takes 1-100 of
    ``[a-zA-Z0-9][a-zA-Z0-9-_]*``. Outside characters become ``-``; when any was replaced or the id is
    too long, 90 characters plus ``-`` and 8 hex of its sha256 keep distinct ids distinct. Our own rows
    (markers, evidence, sessions) keep the CLI id."""
    out = re.sub(r"[^A-Za-z0-9_-]", "-", sid)
    if out != sid or len(out) > 100:
        out = f"{out[:90]}-{hashlib.sha256(sid.encode()).hexdigest()[:8]}"
    return out if out[:1].isalnum() else f"s{out}"  # only ASCII is left


def _ts(value: str | None) -> datetime:
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return datetime.fromtimestamp(0).astimezone()


def _payload(role: str, text: str) -> dict:
    return {"conversational": {"role": role, "content": {"text": text[:9000]}}}


def _events(session: dict, tool_calls: list[dict]) -> list[tuple[datetime, dict]]:
    head = f"{session.get('harness', '')} session in {session.get('repo', '')} on {session.get('branch', '')}, started {session.get('started', '')}"
    out = [(_ts(session.get("started")), _payload("USER", head))]
    for c in sorted(tool_calls, key=lambda c: c.get("seq", 0)):
        out.append(
            (
                _ts(c.get("at") or session.get("started")),
                _payload("TOOL", f"{c.get('tool', '')}: {c.get('summary') or ''}"),
            )
        )
    return out


async def ingest_session(user_id: str, session: dict, tool_calls: list[dict], *, workspace_id: str | None) -> bool:
    """Write one full session as events. False when skipped (no memory, already ingested, or a
    free account past its monthly cap); the session counts toward the cap once, on success."""
    memory_id = settings.AGENTCORE_SESSIONS_MEMORY_ID
    if not memory_id:
        return False
    sid = session["id"]
    table = store._table()
    marker = {"pk": f"EVAL#{user_id}", "sk": f"S#{sid}"}
    # Reserve before writing, so concurrent uploads cannot both pass: the marker once per session,
    # then (free accounts) one slot of the monthly cap.
    if not await _put_new(table, marker):
        return False  # ponytail: a re-upload is not re-ingested (duplicate events would duplicate episodes)
    cap_key = {"pk": f"MEMCAP#{user_id}#{utc_now_iso()[:7]}", "sk": "COUNT"}
    if workspace_id is None and not await _reserve_cap(table, cap_key):
        await run_in_thread(table.delete_item, Key=marker)
        return False
    events = _events(session, tool_calls)
    client = _client()
    sent = 0
    try:
        for i in range(0, len(events), _BATCH):  # one CreateEvent per <=100 payloads, stamped at the first
            chunk = events[i : i + _BATCH]
            await run_in_thread(
                client.create_event,
                memoryId=memory_id, actorId=user_id, sessionId=event_session_id(sid), eventTimestamp=chunk[0][0], payload=[p for _, p in chunk],
            )  # fmt: skip
            sent += 1
    except Exception:
        if sent:  # the retry writes every chunk again: drop the ones already in, or episodes count them twice
            try:
                await run_in_thread(_delete_session_events, client, memory_id, user_id, sid)
            except Exception as e:  # noqa: BLE001 -- best effort; the original failure is what is raised
                logger.warning("partial ingest cleanup failed: %s", type(e).__name__)
        await run_in_thread(table.delete_item, Key=marker)  # a failed ingest neither counts nor blocks the retry
        if workspace_id is None:
            await run_in_thread(
                table.update_item, Key=cap_key, UpdateExpression="ADD n :m", ExpressionAttributeValues={":m": -1}
            )
        raise
    # Reflections are extracted later: collect what is ready now, and the sweep the rest.
    due = {**_due_key(user_id, workspace_id), "user_id": user_id, "expiresAt": int(time.time()) + DUE_S}
    if workspace_id:
        due["workspace_id"] = workspace_id
    try:
        await run_in_thread(table.put_item, Item=due)
        await collect_drafts(user_id, workspace_id=workspace_id)
    except Exception as e:  # noqa: BLE001 -- the session is in; collecting again is the sweep's job
        logger.warning("draft collect after ingest failed: %s", type(e).__name__)
    return True


def _due_key(user_id: str, workspace_id: str | None) -> dict:
    """One partition the sweep can list; a row per person and workspace, so a wipe of one leaves the others."""
    return {"pk": _DUE, "sk": f"U#{user_id}#W#{workspace_id or 'free'}"}


async def collect_due() -> int:
    """The sweep's half: collect drafts for everyone whose sessions were ingested recently."""
    created = 0
    now = time.time()
    for row in await store._query(_DUE):
        if int(row.get("expiresAt") or 0) <= now:
            continue
        try:
            created += await collect_drafts(row["user_id"], workspace_id=row.get("workspace_id"))
        except Exception as e:  # noqa: BLE001
            logger.warning("draft collect failed: %s", type(e).__name__)
    return created


async def _put_new(table, item: dict) -> bool:
    """put_item only if the key is new; False when it already exists."""
    try:
        await run_in_thread(table.put_item, Item=item, ConditionExpression="attribute_not_exists(pk)")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return False
    return True


async def _reserve_cap(table, key: dict) -> bool:
    """Take one slot of the monthly cap atomically; False when none is left."""
    try:
        await run_in_thread(
            table.update_item,
            Key=key,
            UpdateExpression="ADD n :one SET expiresAt = if_not_exists(expiresAt, :exp)",
            ConditionExpression="attribute_not_exists(n) OR n < :cap",
            ExpressionAttributeValues={":one": 1, ":cap": FREE_MONTHLY_CAP, ":exp": int(time.time()) + CAP_ROW_S},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return False
    return True


def _reflections(client, memory_id: str, user_id: str) -> list[dict]:
    ns = _ns(user_id)
    rows = _pages(client.list_memory_records, "memoryRecordSummaries", memoryId=memory_id, namespace=ns)
    return [r for r in rows if ns in (r.get("namespaces") or [ns])]


async def collect_drafts(user_id: str, *, workspace_id: str | None) -> int:
    memory_id = settings.AGENTCORE_SESSIONS_MEMORY_ID
    if not memory_id:
        return 0
    table = store._table()
    mark = await store._get(f"EVAL#{user_id}", "WATERMARK")
    since = _ts(mark["at"]) if mark else _ts(None)
    records = await run_in_thread(_reflections, _client(), memory_id, user_id)
    new = [r for r in records if _ts(str(r.get("createdAt"))) > since] if records else []
    rows = await store._query(f"SESSIONS#{user_id}", begins_with="S#")
    sessions = {r["id"] for r in rows}
    # The person's sessions in this workspace (each also has its WSESSIONS#<ws> row).
    here = {r["id"] for r in rows if workspace_id and r.get("workspace_id") == workspace_id}
    created = 0
    for r in new:
        text = r.get("content", {}).get("text", "")
        meta = " ".join(str(v) for v in (r.get("metadata") or {}).values())
        # A reflection may name the session by our id or by the one AgentCore was given.
        named = [
            s
            for s in sessions
            if any(
                re.search(rf"(?<![\w.-]){re.escape(x)}(?![\w.-])", f"{text} {meta}") for x in {s, event_session_id(s)}
            )
        ]
        item = {
            "id": r["memoryRecordId"], "user_id": user_id, "kind": "finding" if secret_shaped(text) else "lesson",
            "text": text, "evidence": sorted(named), "status": "proposed", "created": utc_now_iso(),
        }  # fmt: skip
        if workspace_id:
            item["workspace_id"] = workspace_id
        # Only a lesson drawn from this workspace's sessions reaches its members.
        item["mirrored"] = item["kind"] == "lesson" and bool(here.intersection(named))
        author = {"pk": f"DRAFTS#{user_id}", "sk": f"D#{item['id']}", **item}
        if await _put_new(table, author):
            created += 1
        else:  # already collected (maybe approved since): never reset it, but repair a mirror a failed write missed
            author = await store._get(author["pk"], author["sk"]) or {}
        if author.get("mirrored"):
            mirror = {**author, "pk": f"WDRAFTS#{author['workspace_id']}", "sk": f"D#{user_id}#{item['id']}"}
            await _put_new(table, mirror)
    if new:
        newest = max(_ts(str(r.get("createdAt"))) for r in new)
        await run_in_thread(table.put_item, Item={"pk": f"EVAL#{user_id}", "sk": "WATERMARK", "at": newest.isoformat()})
    return created


def _row(item: dict) -> dict:
    return {k: v for k, v in item.items() if k not in ("pk", "sk")}


def _own_findings(rows: list[dict], workspace_id: str) -> list[dict]:
    """The caller's own unmirrored drafts in this workspace (findings, lessons from elsewhere): read
    from their partition."""
    return [r for r in rows if not r.get("mirrored") and r.get("workspace_id") == workspace_id]


async def list_drafts(
    *, user_id: str | None = None, workspace_id: str | None = None, status: str = "proposed"
) -> list[dict]:
    pk = f"WDRAFTS#{workspace_id}" if workspace_id else f"DRAFTS#{user_id or ''}"
    rows = [_row(i) for i in await store._query(pk, begins_with="D#")]
    if workspace_id and user_id:
        own = [_row(i) for i in await store._query(f"DRAFTS#{user_id}", begins_with="D#")]
        rows += _own_findings(own, workspace_id)
    return sorted((r for r in rows if r.get("status") == status), key=lambda r: r.get("created", ""), reverse=True)


async def find_draft(draft_id: str, *, user_id: str, workspace_id: str | None) -> dict | None:
    """The caller's draft by id (a workspace member sees the workspace's, and their own findings), else None."""
    if workspace_id:
        rows = await store._query(f"WDRAFTS#{workspace_id}", begins_with="D#")
        row = next((r for r in rows if r.get("id") == draft_id), None)
        if row is None:
            own = await store._get(f"DRAFTS#{user_id}", f"D#{draft_id}")
            row = next(iter(_own_findings([own] if own else [], workspace_id)), None)
    else:
        row = await store._get(f"DRAFTS#{user_id}", f"D#{draft_id}")
    return _row(row) if row else None


async def set_draft_status(draft: dict, status: str, *, frm: str = "proposed") -> bool:
    """Move the draft ``frm`` -> ``status`` in both copies (the author's and the workspace mirror).

    The author's row is the arbiter: the update is conditional on its current status, so two callers
    cannot both win (False if it is not in ``frm``: settled first, or the user wiped it). Once it flips,
    the mirror is written to the same status whatever it held.
    """
    table = store._table()
    names, vals = {"#s": "status"}, {":s": status}
    try:
        await run_in_thread(
            table.update_item,
            Key={"pk": f"DRAFTS#{draft['user_id']}", "sk": f"D#{draft['id']}"},
            UpdateExpression="SET #s = :s",
            ConditionExpression="#s = :from",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues={**vals, ":from": frm},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return False
    if draft.get("workspace_id") and draft.get("mirrored"):  # an unmirrored draft has no copy to follow
        mirror = {"pk": f"WDRAFTS#{draft['workspace_id']}", "sk": f"D#{draft['user_id']}#{draft['id']}"}
        try:
            await run_in_thread(
                table.update_item, Key=mirror, UpdateExpression="SET #s = :s",
                ConditionExpression="#s = :from", ExpressionAttributeNames=names,
                ExpressionAttributeValues={**vals, ":from": frm},
            )  # fmt: skip
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            logger.warning("draft mirror out of step id=%s; forcing status=%s", draft["id"], status)
            await run_in_thread(
                table.update_item, Key=mirror, UpdateExpression="SET #s = :s",
                ExpressionAttributeNames=names, ExpressionAttributeValues=vals,
            )  # fmt: skip
    return True


async def _rm(pk: str, sk: str) -> None:
    await run_in_thread(store._table().delete_item, Key={"pk": pk, "sk": sk})


async def delete_drafts(user_id: str, *, workspace_id: str | None = None) -> int:
    """The user's drafts, their ingest markers, watermark, due row and this month's cap row (the
    account is deleted); with ``workspace_id`` only the drafts collected in that workspace (removed
    from it: the episodes and reflections behind them are the person's, not the workspace's)."""
    drafts = await store._query(f"DRAFTS#{user_id}", begins_with="D#")
    if workspace_id:
        drafts = [d for d in drafts if d.get("workspace_id") == workspace_id]
    jobs = [_rm(d["pk"], d["sk"]) for d in drafts]
    jobs += [_rm(f"WDRAFTS#{d['workspace_id']}", f"D#{user_id}#{d['id']}") for d in drafts if d.get("workspace_id")]
    if workspace_id:
        jobs.append(_rm(**_due_key(user_id, workspace_id)))
    else:
        jobs += [_rm(m["pk"], m["sk"]) for m in await store._query(f"EVAL#{user_id}")]
        jobs += [_rm(d["pk"], d["sk"]) for d in await store._query(_DUE, begins_with=f"U#{user_id}#")]
        # Older months' cap rows expire on their own (CAP_ROW_S).
        jobs.append(_rm(f"MEMCAP#{user_id}#{utc_now_iso()[:7]}", "COUNT"))
    await asyncio.gather(*jobs)
    return len(drafts)


def _delete_session_events(client, memory_id: str, user_id: str, sid: str) -> int:
    esid = event_session_id(sid)
    events = _pages(
        client.list_events, "events", memoryId=memory_id, sessionId=esid, actorId=user_id, includePayloads=False
    )
    for event in events:
        client.delete_event(memoryId=memory_id, sessionId=esid, eventId=event["eventId"], actorId=user_id)
    return len(events)


async def _uncite(user_id: str, sid: str, workspace_id: str | None) -> None:
    """Drop the session from the evidence of the person's drafts (free, and this workspace's); a
    draft left with no evidence goes, both copies."""
    table = store._table()
    for d in await store._query(f"DRAFTS#{user_id}", begins_with="D#"):
        if d.get("workspace_id") not in (None, workspace_id) or sid not in (d.get("evidence") or []):
            continue
        rest = [e for e in d["evidence"] if e != sid]
        keys = [{"pk": d["pk"], "sk": d["sk"]}]
        if d.get("mirrored"):  # an unmirrored draft has no workspace copy
            keys.append({"pk": f"WDRAFTS#{d['workspace_id']}", "sk": f"D#{user_id}#{d['id']}"})
        for key in keys:
            if rest:
                await run_in_thread(
                    table.update_item, Key=key, UpdateExpression="SET evidence = :e",
                    ConditionExpression="attribute_exists(pk)", ExpressionAttributeValues={":e": rest},
                )  # fmt: skip
            else:
                await _rm(**key)


async def forget_session(user_id: str, sid: str, *, workspace_id: str | None) -> None:
    """What the evaluator holds of one deleted session: its ingest marker (so the id can be ingested
    again), its events in the sessions memory, and its place in the person's drafts' evidence (a
    draft citing only it goes). Reflections AgentCore already drew from its episode span the person's
    sessions, are not attributable to this one, and stay until the person's wipe. Never raises: each
    part is logged on failure and the others still run."""

    async def events() -> None:
        if memory_id := settings.AGENTCORE_SESSIONS_MEMORY_ID:
            await run_in_thread(_delete_session_events, _client(), memory_id, user_id, sid)

    parts = (_rm(f"EVAL#{user_id}", f"S#{sid}"), events(), _uncite(user_id, sid, workspace_id))
    for name, result in zip(("marker", "events", "drafts"), await asyncio.gather(*parts, return_exceptions=True)):
        if isinstance(result, Exception):
            logger.warning("forget session %s failed: %s", name, type(result).__name__)


async def delete_workspace_drafts(workspace_id: str) -> int:
    """Every draft collected in this workspace, both copies, mirrored or not, and the due rows (the
    workspace is deleted). Its people are the ones its session and draft rows name, so this runs
    before the sessions go."""
    mirrored = await store._query(f"WDRAFTS#{workspace_id}", begins_with="D#")
    sessions = await store._query(f"WSESSIONS#{workspace_id}", begins_with="S#")
    users = {r["user_id"] for r in [*mirrored, *sessions] if r.get("user_id")}
    deleted = 0
    for user_id in sorted(users):
        deleted += await delete_drafts(user_id, workspace_id=workspace_id)
    await asyncio.gather(*(_rm(r["pk"], r["sk"]) for r in mirrored))  # any mirror whose author row is gone
    return deleted


def _delete_episodes(client, memory_id: str, user_id: str) -> int:
    base = _ns(user_id)
    ids = list(
        dict.fromkeys(
            r["memoryRecordId"]
            for ns in (base, f"{base}episodes/")
            for r in _pages(client.list_memory_records, "memoryRecordSummaries", memoryId=memory_id, namespace=ns)
        )
    )
    failed = 0
    for i in range(0, len(ids), _BATCH):
        resp = client.batch_delete_memory_records(
            memoryId=memory_id, records=[{"memoryRecordId": r} for r in ids[i : i + _BATCH]]
        )
        failed += len(resp.get("failedRecords") or [])
    if failed:
        raise RuntimeError(f"episode wipe: {failed} of {len(ids)} records failed to delete")
    return len(ids)


async def delete_episodes(
    user_id: str, *, resweep_after_s: float = workspace_memory.RESWEEP_AFTER_S, still_gone=None
) -> int:
    """Every record in both of the user's namespaces and every raw event written under their actor
    id, then one late re-sweep of records (extraction may still be in flight), as the private-scope wipe."""
    memory_id = settings.AGENTCORE_SESSIONS_MEMORY_ID
    if not memory_id:
        return 0
    client = _client()
    count = await run_in_thread(_delete_episodes, client, memory_id, user_id)
    await run_in_thread(_delete_events, client, user_id, memory_id)
    base = _ns(user_id)
    task = asyncio.create_task(
        workspace_memory._tail(
            f"user={user_id}", memory_id, [base, f"{base}episodes/"], [], resweep_after_s, still_gone
        )
    )
    workspace_memory._tails.add(task)
    task.add_done_callback(workspace_memory._tails.discard)
    return count
