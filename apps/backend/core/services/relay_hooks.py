"""Per-connection side effects of the gateway relay; every function is safe to
call from the handler that owns the socket.

Moved from the retired API-Gateway operator relay module
(`_maybe_disarm_expired_trial`), `routers/websocket_chat.py`
(`_WAKE_GATED_METHODS`, `_wake_refusal_payload`, `_admit_operator_wake`), and
the retired API-Gateway node relay module (`_publish_node_invalidation` and
the first-node/last-node tool-deny-list edges, previously tracked by an
in-process node-count map and now by the `ws-connections` presence rows via
`ConnectionService.count_relay_nodes`).
"""

from __future__ import annotations

import logging
from typing import Any

from core.containers.config import TOOL_DENY_BASE, TOOL_DENY_WITH_NODES
from core.observability.metrics import put_metric
from core.services import chat_gate, runtime_transport
from core.services.config_patcher import patch_openclaw_config
from core.services.connection_service import ConnectionService
from core.services.desktop_pair_live import approve_pending_node_surface

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


async def _is_agentcore(owner_id: str) -> bool:
    return await runtime_transport.is_agentcore_owner(owner_id)


async def on_node_ready(owner_id: str, user_id: str, device_id: str) -> None:
    # Both of these are OpenClaw-shaped: an RPC chain into a container gateway that does not
    # exist on AgentCore, and a deny-list patch on an openclaw.json that does not exist there
    # either. The broker's `commands` allowlist (node_broker.NodeSession, built by the relay
    # at connect time) replaces the deny-list flip for that transport (spec §4.4).
    if not await _is_agentcore(owner_id):
        await approve_pending_node_surface(owner_id=owner_id, device_id=device_id)
        if await ConnectionService().count_relay_nodes(owner_id) == 1:
            # First live node: drop "nodes" from the deny list. The config patch
            # deep-merge REPLACES the deny list wholesale, so the write must carry
            # every tool that stays denied — sourced from the shared
            # TOOL_DENY_BASE constant so a new base-deny entry can never be
            # silently re-enabled by a node pairing.
            await patch_openclaw_config(owner_id, {"tools": {"deny": list(TOOL_DENY_BASE)}})
    await publish_node_invalidation(owner_id, user_id)


async def on_node_closed(owner_id: str, user_id: str, *, stale_row_undeleted: bool = False) -> None:
    """``stale_row_undeleted``: the caller's own presence-row delete failed,
    so `count_relay_nodes` still counts THIS closing connection's row --
    subtract it (floored at 0) rather than undercounting "last node gone"."""
    if not await _is_agentcore(owner_id):
        count = await ConnectionService().count_relay_nodes(owner_id)
        if stale_row_undeleted:
            count = max(count - 1, 0)
        if count == 0:
            try:
                # Last node gone: re-disable node tools. Deep-merge replaces the
                # deny list wholesale — write the full provision-time set
                # (TOOL_DENY_WITH_NODES = base + "nodes").
                await patch_openclaw_config(owner_id, {"tools": {"deny": list(TOOL_DENY_WITH_NODES)}})
            except Exception:  # noqa: BLE001 — container may be down; invalidation must still publish
                logger.debug(
                    "relay_hooks.on_node_closed: deny-list re-patch failed for owner=%s", owner_id, exc_info=True
                )
    await publish_node_invalidation(owner_id, user_id)
