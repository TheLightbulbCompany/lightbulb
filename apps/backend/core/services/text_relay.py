"""A member who texts the business's line talks to the main room as themselves; the room's answer goes back
by text. The member's room message carries ``textReplyKey`` (the inbox row of their text), so:

- an agent's run reply to that message is texted to the member from the line (``relay_reply``), whichever
  agent answered: the backend sends it, not the agent, so no agent has to remember to and none is refused.
  A failed send is retried by the container: the caller answers 503, and the retry (a replay) sends again;
- while that run works, the member sees typing, and their text reads as read (``signal_working``): for an
  agent, "read" is its run starting on the message. Sent on every start and heartbeat: the first heartbeat can
  be what starts a run whose ``run.started`` was lost, and the read's idempotency key makes repeats free.
"""

import logging

from core.observability.metrics import put_metric
from core.repositories import agent_mail_inbox_repo, messaging_repo
from core.services import photon

logger = logging.getLogger(__name__)

# triggerMessageId -> its textReplyKey, or None for a message that did not come by text. Heartbeats ask every
# ~30 s per run; a message is immutable, so the answer never changes.
# ponytail: per-process dict cleared when full; a shared cache only if the lookups ever show up in DynamoDB cost.
_KEYS: dict[str, str | None] = {}
_KEYS_MAX = 2000

# Outcomes a retry would not change: sent, nothing to send, or a send the line refuses for good.
RELAY_DONE = frozenset({"sent", "skipped", "disabled", "bad_reply_key", "no_line", "human_thread"})


async def _reply_key(room_id: str, message_id: str) -> str | None:
    if message_id not in _KEYS:
        if len(_KEYS) >= _KEYS_MAX:
            _KEYS.clear()
        message = await messaging_repo.get_message_by_id(room_id, message_id)
        _KEYS[message_id] = (message or {}).get("textReplyKey")
    return _KEYS[message_id]


async def _provider(owner_id: str, reply_key: str) -> dict | None:
    """The Photon ids stored with the member's text: its space, the line's user, the message."""
    parsed = agent_mail_inbox_repo.parse_reply_key(reply_key)
    row = await agent_mail_inbox_repo.get_message(*parsed) if parsed else None
    if not row or row.get("owner_id") != owner_id:
        return None
    return row.get("provider") or None


async def signal_working(owner_id: str, run: dict) -> None:
    """Typing on a member's text while an agent's run works on it, and the text marked read. Never raises."""
    try:
        if not run.get("triggerMessageId") or not run.get("roomId") or not photon.enabled():
            return
        key = await _reply_key(run["roomId"], run["triggerMessageId"])
        provider = await _provider(owner_id, key) if key else None
        if not provider or not provider.get("space_id") or not provider.get("line_user_id"):
            return
        await photon.send_typing(sender_id=provider["line_user_id"], space_id=provider["space_id"])
        if provider.get("message_id"):
            await photon.mark_read(provider["message_id"])
    except Exception:  # noqa: BLE001 -- a typing bubble must never fail a run callback
        logger.warning("text_relay: working signal failed for run %s", run.get("runId"), exc_info=True)


async def relay_reply(owner_id: str, trigger: dict, text: str) -> str:
    """Text an agent's room reply to the member whose text triggered it; returns the send's status (``error``
    for an exception). Never raises: the room has the reply, and the caller turns a retryable status into a 503."""
    key = trigger.get("textReplyKey")
    if not key or not text.strip():
        return "skipped"
    try:
        # The one text send door: the line's sender, the idempotency key, the sent row, the Mail panel nudge.
        from routers.agent_text import send_reply  # lazy: routers import this service's caller

        status = await send_reply(owner_id=owner_id, reply_key=key, text=text)
    except Exception:  # noqa: BLE001
        logger.warning("text_relay: relay failed for message %s", trigger.get("messageId"), exc_info=True)
        status = "error"
    put_metric("text.member_reply_relay", dimensions={"status": status})
    return status
