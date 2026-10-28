"""The evaluator: uploaded full sessions go into the sessions memory's episodic strategy
(``session_episodes``, ``AGENTCORE_SESSIONS_MEMORY_ID``: a resource of its own, so no other
strategy reads them and no room turn becomes an episode) under the person's own namespace; its
reflections (patterns across their sessions) become drafts a person can later approve into team memory.

Namespaces are per user and never cross users: episodes in ``/users/<user>/episodes/``,
reflections in ``/users/<user>/``. ListMemoryRecords on the latter also returns the episodes
beneath it, so a reflection is told apart by the record's own ``namespaces``.
Rows live in the messaging table: DRAFTS#<user>/D#<id> (+ WDRAFTS#<ws>/D#<user>#<id> for a lesson:
a finding may hold a secret, so only its author sees it), EVAL#<user>/WATERMARK and /S#<session>
(ingested), EVALDUE/<user> (reflections may still come: the sweep collects until it expires),
MEMCAP#<user>#<YYYY-MM>/COUNT (free cap, expires after the month).
Nothing here logs session text, summaries or reflection text.
"""

import asyncio
import logging
import re
import time
from datetime import datetime

from botocore.exceptions import ClientError

from core.config import settings
from core.dynamodb import run_in_thread, utc_now_iso
from core.repositories import messaging_repo as store
from core.services import workspace_memory
from core.services.workspace_memory import _BATCH, _client, _delete_events, _pages

logger = logging.getLogger(__name__)

FREE_MONTHLY_CAP = 50
DUE_S = 7 * 86400  # how long after an ingest the sweep keeps collecting that person's reflections
CAP_ROW_S = 62 * 86400  # a monthly cap row outlives its month, then expires
_DUE = "EVALDUE"
_SECRET = re.compile(r"secret|credential|token|\bkeys?\b|api[_ -]?key|password", re.I)


def _ns(user_id: str) -> str:
    if not user_id or "/" in user_id or ".." in user_id:
        raise ValueError("unsafe user id for a memory namespace")
    return f"/users/{user_id}/"


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
    try:
        for i in range(0, len(events), _BATCH):  # one CreateEvent per <=100 payloads, stamped at the first
            chunk = events[i : i + _BATCH]
            await run_in_thread(
                client.create_event,
                memoryId=memory_id, actorId=user_id, sessionId=sid, eventTimestamp=chunk[0][0], payload=[p for _, p in chunk],
            )  # fmt: skip
    except Exception:
        await run_in_thread(table.delete_item, Key=marker)  # a failed ingest neither counts nor blocks the retry
        if workspace_id is None:
            await run_in_thread(
                table.update_item, Key=cap_key, UpdateExpression="ADD n :m", ExpressionAttributeValues={":m": -1}
            )
        raise
    # Reflections are extracted later: collect what is ready now, and the sweep the rest.
    due = {"pk": _DUE, "sk": user_id, "workspace_id": workspace_id, "expiresAt": int(time.time()) + DUE_S}
    try:
        await run_in_thread(table.put_item, Item=due)
        await collect_drafts(user_id, workspace_id=workspace_id)
    except Exception as e:  # noqa: BLE001 -- the session is in; collecting again is the sweep's job
        logger.warning("draft collect after ingest failed: %s", type(e).__name__)
    return True


async def collect_due() -> int:
    """The sweep's half: collect drafts for everyone whose sessions were ingested recently."""
    created = 0
    now = time.time()
    for row in await store._query(_DUE):
        if int(row.get("expiresAt") or 0) <= now:
            continue
        try:
            created += await collect_drafts(row["sk"], workspace_id=row.get("workspace_id"))
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
    sessions = {r["id"] for r in await store._query(f"SESSIONS#{user_id}", begins_with="S#")}
    created = 0
    for r in new:
        text = r.get("content", {}).get("text", "")
        meta = " ".join(str(v) for v in (r.get("metadata") or {}).values())
        named = [s for s in sessions if re.search(rf"(?<![\w.-]){re.escape(s)}(?![\w.-])", f"{text} {meta}")]
        item = {
            "id": r["memoryRecordId"], "user_id": user_id, "kind": "finding" if _SECRET.search(text) else "lesson",
            "text": text, "evidence": sorted(named), "status": "proposed", "created": utc_now_iso(),
        }  # fmt: skip
        if workspace_id:
            item["workspace_id"] = workspace_id
        if not await _put_new(table, {"pk": f"DRAFTS#{user_id}", "sk": f"D#{item['id']}", **item}):
            continue  # already collected (maybe approved since): never reset it
        created += 1
        if workspace_id and item["kind"] != "finding":  # a finding may hold a secret: its author's only
            await run_in_thread(
                table.put_item, Item={"pk": f"WDRAFTS#{workspace_id}", "sk": f"D#{user_id}#{item['id']}", **item}
            )
    if new:
        newest = max(_ts(str(r.get("createdAt"))) for r in new)
        await run_in_thread(table.put_item, Item={"pk": f"EVAL#{user_id}", "sk": "WATERMARK", "at": newest.isoformat()})
    return created


def _row(item: dict) -> dict:
    return {k: v for k, v in item.items() if k not in ("pk", "sk")}


def _own_findings(rows: list[dict], workspace_id: str) -> list[dict]:
    """The caller's own findings in this workspace: in no mirror, so read from their partition."""
    return [r for r in rows if r.get("kind") == "finding" and r.get("workspace_id") == workspace_id]


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
    if draft.get("workspace_id") and draft.get("kind") != "finding":  # a finding has no mirror
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
    if not workspace_id:
        jobs += [_rm(m["pk"], m["sk"]) for m in await store._query(f"EVAL#{user_id}")]
        # Older months' cap rows expire on their own (CAP_ROW_S).
        jobs += [_rm(_DUE, user_id), _rm(f"MEMCAP#{user_id}#{utc_now_iso()[:7]}", "COUNT")]
    await asyncio.gather(*jobs)
    return len(drafts)


async def delete_workspace_drafts(workspace_id: str) -> int:
    """Every draft collected in this workspace, both copies (the workspace is deleted)."""
    mirrored = await store._query(f"WDRAFTS#{workspace_id}", begins_with="D#")
    jobs = [_rm(r["pk"], r["sk"]) for r in mirrored]
    jobs += [_rm(f"DRAFTS#{r['user_id']}", f"D#{r['id']}") for r in mirrored]
    await asyncio.gather(*jobs)
    return len(mirrored)


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
