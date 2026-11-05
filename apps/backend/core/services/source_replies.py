"""Replies to a mail or text trigger go back the way the message came (lane A, Task 9; contracts-v2 §14.6).

O's `router_turn` calls `deliver_reply` for a `reply` decision on a trigger that has no room. The trigger's
`ref["reply"]` is what the inbound router recorded: for mail the sender, Message-ID, References and subject
(`agent_mail_reply.send_reply` threads the answer); for text the provider and its line/customer ids
(`sendblue.send_text` / `photon.send_text`, recorded in the agent inbox like the routers' own replies).
"""

from __future__ import annotations

import logging

from core.repositories import agent_mail_inbox_repo
from core.services import agent_mail_reply, photon, sendblue
from core.services.webhook_dedup import WebhookDedupResult, delete_event, record_event_or_skip

logger = logging.getLogger(__name__)


async def deliver_reply(ws: str, trigger: dict, text: str, *, idempotency_key: str | None = None) -> None:
    """Send `text` back to the mail/text sender of `trigger`. `idempotency_key` (the Router's event id) makes
    an Activity retry a no-op; a send that raises frees the key so the retry can send."""
    ref = trigger.get("ref") or {}
    reply = ref.get("reply")
    source = trigger.get("source")
    if not reply or source not in ("mail", "text"):
        return
    key = f"reply:{ws}:{idempotency_key}" if idempotency_key else None  # scoped per workspace
    if key and await record_event_or_skip(key, source="source_replies") is WebhookDedupResult.ALREADY_SEEN:
        return
    try:
        await _send(ws, trigger, ref, reply, source, text, idempotency_key)
    except Exception:
        if key:
            await delete_event(key)
        raise


async def _send(
    ws: str, trigger: dict, ref: dict, reply: dict, source: str, text: str, idempotency_key: str | None
) -> None:
    agent_id = ref.get("agentId") or ""
    if source == "mail":
        status = await agent_mail_reply.send_reply(
            ctx={
                "owner_id": ws,
                "agent_id": agent_id,
                "reply_to": reply["address"],
                "subject": reply.get("subject") or "",
                "in_reply_to": reply.get("messageId"),
                "references": reply.get("references"),
            },
            body_text=text,
        )
        if status != "sent":  # send_reply never raises; a reply that did not go out must free the key
            raise RuntimeError(f"mail reply {status}")
        return
    if reply.get("provider") == "photon":
        sent = await photon.send_text(
            sender_id=reply["senderId"],
            space_id=reply["spaceId"],
            text=text,
            idempotency_key=f"reply:{idempotency_key or trigger['triggerId']}",  # per reply, not per trigger
        )
        message_id = (sent or {}).get("id") or f"photon-reply:{trigger['triggerId']}"
        to = reply.get("to") or ""
    else:
        await sendblue.send_text(from_number=reply["fromNumber"], to_number=reply["toNumber"], content=text)
        message_id = f"sendblue-reply:{trigger['triggerId']}"
        to = reply["toNumber"]
    await agent_mail_inbox_repo.put_sent(
        address=reply.get("address") or "",
        message_id=message_id,
        owner_id=ws,
        agent_id=agent_id,
        to=to,
        subject="",
        text=text,
        channel="text",
    )
