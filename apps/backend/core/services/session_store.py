"""Coding-agent sessions a person chose to upload from the CLI.

A metadata row per session in the messaging table, in the uploader's partition
(SESSIONS#<user> / S#<id>) and, for a workspace member, also in the workspace's
(WSESSIONS#<workspace> / S#<user>#<id>, so one member cannot overwrite another's row).
A full upload also puts the session and its tool calls as one JSON object in S3 at
sessions/<free|workspace>/<user>/<id>.json: those two prefixes are what the lifecycle rules key on.
The rows expire with them (``expiresAt``, the table's TTL: 30 days free, 365 in a workspace, from
the upload), and until DynamoDB removes one an expired row reads as gone.
Nothing here is logged: summaries and rows are the person's.
"""

import asyncio
import json
import re
import time
from datetime import datetime

import boto3

from core.config import settings
from core.dynamodb import run_in_thread, utc_now_iso
from core.repositories import messaging_repo as store
from core.repositories import usage_repo
from core.services.usage_service import _current_period

LIST_CAP = 500
RETENTION_DAYS = {False: 30, True: 365}  # by "in a workspace": the S3 lifecycle rules' ages
_FIELDS = (
    "id", "harness", "computer", "repo", "branch", "started", "ended", "prompt_tokens", "completion_tokens",
)  # fmt: skip


def _user_pk(user_id: str) -> str:
    return f"SESSIONS#{user_id}"


def _ws_pk(workspace_id: str) -> str:
    return f"WSESSIONS#{workspace_id}"


def _key(user_id: str, session_id: str, workspace_id: str | None) -> str:
    return f"sessions/{'workspace' if workspace_id else 'free'}/{user_id}/{session_id}.json"


def _s3():
    return boto3.client("s3", region_name=settings.AWS_REGION)


def _meta(item: dict) -> dict:
    return {k: v for k, v in item.items() if k not in ("pk", "sk", "expiresAt")}


def _live(item: dict | None) -> dict | None:
    """The row, or None once past its retention (DynamoDB's TTL deletes it up to days later)."""
    if item is None or int(item.get("expiresAt") or 0) and int(item["expiresAt"]) <= time.time():
        return None
    return item


def _expires_at(uploaded_at: str, in_workspace: bool) -> int:
    return int(datetime.fromisoformat(uploaded_at).timestamp()) + RETENTION_DAYS[in_workspace] * 86400


async def put_sessions(user_id: str, rows: list[dict], full: bool, workspace_id: str | None = None) -> int:
    """Store (or replace: same id, same row and object) each session. A row may carry its
    ``tool_calls``; they are kept, with their summaries, only when ``full``. Returns the count."""
    table = store._table()
    s3 = _s3()
    now = utc_now_iso()
    for row in rows:
        # Same id re-uploaded under another tier or workspace: the old copies go first, or they
        # outlive the row that names them (and forget-me could not find them).
        old = await store._get(_user_pk(user_id), f"S#{row['id']}")
        if old and old.get("workspace_id") != workspace_id:
            await _delete(old, s3)
        if workspace_id:  # the member's usage carries what their agents spent; a re-upload adds only the change
            kept = old if old and old.get("workspace_id") == workspace_id else {}
            dp = int(row.get("prompt_tokens") or 0) - int(kept.get("prompt_tokens") or 0)
            dc = int(row.get("completion_tokens") or 0) - int(kept.get("completion_tokens") or 0)
            started = str(row.get("started") or "")[:7]
            if dp or dc:
                period = started if re.fullmatch(r"\d{4}-\d{2}", started) else _current_period()
                await usage_repo.add_capture_tokens(workspace_id, f"member:{user_id}:{period}", dp, dc)
        item = {k: row.get(k) for k in _FIELDS}
        item.update(user_id=user_id, full=full, uploaded_at=now, expiresAt=_expires_at(now, bool(workspace_id)))
        if workspace_id:
            item["workspace_id"] = workspace_id
        key = _key(user_id, row["id"], workspace_id)
        if full:
            body = json.dumps({"session": item, "tool_calls": row.get("tool_calls", [])})
            await run_in_thread(s3.put_object, Bucket=settings.OWNER_STATE_BUCKET, Key=key, Body=body.encode())
        else:  # a metadata re-upload replaces a full one: the old object must not outlive it
            await run_in_thread(s3.delete_object, Bucket=settings.OWNER_STATE_BUCKET, Key=key)
        await run_in_thread(table.put_item, Item={"pk": _user_pk(user_id), "sk": f"S#{row['id']}", **item})
        if workspace_id:
            await run_in_thread(
                table.put_item, Item={"pk": _ws_pk(workspace_id), "sk": f"S#{user_id}#{row['id']}", **item}
            )
    return len(rows)


async def list_sessions(
    *, user_id: str | None = None, workspace_id: str | None = None, since: str | None = None
) -> list[dict]:
    """The workspace's sessions if ``workspace_id`` is given, else the user's own; newest first."""
    pk = _ws_pk(workspace_id) if workspace_id else _user_pk(user_id or "")
    rows = [_meta(i) for i in await store._query(pk, begins_with="S#") if _live(i)]
    rows = [r for r in rows if not since or (r.get("started") or "") >= since]
    rows.sort(key=lambda r: r.get("started") or "", reverse=True)
    return rows[:LIST_CAP]


async def _delete(item: dict, s3=None) -> None:
    table = store._table()
    s3 = s3 or _s3()
    owner, sid, ws = item["user_id"], item["id"], item.get("workspace_id")
    await run_in_thread(table.delete_item, Key={"pk": _user_pk(owner), "sk": f"S#{sid}"})
    if ws:
        await run_in_thread(table.delete_item, Key={"pk": _ws_pk(ws), "sk": f"S#{owner}#{sid}"})
    await run_in_thread(s3.delete_object, Bucket=settings.OWNER_STATE_BUCKET, Key=_key(owner, sid, ws))


async def delete_sessions(user_id: str, *, workspace_id: str | None = None) -> int:
    """Everything this user uploaded (account deletion, ``lightbulb`` forget-me), or with
    ``workspace_id`` only what they uploaded in that workspace (removed from it)."""
    items = await store._query(_user_pk(user_id), begins_with="S#")
    if workspace_id:
        items = [i for i in items if i.get("workspace_id") == workspace_id]
    s3 = _s3()
    await asyncio.gather(*(_delete(i, s3) for i in items))
    return len(items)


async def delete_workspace_sessions(workspace_id: str) -> int:
    """Every member's sessions in this workspace, rows and objects (the workspace is deleted)."""
    items = await store._query(_ws_pk(workspace_id), begins_with="S#")
    s3 = _s3()
    await asyncio.gather(*(_delete(i, s3) for i in items))
    return len(items)


async def get_session(session_id: str, *, user_id: str | None, workspace_id: str | None) -> dict | None:
    """The stored row if it is the caller's own, or (for a member) the workspace's."""
    item = await store._get(_user_pk(user_id), f"S#{session_id}") if user_id else None
    if _live(item) is None and workspace_id:
        rows = await store._query(_ws_pk(workspace_id), begins_with="S#")
        item = next((r for r in rows if r["id"] == session_id), None)
    return _live(item)


async def delete_session(session_id: str, *, user_id: str, workspace_id: str | None) -> bool:
    """Delete one of the caller's own sessions (a teammate's reads, but is not theirs to delete)."""
    item = await get_session(session_id, user_id=user_id, workspace_id=workspace_id)
    if item is None or item.get("user_id") != user_id:
        return False
    await _delete(item)
    return True
