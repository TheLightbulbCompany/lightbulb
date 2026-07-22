"""Deliver an inbound agent-email message into the owner's running OpenClaw container.

Mirrors ``routers/hooks_proxy.py``'s resolve-container -> read hook_token ->
POST-to-``/hooks/agent`` pattern, but for internal-origin inbound mail rather
than external webhook callers.

**Never wakes a scaled-to-zero container.** If ``resolve_running_container``
reports no IP, the container is not currently running (e.g. a free-tier box
that scaled to zero while idle) and we return ``"container_not_running"``
without POSTing — silently dropping the wake is intentional, not a bug.

**Never raises.** Callers (the inbound mail webhook handler) must be able to
treat this as a terminal step; every failure mode collapses to ``"error"``.
"""

import logging

import httpx

from core.containers import get_ecs_manager
from core.containers.ecs_manager import GATEWAY_PORT

logger = logging.getLogger(__name__)


def _client_factory() -> httpx.AsyncClient:
    """Build the upstream HTTP client. Module-level so tests can swap in a stub transport."""
    return httpx.AsyncClient(timeout=10)


async def deliver_to_agent(*, owner_id: str, agent_id: str, message: str, idempotency_key: str) -> str:
    """POST an inbound message to the owner's running container's agent hook.

    Returns one of ``"delivered"``, ``"container_not_running"``, ``"error"``.
    Never raises.
    """
    try:
        container, ip = await get_ecs_manager().resolve_running_container(owner_id)
        if not ip:
            return "container_not_running"

        hook_token = (container or {}).get("hook_token") or ""
        target = f"http://{ip}:{GATEWAY_PORT}/hooks/agent"
        body = {
            "message": message,
            "agentId": agent_id,
            "name": "Email",
            "idempotencyKey": idempotency_key,
        }
        headers = {"Authorization": f"Bearer {hook_token}"}

        async with _client_factory() as client:
            response = await client.post(target, json=body, headers=headers)
        response.raise_for_status()
        return "delivered"
    except Exception as exc:  # noqa: BLE001 — must never raise; every failure -> "error"
        logger.warning("agent_mail_delivery failed owner=%s agent=%s err=%s", owner_id, agent_id, exc)
        return "error"
