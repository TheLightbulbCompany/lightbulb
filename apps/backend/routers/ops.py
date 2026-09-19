"""Human operator diagnostics for the platform-admin console."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from core.auth import AuthContext, require_platform_admin
from core.services.admin_audit import audit_admin_action
from core.services.ops import alarms, deploys, fleet
from core.services.ops import envelope as env
from schemas.launch import (
    LaunchCloseRequest,
    LaunchEmailCopyUpdate,
    LaunchEmailTestRequest,
    LaunchSettingsUpdate,
    StripeCouponCreateRequest,
    TeamEntryPatchRequest,
)

router = APIRouter()


@router.get("/fleet")
async def get_fleet(_auth: AuthContext = Depends(require_platform_admin)):
    # Single live read: fleet_counts_live() derives from fleet_grid() internally,
    # so read the grid ONCE and compute counts locally — avoids a redundant
    # second ECS/scan fan-out and a compound timeout. On failure the whole read
    # degrades together (grid is None -> counts None -> source_state != ok).
    grid, state, errors = await env.read_source("ecs", fleet.fleet_grid)
    counts = fleet.counts_from_grid(grid) if grid is not None else None
    return env.envelope({"counts": counts, "grid": grid}, source_state=state, errors=errors)


@router.get("/deploys")
async def get_deploys(_auth: AuthContext = Depends(require_platform_admin)):
    data, state, errors = await env.read_source("cloudformation", deploys.list_stacks)
    return env.envelope(data if data is not None else [], source_state=state, errors=errors)


@router.get("/alarms")
async def get_alarms(_auth: AuthContext = Depends(require_platform_admin)):
    data, state, errors = await env.read_source("cloudwatch", alarms.alarms_in_alarm)
    return env.envelope(data if data is not None else [], source_state=state, errors=errors)


@router.get("/growth")
async def get_growth(_auth: AuthContext = Depends(require_platform_admin)):
    """Fleet growth metrics (the observability audit §6), computed on read, 60s cache.
    Partial sources surface in errors[] — the page must never render a
    missing section as zero-and-green."""
    from core.services.ops import growth

    data, state, errors = await growth.compute()
    return env.envelope(data, source_state=state, errors=errors)


@router.get("/launch")
async def get_launch(_auth: AuthContext = Depends(require_platform_admin)):
    """Launch Competition — the economics sheet with live inputs, 60s cache."""
    from core.services.ops import launch as launch_ops

    data, state, errors = await launch_ops.compute()
    return env.envelope(data, source_state=state, errors=errors)


@router.get("/launch/settings")
async def get_launch_settings(_auth: AuthContext = Depends(require_platform_admin)):
    """The launch-settings row (DEFAULTS ⊕ stored), for the admin edit form.
    Never audited — a read has nothing to log."""
    from core.services import launch_settings

    return launch_settings.to_admin(await launch_settings.effective())


@router.put("/launch/settings")
@audit_admin_action("launch.settings.update", target_user_id_override="__launch__", capture_params=["body"])
async def put_launch_settings(
    body: LaunchSettingsUpdate,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
):
    """Validated full-replace of the settings row. A 422 on bad input is
    FastAPI's default ``{"detail": {"errors": {field: msg}}}`` envelope — the
    codebase's one 422 convention; a saved row invalidates every cache that
    reads it."""
    from core.services import launch_settings

    try:
        return await launch_settings.update(auth, body.model_dump())
    except launch_settings.SettingsInvalid as e:
        raise HTTPException(status_code=422, detail={"errors": e.errors})


@router.get("/launch/email-copy")
async def get_launch_email_copy(_auth: AuthContext = Depends(require_platform_admin)):
    """Per-variant launch email copy for the admin editor: the shipped
    defaults, the stored override, the supported placeholders, and a rendered
    preview of the effective body."""
    from core.services import launch_settings

    return {"variants": launch_settings.email_copy_admin(await launch_settings.effective())}


@router.put("/launch/email-copy")
@audit_admin_action("launch.email_copy.update", target_user_id_override="__launch__", capture_params=["body"])
async def put_launch_email_copy(
    request: Request,
    body: LaunchEmailCopyUpdate,
    auth: AuthContext = Depends(require_platform_admin),
):
    """Save (or, with every field blank, reset) one variant's copy. 422 with
    the usual ``{"detail": {"errors": ...}}`` on an unsupported placeholder."""
    from core.services import launch_settings

    try:
        copy = await launch_settings.update_email_copy(auth, body.variant, body.model_dump(exclude={"variant"}))
    except launch_settings.SettingsInvalid as e:
        raise HTTPException(status_code=422, detail={"errors": e.errors}) from None
    return {"variant": body.variant, "override": copy.get(body.variant, {})}


@router.post("/launch/email-test")
@audit_admin_action("launch.email_test", target_user_id_override="__launch__", capture_params=["body"])
async def post_launch_email_test(
    request: Request,
    body: LaunchEmailTestRequest,
    auth: AuthContext = Depends(require_platform_admin),
):
    """Send ONE variant, with sample values, to the signed-in admin's own
    Clerk address. Never touches a sheet row and never stamps an email claim —
    it cannot double-send or suppress a real close email."""
    from core.services import clerk_admin, launch_settings, launch_sheet_email

    if body.variant not in launch_sheet_email.VARIANTS:
        raise HTTPException(status_code=422, detail={"errors": {"variant": "unknown email variant"}})
    row = await launch_settings.effective()
    try:
        to_email = (await clerk_admin.get_primary_email(auth.user_id)).strip()
    except Exception:  # noqa: BLE001 — no address, no send; say so instead of 500ing
        raise HTTPException(status_code=422, detail={"errors": {"variant": "no email on your admin account"}}) from None
    from core.services import launch_phase

    s = launch_phase.schedule()
    values = launch_sheet_email.sample_values(
        comp_slots=row["comp_slots"],
        referral_jump=row["referral_jump"],
        sheet_closes_at=s.sheet_closes_at if s else None,
    )
    message_id = await launch_sheet_email.send_test(
        body.variant, to_email=to_email, copy=row.get("email_copy"), values=values
    )
    if not message_id:
        raise HTTPException(status_code=502, detail="send_failed")
    return {"sent_to": to_email, "variant": body.variant}


@router.post("/launch/settings/stripe-coupon")
@audit_admin_action("launch.settings.stripe_coupon", target_user_id_override="__launch__", capture_params=["body"])
async def post_launch_stripe_coupon(
    body: StripeCouponCreateRequest,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
):
    """One-click mint of the launch free-month coupon + promotion code.
    409 if one is already configured (unless ``force``); 422 (FastAPI's
    default ``{"detail": {"errors": ...}}`` envelope) if
    ``competition_start_at`` isn't set yet; 502 on a Stripe-side failure —
    the real Stripe error is logged server-side, never in the response."""
    from core.services import launch_settings

    try:
        return await launch_settings.create_stripe_coupon(auth, force=body.force)
    except launch_settings.CouponAlreadyConfigured:
        raise HTTPException(status_code=409, detail="already_configured")
    except launch_settings.SettingsInvalid as e:
        raise HTTPException(status_code=422, detail={"errors": e.errors})
    except launch_settings.StripeMintError:
        raise HTTPException(status_code=502, detail="stripe_error")


@router.post("/launch/team-entries/{owner_id}")
@audit_admin_action("launch.team_entry.update", target_param="owner_id", capture_params=["owner_id", "body"])
async def post_launch_team_entry(
    owner_id: str,
    body: TeamEntryPatchRequest,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
):
    """Correct a team's seat count, or decide whether it counts toward the
    pot. Only a COUNTED entry contributes — an unreviewed submission never
    inflates the pot. 422 with the usual ``{"detail": {"errors": ...}}`` on a
    bad seat count or an unknown owner."""
    from core.services import launch_service, launch_settings
    from core.services.ops import launch as launch_ops

    try:
        row = await launch_service.set_team_entry(owner_id, seats=body.seats, counted=body.counted)
    except launch_settings.SettingsInvalid as e:
        raise HTTPException(status_code=422, detail={"errors": e.errors}) from None
    launch_ops.clear_cache()
    return row


@router.post("/launch/close", status_code=202)
@audit_admin_action("launch.close", target_user_id_override="__launch__", capture_params=["body"])
async def post_launch_close(
    body: LaunchCloseRequest,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
):
    """START the close as a background task and 202 — with a full sheet the
    sequence is hundreds of sequential Stripe calls plus the winner emails,
    far past the API gateway timeout. Refusals are synchronous: 409
    already_closed / close_running unless ``force`` (which is also how a
    crashed task is recovered — every step is idempotent). The finished
    summary lands on the settings row as ``close_result``, surfaced by
    GET /ops/launch alongside ``close_running``."""
    from core.services import launch_close
    from core.services.ops import launch as launch_ops

    try:
        await launch_close.start_close(auth, force=body.force)
    except launch_close.AlreadyClosed:
        raise HTTPException(status_code=409, detail="already_closed")
    except launch_close.CloseRunning:
        raise HTTPException(status_code=409, detail="close_running")
    except launch_close.CloseError as e:
        raise HTTPException(status_code=409, detail=e.reason)
    launch_ops.clear_cache()
    _clear_public_launch_cache()
    return {"status": "started"}


def _clear_public_launch_cache() -> None:
    """The admin console's cache is not the only one — /launch and
    /launch/leaderboard hold their own 20-30s response cache. Drop it too, so a
    taken-down entrant leaves the public board now rather than up to 30s later.
    Lazy import: routers.launch imports launch services this module doesn't."""
    from routers.launch import clear_response_cache

    clear_response_cache()


@router.post("/launch/refresh")
async def refresh_launch(_auth: AuthContext = Depends(require_platform_admin)):
    """Force-refresh every entrant's revenue now (judging day)."""
    from core.services import launch_revenue
    from core.services.ops import launch as launch_ops

    n = await launch_revenue.refresh_all(force=True)
    launch_ops.clear_cache()
    _clear_public_launch_cache()
    return {"refreshed": n}


@router.delete("/launch/entries/{owner_id}")
async def delete_launch_entry(owner_id: str, _auth: AuthContext = Depends(require_platform_admin)):
    """Take an entry off the public board and revoke its stored Stripe key.

    The only removal door — self-serve key revocation is a v1 non-goal, so a
    fraudulent or withdrawn entrant is removed here."""
    from core.repositories import launch_entries_repo
    from core.services import launch_revenue
    from core.services.ops import launch as launch_ops

    # Key first: if the row delete below fails partway, a dangling Secrets
    # Manager entry with no owning row is worse than a row that still has
    # has_key=true pointing at an already-revoked key.
    await launch_revenue.delete_key(owner_id)
    await launch_entries_repo.delete(owner_id)
    launch_ops.clear_cache()
    _clear_public_launch_cache()
    return {"deleted": owner_id}
