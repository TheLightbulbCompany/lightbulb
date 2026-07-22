"""Container provisioning helper.

Lives in core/services/ so both routers/container.py (auto-retry path)
and core/services/user_provision.py (ensure_user_ready) can dispatch
container provisioning the same way.
"""

from __future__ import annotations

import logging

from core.containers import get_ecs_manager
from core.containers.config import BEDROCK_TRIAL_CHOICE
from core.observability.logging import bind_log_context
from core.repositories import billing_repo, container_repo
from core.repositories.container_repo import ProvisionSuperseded

logger = logging.getLogger(__name__)


def provider_choice_from_row(row: dict | None) -> tuple[str, str | None]:
    """Map a raw billing row to (provider_choice, byo_provider). Pure — no I/O.

    The single row-level mapping (resolve_provider_choice and
    owner_facts.build_owner_facts both call it — there is no second copy to
    keep in sync). Falls back to bedrock_claude when no row or no choice is
    persisted, matching the legacy default and keeping recovery working for
    owners onboarded before per-owner provider_choice storage.
    """
    if (row or {}).get("subscription_status") == "trial_local":
        # No-card local trial. `trial_local` is a subscription_status, never a
        # stored provider_choice, so the provider must never be read off the
        # row — a trial owner's row carries none, and this function never
        # writes one back.
        #
        # The free tier runs on Bedrock through the `bedrock_trial` CONFIG MODE:
        # a single, IAM-pinned Sonnet inference profile (ecs_manager mints
        # tiers=["sonnet"], and build_owner_bedrock_policy scopes the owner's
        # task role to exactly that AIP). Returning "bedrock_claude" here would
        # hand a card-less owner the full paid three-tier ladder — Opus included.
        return BEDROCK_TRIAL_CHOICE, None
    provider_choice = (row or {}).get("provider_choice") or "bedrock_claude"
    byo_provider = (row or {}).get("byo_provider") if provider_choice == "byo_key" else None
    return provider_choice, byo_provider


async def resolve_provider_choice(owner_id: str) -> tuple[str, str | None]:
    """Look up the owner's provider_choice (+ byo_provider) from billing_repo.

    The single resolver for the whole provision surface — routers/container.py,
    routers/container_recover.py, catalog_service and the reapply script all
    import THIS function. Row-level semantics live in provider_choice_from_row.
    """
    row = await billing_repo.get_by_owner_id(owner_id)
    return provider_choice_from_row(row)


async def background_provision(owner_id: str, clerk_user_id: str) -> None:
    """Run container provisioning in the background.

    ``owner_id`` is the container scope (org_id in org context, user_id
    in personal). ``clerk_user_id`` is kept for caller compatibility;
    the function reads provider_choice from billing_repo by owner_id
    and does not consult clerk_user_id directly.

    Catches and logs all exceptions — never raises. This function is an
    asyncio.create_task target.

    SINGLE-FLIGHT: the FIRST act — before any side effect, before even the
    provider-choice read — is an atomic lease claim on the container row
    (``container_repo.try_claim_provision``). The frontend polls
    /container/status every ~3s and each no-row/error/stopped poll dispatches
    this task, so under a page refresh several instances run concurrently;
    exactly one wins the claim and provisions, the rest stand down here. This
    is what prevents two interleaved provisions from minting duplicate Bedrock
    inference profiles and splitting them across the container config and the
    IAM policy (the prod incident). Deliberate reprovision doors (conversion,
    /recover, admin, provider change) do NOT come through here — they call
    provision_user_container directly, which takes over the lease and fences
    this path out at its next checkpoint.
    """
    from core.auth import is_org_owner_id

    # Re-bind correlation ids onto the log contextvars: this runs in a detached
    # asyncio task, so the HTTP-middleware binds did not propagate here. Bind
    # owner_id as user_id so provisioning log lines are queryable by the same
    # field as the rest of the owner's flows. (See bind_log_context docstring.)
    bind_log_context(user_id=owner_id)

    claim_id = await container_repo.try_claim_provision(owner_id)
    if claim_id is None:
        logger.info("Provision already in flight for owner %s — standing down", owner_id)
        _emit_provision_metric("claim_lost")
        return

    try:
        provider_choice, byo_provider = await resolve_provider_choice(owner_id)
        # owner_type drives org-specific config (e.g. gateway.nodes.browser.mode
        # = "manual" for orgs vs "auto" for personal). It's derivable from the
        # owner_id prefix, so a re-provision regenerates the SAME org/personal
        # config rather than silently defaulting to personal.
        owner_type = "org" if is_org_owner_id(owner_id) else "personal"
        await get_ecs_manager().provision_user_container(
            owner_id,
            provider_choice=provider_choice,
            byo_provider=byo_provider,
            owner_type=owner_type,
            claim_id=claim_id,
        )
    except ProvisionSuperseded:
        # A deliberate reprovision took the lease over mid-run. The row belongs
        # to it now — stop quietly, write nothing (an error write here would
        # clobber the superseding run's state).
        logger.info("Provision for owner %s superseded by a deliberate reprovision — standing down", owner_id)
        _emit_provision_metric("superseded")
    except Exception as exc:
        logger.exception("Background provisioning failed for owner %s", owner_id)
        # Classify WHY it failed so the status endpoint can tell a transient
        # failure (silently retried by the poll) from a deterministic one
        # (surfaced to the owner instead of looped forever). Shared classifier
        # lives in container_repo alongside the error transition it feeds.
        last_error, retryable = container_repo.classify_provision_error(exc)
        # Release the lease into "error": immediately re-claimable, and the
        # status poll's existing error auto-retry re-dispatches — a failed
        # provision self-heals on the next ~3s poll instead of wedging the
        # owner behind a lease that only expires by staleness. Token-
        # conditioned, so if the lease was meanwhile taken over this no-ops.
        try:
            await container_repo.release_provision_to_error(
                owner_id, claim_id, last_error=last_error, retryable=retryable
            )
        except Exception:  # noqa: BLE001 — release is best-effort
            logger.exception("Failed to release provision lease to error for owner %s", owner_id)
        # This fire-and-forget task swallows exceptions, so its failures are
        # otherwise invisible to alarms. Emit the SAME metric+dimension set the
        # synchronous ecs_manager path uses on failure — {status: "error"}, no
        # extra dims — so it lands on the exact CloudWatch stream the W1
        # provision-error-rate alarm queries. (EMF publishes one stream per full
        # dimension-key set, so a `phase` dim here would create a separate
        # stream that NO alarm watches — defeating the point. The dispatch-vs-
        # sync distinction lives in the logger.exception above instead.)
        _emit_provision_metric("error")


def _emit_provision_metric(status: str) -> None:
    """Best-effort ``container.provision`` metric — a metrics outage must never
    turn the swallow-all task into a raise. Same single dimension key (status)
    as the synchronous ecs_manager emissions, so every value lands on the one
    CloudWatch stream the provision alarms watch."""
    try:
        from core.observability.metrics import put_metric

        put_metric("container.provision", dimensions={"status": status})
    except Exception:  # noqa: BLE001 — telemetry must never break the task
        logger.debug("container.provision metric emission failed", exc_info=True)
