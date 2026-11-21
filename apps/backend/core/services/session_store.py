"""The wipes of the coding-agent sessions people uploaded from the 0.9 CLI (nothing uploads any more).

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

import boto3

from core.config import settings
from core.dynamodb import run_in_thread
from core.repositories import messaging_repo as store


def _user_pk(user_id: str) -> str:
    return f"SESSIONS#{user_id}"


def _ws_pk(workspace_id: str) -> str:
    return f"WSESSIONS#{workspace_id}"


def _key(user_id: str, session_id: str, workspace_id: str | None) -> str:
    return f"sessions/{'workspace' if workspace_id else 'free'}/{user_id}/{session_id}.json"


def _s3():
    return boto3.client("s3", region_name=settings.AWS_REGION)


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
