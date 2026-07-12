"""Shared trial-runway guard for Teams BFF endpoints that dispatch a run NOW.

UX layer only: the authoritative stop is the meter Lambda's revoke-AIP +
``desiredCount=0`` pairing. These guards exist so a click on Run/Retry/
Check-now gets a clean 402 instead of a failed-run card.

The predicate is ``trial_cap.trial_block_reason`` — the SAME one the provision
gate and the container-start chokepoint use. This guard used to check only the
daily cap, which meant a trial owner whose window had EXPIRED (but who was
under today's $3) could still fire a routine, a check-now, or a comment reply
and dispatch a Paperclip run that a chat message would have been refused.
"""

from fastapi import HTTPException

from core.services import provision_gate, trial_cap


async def require_trial_runway(owner_id: str) -> None:
    reason = await trial_cap.trial_block_reason(owner_id)
    if reason is None:
        return

    # Copy comes from the same Gate the provision gate renders, so the Teams
    # limit card and the chat limit card cannot drift apart. The ``owner_role``
    # a Gate carries is not part of this 402 body — both trial gates are
    # action_admin_only=False, so it does not change how the card renders, and
    # only ``code`` and ``message`` are read here.
    gate = provision_gate.gate_for_trial_reason(reason, owner_role="admin")
    raise HTTPException(
        status_code=402,
        detail={"blocked": True, "code": gate.code, "message": gate.message},
    )
