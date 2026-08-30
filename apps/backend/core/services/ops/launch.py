"""Launch Competition numbers for the admin console — the owner's economics
sheet with live inputs. Computed on read, 60s in-process cache."""

from __future__ import annotations

import asyncio
import time

from core.dynamodb import utc_now_iso
from core.repositories import billing_repo, launch_entries_repo, launch_waitlist_repo
from core.services import launch_phase, launch_service, launch_settings, readiness_state
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


def _row_activated(row: dict, resolvable: set[str], activated: set[str]) -> bool | None:
    uid = row.get("user_id")
    if not uid:
        return False  # nobody claimed this waitlist row
    return uid in activated if uid in resolvable else None


async def _compute(s: launch_phase.Schedule) -> dict:
    waitlist, entries, funded, signups, settings_row = await asyncio.gather(
        launch_waitlist_repo.list_all(),
        launch_entries_repo.list_all(),
        launch_waitlist_repo.count_pot_counted(),
        # Same source as GET /launch's `signups` — the atomic position counter,
        # not len(list_all()), which drifts if a row is ever hard-deleted.
        launch_waitlist_repo.count(),
        # Rides along so the admin page can branch on `data.enabled`
        # uniformly whether or not the economics sheet itself is computable
        # (see `compute()`'s disabled branch below, which carries the same
        # two keys with no schedule to compute against).
        launch_settings.effective(),
    )
    constants = launch_phase.constants()
    per_entrant = constants["pot_per_entrant_cents"]
    referral_jump = constants["referral_jump"]
    competing = len(entries)
    on_board = sum(1 for e in entries if e.get("has_key"))

    # Activation is only ever ASKED about the rows that can use the answer: the
    # comped top slots and everyone who actually entered. An uncapped waitlist
    # otherwise means one onboarding resolve per signup on every cache miss —
    # thousands of reads for a sheet that shows two numbers off them.
    # EFFECTIVE position, not raw — a referral can pull a row into the comped
    # window from well outside its raw position (launch_service.effective_position).
    resolvable = {
        w["user_id"]
        for w in waitlist
        if w.get("user_id") and launch_service.effective_position(w, referral_jump) <= s.comp_slots
    }
    resolvable |= {e["owner_id"] for e in entries}
    sem = asyncio.Semaphore(8)

    async def _onb(owner_id: str) -> dict:
        async with sem:
            return await readiness_state.resolve_onboarding_state(owner_id)

    ordered = sorted(resolvable)
    onboarding = dict(zip(ordered, await asyncio.gather(*[_onb(uid) for uid in ordered])))
    activated = {uid for uid, o in onboarding.items() if _activated_by_deadline(o, s)}

    top = [w for w in waitlist if launch_service.effective_position(w, referral_jump) <= s.comp_slots]
    top_claimed = [w for w in top if w.get("user_id")]
    top_activated = [w for w in top_claimed if w["user_id"] in activated]

    async def _billing(owner_id: str) -> dict | None:
        async with sem:
            return await billing_repo.get_by_owner_id(owner_id)

    billing_rows = await asyncio.gather(*[_billing(e["owner_id"]) for e in entries])
    entry_rows = []
    comp_eligible = 0
    for e, billing in zip(entries, billing_rows):
        pos = e.get("waitlist_position")
        # ELIGIBILITY, not redemption. Nothing observes whether the promo code was actually
        # used (that would be a $0 first invoice), so the P&L below treats every eligible
        # entrant as comped — the pessimistic read.
        # Org entrants (owner_id "org_…") are never comped: comps are personal-only per the
        # spec, and `activated` is keyed by the individual user_id, so they land in `paid`.
        # Eligibility is judged on the position FROZEN at join time (the
        # effective position then), not the live one: the promo codes went out
        # at the drop off the CSV, so referrals earned after joining cannot
        # change who got a code. The pre-drop `top_slots_*` counts above use
        # the live effective position on purpose — that is the list still being
        # shaped.
        is_eligible = pos is not None and int(pos) <= s.comp_slots and e["owner_id"] in activated
        comp_eligible += int(is_eligible)
        baseline = int(e.get("baseline_cents", 0))
        # pct is measured against the same prorated baseline `delta_cents` was.
        prorated = int(e.get("baseline_prorated_cents") or 0)
        pct_baseline = prorated if prorated > 0 else baseline
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
                "pct": round(delta / pct_baseline, 4) if pct_baseline > 0 else None,
                "refreshed_at": e.get("refreshed_at"),
                "refresh_error": e.get("refresh_error"),
                "comp_eligible": is_eligible,
            }
        )
    entry_rows.sort(key=lambda r: -r["delta_cents"])

    paid = len(entries) - comp_eligible
    referrals_paid = sum(int(w.get("referrals_paid", 0)) for w in waitlist)
    months_banked = sum(int(w.get("free_months_banked", 0)) for w in waitlist)
    paid_profit = paid * (PLAN_PRICE_CENTS - CONTAINER_COST_CENTS - per_entrant)
    comp_cost = comp_eligible * (CONTAINER_COST_CENTS + per_entrant)
    referral_cost = referrals_paid * (CONTAINER_COST_CENTS + per_entrant)
    pnl = paid_profit - comp_cost - referral_cost

    return {
        "generated_at": utc_now_iso(),
        "settings": launch_settings.to_public(settings_row),
        "enabled": settings_row["enabled"],
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
        "signups": signups,
        "competing": competing,
        "on_board": on_board,
        # The paid/comp_eligible/public split behind the "competing" count —
        # kept (not dropped) for the entries-table hint, just renamed off the
        # legacy "entrants" name now that "competing" is the count of record.
        "competing_breakdown": {
            "paid": paid,
            "comp_eligible": comp_eligible,
            "public": sum(1 for e in entries if e.get("public", True)),
        },
        # Two pots (spec): the public number tracks the uncapped waitlist (what
        # every visitor sees before the drop); the funded number is only backed
        # by an actual Stripe invoice landing (see launch_referral.on_invoice_paid
        # -> launch_waitlist_repo.mark_pot_counted, first invoice of any amount).
        "public_pot_cents": launch_phase.pot_cents(signups),
        "funded_pot_cents": launch_phase.pot_cents(funded),
        "pot_counted": funded,
        # The pot formula's own inputs, so the console renders the arithmetic
        # it is showing instead of hardcoding "× $10 + $1,000".
        "pot_front_cents": constants["pot_front_cents"],
        "pot_per_entrant_cents": per_entrant,
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
                    "effective_position": launch_service.effective_position(w, referral_jump),
                    "referral_link": launch_service.referral_link(w["referral_code"]),
                    "created_at": w.get("created_at"),
                    "user_id": w.get("user_id"),
                    # None where activation was never resolved (see `resolvable`
                    # above) — an unclaimed row is definitively not activated,
                    # so it stays False rather than becoming an unknown.
                    "activated": _row_activated(w, resolvable, activated),
                    "referred_by": w.get("referred_by"),
                    "referrals_signed_up": int(w.get("referrals_signed_up", 0)),
                    "referrals_paid": int(w.get("referrals_paid", 0)),
                    "free_months_banked": int(w.get("free_months_banked", 0)),
                }
                for w in waitlist
            ],
            key=lambda r: r["effective_position"],
        ),
    }


async def compute() -> tuple[dict, str, list[dict]]:
    s = launch_phase.schedule()
    if s is None:
        # Feature off (or misconfigured for this env) — the economics sheet
        # has nothing to compute, but the admin console still needs the
        # settings row + enabled flag so it can render the settings card and
        # let an admin turn the feature on, instead of the page going blank.
        settings_row = launch_settings.to_public(await launch_settings.effective())
        return (
            {"settings": settings_row, "enabled": False},
            env.UNAVAILABLE,
            [{"domain": "launch", "reason": "launch settings are disabled or missing a date for this env"}],
        )
    hit = _cache.get("launch")
    if hit and time.monotonic() - hit[0] < _CACHE_TTL_S:
        return hit[1]
    data, state, errors = await env.read_source("launch", lambda: _compute(s), timeout_s=20.0)
    if state != env.OK:
        # A failed or timed-out sheet read still owes the console the settings
        # row: the admin page renders the settings card off `data`, so
        # `read_source`'s `data=None` would take away the one control that can
        # turn the competition back off. Same two keys as the disabled branch.
        row = await launch_settings.effective()
        return {"settings": launch_settings.to_public(row), "enabled": row["enabled"]}, state, errors
    result = (data, state, errors)
    _cache["launch"] = (time.monotonic(), result)
    return result


def clear_cache() -> None:
    _cache.clear()
