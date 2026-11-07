"""Activity feed v1 (rooms F5): what needs *you*, server-rendered and actionable.

Two sources, one item shape:

* **Message items** (``mention`` / ``dm`` / ``thread_reply``) are per-reader pointer
  rows (``ACTIVITY#<home>#<user>``) written by the messaging projector -- the
  existing stream consumer -- after the message commits. Rows carry no text. The
  read path here re-gates every room on a strong membership read, drops deleted
  messages, and renders title/preview from the CURRENT message, so leaving a room
  or deleting a message leaves nothing stale or leaked behind.
* **Questions** (``task_question``): the viewer's open super-agent Questions, live state merged
  into the FIRST page only. Paperclip items (approvals, failed runs, assigned issues) are gone
  (Phase 1 task 7); ``sources.paperclip`` is always ``ok`` for old clients.

"Needs you" items are pinned above everything else on page 1, and ``attentionCount`` -- the rail
badge -- is those plus unseen mentions and DMs.

Every item carries a server-rendered ``summary`` and the viewer's ``actions``;
clients render both generically. One action route maps an action id to the real
call (the canonical message writer, a session or login decision).
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
from typing import Any, Literal

from pydantic import Field

from core.auth import AuthContext, resolve_owner_id
from core.repositories import messaging_repo
from core.services import messaging
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
# The trial recap (routers/billing.py) never waits on Paperclip longer than this.
PAPERCLIP_BUDGET_S = 3.0
TITLE_CHARS = 120
PREVIEW_TEXT_CHARS = 600
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
        return f"#{name}" if name else "a channel"
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
    return False


def _rows(value: Any, key: str) -> list[dict]:
    if isinstance(value, dict):
        value = value.get(key) or value.get("items") or []
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _admin():
    from routers.teams import agents as teams_agents

    return teams_agents._admin()


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


async def _live_items(owner_id: str, user_id: str) -> list[ActivityItem]:
    """Page 1's live decisions: the viewer's open Questions (§14.2, §14.5). Paperclip items are gone (Phase 1 task 7)."""
    return await _task_question_items(owner_id, user_id)


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


def _unseen_live(items: list[ActivityItem], seen: str | None) -> list[ActivityItem]:
    return [item for item in items if _newer_than(item.created_at, seen)]


async def unseen_counts(owner_id: str, user_id: str) -> dict[str, int]:
    """The unseen badge's items, counted by kind (an open Question by subtype: ``approval`` /
    ``interaction``). Same query and "unseen" rule as the feed's ``unseenCount``."""
    seen = await messaging_repo.get_activity_seen(owner_id, user_id)
    rows = await messaging_repo.list_activity_since(owner_id, user_id, seen, UNSEEN_CAP)
    gates = await _room_gates(owner_id, user_id, {r["roomId"] for r in rows if r.get("roomId")})
    live = await _live_items(owner_id, user_id)
    counts: dict[str, int] = {}
    for kind in [r["kind"] for r in _unseen_message_rows(rows, gates)] + [
        i.subtype or i.kind for i in _unseen_live(live, seen)
    ]:
        counts[kind] = counts.get(kind, 0) + 1
    return counts


async def list_activity(auth: AuthContext, *, cursor: str | None, limit: int) -> ActivityPage:
    owner_id = resolve_owner_id(auth)
    user_id = auth.user_id
    before = _decode_cursor(cursor) if cursor else None
    limit = max(1, min(int(limit), MAX_LIMIT))
    # Page 1's Paperclip fetch runs alongside every DynamoDB read below, bounded by its budget.
    live_task = None if cursor else asyncio.create_task(_live_items(owner_id, user_id))
    try:
        rows, seen = await asyncio.gather(
            messaging_repo.list_activity(owner_id, user_id, before_sk=before, limit=limit),
            messaging_repo.get_activity_seen(owner_id, user_id),
        )
    except BaseException:
        if live_task is not None:
            live_task.cancel()
        raise
    next_cursor = _encode_cursor(rows[-1]["sk"]) if len(rows) == limit else None
    if cursor:
        # Later pages: history only. Paperclip items are live decisions on page 1.
        items = await _render_rows(owner_id, user_id, rows, seen, admin=auth.is_org_admin)
        return ActivityPage(items=items, next=next_cursor, unseen_count=None, sources=ActivitySources(paperclip="ok"))

    assert live_task is not None
    try:
        unseen_rows = await messaging_repo.list_activity_since(owner_id, user_id, seen, UNSEEN_CAP)
        gates = await _room_gates(owner_id, user_id, {r["roomId"] for r in rows + unseen_rows if r.get("roomId")})
        items = await _render_rows(owner_id, user_id, rows, seen, gates, admin=auth.is_org_admin)
    except BaseException:
        live_task.cancel()
        raise
    unseen_messages = _unseen_message_rows(unseen_rows, gates)
    live = await live_task
    unseen_live = len(_unseen_live(live, seen))
    unseen = len(unseen_messages) + unseen_live
    pinned = [item for item in items + live if needs_you(item)]
    # Every needs-you item counts whether or not it was seen; mentions, DMs and Paperclip
    # items count while unseen. A thread reply (or a reaction) never badges.
    # Session items are not in `unseen` (a finished turn never badges), so a waiting one always
    # counts here. ponytail: only waiting items on this page count; an older one still waiting
    # past 30 newer rows is left off the badge.
    attention = (
        sum(1 for r in unseen_messages if r["kind"] in _BADGE_MESSAGE_KINDS)
        + unseen_live
        + sum(
            1
            for item in pinned
            # A login ask badges only the people who can answer it (admins get its actions).
            if item.kind in ("session", "reconnect")
            or (item.kind in ("login", "extension") and item.actions)
            or (item.kind not in ("login", "extension") and not _newer_than(item.created_at, seen))
        )
    )
    rest = sorted([item for item in items + live if not needs_you(item)], key=lambda i: i.created_at, reverse=True)
    # Approvals, then agents' questions, then failed runs; each newest first.
    pinned.sort(key=lambda i: (_PINNED_ORDER[i.subtype or ""], -i.created_at.timestamp()))
    return ActivityPage(
        items=pinned + rest,
        next=next_cursor,
        unseen_count=min(UNSEEN_CAP, unseen),
        attention_count=min(UNSEEN_CAP, attention),
        sources=ActivitySources(paperclip="ok"),  # kept for old clients; Paperclip items are gone
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


async def run_action(auth: AuthContext, item_id: str, action_id: str, body: ActivityActionRequest) -> ActivityItem:
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
    raise ApiProblem("ACTIVITY_NOT_FOUND", "Activity item not found.")
