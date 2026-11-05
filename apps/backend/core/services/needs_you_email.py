"""The away email (T15): a DM, @mention or approval left unseen five minutes while the
person is away gets one short email linking straight to it.

Pending checks are rows on the messaging table's sparse deadline index
(``sweep = "away"``, the run ledger's ``by-run-deadline`` GSI), keyed
``AWAY#<user> / M#<room>#<message>`` or ``A#<company>#<approval>``:

- the messaging projector writes one per DM and @mention it notifies (not muted);
- ``queue_approval`` writes one per workspace member when Paperclip creates an approval.

Every minute one backend task (pass admission) reads the due rows and, per row:
drops it if it was seen (read cursor past the message; approval answered), if the
person turned the email off, or in their quiet hours (22:00-08:00 local, D8); defers it
while they are in the app (``users.last_active_at`` within five minutes; AppSync has no
disconnect, so the signal is a room read or the visible-app ping ``POST /users/me/active``)
up to an hour; and
sends at most one email per (person, room) per 30 minutes (D8). Invited members are
included (D9). Same send rules as the digest: non-prod mails only test inboxes.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import httpx
import jwt as pyjwt
from botocore.exceptions import ClientError

from core.config import settings
from core.dynamodb import run_in_thread
from core.observability.metrics import put_metric
from core.observability.product_analytics import capture_product_event
from core.repositories import container_repo, messaging_repo, user_repo
from core.services import email_service, webhook_dedup
from core.services.daily_digest import _email_allowed

logger = logging.getLogger(__name__)

PASS_NAME = "needs_you_email"
_TICK_SECONDS = 60
DELAY_S = 300  # D9: five minutes unseen
PRESENT_S = 300  # active this recently = in the app
GIVE_UP_S = 3600  # still in the app an hour later: they have seen the badge
PER_ROOM_S = 1800  # D8: at most one per room per 30 minutes
QUIET_FROM, QUIET_UNTIL = 22, 8  # D8: local hours
ROW_TTL_S = 86400
UNSUBSCRIBE_KIND = "away_email_unsubscribe"
UNSUBSCRIBE_TTL = timedelta(days=30)
_NS = uuid.uuid5(uuid.NAMESPACE_URL, "lightbulb:needs_you_email_sent")


# ---------------------------------------------------------------------------
# unsubscribe token: its own kind, so it can only turn this email off
# ---------------------------------------------------------------------------


def mint_unsubscribe_token(user_id: str, *, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    claims = {"sub": user_id, "kind": UNSUBSCRIBE_KIND, "iat": now, "exp": now + UNSUBSCRIBE_TTL}
    return pyjwt.encode(claims, settings.PAPERCLIP_SERVICE_TOKEN_KEY, algorithm="HS256")


def verify_unsubscribe_token(token: str) -> str | None:
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
    return f"{settings.PUBLIC_API_BASE.rstrip('/')}/api/v1/users/away-email/unsubscribe?token={token}"


# ---------------------------------------------------------------------------
# queueing
# ---------------------------------------------------------------------------


async def queue_approval(*, company_id: str, approval_id: str, user_ids: list[str], title: str, body: str) -> None:
    """One pending check per member for a new approval. ``title``/``body`` are the push copy
    (``"Growth · LB-12"``, ``"Needs your approval: Launch email"``). Never raises."""
    try:
        from core.repositories.paperclip_repo import PaperclipRepo

        ws = await PaperclipRepo(table_name="paperclip-companies").find_workspace_by_company(company_id)
        owner_id = ws.org_id if ws is not None else ""
        await _queue(
            user_ids,
            f"A#{company_id}#{approval_id}",
            {"kind": "approval", "ownerId": owner_id, "approvalId": approval_id, "title": title, "body": body},
        )
    except Exception:
        logger.warning("needs_you_email: approval queue failed company=%s", company_id, exc_info=True)


async def queue_question(ws: str, task_id: str, qid: str, user_ids: list[str], title: str, body: str) -> None:
    """One pending check per answerer of a super-agent Question (contracts-v2 §14.5); the sweep drops it once the
    Question is no longer open. Never raises."""
    try:
        await _queue(
            user_ids,
            f"Q#{task_id}#{qid}",
            {"kind": "task_question", "ownerId": ws, "taskId": task_id, "qid": qid, "title": title, "body": body},
        )
    except Exception:
        logger.warning("needs_you_email: question queue failed task=%s qid=%s", task_id, qid, exc_info=True)


async def _queue(user_ids: list[str], sk: str, fields: dict) -> None:
    now = int(time.time())
    table = messaging_repo._table()
    for user_id in user_ids:
        item = {
            "pk": f"AWAY#{user_id}",
            "sk": sk,
            "sweep": "away",
            "deadlineAt": now + DELAY_S,
            **fields,
            "userId": user_id,
            "title": fields["title"][:200],
            "body": fields["body"][:300],
            "createdAt": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "expiresAt": now + ROW_TTL_S,
        }
        try:
            await run_in_thread(table.put_item, Item=item, ConditionExpression="attribute_not_exists(pk)")
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _link(params: dict, sent: int) -> str:
    query = urlencode({**params, "from": "away_email", "sent": str(sent)})
    return f"{settings.FRONTEND_URL.rstrip('/')}/chat?{query}"


def _where(room: dict, author: str = "") -> str:
    kind, name = room.get("kind"), room.get("name")
    if kind == "dm":
        return f"your DM with {author}" if author else "your DM"
    if kind == "group-dm":
        return name or "your group chat"
    return f"#{name}" if name else "a channel"


def render(row: dict, *, author: str = "", room: dict | None = None, sent: int) -> dict:
    """``{subject, lines, url}``: a notification of WHERE something waits, never what it says (owner ruling
    2026-10-19). The message and the approval stay in Lightbulb; the email comes from Lightbulb, never in an
    agent's voice: an agent that wants to reach someone by email uses its own address."""
    if row["kind"] == "task_question":
        url = _link({"panel": "task", "taskId": row["taskId"]}, sent)
        return {
            "subject": "A decision is waiting in Lightbulb",
            "lines": ["Lightbulb is waiting on your answer to a task.", f"Open it: {url}"],
            "url": url,
        }
    if row["kind"] == "approval":
        who = (row.get("title") or "").split(" · ")[0] or "An agent"
        url = _link({"panel": "teams-approval-detail", "detailId": row["approvalId"]}, sent)
        return {
            "subject": "An approval is waiting in Lightbulb",
            "lines": [f"{who} is waiting on your answer in Lightbulb.", f"Open it: {url}"],
            "url": url,
        }
    where = _where(room or {}, author)
    url = _link({"panel": "chat", "room": row["roomId"], "message": row["messageId"]}, sent)
    if row["kind"] == "mention":
        subject, line = "You were mentioned in Lightbulb", f"{author or 'Someone'} mentioned you in {where}."
    else:
        subject, line = "You have unread messages in Lightbulb", f"New messages are waiting for you in {where}."
    return {"subject": subject, "lines": [line, f"Open Lightbulb: {url}"], "url": url}


def _email_parts(content: dict, unsubscribe: str) -> tuple[str, str, dict[str, str]]:
    text = "\n".join(content["lines"]) + f"\n\nTurn these off: {unsubscribe}\n"
    e = html.escape
    body_lines = "".join(f"<p>{e(line)}</p>" for line in content["lines"][:-1])
    body = (
        f'<div style="font-family:sans-serif;max-width:520px">{body_lines}'
        f'<p><a href="{e(content["url"])}">Open it</a></p>'
        f'<p style="color:#888;font-size:12px"><a href="{e(unsubscribe)}">Turn these off</a></p></div>'
    )
    headers = {"List-Unsubscribe": f"<{unsubscribe}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
    return text, body, headers


# ---------------------------------------------------------------------------
# decisions
# ---------------------------------------------------------------------------


def in_quiet_hours(user_row: dict | None, now: datetime) -> bool:
    hour = now.astimezone(ZoneInfo(user_repo.timezone_of(user_row))).hour
    return hour >= QUIET_FROM or hour < QUIET_UNTIL


def is_present(user_row: dict | None, now_epoch: int) -> bool:
    try:
        return int((user_row or {}).get("last_active_at") or 0) >= now_epoch - PRESENT_S
    except (TypeError, ValueError):
        return False


async def _claim_room_slot(user_id: str, slot: str, now_epoch: int) -> bool:
    """At most one email per (person, room) per 30 minutes. DynamoDB TTL deletion lags, so
    the condition reads the expiry itself."""
    try:
        await run_in_thread(
            webhook_dedup._table().put_item,
            Item={
                "event_id": f"away-slot:{user_id}:{slot}",
                "source": "needs_you_email",
                "recorded_at": now_epoch,
                "ttl": now_epoch + PER_ROOM_S,
            },
            ConditionExpression="attribute_not_exists(event_id) OR #ttl <= :now",
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={":now": now_epoch},
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


async def _approval_pending(approval_id: str) -> bool:
    from core.services.paperclip_admin_client import PaperclipAdminClient
    from core.services.paperclip_admin_session import get_admin_session_cookie

    async with httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=5.0) as http:
        admin = PaperclipAdminClient(http_client=http)
        cookie = await get_admin_session_cookie(http)
        approval = await admin.get_approval(session_cookie=cookie, approval_id=approval_id)
    return isinstance(approval, dict) and approval.get("status") == "pending"


async def _still_in_workspace(owner_id: str, user_id: str) -> bool:
    from core.repositories.paperclip_repo import PaperclipRepo

    ws = (
        await PaperclipRepo(table_name="paperclip-companies").get_workspace(owner_id, consistent_read=True)
        if owner_id
        else None
    )
    return ws is not None and user_id in (ws.member_user_ids or set())


async def _done(row: dict, reason: str) -> None:
    await messaging_repo.finish_away_check(row["pk"], row["sk"])
    put_metric("away_email.outcome", dimensions={"outcome": reason, "kind": row.get("kind") or "unknown"})


async def check_one(row: dict, now: datetime) -> bool:
    """Decide and maybe send one pending check. ``True`` when an email went out."""
    now_epoch = int(now.timestamp())
    user_id, kind = row["userId"], row.get("kind")
    message: dict | None = None
    room: dict | None = None
    if kind == "task_question":
        from core.repositories import questions_repo

        q = await questions_repo.get_question(row["ownerId"], row["taskId"], row["qid"])
        if q is None or q["state"] != "open" or user_id not in q.get("answerers", []):
            await _done(row, "seen")  # answered, cancelled, or no longer theirs to answer
            return False
    elif kind == "approval":
        if not await _still_in_workspace(row.get("ownerId") or "", user_id):
            await _done(row, "not_member")
            return False
        if not await _approval_pending(row["approvalId"]):
            await _done(row, "seen")
            return False
    else:
        room = await messaging_repo.get_room(row["roomId"])
        if room is None or room.get("deletedAt"):
            await _done(row, "gone")  # a deleted room keeps its rows; its content is not readable
            return False
        if await messaging_repo.get_read_cursor(row["roomId"], user_id) >= int(row["seq"]):
            await _done(row, "seen")
            return False
        # Removed from the room in the meantime: its messages are no longer theirs to read.
        if await messaging_repo.get_member(row["roomId"], "human", user_id) is None:
            await _done(row, "not_member")
            return False
        message = await messaging_repo.get_message_by_id(row["roomId"], row["messageId"])
        if message is None or message.get("deletedAt"):
            await _done(row, "gone")
            return False
    user_row = await user_repo.get(user_id)
    email = (user_row or {}).get("email")
    if not user_repo.away_email_on(user_row):
        await _done(row, "off")
        return False
    if not email or not _email_allowed(email):
        await _done(row, "no_email")
        return False
    if in_quiet_hours(user_row, now):
        await _done(row, "quiet_hours")
        return False
    created = datetime.fromisoformat(str(row["createdAt"]))
    if is_present(user_row, now_epoch):
        if (now - created).total_seconds() >= GIVE_UP_S:
            await _done(row, "present")
        else:
            await messaging_repo.defer_away_check(row["pk"], row["sk"], now_epoch + PRESENT_S)
        return False
    slot = "approvals" if kind in ("approval", "task_question") else row["roomId"]
    if not await _claim_room_slot(user_id, slot, now_epoch):
        await _done(row, "room_cap")
        return False
    author = ((message or {}).get("author") or {}).get("displayName") or "Someone"
    content = render(row, author=author, room=room, sent=now_epoch)
    text, body, headers = _email_parts(content, unsubscribe_url(user_id))
    message_id = await email_service.send_email(
        to=email,
        subject=content["subject"],
        html=body,
        text=text,
        from_addr=settings.EMAIL_FROM_WELCOME,
        kind="away_email",
        headers=headers,
    )
    if not message_id:
        # SES down or unconfigured (send_email returns None, never raises): give the room slot
        # back and try again next pass.
        await run_in_thread(webhook_dedup._table().delete_item, Key={"event_id": f"away-slot:{user_id}:{slot}"})
        await messaging_repo.defer_away_check(row["pk"], row["sk"], now_epoch + _TICK_SECONDS)
        put_metric("away_email.outcome", dimensions={"outcome": "send_failed", "kind": kind or "unknown"})
        return False
    await _done(row, "sent")
    capture_product_event(
        distinct_id=user_id,
        event="needs_you_email_sent",
        properties={
            "workspace_id": row.get("ownerId") or None,
            "kind": kind,
            "delay_s": int((now - created).total_seconds()),
        },
        event_uuid=str(uuid.uuid5(_NS, f"{row['pk']}:{row['sk']}")),
    )
    return True


async def run_once(now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    sent = 0
    for key in await messaging_repo.list_due_away_checks(int(now.timestamp())):
        try:
            row = await messaging_repo.get_away_check(key["pk"], key["sk"])
            if row is None or row.get("sweep") != "away":
                continue
            sent += await check_one(row, now)
        except Exception:  # noqa: BLE001 -- one row never costs the rest of the pass
            put_metric("away_email.outcome", dimensions={"outcome": "error", "kind": "unknown"})
            logger.warning("needs_you_email: check failed key=%s", key, exc_info=True)
    return sent


async def run_needs_you_sweeper(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            if await container_repo.try_claim_pass(PASS_NAME, interval_s=_TICK_SECONDS):
                await run_once()
        except Exception:  # noqa: BLE001
            logger.warning("needs_you_email pass failed", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=_TICK_SECONDS)
        except asyncio.TimeoutError:
            pass
