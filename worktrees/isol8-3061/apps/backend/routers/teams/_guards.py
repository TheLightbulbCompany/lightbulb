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

from core.repositories import billing_repo
from core.services import provision_gate, trial_cap

DEFAULT_ALWAYS_ON_MESSAGE = (
    "Scheduled routines are part of a paid plan — on the free plan your agents "
    "only run while you're here. Upgrade to let them work on a schedule."
)


async def require_always_on_entitlement(owner_id: str, message: str | None = None) -> None:
    """403 unless the owner may create ALWAYS-ON automation.

    Autonomy is what the subscription buys — ``has_always_on_entitlement`` is
    False for ``trial_local`` — and its docstring has always listed "Paperclip
    routines" among the features it gates. Until now nothing enforced that on
    any routine path: a trial owner could create a routine and attach a cron
    trigger with no check at all.

    Gating CREATION (not just the manual ``/run`` dispatch) is the point. A
    trial container is parked when the tab closes, and no Paperclip or operator dispatch
    path wakes it. So a routine a trial owner schedules for 3am does not "run inside
    the $3/day cap": it does not run AT ALL, silently, every night. Letting them
    build that is worse than refusing it. This is a broken-affordance fix, not a
    paywall move — spend was already fenced by the meter Lambda's daily cap.

    Same predicate as the channel gate in ``routers/config.py``, but a 402 with
    the structured ``{blocked, code, message}`` body every other trial block in
    the system uses — not that gate's bare-string 403. The shared API client
    drops a non-string ``detail`` but keeps the parsed body on ``ApiError``, so
    this shape is what lets the UI render an upgrade card at exactly the moment
    the user reached for autonomy, instead of a stringified exception.
    """
    account = await billing_repo.get_by_owner_id(owner_id)
    if not provision_gate.has_always_on_entitlement(account):
        raise HTTPException(
            status_code=402,
            detail={
                "blocked": True,
                "code": "routines_require_subscription",
                # ``message`` is the only thing a caller may vary: the predicate
                # and the code are the decision, the copy is what the surface
                # reaching for autonomy should say. The provisioning rail passes
                # its own; everything else gets the routines wording.
                "message": message or DEFAULT_ALWAYS_ON_MESSAGE,
            },
        )


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
