"""Join events: ``workspace_created``, ``member_joined``, ``team_formed`` (T02).

Emitted from the Clerk webhooks, the one place every membership passes through.

**The ``via`` marker.** The membership webhook cannot tell which door a person came
through, so each door writes a short-lived marker FIRST, before Clerk creates the
membership, and the webhook reads it:

    await mark_join_via(org_id, via="join_link", user_id=user_id, invited_by=link_creator)
    await mark_join_via(org_id, via="invite", email=invitee_email, invited_by=inviter)

Key by ``user_id`` when the door knows who is joining, by ``email`` when it only knows the
address (an email invitation is accepted on Clerk's side, before we see a user id).
Unmarked joins read ``via="other"`` (e.g. added in Clerk's dashboard). The workspace
creator's own membership is not a ``member_joined``.

Markers live in the webhook-dedup table (TTL'd; already a marker store, see apple_iap's
tombstone). Everything here is best-effort: analytics never fails a webhook or a door.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone

from core.dynamodb import run_in_thread
from core.observability.product_analytics import capture_product_event, remember_org_creator
from core.services import clerk_admin, webhook_dedup

logger = logging.getLogger(__name__)

#: Every door's ``via`` value. T07 (invite page) and T17 (session page) write theirs.
VIA_VALUES = frozenset({"join_link", "invite", "session_page", "other"})

_USER_MARKER_TTL_S = 24 * 3600
# An email invitation can sit unaccepted for weeks; the marker must outlive it.
_EMAIL_MARKER_TTL_S = 31 * 24 * 3600
TEAM_FORMED_WINDOW_H = 48

_NS = uuid.uuid5(uuid.NAMESPACE_URL, "lightbulb:join_events")


def _marker_key(org_id: str, who: str) -> str:
    return f"join-via:{org_id}:{who}"


async def mark_join_via(
    org_id: str, *, via: str, user_id: str | None = None, email: str | None = None, invited_by: str | None = None
) -> None:
    """Record which door is about to add a member. Call BEFORE the membership exists."""
    if via not in VIA_VALUES:
        raise ValueError(f"unknown join via {via!r}")
    who = user_id or (email or "").strip().lower()
    if not org_id or not who:
        return
    now = int(time.time())
    try:
        await run_in_thread(
            webhook_dedup._table().put_item,
            Item={
                "event_id": _marker_key(org_id, who),
                "source": "join_via",
                "via": via,
                "invited_by": invited_by or "",
                "recorded_at": now,
                "ttl": now + (_USER_MARKER_TTL_S if user_id else _EMAIL_MARKER_TTL_S),
            },
        )
    except Exception:
        logger.warning("join_analytics: via marker write failed org=%s via=%s", org_id, via, exc_info=True)


async def _read_join_via(org_id: str, user_id: str, email: str | None) -> dict:
    """The marker for this join (user first, then email), or ``{}``. Not deleted, so a
    Clerk redelivery reads the same answer; the TTL cleans up."""
    for who in (user_id, (email or "").strip().lower()):
        if not who:
            continue
        try:
            row = await webhook_dedup._read_event(_marker_key(org_id, who))
        except Exception:
            logger.warning("join_analytics: via marker read failed org=%s", org_id, exc_info=True)
            return {}
        if row and int(row.get("ttl") or 0) > time.time():
            return row
    return {}


def _hours_since(created_at_ms) -> float | None:
    if not isinstance(created_at_ms, (int, float)) or created_at_ms <= 0:
        return None
    created = datetime.fromtimestamp(created_at_ms / 1000, tz=timezone.utc)
    return round((datetime.now(timezone.utc) - created).total_seconds() / 3600, 2)


def workspace_created(org_id: str, created_by: str, properties: dict | None = None) -> None:
    """``workspace_created`` from ``organization.created``. Once per org (deterministic uuid)."""
    try:
        remember_org_creator(org_id, created_by)
        capture_product_event(
            distinct_id=created_by,
            event="workspace_created",
            properties={**(properties or {}), "workspace_id": org_id, "created_by": created_by},
            event_uuid=str(uuid.uuid5(_NS, f"workspace_created:{org_id}")),
        )
    except Exception:
        logger.warning("join_analytics: workspace_created failed org=%s", org_id, exc_info=True)


async def member_joined(org_id: str, user_id: str, *, email: str | None, creator_id: str, membership_id: str) -> None:
    """``member_joined`` (and, at the second person within 48 h, ``team_formed``) from
    ``organizationMembership.created``. Never raises."""
    try:
        if user_id == creator_id:
            return  # the creator's own seat is workspace_created, not a join
        marker = await _read_join_via(org_id, user_id, email)
        org = await clerk_admin.get_organization(org_id, include_members_count=True) or {}
        members_count = org.get("members_count")
        hours = _hours_since(org.get("created_at"))
        capture_product_event(
            distinct_id=user_id,
            event="member_joined",
            properties={
                "workspace_id": org_id,
                "via": marker.get("via") or "other",
                "invited_by": marker.get("invited_by") or None,
                "members_count": members_count,
                "hours_since_created": hours,
            },
            event_uuid=str(uuid.uuid5(_NS, f"member_joined:{org_id}:{membership_id or user_id}")),
        )
        if (
            isinstance(members_count, int)
            and members_count >= 2
            and hours is not None
            and hours <= TEAM_FORMED_WINDOW_H
            and await webhook_dedup.record_event_or_skip(
                f"team-formed:{org_id}", source="analytics", ttl_seconds=90 * 86400
            )
            is webhook_dedup.WebhookDedupResult.RECORDED
        ):
            capture_product_event(
                distinct_id=creator_id or user_id,
                event="team_formed",
                properties={"workspace_id": org_id, "members_count": members_count, "hours_since_created": hours},
                event_uuid=str(uuid.uuid5(_NS, f"team_formed:{org_id}")),
            )
    except Exception:
        logger.warning("join_analytics: member_joined failed org=%s", org_id, exc_info=True)
