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

from core.repositories import container_repo
from core.services import runtime_transport
from core.services.openclaw_work import _AGENTCORE_INGRESS_TIMEOUT_S
from core.services.trial_cap import trial_block_reason

logger = logging.getLogger(__name__)


async def deliver_to_agent(
    *,
    owner_id: str,
    agent_id: str,
    message: str,
    idempotency_key: str,
    session_key: str | None = None,
) -> str:
    """POST an inbound message to the owner's runtime.

    Returns one of ``"delivered"``, ``"container_not_running"``, ``"error"``. Never raises.
    """
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
