"""Provider-aware spend summary for the user-facing usage surface.

Three modes, one per provider path (see the 2026-07-12 design spec):

- ``bedrock``      — month-to-date BILLED spend from the Metronome draft
                     usage invoice (the number Metronome finalizes), with a
                     per-model split from the ``tier`` pricing group.
- ``oauth_quota``  — ChatGPT plan-quota windows proxied from the container's
                     ``models.authStatus`` gateway RPC.
- ``tokens_only``  — BYOK / trial / any degraded case. The panel renders the
                     token grid it already has; this module adds nothing.

Display-only. The provision/chat gates never read this module; a failure
here must never block anything — every branch degrades, never raises.
"""

import logging
import time
from dataclasses import dataclass

import httpx

from core.config import settings
from core.repositories import billing_repo

logger = logging.getLogger(__name__)

# Cache TTL for the Metronome draft-invoice read. The panel revalidates on
# focus; without this every remount would hit Metronome.
_CACHE_TTL_SECONDS = 60.0
_spend_cache: dict[str, tuple[float, "BilledSpend"]] = {}

# tier (Metronome pricing-group value) -> display name. Tiers are the model
# families the meter Lambda prices by; unknown tiers fall back to the raw key.
_TIER_DISPLAY_NAMES = {
    "opus": "Claude Opus",
    "sonnet": "Claude Sonnet",
    "haiku": "Claude Haiku",
}


@dataclass
class BilledSpend:
    total_cents: int
    by_model: list[dict]
    period_start: str | None


async def _list_draft_invoices(customer_id: str) -> list[dict]:
    """Scoped Metronome client construction (patchable in tests).

    Mirrors the sibling ``async with httpx.AsyncClient(base_url=..., ...)``
    idiom used by ``metronome_gate.py`` / ``metronome_provisioning.py`` /
    ``metronome_funding.py``. A bare ``httpx.AsyncClient()`` (no
    ``base_url``) makes every ``MetronomeClient`` request relative-path,
    which raises ``httpx.UnsupportedProtocol`` on every real call.
    """
    from core.billing.metronome_client import for_settings

    async with httpx.AsyncClient(base_url=settings.METRONOME_BASE_URL, timeout=5.0) as http:
        return await for_settings(http).list_customer_invoices(customer_id=customer_id)


async def get_billed_month_spend(owner_id: str) -> BilledSpend | None:
    """Month-to-date billed spend from the Metronome draft invoice.

    None when the Metronome lifecycle is off, the owner has no Metronome
    customer, or Metronome errors — callers hide the dollar surface then.
    Usage line items only: an item counts iff its pricing_group_values
    carry a ``tier``; commit/credit draw-downs (no tier, negative totals)
    are excluded — remaining credits are the balance surface's job.

    Never raises: a DDB throttle on the billing-account read degrades to
    None exactly like a Metronome error, per this module's "display-only,
    never blocks" contract.
    """
    if not settings.METRONOME_LIFECYCLE_ENABLED:
        return None

    cached = _spend_cache.get(owner_id)
    if cached and (time.monotonic() - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]

    try:
        account = await billing_repo.get_by_owner_id(owner_id) or {}
        customer_id = account.get("metronome_customer_id")
        if not customer_id:
            return None

        invoices = await _list_draft_invoices(customer_id)
    except Exception:
        logger.warning("spend: Metronome draft-invoice read failed for %s", owner_id, exc_info=True)
        return None

    by_tier: dict[str, int] = {}
    period_start: str | None = None
    for invoice in invoices:
        period_start = period_start or invoice.get("start_timestamp")
        for item in invoice.get("line_items") or []:
            tier = (item.get("pricing_group_values") or {}).get("tier")
            if not tier:
                continue
            by_tier[tier] = by_tier.get(tier, 0) + round(float(item.get("total") or 0))

    by_model = [
        {"model": _TIER_DISPLAY_NAMES.get(tier, tier), "tier": tier, "cents": cents}
        for tier, cents in sorted(by_tier.items(), key=lambda kv: -kv[1])
    ]
    billed = BilledSpend(
        total_cents=sum(by_tier.values()),
        by_model=by_model,
        period_start=period_start,
    )
    _spend_cache[owner_id] = (time.monotonic(), billed)
    return billed
