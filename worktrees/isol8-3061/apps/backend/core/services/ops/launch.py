"""Launch Competition numbers for the admin console — the sign-up sheet plus
the revenue board, with the pot arithmetic (ADMIN-ONLY — the pot appears in
no public payload). Computed on read, 60s in-process cache."""

from __future__ import annotations

import asyncio
import time

from core.dynamodb import utc_now_iso
from core.repositories import billing_repo, launch_entries_repo, launch_teams_repo, launch_waitlist_repo
from core.services import launch_phase, launch_service, launch_settings
from core.services.ops import envelope as env

_CACHE_TTL_S = 60
_cache: dict[str, tuple[float, tuple]] = {}


async def _compute(s: launch_phase.Schedule) -> dict:
    waitlist, entries, teams, signups, settings_row = await asyncio.gather(
        launch_waitlist_repo.list_all(),
        launch_entries_repo.list_all(),
        # Team entries — the founders' manual lane. Only the counted ones
        # reach the pot; the rest are here so an admin can review them.
        launch_teams_repo.list_all(),
        # Same source as GET /launch's `signups` — the atomic position counter,
        # not len(list_all()), which drifts if a row is ever hard-deleted.
        launch_waitlist_repo.count(),
        # Rides along so the admin page can branch on `data.enabled`
        # uniformly whether or not the sheet itself is computable (see
        # `compute()`'s disabled branch below).
        launch_settings.effective(),
    )
    constants = launch_phase.constants()
    referral_jump = constants["referral_jump"]
    sheet_rows_raw = [w for w in waitlist if w.get("joined_at")]
    closed = bool(settings_row.get("closed_at"))

    # Board (revenue) section — the launch-entries attachment.
    sem = asyncio.Semaphore(8)

    async def _billing(owner_id: str) -> dict | None:
        async with sem:
            return await billing_repo.get_by_owner_id(owner_id)

    billing_rows = await asyncio.gather(*[_billing(e["owner_id"]) for e in entries])
    entry_rows = []
    for e, billing in zip(entries, billing_rows):
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
                "waitlist_position": e.get("waitlist_position"),
                "currency": e.get("currency"),
                "baseline_cents": baseline,
                "window_cents": int(e.get("window_cents", 0)),
                "delta_cents": delta,
                "pct": round(delta / pct_baseline, 4) if pct_baseline > 0 else None,
                "refreshed_at": e.get("refreshed_at"),
                "refresh_error": e.get("refresh_error"),
            }
        )
    entry_rows.sort(key=lambda r: -r["delta_cents"])

    sheet_rows = sorted(
        [
            {
                "email": w["email"],
                "position": int(w["position"]),
                "effective_position": launch_service.effective_position(w, referral_jump),
                "display_name": w.get("display_name"),
                "public": bool(w.get("public", True)),
                "referral_link": launch_service.referral_link(w["referral_code"]),
                "created_at": w.get("created_at"),
                "joined_at": w.get("joined_at"),
                "user_id": w.get("user_id"),
                "referred_by": w.get("referred_by"),
                "referrals_signed_up": int(w.get("referrals_signed_up", 0)),
                "referrals_subscribed": int(w.get("referrals_subscribed", 0)),
                "winner": bool(w.get("winner")),
                # Pre-close nobody has been ruled out yet; post-close the
                # stored flag is the truth.
                "compete_eligible": bool(w.get("compete_eligible", True)),
                "coupon_attached_at": w.get("coupon_attached_at"),
                "winner_email_sent_at": w.get("winner_email_sent_at"),
            }
            for w in sheet_rows_raw
        ],
        key=lambda r: (r["effective_position"], r["position"]),
    )

    team_rows = [
        {
            "owner_id": t.get("owner_id"),
            "team_name": t.get("team_name"),
            "seats": int(t.get("seats", 0)),
            "note": t.get("note") or "",
            "submitter_email": t.get("submitter_email") or "",
            "submitter_user_id": t.get("submitter_user_id") or "",
            "submitter_org_slug": t.get("submitter_org_slug") or "",
            "counted": bool(t.get("counted")),
            "submitted_at": t.get("submitted_at"),
            # submitted_at survives a resubmission, so this is the only field
            # that moves when a submitter edits after the founders reviewed.
            "updated_at": t.get("updated_at"),
            # What this entry adds to the pot once counted — shown for every
            # row so an admin sees the cost of flipping the toggle.
            "contribution_cents": launch_service.team_contribution_cents(int(t.get("seats", 0))),
        }
        for t in teams
    ]
    counted_seats = [r["seats"] for r in team_rows if r["counted"]]

    return {
        "generated_at": utc_now_iso(),
        "settings": launch_settings.to_admin(settings_row),
        "enabled": settings_row["enabled"],
        "phase": launch_phase.phase(s),
        "schedule": {
            "sheet_closes_at": s.sheet_closes_at.isoformat(),
            "competition_start_at": s.competition_start_at.isoformat(),
            "competition_end_at": s.competition_end_at.isoformat(),
            "comp_slots": s.comp_slots,
        },
        "signups": signups,
        "sheet": {
            "total": len(waitlist),
            "joined": len(sheet_rows),
            "winners": sum(1 for r in sheet_rows if r["winner"]),
        },
        "competing": len(entries),
        "on_board": sum(1 for e in entries if e.get("has_key")),
        # The pot — ADMIN-ONLY. Pre-close `projected_pot_cents` tracks the
        # joined sheet (the roster's upper bound); the frozen numbers land on
        # the settings row at close and are surfaced verbatim here.
        "pot_front_cents": constants["pot_front_cents"],
        "pot_per_entrant_cents": constants["pot_per_entrant_cents"],
        "pot_per_seat_cents": constants["pot_per_seat_cents"],
        "projected_pot_cents": launch_phase.pot_cents(len(sheet_rows), counted_seats),
        "closed": closed,
        "closed_at": settings_row.get("closed_at"),
        # Async close bookkeeping: started-but-no-result = a background task
        # is (or died) mid-run; the finished run's summary counts.
        "close_running": bool(settings_row.get("close_started_at")) and settings_row.get("close_result") is None,
        "close_result": settings_row.get("close_result"),
        "final_pot_cents": settings_row.get("final_pot_cents"),
        "final_roster_count": settings_row.get("final_roster_count"),
        "referrals": {
            "signed_up": sum(int(w.get("referrals_signed_up", 0)) for w in waitlist),
            "subscribed": sum(int(w.get("referrals_subscribed", 0)) for w in waitlist),
        },
        "teams": {
            "total": len(team_rows),
            "counted": len(counted_seats),
            "contribution_cents": sum(r["contribution_cents"] for r in team_rows if r["counted"]),
        },
        "team_entries": team_rows,
        "entries": entry_rows,
        "sheet_rows": sheet_rows,
    }


async def compute() -> tuple[dict, str, list[dict]]:
    s = launch_phase.schedule()
    if s is None:
        # Feature off (or misconfigured for this env) — the sheet has nothing
        # to compute, but the admin console still needs the settings row +
        # enabled flag so it can render the settings card and let an admin
        # turn the feature on, instead of the page going blank.
        settings_row = launch_settings.to_admin(await launch_settings.effective())
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
        return {"settings": launch_settings.to_admin(row), "enabled": row["enabled"]}, state, errors
    result = (data, state, errors)
    _cache["launch"] = (time.monotonic(), result)
    return result


def clear_cache() -> None:
    _cache.clear()
