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
import re
import time
from dataclasses import dataclass

import httpx

from core.config import settings
from core.observability.metrics import put_metric
from core.repositories import billing_repo
from core.services import container_rpc
from core.services.messaging import ApiProblem

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

# provider ids that identify the ChatGPT-OAuth entry in models.authStatus.
# The deployed 7.1 fork reports "openai", but containers keep their
# provision-time image until reprovision, and older images (plus the
# frontend's own llm-auth-status mapping) still emit the retired
# "openai-codex" id. Accept either.
_OAUTH_PROVIDER_IDS = frozenset({"openai", "openai-codex"})

# Plan ids (lowercased) -> display name for the quota card's plan badge.
# Fallback for anything not in this map is a title-cased version of the
# raw value (e.g. "team_plus" -> "Team Plus").
_PLAN_DISPLAY_NAMES = {
    "free": "Free",
    "plus": "Plus",
    "pro": "Pro",
    "prolite": "Pro Lite",
    "business": "Business",
    "team": "Team",
    "enterprise": "Enterprise",
}

_HOURS_LABEL_RE = re.compile(r"^(\d+)h$")

# Literal window labels seen from OpenClaw's models.authStatus quota probe
# that don't fit the "Nh" shorthand.
_LITERAL_WINDOW_LABELS = {
    "Week": "Weekly",
    "Day": "Daily",
}


def _normalize_window_label(label: str) -> str:
    """Normalize a raw quota-window label into display copy.

    Window labels come from OpenClaw's (unofficial) upstream quota probe and
    vary by container image version -- some emit "168h"/"24h"/"5h" shorthand,
    others emit literal "Week"/"Day". Anything unrecognized passes through
    unchanged rather than risk hiding real data behind a guess.
    """
    if label in _LITERAL_WINDOW_LABELS:
        return _LITERAL_WINDOW_LABELS[label]
    match = _HOURS_LABEL_RE.match(label)
    if match:
        hours = int(match.group(1))
        if hours >= 168:
            return "Weekly"
        if hours >= 24:
            return "Daily"
        return f"{hours}-hour"
    return label


def _display_plan(plan: str | None) -> str | None:
    if not plan:
        return plan
    mapped = _PLAN_DISPLAY_NAMES.get(plan.lower())
    if mapped:
        return mapped
    return plan.replace("_", " ").title()


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


async def _oauth_quota_payload(owner_id: str) -> dict | None:
    """ChatGPT plan-quota windows via the container's models.authStatus RPC.

    None on ANY failure (container down, RPC error, usage block absent —
    the upstream quota endpoint is unofficial and may vanish); the caller
    falls back to tokens_only. OpenClaw caches upstream 60s; no cache here.
    """
    try:
        result = await container_rpc.call(owner_id, "models.authStatus", {}, timeout_s=5.0, wake=False)
        for provider in (result or {}).get("providers") or []:
            if provider.get("provider") not in _OAUTH_PROVIDER_IDS:
                continue
            usage = provider.get("usage") or {}
            windows = [
                {
                    "label": _normalize_window_label(w.get("label", "")),
                    "used_percent": w.get("usedPercent"),
                    "resets_at_ms": w.get("resetAt"),
                }
                for w in usage.get("windows") or []
                # A window whose usedPercent isn't a real number carries no
                # honest signal -- never fabricate a 0% bar for it. bool is
                # a subclass of int, so it's excluded explicitly.
                if isinstance(w.get("usedPercent"), (int, float)) and not isinstance(w.get("usedPercent"), bool)
            ]
            if not windows:
                # Keep scanning: a later matching entry (e.g. a second
                # provider id alias) may carry the actual usage windows.
                continue
            return {
                "mode": "oauth_quota",
                "plan": _display_plan(usage.get("plan")),
                "windows": windows,
            }
        return None
    except ApiProblem:
        return None  # parked/unreachable container — silent, not an error
    except Exception:
        put_metric("billing.spend.authstatus_error")
        logger.warning("spend: models.authStatus quota read failed for %s", owner_id, exc_info=True)
        return None


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
    if provider_choice == "chatgpt_oauth":
        return await _oauth_quota_payload(owner_id) or {"mode": "tokens_only"}
    return {"mode": "tokens_only"}
