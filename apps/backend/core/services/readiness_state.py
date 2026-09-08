"""The single readiness + onboarding composer -- INV-1.

``compute_status(owner_id)`` is the SOLE reader that combines the four
readiness inputs into one ``StatusContract``:

  1. the ``containers`` row              (``core.repositories.container_repo``)
  2. the ``billing-accounts`` row        (``core.repositories.billing_repo``)
  3. gateway liveness                    (``core.services.connection_service``,
                                           DDB-backed -- NOT the in-process
                                           ``GatewayConnectionPool``, which is
                                           readiness store R4, now retired)
  4. the ``paperclip-companies`` owner row (``core.repositories.paperclip_repo``
                                           -- carries BOTH ``teams_state`` and
                                           the generation-fenced onboarding
                                           fields, so this is ONE physical
                                           read, not two)

See ``docs/superpowers/specs/2026-07-24-onboarding-architecture-design.md``
§2 ("Readiness composition") and §4 (INV-1), and the ``COMPOSER`` session in
``docs/superpowers/plans/2026-07-24-onboarding-implementation-prompts.md``
(spec PR-5).

No other module may COMPOSE two or more of the four inputs above to derive
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
("provisioning" | "starting" | "ready") -- never by ``lifecycle``,
``funnel``, ``bootstrap``, or ``teams_state``. A degraded/awaiting-brief
owner (funnel not submitted, or Teams seed failed) whose container is
warm still gets ``chat_available: True``.
"""

from __future__ import annotations

import logging
from typing import Any, Literal, Optional

from core.billing.metronome_provisioning import is_ledger_eligible
from core.repositories import billing_repo, container_repo
from core.services import connection_service
from core.services.provision_gate import is_subscription_active

logger = logging.getLogger(__name__)

RuntimePhase = Literal["provisioning", "starting", "ready"]

Lifecycle = Literal[
    "needs_signup",
    "provider_selection_required",
    "blocked",
    "provisioning",
    "starting",
    "parked",
    "error",
    "ready",
]

# Sentinel distinguishing "caller didn't pass a container override" from
# "caller explicitly passed None (no container row)" in compute_status's
# optional `container` kwarg -- see that function's docstring.
_NOT_GIVEN: Any = object()


def resolve_runtime_phase(container: Optional[dict], gateway_connected: bool) -> RuntimePhase:
    """Pure ``(container status, gateway liveness) -> runtime phase`` map.

    Mirrors the pre-COMPOSER ``_resolve_cold_start_phase`` in
    ``routers/container.py`` field-for-field, except the liveness input is
    now a plain bool sourced from the connection-service DDB table instead
    of the in-process gateway pool (readiness store R4).

    INV-6: this function -- and ONLY this function's output
    (``runtime_phase``) -- decides chat availability. It must never take
    onboarding/funnel/teams_state as an input, and no caller may derive
    ``chat_available`` from anything else.

    Returns:
      - "provisioning"  container row absent, or ECS task not RUNNING yet
                         (PENDING / STOPPED / error).
      - "starting"      ECS task RUNNING but the gateway hasn't completed
                         the OpenClaw handshake + health verification yet.
      - "ready"         gateway is connected -- OpenClaw is responsive to
                         RPCs, chat is usable.
    """
    status = (container or {}).get("status")
    if status != "running":
        return "provisioning"
    if gateway_connected:
        return "ready"
    return "starting"


def _paperclip_repo():
    """Local import so ``patch("core.repositories.paperclip_repo.PaperclipRepo", ...)``
    (the pattern every existing test double in this codebase already uses)
    keeps working -- mirrors the pre-COMPOSER ``_resolve_teams_status``'s
    own local-import idiom in ``routers/container.py``.
    """
    from core.repositories.paperclip_repo import PaperclipRepo

    return PaperclipRepo(table_name="paperclip-companies")


async def resolve_onboarding_state(owner_id: str) -> dict:
    """Best-effort wrapper over ``paperclip_repo.get_onboarding_state``.

    A read failure here must never break ``/container/status`` -- mirrors
    the pre-COMPOSER ``_resolve_teams_status``'s degrade-to-None posture.
    Returns the same all-defaults shape ``get_onboarding_state`` returns
    for a genuinely absent row.

    PUBLIC (not underscore-prefixed): callers that need the raw onboarding
    dict for something OTHER than composing it into the StatusContract --
    e.g. ``routers/container.py`` threading it into
    ``core.services.bootstrap_dispatcher.maybe_dispatch_bootstrap`` so the
    dispatcher doesn't re-query the paperclip-companies row a second time on
    every ~3s ready poll -- call this directly, then pass the SAME dict into
    ``compute_status(..., onboarding=...)`` below so IT doesn't re-fetch
    either. This does NOT weaken INV-1: the composer is still the only
    place readiness+onboarding facts are COMBINED into a verdict: this
    function returns one store's raw facts, untouched, and
    ``routers/container.py`` is deliberately not composing anything itself
    with the result the way this same file's INV-1 architecture test
    forbids -- it only re-threads it to the two functions that already
    accept a pre-fetched onboarding dict/row (compute_status,
    maybe_dispatch_bootstrap), and only reads via this readiness_state
    accessor, never ``paperclip_repo`` directly.
    """
    try:
        return await _paperclip_repo().get_onboarding_state(owner_id)
    except Exception:  # noqa: BLE001 — a degraded read must never break /status
        logger.info("readiness_state: paperclip-companies read failed for owner %s", owner_id, exc_info=True)
        return {
            "onboarding_generation": None,
            "funnel_state": "not_started",
            "pending_brief": False,
            "funnel_submitted_at": None,
            "funnel_completed_at": None,
            "onboarding_goal_id": None,
            "onboarding_project_id": None,
            "onboarding_brief_text": None,
            "onboarding_submitted_by": None,
            "bootstrap_state": "not_started",
            "bootstrap_dispatch_id": None,
            "bootstrap_dispatched_at": None,
            "bootstrap_send_attempted_at": None,
            "bootstrap_sent_at": None,
            "bootstrap_delivered_at": None,
            "bootstrap_attempts": 0,
            "bootstrap_last_error": None,
            "teams_state": None,
            "company_id": None,
            "paperclip_user_id": None,
            "user_id": None,
        }


async def compute_status(owner_id: str, *, container: Any = _NOT_GIVEN, onboarding: Any = _NOT_GIVEN) -> dict[str, Any]:
    """Compose the four readiness inputs into one ``StatusContract`` dict.

    Args:
        owner_id: the container/company owner (org_id for org members,
            else the user_id).
        container: an already-fetched (and possibly locally mutated, e.g.
            by ``maybe_restore_trial_bedrock`` or an auto-retry dispatch's
            optimistic ``status`` flip) containers row, OR an explicit
            ``None``. Pass this when the caller already read/mutated the
            row this request -- it avoids a redundant DDB read AND lets
            the contract reflect same-request local mutations a fresh read
            wouldn't see yet. Omit it (the default) to have this function
            read the row itself.
        onboarding: an already-fetched ``resolve_onboarding_state(owner_id)``
            dict. Pass this when the caller already read it (e.g.
            ``routers/container.py`` threading it into
            ``maybe_dispatch_bootstrap`` too) to skip a redundant
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
    try:
        gateway_connected = await connection_service.is_gateway_connected(owner_id)
    except Exception:  # noqa: BLE001 — liveness read must never break /status
        logger.info("readiness_state: gateway liveness read failed for owner %s", owner_id, exc_info=True)
        gateway_connected = False

    is_org = owner_id.startswith("org_")
    provider_choice = (account or {}).get("provider_choice")
    subscription_status = (account or {}).get("subscription_status")
    sub_active = is_subscription_active(account)
    # Derived off the SAME already-fetched `account` — no extra read. Lets
    # routers/container.py's running-container poll re-dispatch
    # ensure_user_ready for a ledger-eligible owner whose Metronome
    # provisioning never landed (e.g. a transient failure at signup), without
    # this composer growing a second INV-1-violating cross-store read.
    metronome_ledger_missing = is_ledger_eligible(account) and not (account or {}).get("metronome_customer_id")

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
            # row is the Teams per-seat paywall, folded into "blocked"
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
                runtime_phase = resolve_runtime_phase(container, gateway_connected)
                lifecycle = runtime_phase  # "provisioning" | "starting" | "ready"

    chat_available = runtime_phase == "ready"  # INV-6: SOLELY runtime_phase.

    funnel_state = onboarding.get("funnel_state") or "not_started"
    # A brief the user has submitted but that the server hasn't been able to
    # fulfil yet (Paperclip still provisioning -- the common case, since the
    # funnel deliberately renders DURING provisioning) is "submitted" as far as
    # the CLIENT is concerned: they answered, so they must not be shown the
    # funnel again. It is emphatically NOT submitted as far as the row is
    # concerned -- no claim, no goal, no project -- which is why this mapping
    # lives here, in the composer, and not in the raw onboarding dict that
    # `bootstrap_dispatcher` reads to decide whether to wake Friday (see
    # `PaperclipRepo._has_pending_brief`). INV-1: combining facts into a
    # verdict is this function's job.
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
        "service_name": (container or {}).get("service_name"),
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
        "bootstrap": {
            "state": onboarding.get("bootstrap_state") or "not_started",
            "dispatch_id": onboarding.get("bootstrap_dispatch_id"),
            "dispatched_at": onboarding.get("bootstrap_dispatched_at"),
            "delivered_at": onboarding.get("bootstrap_delivered_at"),
            "attempts": onboarding.get("bootstrap_attempts") or 0,
            "last_error": onboarding.get("bootstrap_last_error"),
        },
        "provider": {
            "choice": provider_choice,
            "byo_provider": (account or {}).get("byo_provider"),
            "is_org": is_org,
            "subscription_status": subscription_status,
        },
        "teams_state": onboarding.get("teams_state"),
        # Internal-only -- NOT part of the wire StatusContract (see
        # routers/container.py's `_status_response`, which whitelists fields
        # and does not forward this one). Consumed solely by the
        # running-container poll's ensure_user_ready re-dispatch check.
        "metronome_ledger_missing": metronome_ledger_missing,
    }
