"""What is left of the evaluator: the wipes of the rows and records it wrote for uploaded sessions.

Rows in the messaging table: DRAFTS#<user>/D#<id> (``workspace_id``: where it was collected), mirrored to
WDRAFTS#<ws>/D#<user>#<id>; EVAL#<user>/* (ingest markers, watermark); EVALDUE/U#<user>#W#<ws|free>;
MEMCAP#<user>#<YYYY-MM>/COUNT. Records in the sessions memory (``AGENTCORE_SESSIONS_MEMORY_ID``) under
``/users/<user>/`` and its ``episodes/``. Nothing writes them any more; the wipes delete what remains.
"""

import asyncio

from core.config import settings
from core.dynamodb import run_in_thread, utc_now_iso
from core.repositories import messaging_repo as store
from core.services import workspace_memory
from core.services.workspace_memory import _BATCH, _client, _delete_events, _pages

_DUE = "EVALDUE"


def _ns(user_id: str) -> str:
    if not user_id or "/" in user_id or ".." in user_id:
        raise ValueError("unsafe user id for a memory namespace")
    return f"/users/{user_id}/"


def _due_key(user_id: str, workspace_id: str | None) -> dict:
    """One partition the sweep can list; a row per person and workspace, so a wipe of one leaves the others."""
    return {"pk": _DUE, "sk": f"U#{user_id}#W#{workspace_id or 'free'}"}


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
