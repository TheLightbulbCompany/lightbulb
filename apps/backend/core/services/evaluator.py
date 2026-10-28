"""The evaluator: uploaded full sessions go into AgentCore Memory's episodic strategy
(``session_episodes``) under the person's own namespace; its reflections (patterns across their
sessions) become drafts a person can later approve into team memory.

Namespaces are per user and never cross users: episodes in ``/users/<user>/episodes/``,
reflections in ``/users/<user>/``. ListMemoryRecords on the latter also returns the episodes
beneath it, so a reflection is told apart by the record's own ``namespaces``.
Rows live in the messaging table: DRAFTS#<user>/D#<id> (+ WDRAFTS#<ws>/D#<user>#<id>),
EVAL#<user>/WATERMARK and /S#<session> (ingested), MEMCAP#<user>#<YYYY-MM>/COUNT (free cap).
Nothing here logs session text, summaries or reflection text.
"""

import asyncio
import re
from datetime import datetime

from core.config import settings
from core.dynamodb import run_in_thread, utc_now_iso
from core.repositories import messaging_repo as store
from core.services.workspace_memory import _BATCH, _client, _pages

FREE_MONTHLY_CAP = 50
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
    memory_id = settings.AGENTCORE_MEMORY_ID
    if not memory_id:
        return False
    sid = session["id"]
    table = store._table()
    if await store._get(f"EVAL#{user_id}", f"S#{sid}"):
        return False  # ponytail: a re-upload is not re-ingested (duplicate events would duplicate episodes)
    cap_key = {"pk": f"MEMCAP#{user_id}#{utc_now_iso()[:7]}", "sk": "COUNT"}
    if workspace_id is None:
        used = await store._get(cap_key["pk"], "COUNT")
        if used and int(used.get("n", 0)) >= FREE_MONTHLY_CAP:
            return False
    events = _events(session, tool_calls)
    client = _client()
    for i in range(0, len(events), _BATCH):  # one CreateEvent per <=100 payloads, stamped at the first
        chunk = events[i : i + _BATCH]
        await run_in_thread(
            client.create_event,
            memoryId=memory_id, actorId=user_id, sessionId=sid, eventTimestamp=chunk[0][0], payload=[p for _, p in chunk],
        )  # fmt: skip
    await run_in_thread(table.put_item, Item={"pk": f"EVAL#{user_id}", "sk": f"S#{sid}"})
    if workspace_id is None:
        await run_in_thread(
            table.update_item, Key=cap_key, UpdateExpression="ADD n :one", ExpressionAttributeValues={":one": 1}
        )
    return True


def _reflections(client, memory_id: str, user_id: str) -> list[dict]:
    ns = _ns(user_id)
    rows = _pages(client.list_memory_records, "memoryRecordSummaries", memoryId=memory_id, namespace=ns)
    return [r for r in rows if ns in (r.get("namespaces") or [ns])]


async def collect_drafts(user_id: str, *, workspace_id: str | None) -> int:
    memory_id = settings.AGENTCORE_MEMORY_ID
    if not memory_id:
        return 0
    table = store._table()
    mark = await store._get(f"EVAL#{user_id}", "WATERMARK")
    since = _ts(mark["at"]) if mark else _ts(None)
    records = await run_in_thread(_reflections, _client(), memory_id, user_id)
    new = [r for r in records if _ts(str(r.get("createdAt"))) > since] if records else []
    sessions = {r["id"] for r in await store._query(f"SESSIONS#{user_id}", begins_with="S#")}
    for r in new:
        text = r.get("content", {}).get("text", "")
        meta = " ".join(str(v) for v in (r.get("metadata") or {}).values())
        named = [s for s in sessions if s in text or s in meta]
        item = {
            "id": r["memoryRecordId"], "user_id": user_id, "kind": "finding" if _SECRET.search(text) else "lesson",
            "text": text, "evidence": sorted(named), "status": "proposed", "created": utc_now_iso(),
        }  # fmt: skip
        if workspace_id:
            item["workspace_id"] = workspace_id
        await run_in_thread(table.put_item, Item={"pk": f"DRAFTS#{user_id}", "sk": f"D#{item['id']}", **item})
        if workspace_id:
            await run_in_thread(
                table.put_item, Item={"pk": f"WDRAFTS#{workspace_id}", "sk": f"D#{user_id}#{item['id']}", **item}
            )
    if new:
        newest = max(_ts(str(r.get("createdAt"))) for r in new)
        await run_in_thread(table.put_item, Item={"pk": f"EVAL#{user_id}", "sk": "WATERMARK", "at": newest.isoformat()})
    return len(new)


async def list_drafts(
    *, user_id: str | None = None, workspace_id: str | None = None, status: str = "proposed"
) -> list[dict]:
    pk = f"WDRAFTS#{workspace_id}" if workspace_id else f"DRAFTS#{user_id or ''}"
    rows = [{k: v for k, v in i.items() if k not in ("pk", "sk")} for i in await store._query(pk, begins_with="D#")]
    return sorted((r for r in rows if r.get("status") == status), key=lambda r: r.get("created", ""), reverse=True)


async def delete_drafts(user_id: str) -> int:
    """The user's drafts, their ingest markers and watermark (the monthly counter stays)."""
    table = store._table()
    drafts = await store._query(f"DRAFTS#{user_id}", begins_with="D#")
    marks = await store._query(f"EVAL#{user_id}")

    async def rm(pk: str, sk: str) -> None:
        await run_in_thread(table.delete_item, Key={"pk": pk, "sk": sk})

    jobs = [rm(d["pk"], d["sk"]) for d in drafts] + [rm(m["pk"], m["sk"]) for m in marks]
    jobs += [rm(f"WDRAFTS#{d['workspace_id']}", f"D#{user_id}#{d['id']}") for d in drafts if d.get("workspace_id")]
    await asyncio.gather(*jobs)
    return len(drafts)


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


async def delete_episodes(user_id: str) -> int:
    """Every record in both of the user's namespaces (episodes and reflections)."""
    memory_id = settings.AGENTCORE_MEMORY_ID
    if not memory_id:
        return 0
    return await run_in_thread(_delete_episodes, _client(), memory_id, user_id)
