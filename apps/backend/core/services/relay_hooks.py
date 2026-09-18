"""Per-connection side effects of the gateway relay; every function is safe to
call from the handler that owns the socket.

Moved from the retired API-Gateway operator relay module
(`_maybe_disarm_expired_trial`), `routers/websocket_chat.py`
(`_WAKE_GATED_METHODS`, `_wake_refusal_payload`, `_admit_operator_wake`), and
the retired API-Gateway node relay module (`_publish_node_invalidation`; its
first-node/last-node tool-deny-list edges went with the ECS runtime — the node
broker's `commands` allowlist replaces them, spec §4.4).
"""

from __future__ import annotations

import logging
from typing import Any

from core.observability.metrics import put_metric
from core.services import chat_gate

logger = logging.getLogger(__name__)

WAKE_GATED_METHODS = frozenset({"chat.send", "sessions.send", "sessions.steer", "agent"})


def wake_refusal_payload(gate: dict, *, req_id: Any) -> dict:
    code = gate.get("code", "blocked")
    return {
        "type": "res",
        "id": req_id,
        "ok": False,
        "error": {
            "code": code,
            "message": gate.get("message", "Agent wake blocked"),
            "details": {"reason": "wake_admission_gate", "code": code},
        },
    }


async def admit_operator_wake(*, user_id: str, owner_id: str, body: dict[str, Any]) -> dict | None:
    if body.get("method") not in WAKE_GATED_METHODS:
        return None
    params = body.get("params")
    session_key = params.get("sessionKey") if isinstance(params, dict) else ""
    try:
        gate = await chat_gate.gate_chat(user_id=user_id, owner_id=owner_id, session_key=session_key or "")
    except Exception:
        # Paperclip work must not be lost on a transient UX-gate read; the
        # Bedrock gate remains the inference fence.
        put_metric("wake.admission", dimensions={"outcome": "gate_error"})
        return None
    if not gate.get("blocked"):
        return None
    put_metric("wake.admission", dimensions={"outcome": "blocked", "reason": gate.get("code", "blocked")})
    return wake_refusal_payload(gate, req_id=body.get("id"))


async def maybe_disarm_expired_trial(owner_id: str) -> None:
    """A routine's wake was BLOCKED — self-disarm iff the reason is a lapsed
    trial window.

    ``trial_local``'s clock is a local ``trial_end`` stamp with NO webhook, so
    an expired trial's armed nightly routine is the one entitlement change
    nothing else catches: teardown fires on cancel/revoke, not on a trial
    quietly lapsing. Left armed it fires every night against a container the
    trial gate will never let wake — a daily no-op / gateway-timeout storm.
    Pausing it (and cancelling its looping issues) is the fix; a later
    re-subscribe converts through ``run_conversion_reprovision`` → the executor
    arm → ``reconcile_paused`` re-enables it, so the pause self-heals.

    A DAILY_CAP block is deliberately NOT disarmed: the cap resets at UTC
    midnight and the routine should fire again tomorrow. So this re-reads the
    reason (one cheap read on the rare blocked path) rather than disarming on
    any block. Never raises — a best-effort side effect of a wake attempt."""
    try:
        from core.services import trial_cap

        if await trial_cap.trial_block_reason(owner_id) != trial_cap.TRIAL_EXPIRED:
            return
        from core.services.paperclip_agent_lifecycle import disarm_owner_nightly_automation

        summary = await disarm_owner_nightly_automation(owner_id)
        put_metric("nightly_ceo.disarm", dimensions={"trigger": "trial_expired", "outcome": "ran"})
        logger.info(
            "operator connect: disarmed expired-trial nightly routine owner=%s routines_paused=%d issues_cancelled=%d",
            owner_id,
            summary["routines_paused"],
            summary["issues_cancelled"],
        )
    except Exception:
        put_metric("nightly_ceo.disarm", dimensions={"trigger": "trial_expired", "outcome": "error"})
        logger.exception("operator connect: expired-trial disarm failed owner=%s", owner_id)


async def publish_node_invalidation(owner_id: str, user_id: str) -> None:
    # Lazy imports deliberately kept (not hoisted to module top): avoids an
    # import-cycle risk between relay_hooks and live_events/messaging.
    from core.services.live_events import get_live_event_publisher
    from core.services.messaging import make_live_event

    try:
        await get_live_event_publisher().publish_user(
            owner_id,
            user_id,
            make_live_event(
                "runtime.resource.invalidated",
                {"resource": "health", "resourceId": "desktop-node"},
            ),
        )
    except Exception:
        logger.warning("desktop-node invalidation publish failed", exc_info=True)


async def on_node_ready(owner_id: str, user_id: str, device_id: str) -> None:
    """A node came up. The OpenClaw-shaped edges this used to run — the
    pending-surface approval RPC into a container gateway, and the first-node
    tools-deny-list patch on ``openclaw.json`` — have no AgentCore equivalent
    and are gone with the ECS runtime. The broker's ``commands`` allowlist
    (``node_broker.NodeSession``, built by the relay at connect time) is what
    replaces the deny-list flip on this transport (spec §4.4).

    ``device_id`` is still accepted so the relay's two call sites do not churn
    in this PR; it goes with ``routers/gateway_relay.py``'s node leg."""
    await publish_node_invalidation(owner_id, user_id)


async def on_node_closed(owner_id: str, user_id: str, *, stale_row_undeleted: bool = False) -> None:
    """A node went away — see ``on_node_ready`` for why this is now only the
    invalidation publish. ``stale_row_undeleted`` only ever corrected the
    relay-node count for the deny-list re-patch, so it too is inert and kept
    only for the caller's signature."""
    await publish_node_invalidation(owner_id, user_id)
