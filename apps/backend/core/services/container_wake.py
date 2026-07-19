"""Explicit container wake — the ONE lease-idempotent path behind every
intent door.

``request_wake`` backs ``POST /container/wake`` (mount-with-presence,
interaction-after-idle, retry buttons, the Teams wake button) and
``request_wake_silent`` backs the chat-send stopped branch. Both funnel into
the same dispatch so a wake can never race itself: the actual start is
``background_provision``, whose atomic ``try_claim_provision`` lease already
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
from core.services.container_provision import background_provision

logger = logging.getLogger(__name__)

# Wake request reasons (client-declared intent). Passive reasons are gated
# client-side on visibility + input recency; action reasons ARE the
# interaction by construction.
WAKE_REASONS = frozenset({"mount", "interaction", "message_send", "retry", "teams_wake"})


@dataclass(frozen=True)
class WakeResult:
    outcome: str  # dispatched | already_running | in_flight | converting | no_row | blocked
    gate_payload: dict[str, Any] | None = None


async def request_wake(owner_id: str, *, reason: str, is_admin: bool, user_id: str) -> WakeResult:
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

    # stopped / error / stale-lease: dispatch. background_provision's lease
    # claim makes concurrent dispatches (this verb, a legacy poll, a second
    # tab) collapse to exactly one provision.
    asyncio.create_task(background_provision(owner_id, user_id))
    wake_policy.note_wake_dispatched(door="wake_endpoint", intent=reason)
    logger.info("container wake dispatched owner=%s reason=%s", owner_id, reason)
    return WakeResult(outcome="dispatched")


# Stop provenances a chat message must NOT undo: an operator deliberately took
# the box down (admin stop, runaway killswitch). The status poll's auto-retry
# may still recover these per its own rules; the SILENT path never does.
_OPERATOR_STOP_REASONS = frozenset({"admin", "killswitch"})


async def request_wake_silent(owner_id: str, *, user_id: str, door: str = "chat_send") -> str:
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
        asyncio.create_task(background_provision(owner_id, user_id))
        wake_policy.note_wake_dispatched(door=door, intent="message_send")
        logger.info("container wake dispatched (silent) owner=%s door=%s", owner_id, door)
        return "dispatched"
    except Exception:
        logger.exception("request_wake_silent failed owner=%s", owner_id)
        return "error"
