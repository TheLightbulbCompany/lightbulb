"""Deliver an inbound agent-email message into the owner's running OpenClaw container.

Mirrors ``routers/hooks_proxy.py``'s resolve-container -> read hook_token ->
POST-to-``/hooks/agent`` pattern, but for internal-origin inbound mail rather
than external webhook callers.

**Wakes a scaled-to-zero container, gated by the SAME cap predicate chat and
container-start use.** If ``resolve_running_container`` reports no IP, the
container is a free-tier box that scaled to zero while idle. We check
``trial_cap.trial_block_reason`` ourselves before waking: an over-cap owner
is never woken by mail (``"container_not_running"``, same as before -- the
caller's cap check upstream already normally intercepts this, but this
service must not wake an over-cap owner even if called from somewhere that
skipped that check). An under-cap owner gets ``start_user_service`` fired and
the wake stamped (``container_repo.stamp_email_cold_wake``), and we return
``"woken"`` -- we do NOT wait for the container and do NOT POST in this
request. Delivery of this specific message is completed by **SES's own
retry**: the caller maps ``"woken"`` to HTTP 503, the ses-inbound Lambda
re-raises on a 5xx (see ``_post_to_backend`` in
``apps/infra/lambda/ses-inbound/index.py``), and because its SES invocation
is asynchronous, AWS retries it roughly a minute later -- by which time the
Fargate cold start has finished and the normal ``"delivered"`` path runs.
Nothing is queued, polled, or held in memory here; a backend restart between
the wake and the retry loses nothing because there is nothing in-process to
lose.

**Never raises.** Callers (the inbound mail webhook handler) must be able to
treat this as a terminal step; every failure mode collapses to ``"error"``.
"""

import logging

import httpx

from core.containers import get_ecs_manager
from core.containers.ecs_manager import GATEWAY_PORT
from core.repositories import container_repo
from core.services.trial_cap import trial_block_reason

logger = logging.getLogger(__name__)


def _client_factory() -> httpx.AsyncClient:
    """Build the upstream HTTP client. Module-level so tests can swap in a stub transport."""
    return httpx.AsyncClient(timeout=10)


async def deliver_to_agent(*, owner_id: str, agent_id: str, message: str, idempotency_key: str) -> str:
    """POST an inbound message to the owner's running container's agent hook.

    Returns one of ``"delivered"``, ``"woken"``, ``"container_not_running"``,
    ``"error"``. Never raises.
    """
    try:
        container, ip = await get_ecs_manager().resolve_running_container(owner_id)
        if not ip:
            # Asleep. Waking is cost, so it goes through the SAME cap
            # predicate chat and container-start use -- an over-cap owner is
            # never woken by mail.
            if await trial_block_reason(owner_id) is not None:
                return "container_not_running"
            await get_ecs_manager().start_user_service(owner_id)
            await container_repo.stamp_email_cold_wake(owner_id)
            # Delivery is completed by SES's OWN retry, not by anything here.
            # The caller maps "woken" to a 503, the ingest Lambda re-raises on
            # 5xx, and its async invocation is retried ~1 min later -- by which
            # time the Fargate cold start (~40s) has finished. Nothing is
            # queued or held in memory, so a backend restart loses nothing.
            return "woken"

        hook_token = (container or {}).get("hook_token") or ""
        target = f"http://{ip}:{GATEWAY_PORT}/hooks/agent"
        body = {
            "message": message,
            "agentId": agent_id,
            "name": "Email",
            "idempotencyKey": idempotency_key,
            # Delivery opt-out. OpenClaw "channels" are transport PLUGINS
            # (slack/discord/telegram); our email is not one of them — the
            # agent answers by invoking isol8-owner-email.sh, which comes back
            # through our own backend to SES. Upstream defaults `deliver` to
            # true (`resolveHookDeliver`: anything but false means deliver)
            # and `channel` to "last", so after the run did its work OpenClaw
            # tried to deliver the final message to a channel, found none, and
            # ended EVERY inbound-email run with
            #     status=error "Channel is required (no configured channels...)"
            # even on a fully successful reply. That permanently poisoned
            # status=error as a signal — a real failure looked identical to a
            # working one. Opting out is what this field exists for.
            "deliver": False,
        }
        headers = {"Authorization": f"Bearer {hook_token}"}

        async with _client_factory() as client:
            response = await client.post(target, json=body, headers=headers)
        response.raise_for_status()
        return "delivered"
    except Exception as exc:  # noqa: BLE001 — must never raise; every failure -> "error"
        logger.warning("agent_mail_delivery failed owner=%s agent=%s err=%s", owner_id, agent_id, exc)
        return "error"
