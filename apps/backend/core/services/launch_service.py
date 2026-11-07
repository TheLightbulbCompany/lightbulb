"""Launch Competition — sheet membership and the revenue board.

The sheet row (``launch_waitlist_repo``) IS the competition entry:
``join_sheet`` stamps membership on it, referrals move it up
(``effective_position``), and the close action flags ``winner`` /
``compete_eligible`` on it. The separate launch-entries table survives ONLY
as the revenue-board attachment (Stripe key + baseline + refresh machinery,
``launch_revenue``): ``connect_stripe`` mints that row on demand from the
sheet row, and the leaderboard reads it.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from botocore.exceptions import ClientError

from core.auth import AuthContext, resolve_owner_id
from core.config import settings
from core.dynamodb import utc_now_iso
from core.repositories import launch_entries_repo, launch_teams_repo, launch_waitlist_repo, user_repo
from core.services import launch_phase, launch_revenue, readiness_state, workspace_name
from core.services.launch_phase import Schedule, phase
from schemas.launch import TEAM_SEATS_MAX

logger = logging.getLogger(__name__)

# Referral ATTRIBUTION must precede the referee's first payment — used by
# POST /launch/referral's refusal guard (routers/launch.py).
LIVE_FOR_ENTRY = frozenset({"active", "trialing"})


def referral_link(code: str) -> str:
    return f"{settings.FRONTEND_URL.rstrip('/')}/?ref={code}"


MAX_REFERRALS_COUNTED = 20  # ponytail: a flat cap; make it a setting if the owner ever wants to tune it


def effective_position(row: dict, referral_jump: int) -> int:
    """Queue-jump: each COUNTED referral (referee subscribed —
    ``referrals_subscribed``, written by launch_referral) moves the referrer
    up ``referral_jump`` spots, floored at 1. This is the ONE place that math
    happens — every surface that shows or ranks a sheet position (``me()``,
    the close action's winner ordering, the admin sheet) reads through this,
    not raw ``row["position"]``.

    Only the first ``MAX_REFERRALS_COUNTED`` referrals count toward the jump,
    so a jump farm cannot buy the whole top of the sheet."""
    counted = min(int(row.get("referrals_subscribed", 0)), MAX_REFERRALS_COUNTED)
    return max(1, int(row["position"]) - referral_jump * counted)


async def resolve_email(auth: AuthContext) -> str | None:
    """The users-table email FIRST, the live Clerk JWT claim only as fallback.

    Order matters: the sheet is keyed by email, and the referral-counting
    path (``launch_referral._email_for_owner``) can only see the users-table
    value. Preferring the JWT here would key a rename's sheet row to an
    address the webhook path can never look up."""
    row = await user_repo.get(auth.user_id)
    email = (row or {}).get("email")
    if email:
        return email.strip().lower()
    return auth.email.strip().lower() if auth.email else None


def credits_milestone_reached(row: dict | None) -> int:
    subs = int((row or {}).get("referrals_subscribed", 0))
    return 20 if subs >= 20 else 5 if subs >= 5 else 0


async def _suggested_display_name(owner_id: str) -> str | None:
    """The workspace's saved name (``workspace_name.saved_name``), or None on ANY failure.
    Callers MUST gate this on onboarded + not-yet-joined so the hot /launch/me read never
    pays for an unused lookup."""
    try:
        return await workspace_name.saved_name(owner_id)
    except Exception as e:  # noqa: BLE001 -- a lookup hiccup must never fail /launch/me
        logger.warning("launch_service: suggested_display_name lookup failed for %s: %s", owner_id, e)
        return None


async def me(auth: AuthContext, s: Schedule) -> dict:
    """Read-only sheet status for the caller. Never joins — membership is
    the explicit POST /launch/join only. The one write is ``claim_user``:
    the stamp is the ONLY writer of ``user_id`` on the row (referral
    counting and the close action read it), so it runs on the read path."""
    owner_id = resolve_owner_id(auth)
    email = await resolve_email(auth)
    row = await launch_waitlist_repo.get(email) if email else None
    if row is not None:
        await launch_waitlist_repo.claim_user(email, auth.user_id)
    onboarding = await readiness_state.resolve_onboarding_state(owner_id)
    joined = bool(row and row.get("joined_at"))
    onboarded = onboarding.get("funnel_state") == "complete"
    referral_jump = launch_phase.constants()["referral_jump"]
    # The board attachment, so a reload during the competition shows the
    # connected state instead of inviting a key re-paste. Read for joined
    # rows only — the entry can't exist otherwise.
    entry = await launch_entries_repo.get(owner_id) if joined else None
    # Only worth the name lookup when the suggestion would
    # actually be USEFUL: an onboarded caller who hasn't joined yet.
    #
    # The `onboarded` term is now a correctness gate, not just a cost one.
    # Joining no longer requires onboarding (see `join_sheet`), so an
    # unonboarded caller CAN reach `/join` — but a suggestion for them would be
    # actively wrong. Before Friday's first run renames the company,
    # `company_identity.generate_company_name` has seeded it from the owner's
    # email local part, so this would propose the PERSON's name for a board
    # that deliberately ranks businesses. An empty field with a "Your company
    # name" placeholder beats a plausible-looking wrong default the owner
    # accepts by inertia. They can rename later via `rename_display_name`.
    #
    # Org callers are ALSO excluded: POST /launch/join permanently refuses
    # `org_not_supported` for any is_org_context caller (see the `join`
    # route), so an org owner can never reach joined=True — without this,
    # every /launch/me read from an onboarded org owner (LaunchJoinBanner
    # mounts unconditionally in the workspace shell, revalidating on
    # focus/mount) would pay a lookup for a suggestion that
    # can never be used.
    suggested_display_name = (
        await _suggested_display_name(owner_id) if onboarded and not joined and not auth.is_org_context else None
    )
    return {
        "phase": phase(s),
        "joined": joined,
        "position": int(row["position"]) if joined else None,
        "effective_position": effective_position(row, referral_jump) if joined else None,
        "referrals_subscribed": int(row.get("referrals_subscribed", 0)) if row else 0,
        "referral_code": row["referral_code"] if joined else None,
        "referral_link": referral_link(row["referral_code"]) if joined else None,
        "credits_milestone_reached": credits_milestone_reached(row),
        "winner": bool(row and row.get("winner")),
        "compete_eligible": bool(row.get("compete_eligible", True)) if joined else False,
        "onboarded": onboarded,
        # The name currently on the sheet, so the rename affordance can prefill
        # what the board actually shows. Read off the sheet row, NOT the board
        # entry: an owner who joined but never connected Stripe has no entry,
        # and that is the common case.
        "display_name": row.get("display_name") if joined else None,
        "suggested_display_name": suggested_display_name,
        "entry": {
            "has_key": bool(entry.get("has_key", False)),
            "public": bool(entry.get("public", True)),
            "entered_at": entry.get("entered_at"),
            "currency": entry.get("currency"),
            "delta_cents": int(entry.get("delta_cents", 0)),
            "refreshed_at": entry.get("refreshed_at"),
            "refresh_error": entry.get("refresh_error"),
        }
        if entry
        else None,
    }


class LaunchRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


async def join_sheet(auth: AuthContext, s: Schedule, *, display_name: str, public: bool) -> dict:
    """Explicit sheet join — the only door onto the sheet. Requires the
    ``open`` phase and nothing else.

    Deliberately NOT gated on onboarding. It used to be: joining required
    a completed onboarding intake, on the reasoning
    that the sheet row IS the competition entry and an entry should mean a
    real, provisioned business. In practice the gate was a funnel leak — the
    join surfaces are invisible for the whole provisioning window, which is
    exactly when a new owner is most engaged and has nothing else to do, and
    the /launch page could only tell them to come back later. Nobody came
    back. The close still counts only rows that actually joined
    (``launch_close``'s ``joined_at`` filter), an unpaid winner is simply
    emailed a pre-applied free month at checkout, and an owner who never
    activates costs us a comp slot we were not otherwise selling.

    Idempotent: joining twice returns the existing membership untouched (no
    second email, first display_name wins) — but the name is renameable via
    ``rename_display_name`` below, which matters far more now that owners join
    before Friday has named their company."""
    owner_id = resolve_owner_id(auth)
    if phase(s) != "open":
        raise LaunchRefused("closed")
    email = await resolve_email(auth)
    if not email:
        raise LaunchRefused("no_email")

    row = await launch_waitlist_repo.join(email, referred_by=None, source="sheet")
    await launch_waitlist_repo.claim_user(email, auth.user_id)
    # One atomic write: joined_at + display_name + public land together, so a
    # retry after a partial failure can never re-default a private row's
    # `public` back to true.
    first_join = await launch_waitlist_repo.mark_joined(email, display_name=display_name.strip(), public=bool(public))
    if first_join:
        referral_jump = launch_phase.constants()["referral_jump"]
        try:
            from core.services.launch_sheet_email import send_join_email

            await send_join_email(
                to_email=email,
                first_name=None,
                position=effective_position(row, referral_jump),
                referral_link=referral_link(row["referral_code"]),
                referral_jump=referral_jump,
                comp_slots=s.comp_slots,
                sheet_closes_at=s.sheet_closes_at,
                copy=launch_phase.email_copy(),
            )
        except Exception:  # noqa: BLE001 — a failed email must never fail the join
            logger.exception("launch: join email failed for %s", owner_id)
        logger.info("launch: %s joined the sheet (position=%s)", owner_id, row["position"])
    return await me(auth, s)


async def _sheet_row_for(auth: AuthContext, s: Schedule) -> dict:
    """The caller's JOINED sheet row, gated for board actions
    (connect_stripe / set_public): the competition must not be over, and a
    row the close ruled out (``compete_eligible=False``) may not touch the
    board. Pre-close joined rows carry no flag and are eligible."""
    email = await resolve_email(auth)
    row = await launch_waitlist_repo.get(email) if email else None
    if not row or not row.get("joined_at"):
        raise LaunchRefused("not_joined")
    if phase(s) == "ended":
        raise LaunchRefused("ended")
    if row.get("compete_eligible") is False:
        raise LaunchRefused("not_eligible")
    return row


def _public_entry(entry: dict | None) -> dict | None:
    if not entry:
        return None
    return {
        "display_name": entry["display_name"],
        "public": bool(entry.get("public", True)),
        "entered_at": entry.get("entered_at"),
        "has_key": bool(entry.get("has_key", False)),
        "currency": entry.get("currency"),
        "baseline_cents": entry.get("baseline_cents", 0),
        "window_cents": entry.get("window_cents", 0),
        "delta_cents": entry.get("delta_cents", 0),
        "refreshed_at": entry.get("refreshed_at"),
        "refresh_error": entry.get("refresh_error"),
    }


async def connect_stripe(auth: AuthContext, s: Schedule, *, stripe_key: str) -> dict:
    """Board opt-in: validate the pasted restricted key, compute the baseline
    over [start-30d, start) — refined by the refresh path once the window has
    actually closed, see launch_revenue._baseline_is_provisional — store the
    key, then flip the board entry live. The board entry (launch-entries row)
    is minted on demand from the sheet row; replaces any previously-connected
    key."""
    owner_id = resolve_owner_id(auth)
    sheet_row = await _sheet_row_for(auth, s)
    entry = await launch_entries_repo.get(owner_id)
    if not entry:
        referral_jump = launch_phase.constants()["referral_jump"]
        entry = await launch_entries_repo.join(
            owner_id,
            display_name=sheet_row.get("display_name") or sheet_row["email"].split("@")[0],
            public=bool(sheet_row.get("public", True)),
            waitlist_position=effective_position(sheet_row, referral_jump),
        )
    try:
        await launch_revenue.validate_key(stripe_key)
    except launch_revenue.KeyRejected as e:
        raise LaunchRefused(e.reason) from None
    baseline_end = s.competition_start_at
    baseline_start = baseline_end - timedelta(days=launch_revenue.BASELINE_DAYS)
    try:
        baseline_cents, currency = await launch_revenue.sum_net_revenue(stripe_key, baseline_start, baseline_end)
    except launch_revenue.MixedCurrency:
        raise LaunchRefused("mixed_currency") from None
    except launch_revenue.KeyRejected as e:
        raise LaunchRefused(e.reason) from None
    baseline_at = utc_now_iso()
    await launch_revenue.store_key(owner_id, stripe_key)
    try:
        await launch_entries_repo.set_key_connected(
            owner_id, currency=currency, baseline_cents=baseline_cents, baseline_at=baseline_at
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        # The entry was removed between the read above and this write (admin
        # takedown mid-flight). Don't leave the key we just stored dangling
        # with no owning row — that is exactly what routers/ops.py's delete
        # orders its two writes to avoid.
        await launch_revenue.delete_key(owner_id)
        raise LaunchRefused("no_entry") from None
    logger.info("launch: %s connected Stripe (baseline=%s %s)", owner_id, baseline_cents, currency)
    # set_key_connected writes exactly these four fields, so the local merge is
    # the same row a re-get would return — one less DynamoDB read.
    return _public_entry(
        {**entry, "has_key": True, "currency": currency, "baseline_cents": baseline_cents, "baseline_at": baseline_at}
    )


async def rename_display_name(auth: AuthContext, s: Schedule, *, display_name: str) -> dict:
    """Rename the caller on the sheet — and on the board entry too once one
    exists, since ``connect_stripe`` copies the name across and the
    leaderboard reads the copy.

    This exists because joining is no longer gated on onboarding (see
    ``join_sheet``). ``mark_joined`` writes ``display_name`` exactly once
    under ``attribute_not_exists(joined_at)``, so before this the FIRST name
    an owner typed was their permanent identity on a public leaderboard. That
    was tolerable while joining happened after Friday had already researched
    and renamed their company; it is not tolerable now that we ask during
    provisioning, minutes after signup, before the owner has necessarily
    settled on a name at all.

    Gated exactly like the other board actions via ``_sheet_row_for``: joined,
    competition not over, not ruled out by the close."""
    owner_id = resolve_owner_id(auth)
    sheet_row = await _sheet_row_for(auth, s)
    previous = sheet_row.get("display_name")
    name = display_name.strip()
    await launch_waitlist_repo.update_fields(sheet_row["email"], {"display_name": name})
    # Best-effort second leg: an owner who has not connected Stripe has no
    # entry, which is the common case and not an error.
    entry = await launch_entries_repo.get(owner_id)
    if entry:
        try:
            await launch_entries_repo.set_display_name(owner_id, name)
        except ClientError as e:
            # The entry was removed between the read above and this write (an
            # admin takedown mid-flight) — the same race `connect_stripe`
            # handles explicitly. The sheet row is ALREADY renamed at this
            # point, so raising would 500 a request that half-succeeded and
            # leave the caller retrying into the same 500 forever. A row that
            # no longer exists has no stale name to carry, so there is nothing
            # left to reconcile.
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            logger.info("launch: %s board entry vanished mid-rename; sheet row renamed", owner_id)
    # Logged old -> new: this is the only writer that can change a name already
    # on a public leaderboard, and it can run repeatedly, so an operator
    # investigating an abusive board entry needs the history.
    logger.info("launch: %s renamed on the sheet (%r -> %r)", owner_id, previous, name)
    return {"display_name": name}


async def set_public(auth: AuthContext, s: Schedule, *, public: bool) -> dict:
    """Flip the board-visibility flag — on the sheet row always, and on the
    board entry too once one exists. Retractable at any time."""
    owner_id = resolve_owner_id(auth)
    sheet_row = await _sheet_row_for(auth, s)
    await launch_waitlist_repo.update_fields(sheet_row["email"], {"public": bool(public)})
    entry = await launch_entries_repo.get(owner_id)
    if entry:
        await launch_entries_repo.set_public(owner_id, public)
    return {"public": bool(public)}


def rank(entries: list[dict]) -> list[dict]:
    visible = [e for e in entries if e.get("has_key") and e.get("public", True)]
    visible.sort(key=lambda e: (-int(e.get("delta_cents", 0)), e.get("entered_at") or ""))
    rows = []
    for i, e in enumerate(visible, start=1):
        # The prorated baseline is what `delta_cents` was measured against; falling
        # back to the full baseline would understate growth mid-competition.
        prorated = int(e.get("baseline_prorated_cents") or 0)
        baseline = prorated if prorated > 0 else int(e.get("baseline_cents", 0))
        delta = int(e.get("delta_cents", 0))
        rows.append(
            {
                "rank": i,
                "display_name": e["display_name"],
                "delta_cents": delta,
                "pct": round(delta / baseline, 4) if baseline > 0 else None,
                "currency": e.get("currency") or "usd",
            }
        )
    return rows


async def leaderboard(limit: int = 50) -> dict:
    entries = await launch_entries_repo.list_all()
    # The close rules unpaid non-winners out (compete_eligible=False on the
    # sheet row) — their board entries must vanish server-side, not just
    # lose their buttons. Rows without a matching sheet row (pre-pivot
    # legacy) stay visible.
    ineligible = {
        w["user_id"]
        for w in await launch_waitlist_repo.list_all()
        if w.get("user_id") and w.get("compete_eligible") is False
    }
    entries = [e for e in entries if e["owner_id"] not in ineligible]
    on_board = sum(1 for e in entries if e.get("has_key"))
    return {"competing": len(entries), "on_board": on_board, "rows": rank(entries)[:limit]}


# --- team entries: raise a hand, the founders do the rest --------------------
#
# Teams do NOT self-serve. POST /launch/join still refuses an org context (409
# org_not_supported) and nothing here changes that: a submission stores a row
# and emails the founders, who enter the team by hand. No sheet row, no
# position, no coupon, no org onboarding.


def team_contribution_cents(seats: int) -> int:
    """What ONE counted team adds to the pot: the per-entrant amount every
    entrant contributes, plus the per-seat amount for each seat. Same
    arithmetic ``launch_phase.pot_cents`` applies at close — expressed once
    here so the founder email and the admin card quote the real number."""
    c = launch_phase.constants()
    return c["pot_per_entrant_cents"] + c["pot_per_seat_cents"] * max(int(seats), 0)


def _public_team(row: dict) -> dict:
    """What the submitter gets back — their own submission, plus the founders'
    verdict. No pot arithmetic: the pot is admin-only, on every surface."""
    return {
        "team_name": row["team_name"],
        "seats": int(row["seats"]),
        "note": row.get("note") or "",
        "submitted_at": row.get("submitted_at"),
        "counted": bool(row.get("counted")),
    }


async def submit_team_interest(auth: AuthContext, *, team_name: str, seats: int, note: str) -> dict:
    """Store (or update) the caller's team-interest row and tell the founders.
    Idempotent per OWNER — a resubmission edits the same row rather than
    filing a second one. The email is best-effort and never fails the write:
    the row is the record, the email is the nudge."""
    owner_id = resolve_owner_id(auth)
    email = await resolve_email(auth) or ""
    row = await launch_teams_repo.put_interest(
        owner_id,
        team_name=team_name,
        seats=seats,
        note=note,
        submitter_user_id=auth.user_id,
        submitter_email=email,
        submitter_org_slug=auth.org_slug,
    )
    from core.services.launch_sheet_email import send_team_interest_email

    await send_team_interest_email(
        team_name=team_name,
        seats=seats,
        note=note,
        submitter_email=email,
        submitter_user_id=auth.user_id,
        submitter_org_slug=auth.org_slug,
        contribution_cents=team_contribution_cents(seats),
    )
    logger.info("launch: %s submitted team interest (%s seats)", owner_id, seats)
    return _public_team(row)


async def set_team_entry(owner_id: str, *, seats: int | None, counted: bool | None) -> dict:
    """Admin edit of one team entry — the seat correction and the "counts
    toward the pot" verdict. Raises ``SettingsInvalid`` (router -> 422, the
    console's usual ``{"detail": {"errors": …}}`` envelope) on a bad seat
    count or an owner with no submission."""
    from core.services.launch_settings import SettingsInvalid

    fields: dict = {}
    if seats is not None:
        if not 1 <= int(seats) <= TEAM_SEATS_MAX:
            raise SettingsInvalid({"seats": f"must be between 1 and {TEAM_SEATS_MAX}"})
        fields["seats"] = int(seats)
    if counted is not None:
        fields["counted"] = bool(counted)
    if not fields:
        raise SettingsInvalid({"seats": "nothing to change"})
    row = await launch_teams_repo.update_fields(owner_id, fields)
    if row is None:
        raise SettingsInvalid({"owner_id": "no team entry for that owner"})
    return row


async def counted_team_seats() -> list[int]:
    """Seat counts of the team entries the founders marked counted — the only
    ones that reach the pot (``launch_phase.pot_cents``)."""
    return [int(t["seats"]) for t in await launch_teams_repo.list_all() if t.get("counted")]
