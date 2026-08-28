"""Stored inbound agent mail — the inbox an agent reads on demand.

One row per delivered-or-stored inbound message. PK is the agent's address
(mail is routed by recipient), SK is ``{received_at}#{message_id}`` so the
base-table query is newest-first by ISO timestamp. 30-day TTL. "One row per
message" is kept true across SES retries by ``find_by_message_id`` — a 503'd
delivery rolls back the ingest dedup row so SES re-invokes with the same
``message_id``, and the caller must look up the existing row via the
``by-message-id`` GSI before falling back to ``put_message``, or the retry
would mint a second row with a fresh ``received_key`` timestamp.

The ``reply_key`` (``"{address}/{received_key}"``) is what the delivered chat
message carries and what ``lightbulb-mail.sh send --reply`` hands back — it is
NOT a credential: the send door checks the row's ``owner_id`` against the
hook-token-authenticated owner before it is honoured.
"""

import time

from boto3.dynamodb.conditions import Key

from core.dynamodb import get_table, run_in_thread, utc_now_iso

_MAX_TEXT_CHARS = 64_000
_TTL_SECONDS = 30 * 24 * 3600


def _get_table():
    return get_table("agent-mail-inbox")


def _normalize(value: str) -> str:
    return value.strip().lower()


def reply_key(address: str, received_key: str) -> str:
    return f"{_normalize(address)}/{received_key}"


def parse_reply_key(key: str) -> tuple[str, str] | None:
    if not key or "/" not in key:
        return None
    address, _, received_key = key.partition("/")
    if not address or not received_key:
        return None
    return _normalize(address), received_key


async def find_by_message_id(address: str, message_id: str) -> dict | None:
    """The already-stored row for ``message_id`` at ``address``, or None.
    Queried through the ``by-message-id`` GSI (eventually consistent — fine:
    the SES retry that needs it arrives ~a minute after the first store)."""
    if not message_id:
        return None
    resp = await run_in_thread(
        _get_table().query,
        IndexName="by-message-id",
        KeyConditionExpression=Key("message_id").eq(message_id),
    )
    for item in resp.get("Items", []):
        if item.get("address") == _normalize(address):
            return item
    return None


async def put_message(
    *,
    address: str,
    owner_id: str,
    agent_id: str,
    message_id: str,
    from_addr: str,
    subject: str,
    text: str,
    headers: dict,
    attachments: list,
    received_at: str | None = None,
) -> dict:
    received_at = received_at or utc_now_iso()
    item = {
        "address": _normalize(address),
        "received_key": f"{received_at}#{message_id or 'no-id'}",
        "owner_id": owner_id,
        "agent_id": agent_id,
        "message_id": message_id,
        "from_addr": from_addr,
        "subject": subject,
        "text": (text or "")[:_MAX_TEXT_CHARS],
        # Only the threading headers — the full header dict is sender-controlled bulk.
        "headers": {
            k: v for k, v in (headers or {}).items() if k.lower() in ("message-id", "references", "in-reply-to")
        },
        "attachments": attachments or [],
        "received_at": received_at,
        "ttl": int(time.time()) + _TTL_SECONDS,
    }
    await run_in_thread(_get_table().put_item, Item=item)
    return item


async def list_messages(address: str, *, since: str | None = None, limit: int = 20) -> list[dict]:
    cond = Key("address").eq(_normalize(address))
    if since:
        cond = cond & Key("received_key").gt(since)
    resp = await run_in_thread(
        _get_table().query,
        KeyConditionExpression=cond,
        ScanIndexForward=False,
        Limit=max(1, min(int(limit), 50)),
    )
    return resp.get("Items", [])


async def get_message(address: str, received_key: str) -> dict | None:
    resp = await run_in_thread(
        _get_table().get_item, Key={"address": _normalize(address), "received_key": received_key}
    )
    return resp.get("Item")
