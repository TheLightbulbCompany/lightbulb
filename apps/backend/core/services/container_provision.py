"""Container provisioning helper.

Lives in core/services/ so both routers/container.py (auto-retry path)
and core/services/user_provision.py (ensure_user_ready) can dispatch
container provisioning the same way.
"""

from __future__ import annotations

import logging

from core.services.hermes_profiles import _BEDROCK_TRIAL_CHOICE as BEDROCK_TRIAL_CHOICE
from core.config import settings
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
    free = (row or {}).get("subscription_status") == "trial_local" or (
        (row or {}).get("plan") == "base" and not (row or {}).get("provider_choice")
    )
    if free:
        # Cardless Free and Base rows store no provider_choice. Where managed
        # OpenRouter is enabled they ride its zero-price gate policy; where it
        # is not (prod until rollout) they keep the single-AIP Sonnet trial
        # mode -- openrouter_managed there has no model and fails every write.
        return ("openrouter_managed" if settings.OPENROUTER_MANAGED_ENABLED else BEDROCK_TRIAL_CHOICE), None
    provider_choice = (row or {}).get("provider_choice") or "bedrock_claude"
    byo_provider = (row or {}).get("byo_provider") if provider_choice == "byo_key" else None
    return provider_choice, byo_provider


async def resolve_provider_choice(owner_id: str) -> tuple[str, str | None]:
    """Look up the owner's provider_choice (+ byo_provider) from billing_repo.

    The single resolver for the whole provision surface — routers/container.py,
    routers/container_recover.py, catalog_service and the reapply script all
    import THIS function. Row-level semantics live in provider_choice_from_row.
    """
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
