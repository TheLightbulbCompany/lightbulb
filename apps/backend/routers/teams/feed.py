"""Teams BFF — Activity + Costs + Dashboard.

All three are pure-read aggregation surfaces. Dashboard fans out
into two upstream calls (the dashboard summary and the sidebar
badge counts) and merges them into one response so the panel
renders in a single round-trip.

Activity events carry a raw ``actorId`` — the Better Auth (Paperclip)
principal id, NOT a Clerk id and NOT a display name. ``list_activity``
enriches each user-actor event with ``actorEmail`` (and ``actorName``,
which falls back to the email) by reusing the same principalId → Clerk
user_id → email join ``members.py`` uses. The enrichment is best-effort:
any failure leaves the raw id untouched and never crashes the feed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query

from . import agents as _agents
from .deps import TeamsContext

logger = logging.getLogger(__name__)

router = APIRouter()
_ctx = _agents._ctx


def _normalize_activity_events(payload) -> tuple[list, str | None]:
    """Pull the event list out of upstream's activity payload.

    Upstream may return a bare list or one of the
    ``{events|items|activity: [...]}`` envelopes. Returns
    ``(events, envelope_key)`` where ``envelope_key`` is ``None`` for a
    bare-list payload so the caller can re-wrap in the same shape.
    """
    if isinstance(payload, list):
        return payload, None
    if isinstance(payload, dict):
        for key in ("events", "items", "activity"):
            value = payload.get(key)
            if isinstance(value, list):
                return value, key
    return [], None


async def _build_principal_to_clerk(ctx: TeamsContext) -> dict[str, str]:
    """Map Paperclip principalId → Clerk user_id for the caller's org.

    Mirrors ``members.list_members``: for a personal context the caller
    is the only member (seed their own pairing); for an org, build the
    map from the ``paperclip-companies`` ``by-org-id`` GSI rows. GSI
    failures degrade to an empty map (enrichment is skipped, feed still
    ships).
    """
    principal_to_clerk: dict[str, str] = {}
    if ctx.org_id is None:
        if ctx.paperclip_user_id:
            principal_to_clerk[ctx.paperclip_user_id] = ctx.user_id
        return principal_to_clerk
    try:
        org_rows = await _agents._repo().list_by_org_id(ctx.org_id)
    except Exception:
        logger.exception("activity.list: list_by_org_id failed for org=%s", ctx.org_id)
        return principal_to_clerk
    for row in org_rows:
        paperclip_uid = row.get("paperclip_user_id")
        clerk_uid = row.get("user_id")
        if paperclip_uid and clerk_uid:
            principal_to_clerk[paperclip_uid] = clerk_uid
    return principal_to_clerk


async def _enrich_actor_names(ctx: TeamsContext, events: list) -> None:
    """Stamp ``actorEmail`` / ``actorName`` onto user-actor events in place.

    Only ``actorType == "user"`` events carry a resolvable principal
    (agent/system actors are named elsewhere). Each unique actorId is
    resolved at most once (batch). Any failure — missing map entry, Clerk
    lookup error — leaves that event's raw id untouched.
    """
    # Collect the unique principal ids worth resolving (user actors only).
    actor_ids: set[str] = set()
    for evt in events:
        if not isinstance(evt, dict):
            continue
        if evt.get("actorType") == "user":
            actor_id = evt.get("actorId")
            if isinstance(actor_id, str) and actor_id:
                actor_ids.add(actor_id)
    if not actor_ids:
        return

    principal_to_clerk = await _build_principal_to_clerk(ctx)

    # Resolve each unique actor's email once (principalId → Clerk id → email).
    email_by_actor: dict[str, str] = {}
    for actor_id in actor_ids:
        clerk_user_id = principal_to_clerk.get(actor_id)
        if not clerk_user_id:
            continue
        try:
            email = await _agents._resolve_user_email(clerk_user_id)
        except Exception:
            logger.exception(
                "activity.list: clerk email lookup failed for clerk_user_id=%s (paperclip principal=%s)",
                clerk_user_id,
                actor_id,
            )
            continue
        if email:
            email_by_actor[actor_id] = email

    if not email_by_actor:
        return

    for evt in events:
        if not isinstance(evt, dict) or evt.get("actorType") != "user":
            continue
        email = email_by_actor.get(evt.get("actorId"))
        if email:
            evt["actorEmail"] = email
            # No separate display-name source on the Clerk join here, so
            # the email doubles as the name; the frontend prefers
            # actorName/actorEmail over the raw id either way.
            evt.setdefault("actorName", email)


@router.get("/activity")
async def list_activity(
    ctx: TeamsContext = Depends(_ctx),
    limit: Optional[int] = Query(default=None, ge=1, le=500),
):
    """List company activity events, enriched with actor emails/names.

    Forwards ``limit`` to upstream (bounded 1–500, upstream's
    ``MAX_ACTIVITY_LIMIT``). The Activity feed asks for 200; without
    forwarding it the feed silently caps at upstream's default of 100.

    Each user-actor event is enriched with ``actorEmail`` / ``actorName``
    so the dashboard feed shows a human, not a raw Better Auth id. The
    enrichment is best-effort: on any failure the raw payload ships
    unchanged.
    """
    payload = await _agents._admin().list_activity(
        company_id=ctx.company_id,
        session_cookie=ctx.session_cookie,
        limit=limit,
    )
    try:
        events, _envelope_key = _normalize_activity_events(payload)
        if events:
            # Mutates the event dicts in place; since they're the same
            # objects referenced by ``payload``, the returned payload keeps
            # its original envelope shape (bare list or {events|items|...}).
            await _enrich_actor_names(ctx, events)
    except Exception:
        # Enrichment must never take down the feed — return upstream verbatim.
        logger.exception("activity.list: actor enrichment failed; returning raw payload")
    return payload


@router.get("/costs")
async def get_costs(ctx: TeamsContext = Depends(_ctx)):
    """Fetch the company costs summary."""
    return await _agents._admin().get_costs(
        company_id=ctx.company_id,
        session_cookie=ctx.session_cookie,
    )


@router.get("/dashboard")
async def get_dashboard(ctx: TeamsContext = Depends(_ctx)):
    """Fetch the dashboard summary + sidebar badges in parallel.

    Returns the upstream Paperclip dashboard summary verbatim
    (``agents`` / ``tasks`` / ``runActivity`` / ``costs`` /
    ``pendingApprovals`` / ``budgets``) so the panel can render the
    full set of metric tiles + charts without further fan-out.

    Defensive: if upstream returns None or a non-dict, normalize to
    an empty dict so the frontend can still render an empty-state.
    """
    admin = _agents._admin()
    badges, dash = await asyncio.gather(
        admin.get_sidebar_badges(
            company_id=ctx.company_id,
            session_cookie=ctx.session_cookie,
        ),
        admin.get_dashboard(
            company_id=ctx.company_id,
            session_cookie=ctx.session_cookie,
        ),
    )
    return {
        "dashboard": dash if isinstance(dash, dict) else {},
        "sidebar_badges": badges,
    }


@router.get("/sidebar-badges")
async def get_sidebar_badges(ctx: TeamsContext = Depends(_ctx)):
    """Fetch the sidebar badge counts on their own.

    Exposed as a separate endpoint (in addition to being aggregated
    into ``/dashboard``) because the global app shell polls badges
    on a faster cadence than the dashboard panel itself.
    """
    return await _agents._admin().get_sidebar_badges(
        company_id=ctx.company_id,
        session_cookie=ctx.session_cookie,
    )
