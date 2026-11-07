"""Deliver an inbound agent-email or text message to the Router as a trigger.

**Never raises.** Callers (the inbound mail webhook handler) must be able to treat this
as a terminal step; every failure mode collapses to ``"error"``.
"""

import logging
from typing import Literal

from core import tasks_schema as ts
from core.dynamodb import utc_now_iso

logger = logging.getLogger(__name__)

MAX_TRIGGER_TEXT = 32_768


async def deliver_to_agent(
    *,
    owner_id: str,
    agent_id: str,
    message: str,
    idempotency_key: str,
    session_key: str | None = None,
    source: Literal["mail", "text"] = "mail",
    reply_ref: dict | None = None,
) -> str:
    """Admit an inbound message to the Router: one `TRIG#` + `WORK#ADMIT#` pair (contracts-v2 §14.6; `source`
    mail|text, `reply_ref` = how a reply gets back, see `source_replies`). The callers' stranger caps, allowlists
    and trial gates run before this call.

    One trigger per message id: a provider retry of the same message is the same trigger (the row put is
    conditional; the outbox put is unconditional by O6's design). Returns ``"delivered"``, or ``"error"`` when
    the rows could not be written, so the caller rolls back its dedup and asks the provider to retry. Never raises.
    """
    from core.services import admission  # lazy: admission imports the messaging stack

    now = utc_now_iso()
    try:
        trigger = ts.Trigger(
            ws=owner_id,
            trigger_id=ts.trigger_id(source, idempotency_key),
            source=source,
            admission="respond",
            lane="shared",
            # No person sent it: the workspace is the actor, so the tasks it starts have an owner (and a
            # person-visible one is not refused) and are billed like other owner-less usage (`owner_id`).
            actor_user_id=owner_id,
            received_at=now,
            created_at=now,
            updated_at=now,
            ref={
                "agentId": agent_id,
                "text": message[:MAX_TRIGGER_TEXT],
                "sessionKey": session_key,
                "reply": reply_ref,
            },
        )
        await admission.enqueue_trigger(owner_id, trigger)
    except Exception as exc:  # noqa: BLE001 — never raises; the caller 503s and the provider retries
        logger.warning(
            "agent_mail_delivery admit failed owner=%s agent=%s err=%s", owner_id, agent_id, type(exc).__name__
        )
        return "error"
    return "delivered"
