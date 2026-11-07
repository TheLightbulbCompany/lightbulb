"""Daily "needs you" digest: one per human, per workspace, per local day at 08:00.

Content is the Activity feed's own unseen items (``activity.unseen_counts``) of
the kinds that wait on a human -- approvals, assigned issues, mentions. Zero
items sends nothing. One renderer (``render_push``) produces the text, and the
Expo push, the email and the desktop ``digest.ready`` event all carry it
verbatim.

Scheduling rides the backend's sweeper pattern: every task ticks, pass
admission (``container_repo.try_claim_pass``) lets one of them scan per window,
and the per-(human, workspace, local date) conditional put
(``messaging_repo.claim_digest``) is the correctness boundary -- a duplicate
scan can never send twice. No new queue, Lambda or schedule.

The email carries an RFC 8058 one-click unsubscribe pointing at an
unauthenticated endpoint; its token is an HS256 JWT on the existing
``PAPERCLIP_SERVICE_TOKEN_KEY`` with its own ``kind``, so it can only ever turn
the "Daily summary" pref off (every verifier of that key checks ``kind``).
"""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import jwt as pyjwt

from core.config import settings
from core.observability.metrics import put_metric
from core.repositories import container_repo, messaging_repo, user_repo
from core.services import activity, email_service, push_service

logger = logging.getLogger(__name__)

KINDS = ("approval", "assigned_issue", "mention")
_LABELS = {
    "approval": ("approval", "approvals"),
    "assigned_issue": ("assigned issue", "assigned issues"),
    "mention": ("mention", "mentions"),
}
SEND_HOUR = 8
PASS_NAME = "daily_digest"
PASS_SECONDS = 300
_TICK_SECONDS = 60
UNSUBSCRIBE_KIND = "daily_summary_unsubscribe"
UNSUBSCRIBE_TTL = timedelta(days=30)


# ---------------------------------------------------------------------------
# rendering -- the ONE place digest text is composed
# ---------------------------------------------------------------------------


def activity_url() -> str:
    return f"{settings.FRONTEND_URL.rstrip('/')}/chat?panel=activity"


def render_push(counts: dict[str, int]) -> dict:
    """``{title, body, data: {url}}`` -- the exact Expo message, shown verbatim
    by desktop (``digest.ready.push``) and used as the email subject/lead."""
    total = sum(counts.get(k, 0) for k in KINDS)
    parts = [f"{counts[k]} {_LABELS[k][counts[k] != 1]}" for k in KINDS if counts.get(k)]
    return {
        "title": f"{total} thing{'s need' if total != 1 else ' needs'} you",
        "body": " · ".join(parts),
        "data": {"url": activity_url()},
    }


def digest_ready_payload(counts: dict[str, int]) -> dict:
    by_kind = {k: counts.get(k, 0) for k in KINDS}
    return {
        "countsByKind": by_kind,
        "total": sum(by_kind.values()),
        "activityUrl": activity_url(),
        "push": render_push(by_kind),
    }


# ---------------------------------------------------------------------------
# unsubscribe token
# ---------------------------------------------------------------------------


def mint_unsubscribe_token(user_id: str, *, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    claims = {"sub": user_id, "kind": UNSUBSCRIBE_KIND, "iat": now, "exp": now + UNSUBSCRIBE_TTL}
    return pyjwt.encode(claims, settings.PAPERCLIP_SERVICE_TOKEN_KEY, algorithm="HS256")


def verify_unsubscribe_token(token: str) -> str | None:
    """The user id the token was minted for, or ``None`` (bad signature, expired,
    wrong kind, no key configured)."""
    if not settings.PAPERCLIP_SERVICE_TOKEN_KEY:
        return None
    try:
        claims = pyjwt.decode(token, settings.PAPERCLIP_SERVICE_TOKEN_KEY, algorithms=["HS256"])
    except pyjwt.InvalidTokenError:
        return None
    sub = claims.get("sub")
    return sub if claims.get("kind") == UNSUBSCRIBE_KIND and isinstance(sub, str) and sub else None


def unsubscribe_url(user_id: str) -> str:
    token = quote(mint_unsubscribe_token(user_id), safe="")
    return f"{settings.PUBLIC_API_BASE.rstrip('/')}/api/v1/users/digest/unsubscribe?token={token}"


# ---------------------------------------------------------------------------
# email
# ---------------------------------------------------------------------------


def _email_allowed(address: str) -> bool:
    """Prod mails everyone; every other env mails only test and company inboxes."""
    if settings.ENVIRONMENT.strip().lower() in ("prod", "production"):
        return True
    address = address.lower()
    return "+clerk_test" in address or address.endswith("@thelightbulb.company")


def render_email(push: dict, unsubscribe: str) -> tuple[str, str, dict[str, str]]:
    """``(text, html, headers)`` for one digest email."""
    url = push["data"]["url"]
    text = (
        f"{push['title']}\n\n{push['body']}\n\nOpen Activity: {url}\n\n"
        f"You get this once a day when something is waiting on you.\nUnsubscribe: {unsubscribe}\n"
    )
    e = html.escape
    body = (
        f'<div style="font-family:sans-serif;max-width:520px">'
        f"<h2>{e(push['title'])}</h2><p>{e(push['body'])}</p>"
        f'<p><a href="{e(url)}">Open Activity</a></p>'
        f'<p style="color:#888;font-size:12px">You get this once a day when something is waiting on you. '
        f'<a href="{e(unsubscribe)}">Unsubscribe</a></p></div>'
    )
    headers = {"List-Unsubscribe": f"<{unsubscribe}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
    return text, body, headers


async def _send_email(to: str, user_id: str, push: dict) -> str | None:
    text, body, headers = render_email(push, unsubscribe_url(user_id))
    return await email_service.send_email(
        to=to,
        subject=push["title"],
        html=body,
        text=text,
        from_addr=settings.EMAIL_FROM_WELCOME,
        kind="daily_digest",
        headers=headers,
    )


# ---------------------------------------------------------------------------
# the pass
# ---------------------------------------------------------------------------


def due_local_date(row: dict, now: datetime) -> str | None:
    """The human's local date when it is their 08:00 hour and the pref is on."""
    if not user_repo.daily_summary_on(row):
        return None
    local = now.astimezone(ZoneInfo(user_repo.timezone_of(row)))
    return local.date().isoformat() if local.hour == SEND_HOUR else None


async def digest_one(owner_id: str, row: dict, local_date: str) -> bool:
    """Decide and send one (human, workspace) digest. ``True`` when sent."""
    user_id = row["user_id"]
    counts = await activity.unseen_counts(owner_id, user_id)
    counts = {k: counts.get(k, 0) for k in KINDS}
    # Claimed even at zero: the day's digest is decided at the first 08:00 tick.
    if not await messaging_repo.claim_digest(user_id, owner_id, local_date):
        return False
    if not sum(counts.values()):
        put_metric("digest.skipped", dimensions={"reason": "nothing_unseen"})
        return False

    push = render_push(counts)
    await push_service.send_push_to_user(user_id, push["title"], push["body"], push["data"])
    email = row.get("email")
    if email and _email_allowed(email):
        await _send_email(email, user_id, push)
    try:
        from core.services.live_events import get_live_event_publisher
        from core.services.messaging import make_live_event

        await get_live_event_publisher().publish_user(
            owner_id, user_id, make_live_event("digest.ready", digest_ready_payload(counts))
        )
    except Exception:  # noqa: BLE001 -- desktop is best-effort; push and email already went
        logger.warning("digest: digest.ready publish failed user=%s", user_id, exc_info=True)
    put_metric("digest.sent")
    logger.info("digest: sent user=%s owner=%s date=%s total=%d", user_id, owner_id, local_date, sum(counts.values()))
    return True


async def run_once(now: datetime | None = None) -> int:
    """One scan: every due human in every active workspace they joined."""
    from routers.teams import agents as teams_agents

    now = now or datetime.now(timezone.utc)
    due = {}
    for row in await user_repo.list_all():
        local_date = due_local_date(row, now)
        if local_date:
            due[row["user_id"]] = (row, local_date)
    if not due:
        return 0
    sent = 0
    # ponytail: sequential, a few seconds per due human at worst (Paperclip budget);
    # fan out with a semaphore if one 08:00 hour ever holds thousands of humans.
    for owner_id, members in (await teams_agents._repo().list_active_workspace_members()).items():
        for user_id in sorted(members & due.keys()):
            row, local_date = due[user_id]
            try:
                sent += await digest_one(owner_id, row, local_date)
            except Exception:  # noqa: BLE001 -- one human never costs the rest of the pass
                put_metric("digest.skipped", dimensions={"reason": "error"})
                logger.warning("digest: failed user=%s owner=%s", user_id, owner_id, exc_info=True)
    return sent


async def run_digest_sweeper(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            if await container_repo.try_claim_pass(PASS_NAME, interval_s=PASS_SECONDS):
                await run_once()
            from core.services import week4_active  # T23: daily, its own pass, same loop

            if await container_repo.try_claim_pass(week4_active.PASS_NAME, interval_s=week4_active.PASS_SECONDS):
                await week4_active.run_once()
        except Exception:  # noqa: BLE001
            logger.warning("daily digest pass failed", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=_TICK_SECONDS)
        except asyncio.TimeoutError:
            pass
