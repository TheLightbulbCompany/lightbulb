"""Stored inbound agent mail — the inbox an agent reads on demand.

One row per delivered-or-stored inbound message. PK is the workspace's team
address (mail is routed by recipient), SK is ``{received_at}#{message_id}`` so the
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

Agent-SENT mail is recorded here too (``put_sent``, ``direction="out"``) so the
``by-message-id`` GSI can trace a reply's ``In-Reply-To``/``References`` back
to the agent who wrote the thread; ``list_messages`` never shows those rows (``list_page`` does).
"""

import logging
import time

from boto3.dynamodb.conditions import Key

from core.dynamodb import get_table, run_in_thread, utc_now_iso

logger = logging.getLogger(__name__)

MAX_TEXT_CHARS = 64_000
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
    channel: str | None = None,
    provider_ids: dict | None = None,
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
        "text": (text or "")[:MAX_TEXT_CHARS],
        # Only the threading headers — the full header dict is sender-controlled bulk.
        "headers": {
            k: v for k, v in (headers or {}).items() if k.lower() in ("message-id", "references", "in-reply-to")
        },
        "attachments": attachments or [],
        "received_at": received_at,
        "ttl": int(time.time()) + _TTL_SECONDS,
    }
    if channel:
        item["channel"] = channel
    if provider_ids:
        item["provider"] = provider_ids
    await run_in_thread(_get_table().put_item, Item=item)
    return item


async def put_sent(
    *,
    address: str,
    message_id: str,
    owner_id: str,
    agent_id: str,
    to: str,
    subject: str,
    text: str = "",
    channel: str | None = None,
) -> None:
    """Record one agent-sent message (and its plain-text body) under its RFC ``Message-ID``, then
    nudge the workspace's open Mail panels. Best-effort: the mail has already left, so a failed
    write only costs the thread's routing (a reply then goes to the CEO) and must never turn a
    sent mail into an error."""
    from core.services import agent_email  # lazy: a repo reaching up to a service

    try:
        now = utc_now_iso()
        item = {
            "address": _normalize(address),
            "received_key": f"{now}#{message_id}",
            "direction": "out",
            "owner_id": owner_id,
            "agent_id": agent_id,
            "message_id": message_id,
            "to": to,
            "subject": subject,
            "received_at": now,
            "ttl": int(time.time()) + _TTL_SECONDS,
        }
        if text:
            item["text"] = text[:MAX_TEXT_CHARS]
        if channel:
            item["channel"] = channel
        await run_in_thread(_get_table().put_item, Item=item)
    except Exception:  # noqa: BLE001 — see docstring
        logger.warning("agent_mail_inbox: put_sent failed for %s", message_id, exc_info=True)
        return
    await agent_email.mail_changed(owner_id)


async def list_messages(
    address: str, *, since: str | None = None, limit: int = 20, agent_id: str | None = None
) -> list[dict]:
    """Newest-first inbound mail at ``address``, optionally only ``agent_id``'s."""
    cond = Key("address").eq(_normalize(address))
    if since:
        cond = cond & Key("received_key").gt(since)
    limit = max(1, min(int(limit), 50))
    kwargs: dict = {"KeyConditionExpression": cond, "ScanIndexForward": False, "Limit": 50}
    items: list[dict] = []
    # ponytail: at most 5 pages (250 rows) per call — sent rows and other agents'
    # mail share the partition, so mail older than that returns short; raise the
    # page bound if a workspace's traffic outgrows it.
    for _ in range(5):
        resp = await run_in_thread(_get_table().query, **kwargs)
        items += [
            i
            for i in resp.get("Items", [])
            if i.get("direction") != "out" and (agent_id is None or i.get("agent_id") == agent_id)
        ]
        last = resp.get("LastEvaluatedKey")
        if len(items) >= limit or not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return items[:limit]


async def list_page(address: str, *, before: str | None = None, limit: int = 30) -> tuple[list[dict], str | None]:
    """One newest-first page of BOTH directions at ``address`` and the ``received_key`` to pass as
    ``before`` for the next page (None at the end)."""
    cond = Key("address").eq(_normalize(address))
    if before:
        cond = cond & Key("received_key").lt(before)
    resp = await run_in_thread(
        _get_table().query, KeyConditionExpression=cond, ScanIndexForward=False, Limit=max(1, min(int(limit), 50))
    )
    items = resp.get("Items", [])
    return items, (items[-1]["received_key"] if items and resp.get("LastEvaluatedKey") else None)


async def count_received_after(address: str, after: str, *, cap: int) -> int:
    """Received (not sent) mail at ``address`` whose ``received_key`` sorts after ``after``,
    counting no further than ``cap``. Newest first, so the query stops as soon as the cap is met."""
    kwargs: dict = {
        "KeyConditionExpression": Key("address").eq(_normalize(address)) & Key("received_key").gt(after),
        "ScanIndexForward": False,
        "ProjectionExpression": "#d",
        "ExpressionAttributeNames": {"#d": "direction"},
        "Limit": 50,
    }
    count = 0
    # ponytail: at most 10 pages (500 rows) of mostly sent mail; the 30-day TTL bounds the rest.
    for _ in range(10):
        resp = await run_in_thread(_get_table().query, **kwargs)
        count += sum(1 for i in resp.get("Items", []) if i.get("direction") != "out")
        last = resp.get("LastEvaluatedKey")
        if count >= cap or not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return min(count, cap)


async def get_message(address: str, received_key: str) -> dict | None:
    resp = await run_in_thread(
        _get_table().get_item, Key={"address": _normalize(address), "received_key": received_key}
    )
    return resp.get("Item")
