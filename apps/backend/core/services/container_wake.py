"""Explicit container wake — the ONE lease-idempotent path behind every
intent door.

``request_wake`` backs ``POST /container/wake`` (mount-with-presence,
interaction-after-idle, retry buttons, the Teams wake button) and
``request_wake_silent`` backs the chat-send stopped branch. Both funnel into
the same dispatch so a wake can never race itself: the actual start is
``dispatch_provision``, whose atomic ``try_claim_provision`` lease already
serializes concurrent dispatches (this module does NO status pre-write — a
pre-write would race concurrent callers into double-dispatch, the exact
incident class the lease closed).

Ordering is law (verified in the design review): gates are evaluated BEFORE
any dispatch, so a capped/expired owner gets the structured 402 payload and
never a silent wake; ``conversion_pending`` stands the wake down entirely —
the conversion's reprovision owns the row, and a second task under its drain
is the documented EFS-WAL corruption window.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from core.services import wake_policy
from core.services.provision_dispatch import dispatch_provision

logger = logging.getLogger(__name__)

# Wake request reasons (client-declared intent). Passive reasons are gated
# client-side on visibility + input recency; action reasons ARE the
# interaction by construction.
WAKE_REASONS = frozenset({"mount", "interaction", "message_send", "retry", "teams_wake"})


@dataclass(frozen=True)
class WakeResult:
    outcome: str  # dispatched | started | already_running | in_flight | converting | no_row | blocked
    gate_payload: dict[str, Any] | None = None


async def request_wake(owner_id: str, *, reason: str, is_admin: bool) -> WakeResult:
    """Wake the owner's container in response to explicit intent.

    Returns a ``WakeResult`` — the router maps ``blocked`` to a 402 with the
    structured gate payload (the same card every other door renders) and
    everything else to a 200. Never consults the suppress predicate and never
    debits the legacy budget: explicit intent is always allowed to *try*.
    """
    from core.repositories import container_repo
    from core.services import provision_gate, trial_cap

    # Gates first — identical ordering to the status route, so the wake verb
    # can never start a container the status door would have blocked.
    role = "admin" if is_admin else "member"
    trial_gate = provision_gate.gate_for_trial_reason(await trial_cap.trial_block_reason(owner_id), role)
    if trial_gate is not None:
        return WakeResult(outcome="blocked", gate_payload=trial_gate.to_payload())
    gate = await provision_gate.evaluate_provision_gate(owner_id=owner_id, is_admin=is_admin)
    if gate is not None:
        return WakeResult(outcome="blocked", gate_payload=gate.to_payload())

    container = await container_repo.get_by_owner_id(owner_id)
    if not container:
        # No row at all: the status route's signup path owns first provisions
        # (ensure_user_ready has org/member semantics this verb must not
        # duplicate). Report no_row; the caller keeps polling status, which
        # dispatches ensure_user_ready itself.
        return WakeResult(outcome="no_row")
    if container.get("conversion_pending"):
        return WakeResult(outcome="converting")
    status = container.get("status")
    if status == "running":
        return WakeResult(outcome="already_running")
    if status == "provisioning" and not container_repo.is_provision_claim_stale(container):
        return WakeResult(outcome="in_flight")

    # FAST PATH: a cleanly stopped row with no pending config change needs
    # exactly one thing — desiredCount=1. Routing that through the SFN
    # executor cold-launches a whole provisioner Fargate task (~90-120s of
    # pure overhead) to make one update_service call. start_user_service is
    # the existing start chokepoint: it re-runs the start gates, does NOT
    # force a new deployment (so it can never terminate a still-draining
    # task), and fires the detached running-transition poller that flips
    # the row — with the boot reconcile resuming that poller across backend
    # deploys, and the status poll's stale-lease auto-retry as the final
    # backstop. The needs_provision marker (set by task-def-shaped config
    # writes, today provider switches) routes a wake through the FULL
    # provision once, which re-registers the task def, rewrites
    # openclaw.json, and clears the marker. Errored rows always take the
    # full path — a failed provision needs the runner, not a scale-up.
    if status == "stopped" and not container.get("needs_provision"):
        try:
            from core.containers import get_ecs_manager
            from core.containers.ecs_manager import (
                START_REFUSED_CONVERTING,
                START_REFUSED_REPROVISIONING,
                ContainerStartRefused,
            )

            await get_ecs_manager().start_user_service(owner_id)
            wake_policy.note_wake_dispatched(door="wake_endpoint", intent=reason)
            logger.info("container wake fast-path started owner=%s reason=%s", owner_id, reason)
            return WakeResult(outcome="started")
        except ContainerStartRefused as refusal:
            # The chokepoint's re-check refused DELIBERATELY — state moved
            # between our gates above and the start. These are POLICY
            # outcomes, never fodder for the dispatch fallback: the SFN
            # runner starts with force=True, so dispatching here would wake
            # a container the policy just refused (cap/conversion bypass).
            if refusal.reason == START_REFUSED_REPROVISIONING:
                # A config change landed mid-wake — the full provision IS
                # the remedy; fall through to dispatch below.
                logger.info(
                    "container wake fast-path deferred to full provision owner=%s (config change pending)",
                    owner_id,
                )
            elif refusal.reason == START_REFUSED_CONVERTING:
                return WakeResult(outcome="converting")
            else:
                # Cap / expiry raced our earlier gate read — surface the
                # same structured card every other refused door renders.
                gate = provision_gate.gate_for_trial_reason(refusal.reason, role)
                return WakeResult(
                    outcome="blocked",
                    gate_payload=gate.to_payload() if gate is not None else None,
                )
        except Exception:
            # An OPERATIONAL fast-path failure (ECS hiccup) falls back to
            # the full executor — the wake must never be lost to an
            # optimization.
            logger.exception("container wake fast-path failed owner=%s — falling back to dispatch", owner_id)

    # stopped-with-marker / error / stale-lease / fast-path failure:
    # dispatch. dispatch_provision's lease claim makes concurrent dispatches
    # (this verb, a legacy poll, a second tab) collapse to exactly one
    # provision.
    asyncio.create_task(dispatch_provision(owner_id))
    wake_policy.note_wake_dispatched(door="wake_endpoint", intent=reason)
    logger.info("container wake dispatched owner=%s reason=%s", owner_id, reason)
    return WakeResult(outcome="dispatched")


# Stop provenances a chat message must NOT undo: an operator deliberately took
# the box down (admin stop, runaway killswitch). The status poll's auto-retry
# may still recover these per its own rules; the SILENT path never does.
_OPERATOR_STOP_REASONS = frozenset({"admin", "killswitch"})


async def request_wake_silent(owner_id: str, *, door: str = "chat_send") -> str:
    """Best-effort wake for server-side intent (a message arrived for a
    stopped container). Same gate ordering as ``request_wake`` — the trial
    gates AND ``evaluate_provision_gate`` (subscription/credits/OAuth) both
    stand the wake down, so a canceled owner can never resurrect a container
    with a chat message — plus one stricter rule: rows an operator stopped
    (``stop_reason`` admin/killswitch) are never silently restarted. Never
    raises; returns only the outcome string — the caller emits its
    user-facing frame regardless (the wake is a side effect, not the
    reply)."""
    try:
        from core.repositories import container_repo
        from core.services import provision_gate, trial_cap

        # Trial gates still apply — a capped owner's send must not start a
        # container the cap already parked. (The chat path surfaces its own
        # limit card; this helper just declines to wake.)
        if await trial_cap.trial_block_reason(owner_id) is not None:
            return "blocked"
        if await provision_gate.evaluate_provision_gate(owner_id=owner_id, is_admin=True) is not None:
            return "blocked"
        container = await container_repo.get_by_owner_id(owner_id)
        if not container:
            return "no_row"
        if container.get("conversion_pending"):
            return "converting"
        if container.get("stop_reason") in _OPERATOR_STOP_REASONS:
            return "operator_stopped"
        status = container.get("status")
        if status == "running":
            return "already_running"
        if status == "provisioning" and not container_repo.is_provision_claim_stale(container):
            return "in_flight"
        asyncio.create_task(dispatch_provision(owner_id))
        wake_policy.note_wake_dispatched(door=door, intent="message_send")
        logger.info("container wake dispatched (silent) owner=%s door=%s", owner_id, door)
        return "dispatched"
    except Exception:
        logger.exception("request_wake_silent failed owner=%s", owner_id)
        return "error"
