"""Activity feed v1 (rooms F5): what needs *you*, server-rendered and actionable.

Two sources, one item shape:

* **Message items** (``mention`` / ``dm`` / ``thread_reply``) are per-reader pointer
  rows (``ACTIVITY#<home>#<user>``) written by the messaging projector -- the
  existing stream consumer -- after the message commits. Rows carry no text. The
  read path here re-gates every room on a strong membership read, drops deleted
  messages, and renders title/preview from the CURRENT message, so leaving a room
  or deleting a message leaves nothing stale or leaked behind.
* **Paperclip items** (``paperclip`` with ``subtype`` ``approval`` /
  ``assigned_issue``) are live decisions, pulled at read time with the viewer's
  own Paperclip session and merged into the FIRST page only. A Paperclip failure
  never fails the feed: ``sources.paperclip`` says ``unavailable``.

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
ActivityKind = Literal["mention", "dm", "thread_reply", "reaction", "agent_run", "paperclip"]
ActorKind = Literal["human", "agent", "system"]
LinkKind = Literal["issue", "room", "thread", "run", "approval"]

DEFAULT_LIMIT = 30
MAX_LIMIT = 100
UNSEEN_CAP = 99
PAPERCLIP_LIMIT = 20
# Page 1 never waits on Paperclip longer than this (the admin client's own timeout is 15 s).
PAPERCLIP_BUDGET_S = 3.0
TITLE_CHARS = 120
PREVIEW_TEXT_CHARS = 600
FIELD_CHARS = 200
MAX_FIELDS = 8
_MESSAGE_KINDS = ("mention", "dm", "thread_reply")


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
    kind: Literal["text", "select", "person"]
    label: str
    required: bool
    options: list[ActionOption] | None = None


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
    subtype: Literal["approval", "assigned_issue"] | None = None
    room_id: str | None = None
    message_id: str | None = None
    thread_root_message_id: str | None = None
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
    # Pointer sks start with an ISO timestamp; anything else is not a cursor we minted.
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T[^#]+#[0-9a-f-]{36}", sk):
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
    return "your conversation"


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
    read = bool(done_at) or not _newer_than(created, seen) or (kind in ("mention", "dm") and int(row["seq"]) <= floor)
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
        label = _LIGHTBULB_CARDS[key.split(":", 1)[1]]
        return (f"{label} — requested by {who}", p.text("summary", "reason", "title"), None)
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


async def _paperclip_items(ctx: Any) -> list[ActivityItem]:
    """Pending approvals waiting on the viewer (their own attention feed, joined
    to the full approval rows) plus open issues assigned to them."""
    admin = _admin()
    names, feed, approvals, issues = await asyncio.gather(
        _agent_names(ctx),
        admin.list_attention(
            company_id=ctx.company_id, session_cookie=ctx.session_cookie, params={"limit": str(PAPERCLIP_LIMIT)}
        ),
        admin.list_approvals(company_id=ctx.company_id, session_cookie=ctx.session_cookie),
        admin.list_issues(
            company_id=ctx.company_id,
            session_cookie=ctx.session_cookie,
            params={"assigneeUserId": "me", "status": ",".join(_OPEN_ISSUE_STATUSES), "limit": str(PAPERCLIP_LIMIT)},
        ),
    )
    by_id = {row["id"]: row for row in _rows(approvals, "approvals") if isinstance(row.get("id"), str)}
    pending = []
    for entry in _rows(feed, "items"):
        subject = entry.get("subject") if isinstance(entry.get("subject"), dict) else {}
        row = by_id.get(subject.get("id")) if entry.get("sourceKind") == "approval" else None
        if row and row.get("companyId") == ctx.company_id and row.get("status") == "pending":
            pending.append(row)
    mine = [
        issue
        for issue in _rows(issues, "issues")[:PAPERCLIP_LIMIT]
        if issue.get("companyId") == ctx.company_id and isinstance(issue.get("id"), str)
    ]
    people = await _people(ctx, pending + mine, ("requestedByUserId", "createdByUserId"))
    return [render_approval(row, agent_names=names, ctx=ctx, people=people) for row in pending] + [
        render_issue(issue, agent_names=names, ctx=ctx, people=people) for issue in mine
    ]


_APPROVAL_PEOPLE = ("requestedByUserId", "decidedByUserId")


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
    paperclip = await _paperclip_within_budget(teams_ctx)
    if paperclip is None:
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
    paperclip_task = None if cursor else asyncio.create_task(_paperclip_within_budget(teams_ctx))
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
        items = await _render_message_rows(owner_id, user_id, rows, seen)
        return ActivityPage(items=items, next=next_cursor, unseen_count=None, sources=ActivitySources(paperclip="ok"))

    assert paperclip_task is not None
    try:
        unseen_rows = await messaging_repo.list_activity_since(owner_id, user_id, seen, UNSEEN_CAP)
        gates = await _room_gates(owner_id, user_id, {r["roomId"] for r in rows + unseen_rows if r.get("roomId")})
        items = await _render_message_rows(owner_id, user_id, rows, seen, gates)
    except BaseException:
        paperclip_task.cancel()
        raise
    unseen = len(_unseen_message_rows(unseen_rows, gates))
    fetched = await paperclip_task
    paperclip, source = (fetched, "ok") if fetched is not None else ([], "unavailable")
    unseen += len(_unseen_paperclip(paperclip, seen))
    items = sorted(items + paperclip, key=lambda item: item.created_at, reverse=True)
    return ActivityPage(
        items=items, next=next_cursor, unseen_count=min(UNSEEN_CAP, unseen), sources=ActivitySources(paperclip=source)
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
            if await messaging_repo.get_member(room_id, "agent", author["id"]):
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


async def run_action(
    auth: AuthContext, item_id: str, action_id: str, body: ActivityActionRequest, *, teams_ctx: TeamsCtx
) -> ActivityItem:
    """Map one Activity action to the real call, re-authorized for this viewer,
    and answer with the updated item. A repeated decision returns the current state."""
    parts = item_id.split(":")
    if parts[0] in _MESSAGE_KINDS and len(parts) == 4:
        return await _run_message_action(auth, parts[0], parts[1:], action_id, body)
    if len(parts) == 3 and parts[0] == "paperclip" and _ID_RE.match(parts[2]):
        ctx = await teams_ctx()
        if parts[1] == "approval":
            return await _run_approval_action(parts[2], action_id, body, ctx)
        if parts[1] == "assigned_issue":
            return await _run_issue_action(parts[2], action_id, body, ctx)
    raise ApiProblem("ACTIVITY_NOT_FOUND", "Activity item not found.")
