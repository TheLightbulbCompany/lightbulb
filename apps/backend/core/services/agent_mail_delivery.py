"""Deliver an inbound agent-email message into the owner's AgentCore runtime.

Mail enters on the one inbound door the Hermes bridge serves, tagged with its own
``eventKind``. AgentCore starts the microVM on invoke, so there is no scaled-to-zero
service to wake first -- the invoke itself holds through the cold start (~90s budget,
the same one the canonical room ingress uses).

**Still cap-gated.** A cold start is cost, so it goes through the SAME predicate chat
and container-start use: an over-cap trial owner is never started by mail
(``"container_not_running"``). The caller's cap check upstream normally intercepts
this, but this service must not start an over-cap owner even if called from somewhere
that skipped it.

**Never raises.** Callers (the inbound mail webhook handler) must be able to treat this
as a terminal step; every failure mode collapses to ``"error"``.
"""

import logging
from typing import Literal

from core import tasks_schema as ts
from core.dynamodb import utc_now_iso
from core.repositories import container_repo
from core.services import runtime_transport
from core.services.openclaw_work import _AGENTCORE_INGRESS_TIMEOUT_S
from core.services.super_agent_flags import flags_for
from core.services.trial_cap import trial_block_reason

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
    """POST an inbound message to the owner's runtime.

    Returns one of ``"delivered"``, ``"container_not_running"``, ``"error"``. Never raises.

    Super-agent workspaces (contracts-v2 §14.6): the message becomes one `TRIG#` + `WORK#ADMIT#` pair for the
    Router instead (`source` mail|text, `reply_ref` = how a reply gets back, see `source_replies`); the callers'
    stranger caps, allowlists and trial gates run before this call either way.
    """
    if await flags_for(owner_id):
        return await _admit(owner_id, agent_id, message, idempotency_key, session_key, source, reply_ref)
    try:
        container = await container_repo.get_by_owner_id(owner_id)
        if not container or not container.get("agentcore_runtime_arn"):
            return "container_not_running"
        if await trial_block_reason(owner_id) is not None:
            return "container_not_running"

        # The bridge gates this door on the GATEWAY token (server.py's `_authorized`), not the
        # hook token -- the two are independently minted and never coincide (final-review C2).
        gateway_token = container.get("gateway_token") or ""
        response = await runtime_transport.request(
            container,
            f"/plugins/lightbulb-channel/inbound/{agent_id}",
            json_body={
                "version": 1,
                "eventId": idempotency_key,
                "eventKind": "agent_mail",
                "targetAgentId": agent_id,
                # Omitted entirely (never sent as a literal `null`) when there is no usable
                # Message-ID to derive one from. A stable per-thread key is what makes a reply
                # reach the SAME session as the message it answers.
                **({"sessionKey": session_key} if session_key else {}),
                "message": {"text": message},
            },
            headers={"Authorization": f"Bearer {gateway_token}"},
            # AgentCore holds the invoke through a cold microVM start -- a mail delivery to a
            # parked workspace must outlast that, not the old 10s ECS-era budget.
            timeout_s=_AGENTCORE_INGRESS_TIMEOUT_S,
        )
        # 202 accepted and 200 replayed (idempotent replay of the same eventId) both mean the
        # message reached the agent.
        if not 200 <= response.status_code < 300:
            logger.warning(
                "agent_mail_delivery failed owner=%s agent=%s err=agentcore inbound returned %s",
                owner_id,
                agent_id,
                response.status_code,
            )
            return "error"
        return "delivered"
    except Exception as exc:  # noqa: BLE001 — must never raise; every failure -> "error"
        logger.warning("agent_mail_delivery failed owner=%s agent=%s err=%s", owner_id, agent_id, exc)
        return "error"


async def _admit(
    owner_id: str,
    agent_id: str,
    message: str,
    idempotency_key: str,
    session_key: str | None,
    source: str,
    reply_ref: dict | None,
) -> str:
    """One trigger per message id: a provider retry of the same message is the same trigger (the row put is
    conditional; the outbox put is unconditional by O6's design). ``"error"`` when the rows could not be
    written, so the caller rolls back its dedup and asks the provider to retry."""
    from core.services import admission  # lazy: admission imports the messaging stack

    now = utc_now_iso()
    try:
        trigger = ts.Trigger(
            ws=owner_id,
            trigger_id=ts.trigger_id(source, idempotency_key),
            source=source,
            admission="respond",
            lane="shared",
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
