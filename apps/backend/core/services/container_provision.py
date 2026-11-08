"""Container provisioning helper.

Lives in core/services/ so both routers/container.py (auto-retry path)
and core/services/user_provision.py (ensure_user_ready) can dispatch
container provisioning the same way.
"""

from __future__ import annotations

import logging

from core.repositories import billing_repo

logger = logging.getLogger(__name__)


def provider_choice_from_row(row: dict | None) -> tuple[str, str | None]:
    """Map a raw billing row to (provider_choice, byo_provider). Pure — no I/O.

    The single row-level mapping (resolve_provider_choice and
    owner_facts.build_owner_facts both call it — there is no second copy to
    keep in sync). Falls back to the managed gate when no row or no choice is
    persisted — the retired ``bedrock_claude`` default is never produced.
    """
    from core.billing.voice_billing import has_voice_plan

    if (row or {}).get("subscription_status") == "trial_local" and not has_voice_plan(row):
        # Cardless Free rows intentionally store no provider_choice. Resolve
        # them through the managed gate so provisioning/key/profile code has
        # one path; the owner's OpenRouter per-key daily limit is the valve.
        return "openrouter_managed", None
    if (row or {}).get("plan") in {"base", "standard", "ultra_pro"} and not (row or {}).get("provider_choice"):
        # Base, and (interim) a paid Workspace/Ultra row that never chose a
        # provider, run on the cheap capped model through the managed gate.
        # A stored choice is never re-mapped.
        return "openrouter_managed", None
    provider_choice = (row or {}).get("provider_choice") or "openrouter_managed"
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
