"""Lightbulb Phone's and Meetings' own rows in the messaging table, under their own sort keys:

OWNER#<owner>   VOICERUN#<runId>                    a Live voice run's room, agent and speaker (2 h TTL; NOT RUN#:
                                                   chat never steers into a voice run)
OWNER#<owner>   VOICEEDIT#<agent>#<kind>#<day>      a day's count of voice edits per agent (2-day TTL)
OWNER#<owner>   <slot sk>                           a held slot (a live-call slot, the forwarding check's lock)
OWNER#<owner>   MEETNOTES#<messageId>               a meeting's notes request (2-day TTL)
OWNER#<owner>   VOICEAGENT                          the Agent page's document (JSON `config`). The engine reads one
                                                   key of it, `frontAgentId` (its own reader); the product writes it
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from botocore.exceptions import ClientError

from core.dynamodb import get_item, messaging_table, owner_pk, run_in_thread, utc_now_iso


def _voice_run_sk(run_id: str) -> str:
    return f"VOICERUN#{run_id}"


def meeting_room_id(owner_id: str, uid: str) -> str:
    """The channel a calendar invite (by its UID) opens in the owner's workspace."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"lightbulb-meeting|{owner_id}|{uid}"))


_VOICE_RUN_TTL_S = 2 * 3600


async def put_voice_run(
    owner_id: str, run_id: str, fields: dict[str, Any], once: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """Create the run row, or set ``fields`` on it, and return the row. One atomic SET, never a whole-row
    put: a /turns retry writes the same fields again and cannot drop what another writer set in between.
    A field in ``once`` is written only if the row has none yet (a lowered ``None`` counts),
    so a retry can never undo what a steer lowered."""
    fields = {"runId": run_id, **fields, "expiresAt": int(time.time()) + _VOICE_RUN_TTL_S}
    response = await run_in_thread(
        messaging_table().update_item,
        Key={"pk": owner_pk(owner_id), "sk": _voice_run_sk(run_id)},
        UpdateExpression="SET "
        + ", ".join(
            f"#f{i} = if_not_exists(#f{i}, :v{i})" if k in once else f"#f{i} = :v{i}" for i, k in enumerate(fields)
        ),
        ExpressionAttributeNames={f"#f{i}": k for i, k in enumerate(fields)},
        ExpressionAttributeValues={f":v{i}": v for i, v in enumerate(fields.values())},
        ReturnValues="ALL_NEW",
    )
    return response.get("Attributes") or {}


#: A meeting's "write the notes" request message -> everything its after-meeting mail needs (the
#: meeting room, occurrence, where the request sits, and a snapshot of the meeting), so the mail no
#: longer depends on the live meeting record, which a recurring series rewrites when it advances.
#: Its own once-only markers live here too. Kept a day longer than any notes run.
_MEETING_NOTES_TTL_S = 2 * 86400


def _meeting_notes_sk(message_id: str) -> str:
    return f"MEETNOTES#{message_id}"


async def put_meeting_notes_request(owner_id: str, message_id: str, fields: dict[str, Any]) -> None:
    item = {
        "pk": owner_pk(owner_id), "sk": _meeting_notes_sk(message_id), **fields,
        "expiresAt": int(time.time()) + _MEETING_NOTES_TTL_S,
    }  # fmt: skip
    try:  # first write wins: a retried dispatch must not clear a mail claim already taken
        await run_in_thread(messaging_table().put_item, Item=item, ConditionExpression="attribute_not_exists(pk)")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


async def get_meeting_notes_request(owner_id: str, message_id: str) -> dict | None:
    row = await get_item(messaging_table(), owner_pk(owner_id), _meeting_notes_sk(message_id))
    return row if row and int(row.get("expiresAt", 0)) > time.time() else None


async def claim_meeting_notes_mail(owner_id: str, message_id: str) -> bool:
    """True once per notes request: the mail is attempted at most once (a lost mail beats a double)."""
    try:
        await run_in_thread(
            messaging_table().update_item,
            Key={"pk": owner_pk(owner_id), "sk": _meeting_notes_sk(message_id)},
            UpdateExpression="SET mailAttemptedAt = :now",
            ConditionExpression="attribute_exists(pk) AND attribute_not_exists(mailAttemptedAt)",
            ExpressionAttributeValues={":now": int(time.time())},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
    return True


async def get_voice_run(owner_id: str, run_id: str) -> dict | None:
    row = await get_item(messaging_table(), owner_pk(owner_id), _voice_run_sk(run_id))
    # DynamoDB TTL deletes lazily: an expired row must not authorize anything.
    return row if row and int(row.get("expiresAt", 0)) > time.time() else None


async def count_voice_edit(owner_id: str, agent_id: str, kind: str, limit: int) -> bool:
    """One voice edit of ``kind`` for this agent today (UTC); False once ``limit`` are used.
    Not WORK#/LIVE#: neither the projector nor the Pipe reads this sk."""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        await run_in_thread(
            messaging_table().update_item,
            Key={"pk": owner_pk(owner_id), "sk": f"VOICEEDIT#{agent_id}#{kind}#{day}"},
            UpdateExpression="ADD edits :one SET expiresAt = :exp",
            ConditionExpression="attribute_not_exists(edits) OR edits < :limit",
            ExpressionAttributeValues={":one": 1, ":limit": limit, ":exp": int(time.time()) + 2 * 86400},
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


async def claim_voice_slot(owner_id: str, sk: str, holder: str, hold_s: int) -> bool:
    """Hold the row ``sk`` for ``holder`` for ``hold_s`` seconds: free, expired, or already this holder's (a replay).
    A live-call slot or the forwarding check's in-progress lock; ``expiresAt`` is the lease and the TTL alike."""
    now = int(time.time())
    try:
        await run_in_thread(
            messaging_table().put_item,
            Item={"pk": owner_pk(owner_id), "sk": sk, "holder": holder, "expiresAt": now + hold_s},
            ConditionExpression="attribute_not_exists(sk) OR expiresAt < :now OR holder = :h",
            ExpressionAttributeValues={":now": now, ":h": holder},
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


async def release_voice_slot(owner_id: str, sk: str, holder: str) -> None:
    try:
        await run_in_thread(
            messaging_table().delete_item,
            Key={"pk": owner_pk(owner_id), "sk": sk},
            ConditionExpression="holder = :h",
            ExpressionAttributeValues={":h": holder},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


# --- the Agent page's document -----------------------------------------------------------------------------------


def voice_agent_config(config: dict) -> dict:
    """The current app-only meeting settings; a retired email toggle has no behavior or promise."""
    meetings = config.get("meetings")
    return (
        {**config, "meetings": {k: v for k, v in meetings.items() if k != "emailNotes"}}
        if isinstance(meetings, dict)
        else dict(config)
    )


async def get_voice_agent(owner_id: str) -> dict | None:
    row = await get_item(messaging_table(), owner_pk(owner_id), "VOICEAGENT")
    return voice_agent_config(json.loads(row["config"])) if row else None


def _voice_agent_item(owner_id: str, config: dict, updated_by: str) -> dict:
    return {"pk": owner_pk(owner_id), "sk": "VOICEAGENT", "config": json.dumps(voice_agent_config(config)),
            "updatedAt": utc_now_iso(), "updatedBy": updated_by}  # fmt: skip


async def put_voice_agent(owner_id: str, config: dict, updated_by: str) -> None:
    await run_in_thread(messaging_table().put_item, Item=_voice_agent_item(owner_id, config, updated_by))


async def modify_voice_agent(owner_id: str, change, updated_by: str) -> bool:
    """Read-modify-write for the phone paths, which must not undo an owner's save made in between (a Pause, a
    permission): the put is conditional on the `updatedAt` it read, and on a clash re-reads and re-applies.
    `change(cfg)` returns the new config, or None for nothing to write. False: no Agent page, or nothing to write.
    The owner's own save (`put_voice_agent`) stays unconditional. ponytail: 5 tries, then the write is dropped."""
    for _ in range(5):
        row = await get_item(messaging_table(), owner_pk(owner_id), "VOICEAGENT")
        if not row:
            return False
        new = change(voice_agent_config(json.loads(row["config"])))
        if new is None:
            return False
        try:
            await run_in_thread(
                messaging_table().put_item,
                Item=_voice_agent_item(owner_id, new, updated_by),
                ConditionExpression="updatedAt = :u",
                ExpressionAttributeValues={":u": row["updatedAt"]},
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
    return False
