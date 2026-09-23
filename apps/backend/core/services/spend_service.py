"""Provider-aware spend summary for the user-facing usage surface.

Two modes, one per provider path (see the 2026-07-12 design spec):

- ``bedrock``      — month-to-date BILLED spend from the Metronome draft
                     usage invoice (the number Metronome finalizes), with a
                     per-model split from the ``tier`` pricing group.
- ``tokens_only``  — everything else (ChatGPT OAuth / BYOK / trial / any
                     degraded case). The panel renders the token grid it
                     already has; this module adds nothing. ChatGPT OAuth used
                     to proxy plan-quota windows from the container's
                     ``models.authStatus`` gateway RPC, but the Hermes bridge
                     never returns a ``usage`` block, so that read was always
                     None while still costing an InvokeAgentRuntime wake on
                     every spend read (deleted 2026-09-23, ponytail audit #14).

Display-only. The provision/chat gates never read this module; a failure
here must never block anything — every branch degrades, never raises.
"""

import logging
import time
from dataclasses import dataclass

import httpx

from core.config import settings
from core.observability.metrics import put_metric
from core.repositories import billing_repo

logger = logging.getLogger(__name__)

# Cache TTL for the Metronome draft-invoice read. The panel revalidates on
# focus; without this every remount would hit Metronome.
_CACHE_TTL_SECONDS = 60.0
_spend_cache: dict[str, tuple[float, "BilledSpend"]] = {}

# tier (Metronome pricing-group value) -> display name. Tiers are the model
# families the meter Lambda prices by; unknown tiers fall back to the raw key.
# PUBLIC (M6 task 10-C): the one place the platform names a Bedrock tier. The AgentCore
# ``models.list`` intercept (``runtime_rpc``) reads it for the model picker's labels rather
# than starting a second copy that can drift from the usage panel's.
TIER_DISPLAY_NAMES = {
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

    Metronome's own docs describe the list response as an invoice SUMMARY
    and point callers at the single-invoice Get Invoice endpoint for
    detailed line items — we could not disprove that the list response
    omits ``line_items`` (dev has had no usage this billing period to
    check against). So every invoice id from the list is re-fetched via
    ``get_customer_invoice``, and the DETAIL response is what gets
    returned (and summed) — never the list's own ``line_items``. Both the
    list call and every per-invoice detail call share the one AsyncClient
    for this refresh.
    """
    from core.billing.metronome_client import for_settings

    async with httpx.AsyncClient(base_url=settings.METRONOME_BASE_URL, timeout=5.0) as http:
        client = for_settings(http)
        summaries = await client.list_customer_invoices(customer_id=customer_id)
        invoices = []
        for summary in summaries:
            invoice_id = summary.get("id")
            if not invoice_id:
                invoices.append(summary)
                continue
            detail = await client.get_customer_invoice(customer_id=customer_id, invoice_id=invoice_id)
            invoices.append(detail or summary)
        return invoices


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
        put_metric("billing.spend.metronome_error")
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
        {"model": TIER_DISPLAY_NAMES.get(tier, tier), "tier": tier, "cents": cents}
        for tier, cents in sorted(by_tier.items(), key=lambda kv: -kv[1])
    ]
    billed = BilledSpend(
        total_cents=sum(by_tier.values()),
        by_model=by_model,
        period_start=period_start,
    )
    _spend_cache[owner_id] = (time.monotonic(), billed)
    return billed


async def get_spend_summary(owner_id: str, provider_choice: str | None) -> dict:
    """The /billing/spend payload. Never raises — degraded == tokens_only."""
    if provider_choice == "bedrock_claude":
        billed = await get_billed_month_spend(owner_id)
        if billed is None:
            return {"mode": "tokens_only"}
        return {
            "mode": "bedrock",
            "billed_month_to_date_cents": billed.total_cents,
            "by_model": billed.by_model,
            "period_start": billed.period_start,
        }
    # chatgpt_oauth, byo_key, trial, None, and anything else all render the
    # panel's existing token grid — see the module docstring.
    return {"mode": "tokens_only"}
