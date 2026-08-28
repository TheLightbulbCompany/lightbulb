"""Launch Competition numbers for the admin console — the owner's economics
sheet with live inputs. Computed on read, 60s in-process cache."""

from __future__ import annotations

import asyncio
import time

from core.config import settings
from core.dynamodb import utc_now_iso
from core.repositories import billing_repo, launch_entries_repo, launch_waitlist_repo
from core.services import launch_phase, launch_service, readiness_state
from core.services.ops import envelope as env

CONTAINER_COST_CENTS = 1800
PLAN_PRICE_CENTS = 5000
AWS_CREDITS_CENTS = 500_000
_CACHE_TTL_S = 60
_cache: dict[str, tuple[float, tuple]] = {}


def _activated_by_deadline(onboarding: dict, s: launch_phase.Schedule) -> bool:
    raw = onboarding.get("bootstrap_delivered_at")
    if onboarding.get("bootstrap_state") != "delivered" or not raw:
        return False
    return launch_phase.parse_iso(raw) <= s.competition_start_at


async def _compute(s: launch_phase.Schedule) -> dict:
    waitlist, entries = await asyncio.gather(launch_waitlist_repo.list_all(), launch_entries_repo.list_all())
    per_entrant = settings.LAUNCH_POT_PER_ENTRANT_CENTS

    claimed = [w for w in waitlist if w.get("user_id")]
    sem = asyncio.Semaphore(8)

    async def _onb(owner_id: str) -> dict:
        async with sem:
            return await readiness_state.resolve_onboarding_state(owner_id)

    onboarding = dict(
        zip([w["user_id"] for w in claimed], await asyncio.gather(*[_onb(w["user_id"]) for w in claimed]))
    )
    activated = {uid for uid, o in onboarding.items() if _activated_by_deadline(o, s)}

    top = [w for w in waitlist if int(w["position"]) <= s.comp_slots]
    top_claimed = [w for w in top if w.get("user_id")]
    top_activated = [w for w in top_claimed if w["user_id"] in activated]

    billing_rows = await asyncio.gather(*[billing_repo.get_by_owner_id(e["owner_id"]) for e in entries])
    entry_rows = []
    comped = 0
    for e, billing in zip(entries, billing_rows):
        pos = e.get("waitlist_position")
        # Org entrants (owner_id "org_…") are never comped: comps are personal-only per the
        # spec, and `activated` is keyed by the individual user_id, so they land in `paid`.
        is_comped = pos is not None and int(pos) <= s.comp_slots and e["owner_id"] in activated
        comped += int(is_comped)
        baseline = int(e.get("baseline_cents", 0))
        delta = int(e.get("delta_cents", 0))
        entry_rows.append(
            {
                "owner_id": e["owner_id"],
                "has_subscription": bool((billing or {}).get("stripe_subscription_id")),
                "display_name": e.get("display_name"),
                "public": bool(e.get("public", True)),
                "waitlist_position": pos,
                "currency": e.get("currency"),
                "baseline_cents": baseline,
                "window_cents": int(e.get("window_cents", 0)),
                "delta_cents": delta,
                "pct": round(delta / baseline, 4) if baseline > 0 else None,
                "refreshed_at": e.get("refreshed_at"),
                "refresh_error": e.get("refresh_error"),
                "comped": is_comped,
            }
        )
    entry_rows.sort(key=lambda r: -r["delta_cents"])

    paid = len(entries) - comped
    referrals_paid = sum(int(w.get("referrals_paid", 0)) for w in waitlist)
    months_banked = sum(int(w.get("free_months_banked", 0)) for w in waitlist)
    paid_profit = paid * (PLAN_PRICE_CENTS - CONTAINER_COST_CENTS - per_entrant)
    comp_cost = comped * (CONTAINER_COST_CENTS + per_entrant)
    referral_cost = referrals_paid * (CONTAINER_COST_CENTS + per_entrant)
    pnl = paid_profit - comp_cost - referral_cost

    return {
        "generated_at": utc_now_iso(),
        "phase": launch_phase.phase(s),
        "schedule": {
            "drop_at": s.drop_at.isoformat(),
            "competition_start_at": s.competition_start_at.isoformat(),
            "competition_end_at": s.competition_end_at.isoformat(),
            "comp_slots": s.comp_slots,
        },
        "waitlist": {
            "total": len(waitlist),
            "top_slots_activated": len(top_activated),
            "top_slots_claimed": len(top_claimed),
        },
        "entrants": {
            "total": len(entries),
            "paid": paid,
            "comped": comped,
            "public": sum(1 for e in entries if e.get("public", True)),
        },
        "pot_cents": launch_phase.pot_cents(len(entries)),
        "referrals": {
            "signed_up": sum(int(w.get("referrals_signed_up", 0)) for w in waitlist),
            "paid": referrals_paid,
            "months_banked": months_banked,
        },
        "economics": {
            "paid_profit_cents": paid_profit,
            "comp_cost_cents": comp_cost,
            "referral_cost_cents": referral_cost,
            "pnl_cents": pnl,
            "aws_credits_cents": AWS_CREDITS_CENTS,
            "net_cents": pnl + AWS_CREDITS_CENTS,
        },
        "entries": entry_rows,
        "waitlist_rows": sorted(
            [
                {
                    "email": w["email"],
                    "position": int(w["position"]),
                    "referral_link": launch_service.referral_link(w["referral_code"]),
                    "created_at": w.get("created_at"),
                    "user_id": w.get("user_id"),
                    "activated": w.get("user_id") in activated,
                    "referred_by": w.get("referred_by"),
                    "referrals_signed_up": int(w.get("referrals_signed_up", 0)),
                    "referrals_paid": int(w.get("referrals_paid", 0)),
                    "free_months_banked": int(w.get("free_months_banked", 0)),
                }
                for w in waitlist
            ],
            key=lambda r: r["position"],
        ),
    }


async def compute() -> tuple[dict | None, str, list[dict]]:
    s = launch_phase.schedule()
    if s is None:
        return None, env.UNAVAILABLE, [{"domain": "launch", "reason": "LAUNCH_DROP_AT is not set"}]
    hit = _cache.get("launch")
    if hit and time.monotonic() - hit[0] < _CACHE_TTL_S:
        return hit[1]
    data, state, errors = await env.read_source("launch", lambda: _compute(s), timeout_s=20.0)
    result = (data, state, errors)
    if state == env.OK:
        _cache["launch"] = (time.monotonic(), result)
    return result
