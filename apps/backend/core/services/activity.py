"""Activity feed v1 (rooms F5): what needs *you*, server-rendered and actionable.

Two sources, one item shape:

* **Message items** (``mention`` / ``dm`` / ``thread_reply``) are per-reader pointer
  rows (``ACTIVITY#<home>#<user>``) written by the messaging projector -- the
  existing stream consumer -- after the message commits. Rows carry no text. The
  read path here re-gates every room on a strong membership read, drops deleted
  messages, and renders title/preview from the CURRENT message, so leaving a room
  or deleting a message leaves nothing stale or leaked behind.
* **Paperclip items** (``paperclip`` with ``subtype`` ``approval`` /
  ``failed_run`` / ``assigned_issue``) are live state, pulled at read time with
  the viewer's own Paperclip session and merged into the FIRST page only. A
  Paperclip failure never fails the feed: ``sources.paperclip`` says ``unavailable``.

"Needs you" (stock Paperclip's inbox badge: actionable approvals + each agent's
latest run when it failed) is pinned above everything else on page 1, and
``attentionCount`` -- the rail badge -- is those plus unseen mentions, DMs and Paperclip items. Every
pending approval is listed: the Activity v1 read went through the viewer's
attention feed capped at 20 and sorted by recent activity, so older approvals
fell off behind newer failed runs and blockers and never showed at all.

Every item carries a server-rendered ``summary`` and the viewer's ``actions``;
clients render both generically. One action route maps an action id to the real
call (the Teams approvals BFF, the canonical message writer, an issue patch).

Raw Paperclip type keys (``hire_agent``, ``distribution.public-post``, ...) never
reach the client: renderers are keyed by them, and every payload string is
scrubbed of them before it is shown.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal

from pydantic import Field

from core.auth import AuthContext, resolve_owner_id
from core.repositories import messaging_repo
from core.services import messaging
from core.services.agent_redaction import strip_agent_secrets
from core.services.messaging import ApiProblem, ContractModel

logger = logging.getLogger(__name__)

# ``reaction`` and ``agent_run`` arrive in a later PR (new writers, same shape).
ActivityKind = Literal[
    "mention",
    "dm",
    "thread_reply",
    "reaction",
    "agent_run",
    "paperclip",
    "session",
    "login",
    "extension",
    "reconnect",
    "task_question",
]
ActorKind = Literal["human", "agent", "system"]
LinkKind = Literal["issue", "room", "thread", "run", "approval", "session", "task"]

DEFAULT_LIMIT = 30
MAX_LIMIT = 100
UNSEEN_CAP = 99
PAPERCLIP_LIMIT = 20
# Stock Paperclip's inbox badge reads this many recent runs to find each agent's latest.
RUN_SCAN_LIMIT = 200
_FAILED_RUN_STATUSES = ("failed", "timed_out")
# Page 1 never waits on Paperclip longer than this (the admin client's own timeout is 15 s).
PAPERCLIP_BUDGET_S = 3.0
TITLE_CHARS = 120
PREVIEW_TEXT_CHARS = 600
FIELD_CHARS = 200
MAX_FIELDS = 8
_MESSAGE_KINDS = ("mention", "dm", "thread_reply")
# The ones the Activity badge counts (owner 2026-10-15: needs you, mentions and DMs).
_BADGE_MESSAGE_KINDS = ("mention", "dm")


# ---------------------------------------------------------------------------
# contract
# ---------------------------------------------------------------------------


class ActivityActor(ContractModel):
    kind: ActorKind
    id: str
    name: str


class ActivityLink(ContractModel):
    kind: LinkKind
    id: str
    label: str


class ActivityField(ContractModel):
    label: str
    value: str


class ActivityPreview(ContractModel):
    text: str | None = None
    destination: str | None = None
    fields: list[ActivityField] = Field(default_factory=list, max_length=MAX_FIELDS)


class ActivitySummary(ContractModel):
    title: str
    requester: ActivityActor
    reason: str | None = None
    preview: ActivityPreview | None = None
    links: list[ActivityLink] = Field(default_factory=list)


class ActionOption(ContractModel):
    value: str
    label: str


class ActionInput(ContractModel):
    # ``login`` (username + password): a secret form whose answer goes straight to the
    # website-logins store; the client posts it as ``value`` only, never as a note.
    kind: Literal["text", "select", "person", "login"]
    label: str
    required: bool
    options: list[ActionOption] | None = None
    # ``login`` only: a line shown above the fields (e.g. the site's connector is already
    # connected, so this password is only for the browser).
    hint: str | None = None


class ActionNavigate(ContractModel):
    kind: LinkKind
    id: str
    room_id: str | None = None


class ActivityAction(ContractModel):
    id: str
    label: str
    style: Literal["primary", "destructive"] | None = None
    confirm: str | None = None
    input: ActionInput | None = None
    navigate: ActionNavigate | None = None


class ActivityItem(ContractModel):
    id: str
    kind: ActivityKind
    # ``session`` items: ``waiting`` (on its owner's input), ``finished`` (a turn ended) or
    # ``join_request`` (a teammate asked to send to it; Allow / Decline).
    # ``login`` items: ``login`` (an agent needs a website login). A sign-in code is typed into the
    # shared browser, never asked for here.
    subtype: (
        Literal[
            "approval",
            "assigned_issue",
            "failed_run",
            "interaction",
            "waiting",
            "finished",
            "join_request",
            "login",
            "extension",
            "reconnect",
        ]
        | None
    ) = None
    room_id: str | None = None
    message_id: str | None = None
    thread_root_message_id: str | None = None
    # Message items: the room's kind, so a client can say "in a DM" without parsing labels.
    room_kind: Literal["channel", "dm", "group-dm"] | None = None
    actor: ActivityActor
    created_at: datetime
    state: Literal["open", "done", "resolved"]
    resolved_by: ActivityActor | None = None
    resolved_at: datetime | None = None
    read: bool
    summary: ActivitySummary
    actions: list[ActivityAction]


class ActivitySources(ContractModel):
    paperclip: Literal["ok", "unavailable"]


class ActivityPage(ContractModel):
    items: list[ActivityItem]
    next: str | None
    # First page only (None after it): items newer than the seen cursor, capped at 99.
    unseen_count: int | None
    # First page only: every "needs you" item (see ``needs_you``) plus unseen mentions, DMs and
    # Paperclip items, capped at 99. Thread replies never count.
    attention_count: int | None = None
    sources: ActivitySources


class ActivitySeenRequest(ContractModel):
    up_to: datetime


class ActivitySeenResponse(ContractModel):
    seen_up_to: datetime | None


class ActivityActionRequest(ContractModel):
    note: str | None = Field(default=None, max_length=2000)
    value: str | None = Field(default=None, max_length=20_000)
    # A fresh UUID per tap: a retried tap reuses it (posts once), a second deliberate
    # identical reply sends a new one. Absent, the reply key falls back to the text.
    idempotency_key: uuid.UUID | None = None


TeamsCtx = Callable[[], Awaitable[Any]]


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _encode_cursor(sk: str) -> str:
    return base64.urlsafe_b64encode(sk.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> str:
    try:
        sk = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        sk = ""
    # Pointer sks are an ISO timestamp and an id; anything else is not a cursor we minted.
    # The id is a message's uuid, or a kind and an id for the rows that are not messages
    # (``login:``, ``extension:``, ``session:``). Accepting the bare uuid alone refused
    # our own cursor whenever a page ended on one of those.
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T[^#]+#(?:[a-z]+:)?[0-9a-f-]{32,36}", sk):
        raise ApiProblem("INVALID_REQUEST", "Invalid activity cursor.")
    return sk


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return value if isinstance(value, str) and value else None


def _when(value: Any) -> datetime:
    """A Paperclip or DynamoDB timestamp as an aware datetime (epoch when absent)."""
    iso = _iso(value)
    if iso:
        try:
            parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.fromtimestamp(0, tz=timezone.utc)


def _newer_than(created: datetime, seen: str | None) -> bool:
    return seen is None or created > _when(seen)


# ---------------------------------------------------------------------------
# message items
# ---------------------------------------------------------------------------


def _where(meta: dict) -> str:
    kind, name = meta.get("kind"), meta.get("name")
    if kind == "channel":
        return name or "a project"
    if kind == "group-dm":
        return name or "a group chat"
    return "your chat"


def _message_title(kind: str, actor: str, meta: dict) -> str:
    if kind == "dm":
        if meta.get("kind") == "group-dm":
            return f"{actor} messaged {meta.get('name') or 'your group chat'}"
        return f"{actor} sent you a direct message"
    if kind == "mention":
        return f"{actor} mentioned you in {_where(meta)}"
    return f"{actor} replied in a thread in {_where(meta)}"


def _message_preview(msg: dict) -> str:
    text = msg.get("editedText") if int(msg.get("messageRevision", 0)) else msg.get("text")
    plain = messaging_repo._plain_preview(text or "")
    if not plain and msg.get("attachments"):
        return "Sent an attachment"
    return _clip(plain, PREVIEW_TEXT_CHARS)


def _message_item_id(kind: str, row: dict) -> str:
    return f"{kind}:{row['messageId']}:{row['roomId']}:{int(row['seq'])}"


def _render_message_item(
    row: dict, meta: dict, member: dict, read_row: dict | None, msg: dict, seen: str | None
) -> ActivityItem:
    kind = row["kind"]
    author = msg["author"]
    actor = ActivityActor(kind=author["kind"], id=author["id"], name=author.get("displayName") or "Someone")
    room_id, message_id = row["roomId"], row["messageId"]
    root = msg.get("threadRootMessageId")
    created = _when(row["createdAt"])
    done_at = row.get("doneAt")
    floor = messaging_repo.read_floor(member, read_row)
    # Read = opened: done, or the room's read cursor passed it (opening the room or thread moves
    # it). Viewing Activity ("seen") clears the badge, never the dot.
    read = bool(done_at) or int(row["seq"]) <= floor
    links = [ActivityLink(kind="room", id=room_id, label=_where(meta))]
    if root:
        links.append(ActivityLink(kind="thread", id=root, label="Thread"))
    # Where "open" lands: the thread when there is one; a channel mention opens (and
    # replies into) a thread on the message itself; a DM opens the conversation.
    thread_id = root or (message_id if meta.get("kind") == "channel" else None)
    navigate = (
        ActionNavigate(kind="thread", id=thread_id, room_id=room_id)
        if thread_id
        else ActionNavigate(kind="room", id=room_id, room_id=room_id)
    )
    reply = ActivityAction(
        id="reply",
        label="Reply",
        style="primary",
        input=ActionInput(kind="text", label=f"Reply to {actor.name}", required=True),
    )
    actions = [reply]
    if kind == "mention" and not done_at:
        actions.append(ActivityAction(id="mark_done", label="Mark done"))
    actions.append(
        ActivityAction(id="open", label="Open thread" if thread_id else "Open conversation", navigate=navigate)
    )
    return ActivityItem(
        id=_message_item_id(kind, row),
        kind=kind,
        room_id=room_id,
        message_id=message_id,
        thread_root_message_id=root,
        room_kind=meta.get("kind") if meta.get("kind") in ("channel", "dm", "group-dm") else None,
        actor=actor,
        created_at=created,
        state="done" if done_at else "open",
        resolved_at=_when(done_at) if done_at else None,
        read=read,
        summary=ActivitySummary(
            title=_clip(_message_title(kind, actor.name, meta), TITLE_CHARS),
            requester=actor,
            preview=ActivityPreview(text=_message_preview(msg)),
            links=links,
        ),
        actions=actions,
    )


async def _room_gates(owner_id: str, user_id: str, room_ids: set[str]) -> dict[str, tuple[dict, dict]]:
    """``{room: (meta, member)}`` for the rooms the caller is STILL in (strong reads)."""
    keys = []
    for room_id in room_ids:
        keys += [
            (messaging_repo.room_pk(room_id), "META"),
            (messaging_repo.room_pk(room_id), messaging_repo.member_sk("human", user_id)),
        ]
    rows = await messaging_repo.batch_get(keys)
    gates = {}
    for room_id in room_ids:
        meta = rows.get((messaging_repo.room_pk(room_id), "META"))
        member = rows.get((messaging_repo.room_pk(room_id), messaging_repo.member_sk("human", user_id)))
        if not meta or meta.get("deletedAt") or not member:
            continue
        if messaging_repo.member_home(member, meta["ownerId"]) != owner_id:
            continue
        gates[room_id] = (meta, member)
    return gates


async def _render_message_rows(
    owner_id: str, user_id: str, rows: list[dict], seen: str | None, gates: dict[str, tuple[dict, dict]] | None = None
) -> list[ActivityItem]:
    rows = [row for row in rows if row.get("kind") in _MESSAGE_KINDS]
    if not rows:
        return []
    if gates is None:
        gates = await _room_gates(owner_id, user_id, {row["roomId"] for row in rows})
    visible = [row for row in rows if row["roomId"] in gates]
    keys = [(messaging_repo.room_pk(r["roomId"]), messaging_repo.msg_sk(int(r["seq"]))) for r in visible]
    keys += [
        (messaging_repo.room_pk(room_id), messaging_repo.read_sk(user_id)) for room_id in {r["roomId"] for r in visible}
    ]
    fetched = await messaging_repo.batch_get(keys)
    items = []
    for row in visible:
        room_pk = messaging_repo.room_pk(row["roomId"])
        msg = fetched.get((room_pk, messaging_repo.msg_sk(int(row["seq"]))))
        if not msg or msg.get("messageId") != row["messageId"] or msg.get("deletedAt"):
            continue  # a deleted message leaves the feed with its text
        meta, member = gates[row["roomId"]]
        items.append(
            _render_message_item(row, meta, member, fetched.get((room_pk, messaging_repo.read_sk(user_id))), msg, seen)
        )
    return items


async def _render_session_rows(owner_id: str, user_id: str, rows: list[dict], seen: str | None) -> list[ActivityItem]:
    """The relay's pointer rows for the viewer's OWN shared sessions (local_session_relay.
    notify_attention), rendered from the session's current row: a waiting item stays open
    only while the session is still waiting, so a stale one never holds the badge."""
    from core.repositories import local_sessions_repo
    from core.services.local_session_relay import attention_copy
    from core.services.local_sessions import _session_public

    rows = [row for row in rows if row.get("kind") == "session"]
    sessions = await asyncio.gather(*(local_sessions_repo.get_session(owner_id, row["sessionId"]) for row in rows))
    items = []
    for row, session in zip(rows, sessions):
        if not session or session.get("ownerId") != owner_id or session.get("humanUserId") != user_id:
            continue
        sid, state = session["sessionId"], row.get("subtype")
        if state == "join_request" and row.get("request") == "agent_message":
            continue  # an agent's ask to message a session: agents message through the engine now
        if state == "join_request":
            items.append(render_join_request(row, session, seen=seen))
            continue
        if state not in ("waiting", "finished"):
            continue
        title, body = attention_copy(session, state, row.get("subagent"))
        created = _when(row["createdAt"])
        # Open only while THIS wait lasts: a later wait on the same session is its own item.
        still = state == "finished" or (
            _session_public(session).status == "waiting-approval"
            and session.get("waitingSince") == row.get("waitingSince")
        )
        tool = title.split(" · ", 1)[0]
        actor = ActivityActor(kind="system", id=session.get("provider") or "session", name=tool)
        items.append(
            ActivityItem(
                id=row["sk"].split("#", 1)[1],
                kind="session",
                subtype=state,
                actor=actor,
                created_at=created,
                state="open" if still else "resolved",
                read=not _newer_than(created, seen),
                summary=ActivitySummary(
                    title=_clip(title, TITLE_CHARS),
                    requester=actor,
                    preview=ActivityPreview(text=_clip(body, PREVIEW_TEXT_CHARS)),
                    links=[ActivityLink(kind="session", id=sid, label="Session")],
                ),
                actions=[
                    ActivityAction(id="open", label="Open session", navigate=ActionNavigate(kind="session", id=sid))
                ],
            )
        )
    return items


def join_item_id(session_id: str, requester: str, request_id: str) -> str:
    return f"join:{session_id}:{requester}:{request_id}"


def pickup_item_id(session_id: str, requester: str, request_id: str) -> str:
    return f"pickup:{session_id}:{requester}:{request_id}"


def render_join_request(row: dict, session: dict, *, seen: str | None) -> ActivityItem:
    """A teammate's ask to join one of the viewer's sessions (``local_sessions.request_join``),
    or to pick it up (``request_pickup_access``, ``request: "pickup"`` on the row), open while
    THAT ask is still pending on the session row."""
    from core.services.local_sessions import (
        OUTSIDER_ALLOW_COPY,
        PICKUP_ALLOW_COPY,
        join_request_title,
        pickup_request_title,
    )

    sid, requester = session["sessionId"], row["requesterId"]
    pickup = row.get("request") == "pickup"
    from core.services import session_pickup  # lazy: it imports this module

    current = (session.get("pickupRequests" if pickup else "joinRequests") or {}).get(requester) or {}
    open_ = (
        (
            not session.get("pickupAllowed") and not session_pickup._expired(session)
            if pickup
            else session.get("endedAt") is None
        )
        and current.get("state") == "pending"
        and current.get("requestId") == row["requestId"]
    )
    created = _when(row["createdAt"])
    actor = ActivityActor(kind="human", id=requester, name=str(row.get("requesterName") or "A teammate"))
    return ActivityItem(
        id=(pickup_item_id if pickup else join_item_id)(sid, requester, row["requestId"]),
        kind="session",
        subtype="join_request",
        actor=actor,
        created_at=created,
        state="open" if open_ else "resolved",
        read=not _newer_than(created, seen),
        summary=ActivitySummary(
            title=_clip((pickup_request_title if pickup else join_request_title)(actor.name, session), TITLE_CHARS),
            requester=actor,
            reason=PICKUP_ALLOW_COPY
            if pickup
            else OUTSIDER_ALLOW_COPY
            if current.get("outsider")
            else "Allow lets just them send follow-ups and answer prompts, until the session ends.",
            links=[ActivityLink(kind="session", id=sid, label="Session")],
        ),
        actions=[
            ActivityAction(id="allow", label="Allow", style="primary"),
            ActivityAction(id="decline", label="Decline"),
        ]
        if open_
        else [],
    )


async def _run_join_action(
    auth: AuthContext, session_id: str, requester: str, request_id: str, action_id: str, *, pickup: bool = False
) -> ActivityItem:
    """Allow / Decline from Activity: the same owner-only door as the session route."""
    from core.dynamodb import utc_now_iso
    from core.repositories import local_sessions_repo
    from core.services import local_sessions

    owner_id = resolve_owner_id(auth)

    async def current() -> ActivityItem:
        session = await local_sessions_repo.get_session(owner_id, session_id)
        if not session or session.get("ownerId") != owner_id or session.get("humanUserId") != auth.user_id:
            raise ApiProblem("ACTIVITY_NOT_FOUND", "Activity item not found.")
        entry = (session.get("pickupRequests" if pickup else "joinRequests") or {}).get(requester) or {}
        row = {
            "request": "pickup" if pickup else "join",
            "requesterId": requester,
            # An ask from the public page carries its own label (name and email): not a member yet.
            "requesterName": entry.get("name") or await messaging.human_display_name(requester),
            "requestId": request_id,
            "createdAt": entry.get("createdAt") or utc_now_iso(),
        }
        return render_join_request(row, session, seen=None)

    item = await current()
    if item.state != "open":
        return item  # already answered (a retried tap), or the session ended
    _require_action(item, action_id)
    decision = "allow" if action_id == "allow" else "decline"
    decide = local_sessions.decide_pickup_request if pickup else local_sessions.decide_join
    await decide(auth, session_id, requester, decision, request_id=request_id)
    return await current()


LOGIN_ADMIN_ONLY = "Only a workspace admin can add website sign-ins."


def render_login_request(row: dict, *, seen: str | None, admin: bool) -> ActivityItem:
    """An agent's ask for a website login (``website_logins.create_request``), rendered from the
    request's current row: open only while the agent is still waiting. (A sign-in code is typed
    into the shared browser and never becomes an item.)"""
    from core.services import website_logins

    request_id = row["requestId"]
    status = website_logins.request_status(row)
    open_ = status == "pending"
    created = _when(row.get("createdAt"))
    actor = ActivityActor(kind="agent", id=str(row.get("agentId") or ""), name=str(row.get("agentName") or "Agent"))
    site = website_logins.host_of(str(row.get("origin") or ""))
    reason = (
        f"Saved to the workspace's website sign-ins for {site}, so every agent can sign in there. "
        "The password never enters chat."
    )
    actions: list[ActivityAction] = []
    if open_ and admin:
        actions = [
            ActivityAction(
                id="provide",
                label="Add login",
                style="primary",
                input=ActionInput(kind="login", label=f"Login for {site}", required=True, hint=row.get("hint")),
            ),
            ActivityAction(id="decline", label="Not now"),
        ]
    links = [ActivityLink(kind="room", id=row["roomId"], label="Room")] if row.get("roomId") else []
    return ActivityItem(
        id=website_logins.activity_item_id(request_id),
        kind="login",
        subtype="login",
        room_id=row.get("roomId"),
        thread_root_message_id=row.get("threadRootMessageId"),
        actor=actor,
        created_at=created,
        state="open" if open_ else "resolved",
        resolved_at=None if open_ else _when(row.get("resolvedAt") or row.get("createdAt")),
        read=not _newer_than(created, seen),
        summary=ActivitySummary(
            title=_clip(website_logins.request_title(row), TITLE_CHARS),
            requester=actor,
            reason=reason if admin or not open_ else f"{reason} {LOGIN_ADMIN_ONLY}",
            links=links,
        ),
        actions=actions,
    )


async def _render_login_rows(owner_id: str, rows: list[dict], seen: str | None, admin: bool) -> list[ActivityItem]:
    from core.services import website_logins

    rows = [row for row in rows if row.get("kind") == "login"]
    found = await asyncio.gather(*(website_logins.get_request(owner_id, str(row.get("requestId"))) for row in rows))
    return [render_login_request(req, seen=seen, admin=admin) for req in found if req]


def render_extension_request(row: dict, *, seen: str | None) -> ActivityItem:
    """An agent's ask to add a Chrome Web Store extension to the shared browser
    (``browser_extensions.request_install``). Any member can accept it: the ask exists because
    the agent may not install on its own, not because members may not."""
    from core.services import browser_extensions

    open_ = row.get("status") == "pending"
    created = _when(row.get("createdAt"))
    actor = ActivityActor(kind="agent", id=str(row.get("agentId") or ""), name=str(row.get("agentName") or "Agent"))
    name = str(row.get("name") or "an extension")
    return ActivityItem(
        id=browser_extensions.activity_item_id(row["requestId"]),
        kind="extension",
        subtype="extension",
        actor=actor,
        created_at=created,
        state="open" if open_ else "resolved",
        resolved_at=None if open_ else _when(row.get("resolvedAt") or row.get("createdAt")),
        read=not _newer_than(created, seen),
        summary=ActivitySummary(
            title=_clip(f"{actor.name} wants to add {name} to the shared browser", TITLE_CHARS),
            requester=actor,
            reason=(
                "Extensions can read the pages the shared browser opens, and that browser holds the "
                "workspace's sign-ins. Add it only if you expect it. The browser restarts to load it."
            ),
            preview=ActivityPreview(destination=f"chromewebstore.google.com/detail/{row.get('extensionId')}"),
        ),
        actions=[ActivityAction(id="add", label="Add", style="primary"), ActivityAction(id="decline", label="Not now")]
        if open_
        else [],
    )


def reconnect_item_id(provider: str) -> str:
    # The cursor regex wants a 32-hex id after the kind, so the provider key is hashed.
    return f"reconnect:{hashlib.sha256(provider.encode()).hexdigest()[:32]}"


async def _render_reconnect_rows(owner_id: str, rows: list[dict], seen: str | None) -> list[ActivityItem]:
    """An integration's grant went ``reconnect_required`` (``access_token._mark_reconnect``).
    Open while the integration row still says so; a reconnect flips it back and the item
    disappears. A provider that failed twice has two rows: only the newest renders."""
    from core.repositories import integrations_repo
    from core.services.credentials import registry

    newest: dict[str, dict] = {}
    for row in rows:
        if row.get("kind") == "reconnect" and row.get("provider"):
            if row["provider"] not in newest or row["createdAt"] > newest[row["provider"]]["createdAt"]:
                newest[row["provider"]] = row
    items = []
    for provider, row in newest.items():
        if provider == "openai-codex":
            # ChatGPT login (spec 13): the grant lives on the oauth-tokens row, not an integrations
            # row, and the backend broker marks it reconnect_required when OpenAI refuses a refresh.
            from core.services import oauth_service

            connector = await oauth_service.resolve_token_owner(owner_id=owner_id)
            if not await oauth_service.token_row_needs_reconnect(connector, bound_to=owner_id):
                continue
            name = "ChatGPT"
        else:
            # consistent: the item is filed right after the flip, and a stale "connected" would hide it
            integration = await integrations_repo.get(owner_id, provider, consistent=True)
            if not integration or integration.get("status") != "reconnect_required":
                continue
            try:
                name = registry.get_provider(provider).display_name
            except KeyError:
                continue
        created = _when(row["createdAt"])
        actor = ActivityActor(kind="system", id="", name="Lightbulb")
        items.append(
            ActivityItem(
                id=reconnect_item_id(provider),
                kind="reconnect",
                subtype="reconnect",
                actor=actor,
                created_at=created,
                state="open",
                read=not _newer_than(created, seen),
                summary=ActivitySummary(
                    title=_clip(f"Reconnect {name}", TITLE_CHARS),
                    requester=actor,
                    reason=(
                        f"{name} stopped working (its sign-in expired or was revoked), so your agents "
                        "cannot use it. Open Settings, then Connections, and press Reconnect."
                    ),
                ),
                actions=[],
            )
        )
    return items


async def _render_extension_rows(owner_id: str, rows: list[dict], seen: str | None) -> list[ActivityItem]:
    from core.services import browser_extensions

    rows = [row for row in rows if row.get("kind") == "extension"]
    found = await asyncio.gather(*(browser_extensions.get_request(owner_id, str(row.get("requestId"))) for row in rows))
    return [render_extension_request(req, seen=seen) for req in found if req]


NETWORK_HOLD_REASON = (
    "Send delivers what they sent. Always allow also lets them, and their agents, reach it directly from now on."
)


def render_network_hold(row: dict, *, seen: str | None) -> ActivityItem:
    """A stranger's first message on the network, held for a person's Allow
    (``network_member.hold_notice``). The notice carries no text, so the item has no preview."""
    from core.services import network_member

    label = str(row.get("label") or "Someone on the network")
    target = str(row.get("target") or "")
    app = network_member.app_target(target)
    open_ = row.get("state") == "open"
    created = _when(row["createdAt"])
    actor = ActivityActor(kind="agent", id="", name=label)
    return ActivityItem(
        id=network_member.hold_item_id(row["holdId"]),
        kind="session",
        subtype="join_request",
        actor=actor,
        created_at=created,
        state="open" if open_ else "resolved",
        resolved_at=None if open_ else _when(row.get("resolvedAt") or row["createdAt"]),
        read=not _newer_than(created, seen),
        summary=ActivitySummary(
            title=_clip(f"{label} wants to message {f'your {app[0]}' if app else target}", TITLE_CHARS),
            requester=actor,
            reason=NETWORK_HOLD_REASON,
            preview=None,
            links=[],
        ),
        actions=[
            # Safe first, the decision card's verbs (UX audit slice 2): Deny, not Decline.
            ActivityAction(id="decline", label="Deny"),
            ActivityAction(id="send", label="Send", style="primary"),
            ActivityAction(id="allow", label="Always allow"),
        ]
        if open_
        else [],
    )


async def _render_network_hold_rows(owner_id: str, rows: list[dict], seen: str | None) -> list[ActivityItem]:
    from core.services import network_member

    rows = [row for row in rows if row.get("kind") == "network_hold"]
    found = await asyncio.gather(*(network_member.get_hold(owner_id, str(row.get("holdId"))) for row in rows))
    return [render_network_hold(hold, seen=seen) for hold in found if hold]


async def _run_network_hold_action(auth: AuthContext, hold_id: str, action_id: str) -> ActivityItem:
    """Deny / Send / Always allow, for a person the hold was filed to."""
    from core.services import network_member

    owner_id = resolve_owner_id(auth)

    async def current() -> ActivityItem:
        hold = await network_member.get_hold(owner_id, hold_id)
        if not hold or auth.user_id not in (hold.get("askedUserIds") or []):
            raise ApiProblem("ACTIVITY_NOT_FOUND", "Activity item not found.")
        return render_network_hold(hold, seen=None)

    item = await current()
    if item.state != "open":
        return item  # already answered (a retried tap, another person, or the engine closed it)
    _require_action(item, action_id)
    await network_member.decide_hold(auth, hold_id, action_id)
    return await current()


async def _render_rows(
    owner_id: str,
    user_id: str,
    rows: list[dict],
    seen: str | None,
    gates: dict[str, tuple[dict, dict]] | None = None,
    *,
    admin: bool = False,
) -> list[ActivityItem]:
    messages, sessions, logins, extensions, reconnects, holds = await asyncio.gather(
        _render_message_rows(owner_id, user_id, rows, seen, gates),
        _render_session_rows(owner_id, user_id, rows, seen),
        _render_login_rows(owner_id, rows, seen, admin),
        _render_extension_rows(owner_id, rows, seen),
        _render_reconnect_rows(owner_id, rows, seen),
        _render_network_hold_rows(owner_id, rows, seen),
    )
    return sorted(
        messages + sessions + logins + extensions + reconnects + holds,
        key=lambda i: i.created_at,
        reverse=True,
    )


# ---------------------------------------------------------------------------
# Paperclip rendering
# ---------------------------------------------------------------------------

# Every approval type upstream validates, plus the discriminators our own cards and
# agents file under ``request_board_approval``. Scrubbed out of any shown string.
_KNOWN_TYPE_KEYS = frozenset(
    {
        "hire_agent",
        "approve_ceo_strategy",
        "budget_override_required",
        "request_board_approval",
        "distribution.public-post",
    }
)
PUBLIC_POST = "distribution.public-post"
_DISCRIMINATOR_KEYS = ("ruleKey", "action", "kind", "type", "category")
TYPE_KEY_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*[._][a-z0-9]+(?:[._-][a-z0-9]+)*$")
_SECRET_KEY_RE = re.compile(r"(key|secret|token|password|passwd|bearer|authorization|cookie|credential|session)", re.I)
_HIDDEN_FIELDS = frozenset(
    {
        "isol8_action",
        *_DISCRIMINATOR_KEYS,
        "adapterConfig",
        "adapterType",
        "title",
        "summary",
        "recommendedAction",
        "reason",
    }
)

# Lightbulb's own cards ride ``request_board_approval`` with ``payload.isol8_action``;
# each has its own flow in Teams, so Activity only opens them (see board_cards.py).
_LIGHTBULB_CARDS = {
    "company_stripe": "Connect Stripe",
    "provider_consent": "Approve a provider",
    "provider_connect": "Connect a provider account",
    "provider_information": "A provider needs information",
    "payment_setup": "Add a card",
}

ISSUE_STATUS_LABELS = {
    "backlog": "Backlog",
    "todo": "To do",
    "in_progress": "In progress",
    "in_review": "In review",
    "blocked": "Blocked",
    "done": "Done",
    "cancelled": "Cancelled",
}
_OPEN_ISSUE_STATUSES = ("todo", "in_progress", "in_review", "blocked")
_CHANGEABLE_STATUSES = ("todo", "in_progress", "in_review", "blocked", "done", "cancelled")


def _humanize(key: str) -> str:
    spaced = re.sub(r"[._-]+", " ", re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", key)).strip()
    return spaced[:1].upper() + spaced[1:].lower()


def _scrub(text: str, raw_keys: set[str]) -> str:
    for key in sorted(raw_keys, key=len, reverse=True):
        if key and key in text:
            text = text.replace(key, _humanize(key).lower())
    return text


class _Payload:
    """Read strings off an untrusted, agent-authored payload -- plain, bounded,
    and never a bare type key."""

    def __init__(self, payload: Any, raw_keys: set[str]):
        self.data = strip_agent_secrets(payload) if isinstance(payload, dict) else {}
        self.raw_keys = raw_keys

    def text(self, *keys: str, limit: int = PREVIEW_TEXT_CHARS) -> str | None:
        for key in keys:
            value = self.data.get(key)
            if isinstance(value, list):
                value = ", ".join(str(v).strip() for v in value if isinstance(v, (str, int, float)) and str(v).strip())
            if not isinstance(value, str):
                continue
            plain = messaging_repo._plain_preview(value)
            if not plain or TYPE_KEY_RE.match(plain):
                continue
            return _clip(_scrub(plain, self.raw_keys), limit)
        return None

    def fields(self, pairs: list[tuple[str, str]]) -> list[ActivityField]:
        out = []
        for key, label in pairs:
            value = self.text(key, limit=FIELD_CHARS)
            if value:
                out.append(ActivityField(label=label, value=value))
        return out[:MAX_FIELDS]

    def fallback_fields(self) -> list[ActivityField]:
        """Up to 8 redacted key/values: scalar values only, no secret-named keys,
        nothing that looks like a type key, labels humanized."""
        pairs = []
        for key, value in self.data.items():
            if not isinstance(key, str) or key in _HIDDEN_FIELDS or _SECRET_KEY_RE.search(key):
                continue
            if isinstance(value, bool):
                self.data[key] = "Yes" if value else "No"
            elif isinstance(value, (int, float)):
                self.data[key] = str(value)
            pairs.append((key, _humanize(key)))
        return self.fields(pairs)


def _approval_key(approval: dict, payload: dict) -> str:
    action = payload.get("isol8_action")
    if isinstance(action, str) and action in _LIGHTBULB_CARDS:
        return f"lightbulb:{action}"
    if any(payload.get(k) == PUBLIC_POST for k in (*_DISCRIMINATOR_KEYS, "title")):
        return PUBLIC_POST
    return str(approval.get("type") or "")


def _raw_keys(approval: dict, payload: dict) -> set[str]:
    keys = set(_KNOWN_TYPE_KEYS)
    for value in (approval.get("type"), *(payload.get(k) for k in (*_DISCRIMINATOR_KEYS, "isol8_action"))):
        # Only identifier-shaped values: a discriminator like "Launch" is ordinary prose.
        if isinstance(value, str) and TYPE_KEY_RE.match(value):
            keys.add(value)
    return keys


def _paperclip_actor(
    agent_id: Any, user_id: Any, agent_names: dict[str, str], ctx: Any, people: dict[str, ActivityActor] | None = None
) -> ActivityActor:
    """Agents keep their Paperclip agent id (it IS the room member id). A human is
    mapped from their Paperclip user id to their Clerk user id through ``people``;
    one we cannot map keeps the Paperclip id -- see ``ActivityActor`` in the shared types."""
    if isinstance(agent_id, str) and agent_id:
        return ActivityActor(kind="agent", id=agent_id, name=agent_names.get(agent_id) or "An agent")
    if isinstance(user_id, str) and user_id:
        if user_id == ctx.paperclip_user_id:
            return ActivityActor(kind="human", id=ctx.user_id, name="You")
        return (people or {}).get(user_id) or ActivityActor(kind="human", id=user_id, name="A teammate")
    return ActivityActor(kind="system", id="paperclip", name="Your team")


async def _people(ctx: Any, rows: list[dict], fields: tuple[str, ...]) -> dict[str, ActivityActor]:
    """``{paperclipUserId: actor-with-Clerk-id}`` for the teammates these rows name.
    Costs nothing unless a row names a human other than the viewer; then one
    workspace read plus one identity read per workspace member."""
    wanted = {row.get(f) for row in rows for f in fields if isinstance(row.get(f), str) and row.get(f)} - {
        ctx.paperclip_user_id
    }
    if not wanted:
        return {}
    from routers.teams import agents as teams_agents

    repo = teams_agents._repo()
    workspace = await repo.get_workspace(ctx.owner_id)
    members = list(getattr(workspace, "member_user_ids", None) or [])
    identities = await asyncio.gather(*(repo.get_identity(uid) for uid in members))
    found = {
        identity.paperclip_user_id: uid
        for uid, identity in zip(members, identities)
        if identity is not None and identity.paperclip_user_id in wanted
    }
    names = await asyncio.gather(*(messaging.human_display_name(uid) for uid in found.values()))
    return {pc_id: ActivityActor(kind="human", id=uid, name=name) for (pc_id, uid), name in zip(found.items(), names)}


def _render_by_type(key: str, p: _Payload, who: str) -> tuple[str, str | None, ActivityPreview | None]:
    """``(title, reason, preview)`` per approval type. Unknown types fall back."""
    if key == "hire_agent":
        name = p.text("name", limit=60)
        preview = ActivityPreview(
            fields=p.fields(
                [("role", "Role"), ("title", "Title"), ("capabilities", "Capabilities"), ("desiredSkills", "Skills")]
            )
        )
        return (f"{who} wants to hire {name or 'a new agent'}", p.text("reason", "summary"), preview)
    if key == PUBLIC_POST:
        platform = p.text("platform", "network", "channel", "destination", limit=60)
        account = p.text("account", "page", "handle", "audience", limit=80)
        preview = ActivityPreview(
            text=p.text("text", "body", "content", "post", "postText", "draft", "message", "proposedComment"),
            destination=" · ".join(part for part in (platform, account) if part) or None,
        )
        where = f"on {platform}" if platform else "publicly"
        return (f"{who} wants to publish a post {where}", p.text("reason", "summary", "recommendedAction"), preview)
    if key == "request_board_approval":
        title = p.text("title", limit=100)
        preview = ActivityPreview(
            text=p.text("proposedComment"),
            fields=p.fields(
                [
                    ("recommendedAction", "Recommended action"),
                    ("nextActionOnApproval", "On approval"),
                    ("risks", "Risks"),
                ]
            ),
        )
        return (
            f"{who}: {title}" if title else f"{who} needs your approval",
            p.text("summary", "reason", "recommendedAction"),
            preview,
        )
    if key == "approve_ceo_strategy":
        title = p.text("title", limit=80)
        preview = ActivityPreview(text=p.text("plan", "description", "strategy", "text"))
        return (f"{who} proposes a strategy" + (f": {title}" if title else ""), p.text("summary", "reason"), preview)
    if key == "budget_override_required":
        scope = p.text("scopeName", "scopeType", limit=60)
        preview = ActivityPreview(fields=p.fields([("windowKind", "Window"), ("metric", "Metric")]))
        return ("Budget override needed" + (f" for {scope}" if scope else ""), p.text("guidance", "reason"), preview)
    if key.startswith("lightbulb:"):
        # The card's own title ("Approve Neon postgres for Acme") and the agent's reason
        # ("Workshop Signup: hosting (live)") say what this is; the generic label is the fallback.
        label = _LIGHTBULB_CARDS[key.split(":", 1)[1]]
        title = p.text("title", limit=100) or label
        reason = p.text("reason", limit=120)
        return (f"{title} — {reason}" if reason else title, p.text("summary", "description"), None)
    return (
        f"Approval requested by {who}",
        p.text("title", "summary", "recommendedAction", "reason"),
        ActivityPreview(fields=p.fallback_fields()),
    )


def render_approval(
    approval: dict, *, agent_names: dict[str, str], ctx: Any, people: dict[str, ActivityActor] | None = None
) -> ActivityItem:
    raw_payload = approval.get("payload") if isinstance(approval.get("payload"), dict) else {}
    key = _approval_key(approval, raw_payload)
    payload = _Payload(raw_payload, _raw_keys(approval, raw_payload))
    requester = _paperclip_actor(
        approval.get("requestedByAgentId"), approval.get("requestedByUserId"), agent_names, ctx, people
    )
    title, reason, preview = _render_by_type(key, payload, requester.name)
    approval_id = str(approval["id"])
    status = approval.get("status")
    pending = status == "pending"
    open_action = ActivityAction(
        id="open", label="Open approval", navigate=ActionNavigate(kind="approval", id=approval_id)
    )
    actions: list[ActivityAction] = []
    # Shown to every viewer who sees the item: it only reaches them through their own
    # board-only attention feed, the Teams context carries no role, and Paperclip
    # itself enforces who may decide (its 403 reaches the caller).
    if pending and not key.startswith("lightbulb:"):
        actions = [
            ActivityAction(id="approve", label="Approve", style="primary"),
            ActivityAction(
                id="reject",
                label="Reject",
                style="destructive",
                confirm="Reject this request?",
                input=ActionInput(kind="text", label="Why? (optional)", required=False),
            ),
            ActivityAction(
                id="request_revision",
                label="Request changes",
                input=ActionInput(kind="text", label="What should change?", required=True),
            ),
        ]
    if pending:
        actions.append(open_action)
    resolved_by = None
    if not pending and approval.get("decidedByUserId"):
        resolved_by = _paperclip_actor(None, approval.get("decidedByUserId"), agent_names, ctx, people)
    return ActivityItem(
        id=f"paperclip:approval:{approval_id}",
        kind="paperclip",
        subtype="approval",
        actor=requester,
        created_at=_when(approval.get("createdAt")),
        state="open" if pending else "resolved",
        resolved_by=resolved_by,
        resolved_at=None if pending else _when(approval.get("decidedAt") or approval.get("updatedAt")),
        read=not pending,
        summary=ActivitySummary(
            title=_clip(title, TITLE_CHARS),
            requester=requester,
            reason=reason,
            preview=preview if preview and (preview.text or preview.destination or preview.fields) else None,
            links=[ActivityLink(kind="approval", id=approval_id, label="Approval")],
        ),
        actions=actions,
    )


def render_issue(
    issue: dict, *, agent_names: dict[str, str], ctx: Any, people: dict[str, ActivityActor] | None = None
) -> ActivityItem:
    issue_id = str(issue["id"])
    status = str(issue.get("status") or "")
    p = _Payload(issue, set(_KNOWN_TYPE_KEYS))
    ident = p.text("identifier", limit=20) or "A task"
    title = p.text("title", limit=100)
    requester = _paperclip_actor(issue.get("createdByAgentId"), issue.get("createdByUserId"), agent_names, ctx, people)
    open_state = status not in ("done", "cancelled")
    actions: list[ActivityAction] = []
    if open_state:
        actions = [
            ActivityAction(id="open", label="Open issue", navigate=ActionNavigate(kind="issue", id=issue_id)),
            ActivityAction(
                id="change_status",
                label="Change status",
                input=ActionInput(
                    kind="select",
                    label="Status",
                    required=True,
                    options=[ActionOption(value=s, label=ISSUE_STATUS_LABELS[s]) for s in _CHANGEABLE_STATUSES],
                ),
            ),
            ActivityAction(
                id="reassign", label="Reassign", input=ActionInput(kind="person", label="Assign to", required=True)
            ),
        ]
    return ActivityItem(
        id=f"paperclip:assigned_issue:{issue_id}",
        kind="paperclip",
        subtype="assigned_issue",
        actor=requester,
        # Creation, not updatedAt: every agent edit would otherwise re-date the item
        # (re-sorting it and re-counting it as unseen). The row has no assignment time.
        created_at=_when(issue.get("createdAt")),
        state="open" if open_state else "resolved",
        resolved_at=None
        if open_state
        else _when(issue.get("completedAt") or issue.get("cancelledAt") or issue.get("updatedAt")),
        read=not open_state,
        summary=ActivitySummary(
            title=_clip(f"{ident} is assigned to you" + (f": {title}" if title else ""), TITLE_CHARS),
            requester=requester,
            reason=f"Status: {ISSUE_STATUS_LABELS.get(status, 'Unknown')}",
            preview=ActivityPreview(text=p.text("description")) if p.text("description") else None,
            links=[ActivityLink(kind="issue", id=issue_id, label=ident)],
        ),
        actions=actions,
    )


def render_failed_run(run: dict, *, agent_names: dict[str, str], ctx: Any) -> ActivityItem:
    """An agent whose latest run failed (stock Paperclip's inbox ``failed_run``).
    Nothing to decide inline, so the one action opens the run."""
    run_id = str(run["id"])
    agent = _paperclip_actor(run.get("agentId"), None, agent_names, ctx)
    p = _Payload(run, set(_KNOWN_TYPE_KEYS))
    # An error code is identifier-shaped ("setup_failed"), which ``text`` refuses; humanize it.
    code = run.get("errorCode") if isinstance(run.get("errorCode"), str) else None
    timed_out = run.get("status") == "timed_out"
    return ActivityItem(
        id=f"paperclip:failed_run:{run_id}",
        kind="paperclip",
        subtype="failed_run",
        actor=agent,
        created_at=_when(run.get("finishedAt") or run.get("createdAt")),
        state="open",
        read=False,
        summary=ActivitySummary(
            title=_clip(f"{agent.name}'s last run {'timed out' if timed_out else 'failed'}", TITLE_CHARS),
            requester=agent,
            reason=p.text("error", limit=FIELD_CHARS) or (_humanize(code[:60]) if code else None),
            links=[ActivityLink(kind="run", id=run_id, label="Run")],
        ),
        actions=[ActivityAction(id="open", label="Open run", navigate=ActionNavigate(kind="run", id=run_id))],
    )


_INTERACTION_VERBS = {
    "request_confirmation": "needs your confirmation",
    "request_checkbox_confirmation": "needs your confirmation",
    "ask_user_questions": "has a question for you",
    "suggest_tasks": "suggested tasks for your review",
    "request_item_verdicts": "needs your verdicts",
}


def interaction_activity_id(issue_id: str, interaction_id: str) -> str:
    return f"paperclip:interaction:{issue_id}:{interaction_id}"


def _questions(payload: dict) -> list[dict]:
    questions = payload.get("questions")
    return [q for q in questions if isinstance(q, dict)] if isinstance(questions, list) else []


def _options(value: Any) -> list[dict]:
    return [o for o in value if isinstance(o, dict) and isinstance(o.get("id"), str)] if isinstance(value, list) else []


def _label(row: dict) -> str:
    return _Payload(row, set()).text("label", limit=80) or row["id"]


def _answerable(payload: dict) -> dict | None:
    """The one question an Activity row answers inline: a lone single-choice (or
    free-text) question. Anything richer is answered in the task."""
    questions = _questions(payload)
    if len(questions) != 1 or not isinstance(questions[0].get("id"), str):
        return None
    question = questions[0]
    if question.get("selectionMode") == "multi" or any(o.get("freeText") for o in _options(question.get("options"))):
        return None
    return question


def render_interaction(interaction: dict, issue: dict | None, *, agent_names: dict[str, str], ctx: Any) -> ActivityItem:
    """A pending issue-thread interaction (stock Paperclip's inbox ``issue_thread_interaction``):
    an agent stopped and asked a human to confirm or answer. Paperclip gives these no
    expiry time -- they expire only when their task closes -- so none is shown."""
    interaction_id = str(interaction["id"])
    issue_id = str(interaction.get("issueId") or (issue or {}).get("id") or "")
    kind = str(interaction.get("kind") or "")
    raw = interaction.get("payload") if isinstance(interaction.get("payload"), dict) else {}
    p = _Payload(raw, set(_KNOWN_TYPE_KEYS))
    issue_p = _Payload(issue or {}, set(_KNOWN_TYPE_KEYS))
    ident = issue_p.text("identifier", limit=20) or "Task"
    requester = _paperclip_actor(
        interaction.get("createdByAgentId"), interaction.get("createdByUserId"), agent_names, ctx
    )
    subject = _Payload(interaction, set(_KNOWN_TYPE_KEYS)).text("title", limit=100) or issue_p.text("title", limit=100)
    verb = _INTERACTION_VERBS.get(kind, "needs your input")
    questions = _questions(raw)
    prompt = p.text("prompt", limit=FIELD_CHARS) or (
        _Payload(questions[0], set()).text("prompt", limit=FIELD_CHARS) if questions else None
    )
    fields: list[ActivityField] = []
    if kind == "request_checkbox_confirmation":
        labels = {o["id"]: _label(o) for o in _options(raw.get("options"))}
        fields = (
            [ActivityField(label="Options", value=_clip(", ".join(labels.values()), FIELD_CHARS))] if labels else []
        )
        picked = raw.get("defaultSelectedOptionIds")
        if isinstance(picked, list) and picked:
            chosen = ", ".join(label for key, label in labels.items() if key in picked)
            fields.append(ActivityField(label="Confirms", value=_clip(chosen, FIELD_CHARS)))
    elif kind == "ask_user_questions" and len(questions) > 1:
        fields = [
            ActivityField(label=f"Question {n}", value=text)
            for n, q in enumerate(questions[:MAX_FIELDS], 1)
            if (text := _Payload(q, set()).text("prompt", limit=FIELD_CHARS))
        ]
    pending = interaction.get("status") == "pending"
    actions: list[ActivityAction] = []
    question = _answerable(raw) if kind == "ask_user_questions" else None
    if pending and kind in ("request_confirmation", "request_checkbox_confirmation"):
        # A checkbox confirmation confirms its suggested picks ("Confirms"); other picks are made in the task.
        actions = [
            ActivityAction(id="accept", label=p.text("acceptLabel", limit=40) or "Confirm", style="primary"),
            ActivityAction(
                id="reject",
                label=p.text("rejectLabel", limit=40) or "Reject",
                style="destructive",
                input=ActionInput(
                    kind="text",
                    label=p.text("rejectReasonLabel", limit=80) or "Why? (optional)",
                    required=bool(raw.get("rejectRequiresReason")),
                ),
            ),
        ]
    elif pending and question is not None:
        options = _options(question.get("options"))
        actions = [
            ActivityAction(
                id="answer",
                label="Answer",
                style="primary",
                input=ActionInput(
                    kind="select",
                    label="Your answer",
                    required=True,
                    options=[ActionOption(value=o["id"], label=_label(o)) for o in options],
                )
                if options
                else ActionInput(kind="text", label="Your answer", required=True),
            )
        ]
    if pending:
        label = "Answer in task" if kind == "ask_user_questions" and question is None else "Open task"
        actions.append(ActivityAction(id="open", label=label, navigate=ActionNavigate(kind="issue", id=issue_id)))
    resolved_by = None
    if not pending and interaction.get("resolvedByUserId"):
        resolved_by = _paperclip_actor(None, interaction.get("resolvedByUserId"), agent_names, ctx)
    details = p.text("detailsMarkdown")
    return ActivityItem(
        id=interaction_activity_id(issue_id, interaction_id),
        kind="paperclip",
        subtype="interaction",
        actor=requester,
        created_at=_when(interaction.get("createdAt")),
        state="open" if pending else "resolved",
        resolved_by=resolved_by,
        resolved_at=None if pending else _when(interaction.get("resolvedAt") or interaction.get("updatedAt")),
        read=not pending,
        summary=ActivitySummary(
            title=_clip(f"{requester.name} {verb}" + (f": {subject}" if subject else ""), TITLE_CHARS),
            requester=requester,
            reason=prompt or _Payload(interaction, set()).text("summary", limit=FIELD_CHARS),
            preview=ActivityPreview(text=details, fields=fields) if details or fields else None,
            links=[ActivityLink(kind="issue", id=issue_id, label=ident)],
        ),
        actions=actions,
    )


def latest_failed_runs(runs: list[dict]) -> list[dict]:
    """Each agent's newest run, kept only when it failed (stock ``getLatestFailedRunsByAgent``)."""
    latest: dict[str, dict] = {}
    for run in sorted(runs, key=lambda r: _when(r.get("createdAt")), reverse=True):
        agent_id = run.get("agentId")
        if isinstance(agent_id, str) and agent_id not in latest:
            latest[agent_id] = run
    return [
        run for run in latest.values() if run.get("status") in _FAILED_RUN_STATUSES and isinstance(run.get("id"), str)
    ]


# Pinned order: decisions first (approvals, then an agent's question or a session
# waiting on you), failed runs last.
_PINNED_ORDER = {
    "approval": 0,
    "interaction": 1,
    "waiting": 1,
    "join_request": 1,
    "login": 1,
    "code": 1,
    "extension": 1,
    "reconnect": 1,
    "failed_run": 2,
}


def needs_you(item: ActivityItem) -> bool:
    """Pinned to the top and always counted on the badge: an open approval, a pending
    issue-thread interaction (an agent waiting on a human), a failed run, one of your
    shared sessions still waiting for your input, or a task question you can answer."""
    if item.kind == "task_question":
        return item.state == "open"
    if item.kind == "session":
        return item.state == "open" and item.subtype in ("waiting", "join_request")
    if item.kind in ("login", "extension", "reconnect"):
        return item.state == "open"
    return item.kind == "paperclip" and item.state == "open" and item.subtype in _PINNED_ORDER


def _rows(value: Any, key: str) -> list[dict]:
    if isinstance(value, dict):
        value = value.get(key) or value.get("items") or []
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _admin():
    from routers.teams import agents as teams_agents

    return teams_agents._admin()


async def _agent_names(ctx: Any) -> dict[str, str]:
    agents = _rows(await _admin().list_agents(company_id=ctx.company_id, session_cookie=ctx.session_cookie), "agents")
    return {a["id"]: a.get("name") or "An agent" for a in agents if isinstance(a.get("id"), str)}


async def _failed_runs(admin: Any, ctx: Any) -> list[dict]:
    """Best effort: run telemetry the viewer cannot read (or a slow runs list) costs
    only this section, never the approvals beside it."""
    try:
        runs = await admin.list_company_heartbeat_runs(
            company_id=ctx.company_id, session_cookie=ctx.session_cookie, limit=RUN_SCAN_LIMIT, summary=True
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("activity: failed runs unavailable error_type=%s", type(exc).__name__)
        return []
    return latest_failed_runs([r for r in _rows(runs, "runs") if r.get("companyId") in (None, ctx.company_id)])


# Inside PAPERCLIP_BUDGET_S, so a slow attention read costs only the interactions.
INTERACTIONS_BUDGET_S = 2.5


async def _pending_interactions(admin: Any, ctx: Any) -> list[tuple[dict, dict | None]]:
    """Every pending issue-thread interaction waiting on the viewer, as
    ``(interaction, issue)``. Stock Paperclip's attention feed applies the "waiting
    on a human" rule (unaddressed or addressed to this user, one pending
    confirmation per issue); ``all`` so none falls off a page. Best effort."""
    from routers.teams import attention

    async def fetch() -> list[dict]:
        feed = await admin.list_attention(
            company_id=ctx.company_id, session_cookie=ctx.session_cookie, params={"all": "true"}
        )
        rows = [i for i in attention._items(feed) if i.get("sourceKind") == "issue_thread_interaction"]
        # ponytail: one interactions read per issue, capped at attention._MAX_HYDRATED_ISSUES;
        # a backlog past that cap leaves the rest out until earlier ones are answered.
        await attention._hydrate_interactions(ctx, rows)
        return rows

    try:
        rows = await asyncio.wait_for(fetch(), INTERACTIONS_BUDGET_S)
    except Exception as exc:  # noqa: BLE001 -- costs only this section
        logger.warning("activity: interactions unavailable error_type=%s", type(exc).__name__)
        return []
    out = []
    for row in rows:
        interaction = row.get("interaction")
        if (
            isinstance(interaction, dict)
            and interaction.get("status") == "pending"
            and interaction.get("companyId") in (None, ctx.company_id)
            and _ID_RE.match(str(interaction.get("id") or ""))
            and _ID_RE.match(str(interaction.get("issueId") or ""))
        ):
            issue = row.get("relatedIssue")
            out.append((interaction, issue if isinstance(issue, dict) else None))
    return out


async def _paperclip_items(ctx: Any) -> list[ActivityItem]:
    """EVERY pending approval in the company (Paperclip decides who may act on one),
    every pending issue-thread interaction waiting on the viewer, each agent's
    latest run when it failed, and open issues assigned to the viewer."""
    admin = _admin()
    names, approvals, issues, failed, interactions = await asyncio.gather(
        _agent_names(ctx),
        admin.list_approvals(company_id=ctx.company_id, session_cookie=ctx.session_cookie, status="pending"),
        admin.list_issues(
            company_id=ctx.company_id,
            session_cookie=ctx.session_cookie,
            params={"assigneeUserId": "me", "status": ",".join(_OPEN_ISSUE_STATUSES), "limit": str(PAPERCLIP_LIMIT)},
        ),
        _failed_runs(admin, ctx),
        _pending_interactions(admin, ctx),
    )
    pending = [
        row
        for row in _rows(approvals, "approvals")
        if isinstance(row.get("id"), str) and row.get("companyId") == ctx.company_id and row.get("status") == "pending"
    ]
    mine = [
        issue
        for issue in _rows(issues, "issues")[:PAPERCLIP_LIMIT]
        if issue.get("companyId") == ctx.company_id and isinstance(issue.get("id"), str)
    ]
    people = await _people(ctx, pending + mine, ("requestedByUserId", "createdByUserId"))
    return (
        [render_approval(row, agent_names=names, ctx=ctx, people=people) for row in pending]
        + [render_interaction(row, issue, agent_names=names, ctx=ctx) for row, issue in interactions]
        + [render_failed_run(run, agent_names=names, ctx=ctx) for run in failed]
        + [render_issue(issue, agent_names=names, ctx=ctx, people=people) for issue in mine]
    )


_APPROVAL_PEOPLE = ("requestedByUserId", "decidedByUserId")


def render_task_question(q: dict) -> ActivityItem:
    """An open super-agent Question (contracts-v2 §14.2) for one of its answerers. Copy from `questions.card_copy`.
    ponytail: Open only; the answer buttons arrive with the answer route (wave2-Q Task 4)."""
    from core.services import questions

    title, _ = questions.card_copy(q, "Lightbulb")
    actor = ActivityActor(kind="agent", id="", name="Lightbulb")
    created = _when(q["createdAt"])
    return ActivityItem(
        id=f"taskq:{q['taskId']}:{q['qid']}",
        kind="task_question",
        subtype="interaction" if q["kind"] == "question" else "approval",
        actor=actor,
        created_at=created,
        state="open",
        read=False,  # open = unanswered, as an open Paperclip approval
        summary=ActivitySummary(
            title=_clip(title.replace("**", ""), TITLE_CHARS),
            requester=actor,
            preview=ActivityPreview(text=_clip(q["text"], PREVIEW_TEXT_CHARS)) if q.get("text") else None,
            links=[ActivityLink(kind="task", id=q["taskId"], label=_clip(q["title"], TITLE_CHARS))],
        ),
        actions=[
            ActivityAction(
                id="open", label="Open thread", style="primary", navigate=ActionNavigate(kind="task", id=q["taskId"])
            )
        ],
    )


async def _task_question_items(owner_id: str, user_id: str) -> list[ActivityItem]:
    """The viewer's open Questions, from their `NEEDS#` rows; each re-read so a closed one never shows."""
    from core.repositories import questions_repo

    try:
        needs, _ = await questions_repo.needs_for_user(owner_id, user_id, limit=MAX_LIMIT)
        found = await asyncio.gather(*(questions_repo.get_question(owner_id, n["taskId"], n["qid"]) for n in needs))
    except Exception as exc:  # noqa: BLE001 -- a tasks-table error never takes the feed down
        logger.warning("activity: task questions unavailable error_type=%s", type(exc).__name__)
        return []
    return [render_task_question(q) for q in found if q and q["state"] == "open" and user_id in q.get("answerers", [])]


async def _live_items(owner_id: str, user_id: str, teams_ctx: TeamsCtx) -> tuple[list[ActivityItem], bool]:
    """Page 1's live decisions and whether Paperclip answered. A super-agent workspace adds its open Questions
    (§14.2, §14.5); Paperclip's items stay, because room runs there still ask through Paperclip (spec 05 §7),
    and a Paperclip outage never hides a Question. Every other workspace is exactly as before."""
    from core.services.super_agent_flags import flags_for

    if not await flags_for(owner_id):
        paperclip = await _paperclip_within_budget(teams_ctx)
        return paperclip or [], paperclip is not None
    paperclip, questions = await asyncio.gather(
        _paperclip_within_budget(teams_ctx), _task_question_items(owner_id, user_id)
    )
    return (paperclip or []) + questions, paperclip is not None


async def _paperclip_within_budget(teams_ctx: TeamsCtx) -> list[ActivityItem] | None:
    """Paperclip items, or ``None`` when Paperclip is slow, down or unreachable for
    this viewer. Session resolution and the fetches share one budget."""

    async def fetch() -> list[ActivityItem]:
        return await _paperclip_items(await teams_ctx())

    try:
        return await asyncio.wait_for(fetch(), PAPERCLIP_BUDGET_S)
    except Exception as exc:  # noqa: BLE001 -- Paperclip never takes the feed down (TimeoutError included)
        logger.warning("activity: paperclip unavailable error_type=%s", type(exc).__name__)
        return None


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


async def teams_context(auth: AuthContext, request: Any, response: Any) -> Any:
    """The viewer's Paperclip context WITHOUT the Teams self-heal side effect.
    Raises (202/409/503 HTTPException) when the viewer has no usable session."""
    from routers.teams import agents as teams_agents
    from routers.teams import deps as teams_deps

    request.state.teams_user_id = auth.user_id  # the 401 self-heal handler reads it
    return await teams_deps.resolve_teams_context(
        auth=auth,
        repo=teams_agents._repo(),
        session_factory=teams_agents.get_user_session_cookie_for,
        cookies=request.cookies,
        response=response,
    )


def _unseen_message_rows(unseen_rows: list[dict], gates: dict) -> list[dict]:
    # ponytail: the badge counts unseen pointers in rooms the viewer is still in; a
    # deleted-but-unseen message still counts until it is read or seen.
    return [
        r for r in unseen_rows if r.get("kind") in _MESSAGE_KINDS and r.get("roomId") in gates and not r.get("doneAt")
    ]


def _unseen_paperclip(items: list[ActivityItem], seen: str | None) -> list[ActivityItem]:
    return [item for item in items if _newer_than(item.created_at, seen)]


async def unseen_counts(owner_id: str, user_id: str, teams_ctx: TeamsCtx) -> dict[str, int] | None:
    """The unseen badge's items, counted by kind (``paperclip`` items by subtype:
    ``approval`` / ``assigned_issue``). Same query and "unseen" rule as the feed's
    ``unseenCount``; ``None`` when Paperclip is unavailable, so a caller that
    must not under-report (the daily digest) can retry instead."""
    seen = await messaging_repo.get_activity_seen(owner_id, user_id)
    rows = await messaging_repo.list_activity_since(owner_id, user_id, seen, UNSEEN_CAP)
    gates = await _room_gates(owner_id, user_id, {r["roomId"] for r in rows if r.get("roomId")})
    paperclip, ok = await _live_items(owner_id, user_id, teams_ctx)
    if not ok:
        return None
    counts: dict[str, int] = {}
    for kind in [r["kind"] for r in _unseen_message_rows(rows, gates)] + [
        i.subtype or i.kind for i in _unseen_paperclip(paperclip, seen)
    ]:
        counts[kind] = counts.get(kind, 0) + 1
    return counts


async def list_activity(auth: AuthContext, *, cursor: str | None, limit: int, teams_ctx: TeamsCtx) -> ActivityPage:
    owner_id = resolve_owner_id(auth)
    user_id = auth.user_id
    before = _decode_cursor(cursor) if cursor else None
    limit = max(1, min(int(limit), MAX_LIMIT))
    # Page 1's Paperclip fetch runs alongside every DynamoDB read below, bounded by its budget.
    paperclip_task = None if cursor else asyncio.create_task(_live_items(owner_id, user_id, teams_ctx))
    try:
        rows, seen = await asyncio.gather(
            messaging_repo.list_activity(owner_id, user_id, before_sk=before, limit=limit),
            messaging_repo.get_activity_seen(owner_id, user_id),
        )
    except BaseException:
        if paperclip_task is not None:
            paperclip_task.cancel()
        raise
    next_cursor = _encode_cursor(rows[-1]["sk"]) if len(rows) == limit else None
    if cursor:
        # Later pages: history only. Paperclip items are live decisions on page 1.
        items = await _render_rows(owner_id, user_id, rows, seen, admin=auth.is_org_admin)
        return ActivityPage(items=items, next=next_cursor, unseen_count=None, sources=ActivitySources(paperclip="ok"))

    assert paperclip_task is not None
    try:
        unseen_rows = await messaging_repo.list_activity_since(owner_id, user_id, seen, UNSEEN_CAP)
        gates = await _room_gates(owner_id, user_id, {r["roomId"] for r in rows + unseen_rows if r.get("roomId")})
        items = await _render_rows(owner_id, user_id, rows, seen, gates, admin=auth.is_org_admin)
    except BaseException:
        paperclip_task.cancel()
        raise
    unseen_messages = _unseen_message_rows(unseen_rows, gates)
    paperclip, ok = await paperclip_task
    source = "ok" if ok else "unavailable"
    unseen_paperclip = len(_unseen_paperclip(paperclip, seen))
    unseen = len(unseen_messages) + unseen_paperclip
    pinned = [item for item in items + paperclip if needs_you(item)]
    # Every needs-you item counts whether or not it was seen; mentions, DMs and Paperclip
    # items count while unseen. A thread reply (or a reaction) never badges.
    # Session items are not in `unseen` (a finished turn never badges), so a waiting one always
    # counts here. ponytail: only waiting items on this page count; an older one still waiting
    # past 30 newer rows is left off the badge.
    attention = (
        sum(1 for r in unseen_messages if r["kind"] in _BADGE_MESSAGE_KINDS)
        + unseen_paperclip
        + sum(
            1
            for item in pinned
            # A login ask badges only the people who can answer it (admins get its actions).
            if item.kind in ("session", "reconnect")
            or (item.kind in ("login", "extension") and item.actions)
            or (item.kind not in ("login", "extension") and not _newer_than(item.created_at, seen))
        )
    )
    rest = sorted([item for item in items + paperclip if not needs_you(item)], key=lambda i: i.created_at, reverse=True)
    # Approvals, then agents' questions, then failed runs; each newest first.
    pinned.sort(key=lambda i: (_PINNED_ORDER[i.subtype or ""], -i.created_at.timestamp()))
    return ActivityPage(
        items=pinned + rest,
        next=next_cursor,
        unseen_count=min(UNSEEN_CAP, unseen),
        attention_count=min(UNSEEN_CAP, attention),
        sources=ActivitySources(paperclip=source),
    )


async def mark_seen(auth: AuthContext, request: ActivitySeenRequest) -> ActivitySeenResponse:
    owner_id = resolve_owner_id(auth)
    # Never past now: "mark all read" is upTo=now, and a future cursor would hide
    # items that have not happened yet.
    up_to = min(request.up_to.astimezone(timezone.utc), datetime.now(timezone.utc))
    stored = await messaging_repo.set_activity_seen(owner_id, auth.user_id, up_to.isoformat())
    return ActivitySeenResponse(seen_up_to=_when(stored))


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _require_action(item: ActivityItem, action_id: str) -> ActivityAction:
    action = next((a for a in item.actions if a.id == action_id), None)
    if action is None:
        raise ApiProblem("ACTION_UNAVAILABLE", "That action is not available on this item.")
    if action.navigate is not None:
        raise ApiProblem("INVALID_REQUEST", "Navigation actions are handled by the client.")
    return action


async def _message_item(
    auth: AuthContext, owner_id: str, kind: str, message_id: str, room_id: str, seq: int
) -> tuple[ActivityItem, dict, dict]:
    """The caller's own item, re-authorized: current membership, their own row."""
    meta, member = await messaging.require_room_member(owner_id, auth.user_id, room_id)
    msg = await messaging_repo.get_message(room_id, seq)
    if not msg or msg.get("messageId") != message_id or msg.get("deletedAt"):
        raise ApiProblem("ACTIVITY_NOT_FOUND", "Activity item not found.")
    sk = messaging_repo.activity_sk(msg["createdAt"], message_id)
    row = await messaging_repo.get_activity_row(owner_id, auth.user_id, sk)
    if not row or row.get("kind") != kind:
        raise ApiProblem("ACTIVITY_NOT_FOUND", "Activity item not found.")
    read_row = await messaging_repo.get_read_row(room_id, auth.user_id)
    seen = await messaging_repo.get_activity_seen(owner_id, auth.user_id)
    return _render_message_item(row, meta, member, read_row, msg, seen), row, msg


async def _run_message_action(
    auth: AuthContext, kind: str, rest: list[str], action_id: str, body: ActivityActionRequest
) -> ActivityItem:
    owner_id = resolve_owner_id(auth)
    try:
        message_id = str(uuid.UUID(rest[0]))
        room_id = str(uuid.UUID(rest[1]))
        seq = int(rest[2])
    except (ValueError, IndexError) as exc:
        raise ApiProblem("ACTIVITY_NOT_FOUND", "Activity item not found.") from exc
    item, row, msg = await _message_item(auth, owner_id, kind, message_id, room_id, seq)
    if action_id == "mark_done" and item.state == "done":
        return item  # idempotent repeat
    _require_action(item, action_id)
    if action_id == "mark_done":
        await messaging_repo.mark_activity_done(owner_id, auth.user_id, row["sk"])
    elif action_id == "reply":
        text = (body.value or "").strip()
        if not text:
            raise ApiProblem("INVALID_REQUEST", "A reply needs text.")
        meta = await messaging_repo.get_room(room_id)
        thread_root = msg.get("threadRootMessageId") or (message_id if (meta or {}).get("kind") == "channel" else None)
        mentions = []
        author = msg["author"]
        if author["kind"] == "agent" and (meta or {}).get("kind") == "channel":
            # Replying to an agent should reach it, as tapping Reply in the thread would.
            if await messaging.room_agent(room_id, author["id"], meta):  # a feed author is not seated
                mentions = [messaging.MemberRef(kind="agent", id=author["id"])]
        # Scoped to user + item so a client key can never replay someone else's send.
        basis = str(body.idempotency_key) if body.idempotency_key else f"text|{text}"
        client_id = uuid.uuid5(uuid.NAMESPACE_URL, f"activity-reply|{auth.user_id}|{item.id}|{basis}")
        await messaging.create_human_message(
            auth,
            room_id,
            messaging.CreateMessageRequest(
                client_message_id=client_id,
                text=text,
                mentions=mentions,
                reply_to_message_id=None,
                thread_root_message_id=uuid.UUID(thread_root) if thread_root else None,
            ),
        )
    return (await _message_item(auth, owner_id, kind, message_id, room_id, seq))[0]


async def _fresh_approval(ctx: Any, approval_id: str) -> dict:
    approval = await _admin().get_approval(approval_id=approval_id, session_cookie=ctx.session_cookie)
    if not isinstance(approval, dict) or approval.get("companyId") != ctx.company_id:
        raise ApiProblem("ACTIVITY_NOT_FOUND", "Activity item not found.")
    return approval


async def _run_approval_action(approval_id: str, action_id: str, body: ActivityActionRequest, ctx: Any) -> ActivityItem:
    from core.services.paperclip_admin_client import PaperclipApiError
    from routers.teams import approvals as teams_approvals
    from routers.teams.schemas import ApproveApprovalBody, RejectApprovalBody, RequestApprovalRevisionBody

    names = await _agent_names(ctx)
    approval = await _fresh_approval(ctx, approval_id)
    item = render_approval(
        approval, agent_names=names, ctx=ctx, people=await _people(ctx, [approval], _APPROVAL_PEOPLE)
    )
    if approval.get("status") != "pending" and action_id in ("approve", "reject", "request_revision"):
        return item  # already decided (by this viewer's retry or someone else): the current state
    _require_action(item, action_id)
    note = (body.note or "").strip() or None
    try:
        if action_id == "approve":
            await teams_approvals.approve(approval_id, ApproveApprovalBody(note=note), ctx)
        elif action_id == "reject":
            await teams_approvals.reject(approval_id, RejectApprovalBody(reason=note or "Rejected from Activity"), ctx)
        elif action_id == "request_revision":
            if not note:
                raise ApiProblem("INVALID_REQUEST", "Say what should change.")
            await teams_approvals.request_approval_revision(
                approval_id, RequestApprovalRevisionBody(decision_note=note), ctx
            )
    except PaperclipApiError:
        # Lost a race with another decision: answer with what stands, never a 500.
        approval = await _fresh_approval(ctx, approval_id)
        if approval.get("status") == "pending":
            raise
    approval = await _fresh_approval(ctx, approval_id)
    return render_approval(
        approval, agent_names=names, ctx=ctx, people=await _people(ctx, [approval], _APPROVAL_PEOPLE)
    )


async def _roster_assignee(ctx: Any, person_id: str) -> dict[str, str | None]:
    """The patch fields for a workspace member: an agent of this company, or a human
    member of this workspace with a Paperclip identity. Anything else is refused."""
    from routers.teams import agents as teams_agents

    agents = await _agent_names(ctx)
    if person_id in agents:
        return {"assignee_agent_id": person_id, "assignee_user_id": None}
    repo = teams_agents._repo()
    workspace = await repo.get_workspace(ctx.owner_id)
    if workspace is not None and person_id in (workspace.member_user_ids or []):
        identity = await repo.get_identity(person_id)
        if identity is not None and identity.paperclip_user_id:
            return {"assignee_agent_id": None, "assignee_user_id": identity.paperclip_user_id}
    raise ApiProblem("INVALID_REQUEST", "Choose someone in this workspace.")


async def _run_issue_action(issue_id: str, action_id: str, body: ActivityActionRequest, ctx: Any) -> ActivityItem:
    from routers.teams import issues as teams_issues
    from routers.teams.schemas import PatchIssueBody

    admin = _admin()

    async def fresh() -> dict:
        issue = await admin.get_issue(issue_id=issue_id, session_cookie=ctx.session_cookie)
        if not isinstance(issue, dict) or issue.get("companyId") != ctx.company_id:
            raise ApiProblem("ACTIVITY_NOT_FOUND", "Activity item not found.")
        return issue

    names = await _agent_names(ctx)
    issue = await fresh()
    item = render_issue(issue, agent_names=names, ctx=ctx, people=await _people(ctx, [issue], ("createdByUserId",)))
    value = (body.value or "").strip()
    if action_id == "change_status":
        if value not in _CHANGEABLE_STATUSES:
            raise ApiProblem("INVALID_REQUEST", "Choose one of the listed statuses.")
        if issue.get("status") == value:
            return item  # idempotent repeat
        _require_action(item, action_id)
        await teams_issues.patch_issue(issue_id, PatchIssueBody(status=value), ctx)
    elif action_id == "reassign":
        _require_action(item, action_id)
        fields = await _roster_assignee(ctx, value)
        if (fields["assignee_agent_id"] and issue.get("assigneeAgentId") == fields["assignee_agent_id"]) or (
            fields["assignee_user_id"] and issue.get("assigneeUserId") == fields["assignee_user_id"]
        ):
            return item  # idempotent repeat
        await teams_issues.patch_issue(issue_id, PatchIssueBody(**fields), ctx)
    else:
        _require_action(item, action_id)
    issue = await fresh()
    return render_issue(issue, agent_names=names, ctx=ctx, people=await _people(ctx, [issue], ("createdByUserId",)))


async def _fresh_interaction(ctx: Any, issue_id: str, interaction_id: str) -> tuple[dict, dict]:
    """The interaction and its issue, re-read and fenced to this workspace's company
    (the same check as the Teams ``CompanyIssueId`` fence)."""
    admin = _admin()
    issue, rows = await asyncio.gather(
        admin.get_issue(issue_id=issue_id, session_cookie=ctx.session_cookie),
        admin.list_issue_interactions(issue_id=issue_id, session_cookie=ctx.session_cookie),
    )
    if not isinstance(issue, dict) or issue.get("companyId") != ctx.company_id:
        raise ApiProblem("ACTIVITY_NOT_FOUND", "Activity item not found.")
    found = next((r for r in _rows(rows, "interactions") if r.get("id") == interaction_id), None)
    if found is None or found.get("companyId") not in (None, ctx.company_id):
        raise ApiProblem("ACTIVITY_NOT_FOUND", "Activity item not found.")
    return found, issue


async def _run_interaction_action(
    issue_id: str, interaction_id: str, action_id: str, body: ActivityActionRequest, ctx: Any
) -> ActivityItem:
    """Confirm / reject / answer through the Teams interaction routes, their trial gate included."""
    from core.services.paperclip_admin_client import PaperclipApiError
    from routers.teams import interactions as teams_interactions
    from routers.teams.schemas import (
        AcceptInteractionBody,
        InteractionAnswer,
        RejectInteractionBody,
        RespondInteractionBody,
    )

    names = await _agent_names(ctx)
    interaction, issue = await _fresh_interaction(ctx, issue_id, interaction_id)
    item = render_interaction(interaction, issue, agent_names=names, ctx=ctx)
    if interaction.get("status") != "pending" and action_id in ("accept", "reject", "answer"):
        return item  # already resolved (a retried tap, or someone else): the current state
    action = _require_action(item, action_id)
    note = (body.note or "").strip() or None
    value = (body.value or "").strip()
    try:
        if action_id == "accept":
            await teams_interactions.accept_interaction(issue_id, interaction_id, AcceptInteractionBody(), ctx)
        elif action_id == "reject":
            if action.input and action.input.required and not note:
                raise ApiProblem("INVALID_REQUEST", "Say why.")
            await teams_interactions.reject_interaction(
                issue_id, interaction_id, RejectInteractionBody(reason=note), ctx
            )
        elif action_id == "answer" and action.input is not None:
            question = _answerable(interaction.get("payload") or {}) or {}
            if action.input.kind == "select":
                if value not in {o.value for o in action.input.options or []}:
                    raise ApiProblem("INVALID_REQUEST", "Choose one of the listed answers.")
                answer = InteractionAnswer(question_id=question["id"], option_ids=[value])
            else:
                if not value:
                    raise ApiProblem("INVALID_REQUEST", "An answer needs text.")
                answer = InteractionAnswer(question_id=question["id"], option_ids=[], other_text=value[:4000])
            await teams_interactions.respond_interaction(
                issue_id, interaction_id, RespondInteractionBody(answers=[answer]), ctx
            )
    except PaperclipApiError:
        # Lost a race with another answer: answer with what stands, never a 500.
        interaction, issue = await _fresh_interaction(ctx, issue_id, interaction_id)
        if interaction.get("status") == "pending":
            raise
    interaction, issue = await _fresh_interaction(ctx, issue_id, interaction_id)
    return render_interaction(interaction, issue, agent_names=names, ctx=ctx)


async def _run_login_action(
    auth: AuthContext, request_id: str, action_id: str, body: ActivityActionRequest
) -> ActivityItem:
    """Answer an agent's ask. The secret arrives as ``value`` (JSON for a login) and goes straight
    to the website-logins store; it is never logged, echoed or written to a message."""
    import json

    from core.services import website_logins

    owner_id = resolve_owner_id(auth)
    row = await website_logins.get_request(owner_id, request_id)
    if row is None:
        raise ApiProblem("ACTIVITY_NOT_FOUND", "Activity item not found.")
    item = render_login_request(row, seen=None, admin=auth.is_org_admin)
    if item.state != "open":
        return item  # answered by someone else, declined, or the agent stopped waiting
    if not auth.is_org_admin:
        raise ApiProblem("ACTION_UNAVAILABLE", LOGIN_ADMIN_ONLY)
    _require_action(item, action_id)
    answer: dict[str, str] | None = None
    if action_id == "provide":
        try:
            parsed = json.loads(body.value or "") if row.get("kind") == "login" else {"code": body.value or ""}
        except ValueError:
            parsed = None
        if not isinstance(parsed, dict):
            raise ApiProblem("INVALID_REQUEST", "Enter the username and password.")
        answer = {k: str(v) for k, v in parsed.items() if k in ("username", "password", "code")}
    try:
        row = await website_logins.answer(
            owner_id, request_id, user_id=auth.user_id, value=answer, user_name=auth.email
        )
    except website_logins.LoginError as exc:
        raise ApiProblem("INVALID_REQUEST", str(exc)) from exc
    return render_login_request(row, seen=None, admin=True)


async def _run_extension_action(auth: AuthContext, request_id: str, action_id: str) -> ActivityItem:
    """Any member's answer to an agent's extension ask: "Add" installs it (and restarts a running
    browser), exactly once however many people press it."""
    from core.services import browser_control, browser_extensions

    owner_id = resolve_owner_id(auth)
    row = await browser_extensions.get_request(owner_id, request_id)
    if row is None:
        raise ApiProblem("ACTIVITY_NOT_FOUND", "Activity item not found.")
    item = render_extension_request(row, seen=None)
    if item.state != "open":
        return item
    _require_action(item, action_id)
    if action_id == "add":
        try:
            await browser_extensions.add_from_webstore(owner_id, row["extensionId"], auth.user_id)
        except browser_extensions.ExtensionError as exc:
            raise ApiProblem("INVALID_REQUEST", str(exc)) from exc
        try:
            await browser_control.restart_soon(owner_id)
        except browser_control.BrowserError:
            pass  # no browser yet: the next start loads it
    await browser_extensions.settle_request(
        owner_id, request_id, "added" if action_id == "add" else "declined", auth.user_id
    )
    return render_extension_request(await browser_extensions.get_request(owner_id, request_id) or row, seen=None)


async def run_action(
    auth: AuthContext, item_id: str, action_id: str, body: ActivityActionRequest, *, teams_ctx: TeamsCtx
) -> ActivityItem:
    """Map one Activity action to the real call, re-authorized for this viewer,
    and answer with the updated item. A repeated decision returns the current state."""
    parts = item_id.split(":")
    if parts[0] == "login" and len(parts) == 2:
        return await _run_login_action(auth, parts[1], action_id, body)
    if parts[0] == "extension" and len(parts) == 2:
        return await _run_extension_action(auth, parts[1], action_id)
    if parts[0] == "nethold" and len(parts) == 2:
        return await _run_network_hold_action(auth, parts[1], action_id)
    if parts[0] in ("join", "pickup") and len(parts) == 4 and all(_ID_RE.match(x) for x in parts[1:]):
        return await _run_join_action(auth, parts[1], parts[2], parts[3], action_id, pickup=parts[0] == "pickup")
    if parts[0] in _MESSAGE_KINDS and len(parts) == 4:
        return await _run_message_action(auth, parts[0], parts[1:], action_id, body)
    if len(parts) == 3 and parts[0] == "paperclip" and _ID_RE.match(parts[2]):
        ctx = await teams_ctx()
        if parts[1] == "approval":
            return await _run_approval_action(parts[2], action_id, body, ctx)
        if parts[1] == "assigned_issue":
            return await _run_issue_action(parts[2], action_id, body, ctx)
    if len(parts) == 4 and parts[:2] == ["paperclip", "interaction"] and all(_ID_RE.match(x) for x in parts[2:]):
        return await _run_interaction_action(parts[2], parts[3], action_id, body, await teams_ctx())
    raise ApiProblem("ACTIVITY_NOT_FOUND", "Activity item not found.")
