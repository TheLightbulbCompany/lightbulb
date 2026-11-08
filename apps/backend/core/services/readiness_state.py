"""The single readiness + onboarding composer -- INV-1.

``compute_status(owner_id)`` is the SOLE reader that combines the four
readiness inputs into one ``StatusContract``:

  1. the ``containers`` row              (``core.repositories.container_repo``)
  2. the ``billing-accounts`` row        (``core.repositories.billing_repo``)
  3. the ``paperclip-companies`` owner row (``core.repositories.workspace_repo``
                                           -- carries BOTH ``teams_state`` and
                                           the generation-fenced onboarding
                                           fields, so this is ONE physical
                                           read, not two)

No other module may COMPOSE two or more of the inputs above to derive
a phase / status / onboarding-progress verdict -- that is what INV-1
forbids, and what
``tests/unit/services/test_readiness_state_architecture.py`` enforces (a
lightweight AST import-count check, not a full import-linter). A module
reading exactly ONE of the four stores for its own unrelated purpose (e.g.
a Teams router reading only the paperclip-companies row for Paperclip
session/auth plumbing) is fine and untouched -- see that
router's own docstring for why it was deliberately NOT folded into this
composer (a per-request Teams-BFF hot path pulling in three unrelated DDB
reads it doesn't need would be a real latency regression, not an INV-1 fix).

INV-6: chat availability is decided SOLELY by ``runtime_phase``
("provisioning" | "ready") -- never by ``lifecycle``,
``funnel``, ``bootstrap``, or ``teams_state``. A degraded/awaiting-brief
owner (funnel not submitted, or Teams seed failed) whose container is
warm still gets ``chat_available: True``.
"""

from __future__ import annotations

import logging
from typing import Any, Literal, Optional

from core.repositories import billing_repo, container_repo
from core.repositories.workspace_repo import ONBOARDING_STATE_DEFAULTS
from core.services.provision_gate import is_subscription_active

logger = logging.getLogger(__name__)

RuntimePhase = Literal["provisioning", "ready"]

Lifecycle = Literal[
    "needs_signup",
    "provider_selection_required",
    "blocked",
    "provisioning",
    "parked",
    "error",
    "ready",
]

# Sentinel distinguishing "caller didn't pass a container override" from
# "caller explicitly passed None (no container row)" in compute_status's
# optional `container` kwarg -- see that function's docstring.
_NOT_GIVEN: Any = object()


def resolve_runtime_phase(container: Optional[dict]) -> RuntimePhase:
    """Pure ``container status -> runtime phase`` map.

    INV-6: this function -- and ONLY this function's output
    (``runtime_phase``) -- decides chat availability. It must never take
    onboarding/funnel/teams_state as an input, and no caller may derive
    ``chat_available`` from anything else.

    Returns:
      - "provisioning"  container row absent, or the runtime is not provisioned
                         yet (error / anything but RUNNING).
      - "ready"         chat is usable. Reported WITHOUT a health probe:
                         AgentCore parks an idle microVM invisibly and wakes it
                         on invoke, so a failed probe would mean "asleep", not
                         "not ready" -- and probing at all would wake the very
                         box the poll is meant to leave parked.
    """
    return "ready" if (container or {}).get("status") == "running" else "provisioning"


def _workspace_repo():
    """Local import so ``patch("core.repositories.workspace_repo.WorkspaceRepo", ...)``
    (the pattern every existing test double in this codebase already uses)
    keeps working -- mirrors the pre-COMPOSER ``_resolve_teams_status``'s
    own local-import idiom in ``routers/container.py``.
    """
    from core.repositories.workspace_repo import WorkspaceRepo

    return WorkspaceRepo()


async def resolve_onboarding_state(owner_id: str) -> dict:
    """Best-effort wrapper over ``WorkspaceRepo.get_onboarding_state``.

    A read failure here must never break ``/container/status`` -- mirrors
    the pre-COMPOSER ``_resolve_teams_status``'s degrade-to-None posture.
    Returns the same all-defaults shape ``get_onboarding_state`` returns
    for a genuinely absent row.

    PUBLIC (not underscore-prefixed): callers that need the raw onboarding
    dict for something OTHER than composing it into the StatusContract —
    for example, ``routers/container.py`` checks its ``pending_brief`` bit
    before scheduling a retry. Pass that same dict into
    ``compute_status(..., onboarding=...)`` to avoid a second read. This does
    not weaken INV-1: the composer is still the only place readiness and
    onboarding facts are combined into a verdict; this function returns one
    store's raw facts untouched.
    """
    try:
        return await _workspace_repo().get_onboarding_state(owner_id)
    except Exception:  # noqa: BLE001 — a degraded read must never break /status
        logger.info("readiness_state: paperclip-companies read failed for owner %s", owner_id, exc_info=True)
        return dict(ONBOARDING_STATE_DEFAULTS)


async def compute_status(owner_id: str, *, container: Any = _NOT_GIVEN, onboarding: Any = _NOT_GIVEN) -> dict[str, Any]:
    """Compose the four readiness inputs into one ``StatusContract`` dict.

    Args:
        owner_id: the container/company owner (org_id for org members,
            else the user_id).
        container: an already-fetched (and possibly locally mutated, e.g.
            by the caller) containers row, OR an explicit
            ``None``. Pass this when the caller already read/mutated the
            row this request -- it avoids a redundant DDB read AND lets
            the contract reflect same-request local mutations a fresh read
            wouldn't see yet. Omit it (the default) to have this function
            read the row itself.
        onboarding: an already-fetched ``resolve_onboarding_state(owner_id)``
            dict. Pass this when the caller already read it to skip a redundant
            paperclip-companies read. Omit it (the default) to have this
            function read it itself.

    Returns a plain dict shaped like the shared ``StatusContract`` TS type
    in ``packages/shared/src/hooks/statusContract.ts`` -- keep the two in
    lockstep.
    """
    if container is _NOT_GIVEN:
        try:
            container = await container_repo.get_by_owner_id(owner_id)
        except Exception:  # noqa: BLE001 — status must always answer
            logger.warning("readiness_state: containers read failed for owner %s", owner_id, exc_info=True)
            container = None

    try:
        account = await billing_repo.get_by_owner_id(owner_id)
    except Exception:  # noqa: BLE001 — status must always answer
        logger.warning("readiness_state: billing read failed for owner %s", owner_id, exc_info=True)
        account = None

    if onboarding is _NOT_GIVEN:
        onboarding = await resolve_onboarding_state(owner_id)

    is_org = owner_id.startswith("org_")
    provider_choice = (account or {}).get("provider_choice")
    subscription_status = (account or {}).get("subscription_status")
    sub_active = is_subscription_active(account)

    parked = False
    runtime_phase: RuntimePhase
    lifecycle: Lifecycle

    if container is None:
        runtime_phase = "provisioning"
        if not sub_active:
            # Personal: no billing row (or an inactive one) means "never
            # started signup" / "needs to re-subscribe" -- the picker.
            # Org: the free tier is personal-only (trial_local never
            # applies to an org_ owner), so an inactive/absent org billing
            # row is the workspace billing paywall, folded into "blocked"
            # here -- the router's own evaluate_provision_gate call still
            # supplies the precise blocked-gate payload (team_billing_required
            # vs subscription_required vs payment_past_due); this composer
            # only needs to name the COARSE lifecycle bucket.
            lifecycle = "blocked" if is_org else "needs_signup"
        elif is_org and subscription_status != "trial_local" and provider_choice is None:
            lifecycle = "provider_selection_required"
        else:
            lifecycle = "provisioning"
    else:
        status = container.get("status")
        if status == "error":
            lifecycle = "error"
            runtime_phase = "provisioning"
        else:
            from core.services import wake_policy

            try:
                parked = await wake_policy.is_parked_trial(owner_id, container, account=account)
            except Exception:  # noqa: BLE001 — parked detection must never break /status
                logger.info("readiness_state: parked-trial check failed for owner %s", owner_id, exc_info=True)
                parked = False
            if parked:
                lifecycle = "parked"
                runtime_phase = "provisioning"
            else:
                runtime_phase = resolve_runtime_phase(container)
                lifecycle = runtime_phase  # "provisioning" | "ready"

    chat_available = runtime_phase == "ready"  # INV-6: SOLELY runtime_phase.

    funnel_state = onboarding.get("funnel_state") or "not_started"
    # A brief the user has submitted but that the server hasn't been able to
    # fulfil yet (Paperclip still provisioning -- the common case, since the
    # funnel deliberately renders DURING provisioning) is "submitted" as far as
    # the CLIENT is concerned: their first message is safely captured. It is
    # emphatically NOT submitted as far as the row is concerned — no claim,
    # no goal, no project — which is why this mapping lives here in the
    # composer rather than in the raw onboarding dict. INV-1: combining facts
    # into a verdict is this function's job.
    if funnel_state == "not_started" and onboarding.get("pending_brief"):
        funnel_state = "submitted"
    # RECONCILE-WITH-ONBOARD: container.py:554's legacy "onboarded" fact
    # becomes `funnel_state == "complete"` once ONBOARD's writers exist.
    # Until then `onboarding_generation` is always None on this branch (no
    # writer ships it here), so fall back to the legacy per-container
    # `onboarded_at` stamp -- preserves existing behavior (and existing
    # tests) until ONBOARD's funnel writers land.
    onboarded_legacy = container is not None and container.get("onboarded_at") is not None
    onboarded = (funnel_state == "complete") if onboarding.get("onboarding_generation") else onboarded_legacy

    return {
        "owner_id": owner_id,
        "lifecycle": lifecycle,
        "runtime_phase": runtime_phase,
        "chat_available": chat_available,
        "container_status": (container or {}).get("status"),
        "container_substatus": (container or {}).get("substatus"),
        "parked": parked,
        "onboarded": onboarded,
        "last_error": (container or {}).get("last_error"),
        "last_error_at": (container or {}).get("last_error_at"),
        "created_at": (container or {}).get("created_at"),
        "updated_at": (container or {}).get("updated_at"),
        "generations": {
            "provision_generation": (container or {}).get("provision_generation"),
            "onboarding_generation": onboarding.get("onboarding_generation"),
        },
        "funnel": {
            "state": funnel_state,
            "goal_id": onboarding.get("onboarding_goal_id"),
            "project_id": onboarding.get("onboarding_project_id"),
            "submitted_at": onboarding.get("funnel_submitted_at"),
            "completed_at": onboarding.get("funnel_completed_at"),
        },
        "provider": {
            "choice": provider_choice,
            "byo_provider": (account or {}).get("byo_provider"),
            "is_org": is_org,
            "subscription_status": subscription_status,
        },
        "teams_state": onboarding.get("teams_state"),
    }
