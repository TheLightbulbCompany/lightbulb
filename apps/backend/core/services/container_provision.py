"""Container provisioning helper.

Lives in core/services/ so both routers/container.py (auto-retry path)
and core/services/user_provision.py (ensure_user_ready) can dispatch
container provisioning the same way.
"""

from __future__ import annotations

import logging

from core.services.hermes_profiles import _BEDROCK_TRIAL_CHOICE as BEDROCK_TRIAL_CHOICE
from core.repositories import billing_repo

logger = logging.getLogger(__name__)


def provider_choice_from_row(row: dict | None) -> tuple[str, str | None]:
    """Map a raw billing row to (provider_choice, byo_provider). Pure — no I/O.

    The single row-level mapping (resolve_provider_choice and
    owner_facts.build_owner_facts both call it — there is no second copy to
    keep in sync). Falls back to bedrock_claude when no row or no choice is
    persisted, matching the legacy default and keeping recovery working for
    owners onboarded before per-owner provider_choice storage.
    """
    from core.config import settings

    if (row or {}).get("plan") == "entry":
        return "opencode_zen", None
    if (row or {}).get("subscription_status") == "trial_local":
        if settings.MUSE_ENABLED:
            return "opencode_zen", None
        # No-card local trial. `trial_local` is a subscription_status, never a
        # stored provider_choice, so the provider must never be read off the
        # row — a trial owner's row carries none, and this function never
        # writes one back.
        #
        # The free tier runs on Bedrock through the `bedrock_trial` CONFIG MODE:
        # a single, IAM-pinned Sonnet inference profile (the provisioner mints
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
    # A stale replica could project the old provider and then clear the new refresh marker.
    row = await billing_repo.get_by_owner_id(owner_id, consistent_read=True)
    return provider_choice_from_row(row)


def emit_provision_metric(status: str) -> None:
    """Best-effort ``container.provision`` metric — a metrics outage must never
    turn the swallow-all task into a raise. Same single dimension key (status)
    every other provision emission uses, so every value lands on the one
    CloudWatch stream the provision alarms watch."""
    try:
        from core.observability.metrics import put_metric

        put_metric("container.provision", dimensions={"status": status})
    except Exception:  # noqa: BLE001 — telemetry must never break the task
        logger.debug("container.provision metric emission failed", exc_info=True)
