"""Each app's own conversation: a hidden channel where its builder works and the people who can open
the app talk to it (docs/apps/app-conversation-design.md, 4 Nov 2026).

Like session chat (``session_chat``): a deterministic room id, created the first time it is needed (an
older app's on its first open in Canvas), readers seated when they open it. The bridge keys an agent's
session by room, so each app builds in parallel with the room it came from and with other apps.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta

from core.auth import AuthContext, resolve_owner_id
from core.constants import SYSTEM_ACTOR_ID
from core.dynamodb import plain
from core.repositories import apps_repo, messaging_repo, tasks_repo
from core.services import apps_room, clerk_admin, messaging, session_chat, tasks_access
from core.services import apps_service as svc
from core.services.apps_service import AppsRefused, BuilderCaller

logger = logging.getLogger(__name__)

# Fixed forever: a new value would orphan every app's conversation.
NAMESPACE = uuid.UUID("5d0f3b9e-8c2a-4f61-9e47-a1b2c3d4e5f6")
_NO_AGENT = AppsRefused("no_agent", "There is no agent to build this app.", "add an agent to the workspace")


def conversation_room_id(app_id: str) -> str:
    return str(uuid.uuid5(NAMESPACE, "app-conversation:" + app_id))


def _seat_shape(agent: dict) -> dict:
    """A builder as the conversation seats it. Every human message there is for it (``messaging``'s
    dispatch treats an app's conversation like a DM), so ``mentions`` is enough."""
    return {
        "kind": "agent",
        "id": agent["id"],
        "displayName": agent.get("displayName") or "Agent",
        "mentionHandle": agent.get("mentionHandle"),
        "role": "member",
        "listenMode": "mentions",
    }


async def _roster_agent(owner_id: str, agent_id: str) -> dict | None:
    try:
        roster = await messaging.load_agent_roster(owner_id)
    except messaging.ApiProblem:
        return None
    agent = next((a for a in roster if a["id"] == agent_id), None)
    if agent is None:
        return None
    handle = messaging.mention_handles(roster)[agent["id"]]
    return _seat_shape({"id": agent["id"], "displayName": agent.get("name"), "mentionHandle": handle})


async def _default_builder(app: dict, room_id: str | None) -> dict | None:
    """The builder when no agent asked: whom ``apps_room._builder`` picks in ``room_id``, else the CEO."""
    if room_id:
        picked = await apps_room._builder(app, room_id)
        if picked:
            return _seat_shape(picked)
    ceo = await session_chat._default_agents(app["owner_id"])
    return _seat_shape(ceo[0]) if ceo else None


async def _create(app: dict, room_id: str, builder: dict | None) -> dict:
    try:
        return await messaging_repo.create_room(
            owner_id=app["owner_id"],
            room_id=room_id,
            client_room_id=f"app-conversation-v1:{app['app_id']}",
            creator_user_id=SYSTEM_ACTOR_ID,
            # The agent's turn carries the room name: this is how it knows where it is.
            name=f"App · {svc._display(app)}",
            kind="channel",
            members=[builder] if builder else [],
            req_hash=messaging_repo.request_hash("app-conversation-v1", app["app_id"]),
            extra={"hidden": True, "app": {"app_id": app["app_id"]}},
        )
    except messaging_repo.MessagingRepoError:
        # A concurrent first open created it: use theirs.
        meta = await messaging_repo.get_room(room_id)
        if meta is None:
            raise
        return meta


async def _ensure(app: dict, builder: dict | None = None) -> dict:
    """The app's conversation, created on first need, and recorded on the app. An older app's builder
    is the one its room would pick (``apps_room._builder``)."""
    room_id = conversation_room_id(app["app_id"])
    meta = await messaging_repo.get_room(room_id)
    if meta is None:
        meta = await _create(app, room_id, builder or await _default_builder(app, app.get("room_id")))
    if app.get("conversation_id") != room_id:
        await apps_repo.patch(app["app_id"], {"conversation_id": room_id})
        app["conversation_id"] = room_id
    return meta


async def _seat(owner_id: str, meta: dict, user_id: str) -> None:
    if await messaging_repo.get_member(meta["roomId"], "human", user_id) is not None:
        return
    try:
        await session_chat._seat(owner_id, meta, user_id, full="This app's conversation is full.")
    except messaging.ApiProblem as e:
        raise AppsRefused("conversation_full", str(e)) from None


async def _can_open(owner_id: str, app: dict, auth: AuthContext) -> bool:
    """Whoever can open the app, by its sharing (``apps_service._can_open``). A seat is never a grant:
    narrowing the sharing must take the conversation away too (``unseat_who_cannot_open``)."""
    if svc._can_open(app, auth, False):
        return True
    room_id = app.get("room_id") if app.get("open_to") == "room" else None
    return bool(room_id) and await messaging.still_reads_room(owner_id, auth.user_id, room_id, owner_id)


async def may_open(owner_id: str, app: dict, user_id: str) -> bool:
    """``_can_open`` for a person known only by id (the CLI, a room door): no token says whether they are
    an admin, so Clerk is asked, and only when the sharing alone says no."""
    if await _can_open(owner_id, app, AuthContext(user_id=user_id, org_id=owner_id)):
        return True
    members = await clerk_admin.list_organization_members(owner_id)  # [] on a Clerk error: fail closed
    return any(m["user_id"] == user_id and m.get("role") in ("org:admin", "admin") for m in members)


async def require_opens(owner_id: str, app: dict, caller: BuilderCaller) -> None:
    """A person (not an agent: builders keep the workspace) reads or talks to an app only if they can open it."""
    if caller.actor_kind == "person" and not await may_open(owner_id, app, caller.actor_id):
        raise AppsRefused("not_allowed", "This app is not shared with you.", "ask its creator to share it")


async def seat_still_opens(owner_id: str, app_id: str, user_id: str) -> bool:
    """``messaging.require_room_member``'s check on an app's conversation: a seat never outlives the
    right to open the app (a gone app opens for nobody)."""
    try:
        app = await svc._owned(owner_id, app_id)
    except AppsRefused:
        return False
    return await may_open(owner_id, app, user_id)


async def unseat_who_cannot_open(app: dict) -> int:
    """After a sharing change: every person seated in the conversation who may no longer open the app
    leaves it (its creator stays; an admin is seated again on their next open). Returns how many left."""
    room_id = app.get("conversation_id")
    if not room_id or app["open_to"] in ("workspace", "public"):
        return 0
    room = app.get("room_id") if app["open_to"] == "room" else None
    removed = 0
    for m in await messaging_repo.list_members(room_id):
        if m.get("kind") != "human" or m["id"] == app.get("created_by"):
            continue
        if room and await messaging.still_reads_room(app["owner_id"], m["id"], room, app["owner_id"]):
            continue
        removed += await session_chat._unseat(room_id, m["id"])
    return removed


async def open_conversation(auth: AuthContext, app_id: str) -> dict:
    """Canvas's door: the app's conversation, created on the first open, with the caller seated."""
    owner_id = resolve_owner_id(auth)
    app = await svc._owned(owner_id, app_id)
    # A removed member whose token has not expired yet is refused before any write (as session chat).
    reader = await messaging_repo.get_user_stream(owner_id, auth.user_id)
    if reader is not None and not reader.get("active"):
        raise AppsRefused("app_not_found", "That app does not exist.")
    if not await _can_open(owner_id, app, auth):
        raise AppsRefused("not_a_member", "Only people who can open the app can talk to its builder.")
    meta = await _ensure(app)
    await _seat(owner_id, meta, auth.user_id)
    return {"room_id": meta["roomId"]}


async def _request_of(owner_id: str, room_id: str | None) -> tuple[dict, str | None] | None:
    """A Worker hands an app over from its request's own room (``--room <request room>``): that request (task row)
    and the room it was asked in. Who asked and where come from the row, never from the caller's word."""
    meta = (await messaging_repo.get_room(room_id) if room_id else None) or {}
    task_id = (meta.get("task") or {}).get("taskId")
    if not task_id or meta.get("ownerId") != owner_id:
        return None
    tree = await tasks_repo.get_task_tree(owner_id, task_id)
    if tree["task"] is None:
        return None
    return tree["task"], tasks_access._origin_room(tree["subscriptions"], tree["task"].get("audience") or {})


async def _requester(owner_id: str, user_id: str | None, room_id: str | None) -> str | None:
    """The person an agent speaks for, only when they sit in the room they asked in (as ``up``'s ``requested_by``)."""
    if not user_id or not room_id:
        return None
    try:
        await messaging.require_room_member(owner_id, user_id, room_id)
    except messaging.ApiProblem:
        logger.info("apps: requested_by is not a member of the origin room; ignored")
        return None
    return user_id


async def _attributed(
    caller: BuilderCaller, text: str, person: str | None, room_id: str | None, via: str = "via CLI"
) -> str:
    """``"{name} (in #{room}): {text}"`` or ``"{name} (via CLI): {text}"``: who said it, and where."""
    if person:
        who = await messaging.human_display_name(person)
    else:
        who = ((await _roster_agent(caller.owner_id, caller.actor_id)) or {}).get("displayName") or "An agent"
    if room_id:
        meta = await messaging_repo.get_room(room_id) or {}
        dm = meta.get("kind") in ("dm", "group-dm") or not meta.get("name")
        where = "a direct message" if dm else f"#{meta['name']}"
        return f"{who} (in {where}): {text}"
    return f"{who} ({via}): {text}"


async def _relay(app: dict, meta: dict, key: str, text: str) -> dict:
    """One system line in the conversation that wakes its builder. ``key`` is the receipt: a resend
    (the same source message) replays it, so the builder hears it once."""
    builder = await apps_room._builder(app, meta["roomId"])
    if builder is None:
        raise _NO_AGENT
    message = await apps_room.post_system_line(
        app, key, text, [{"agentId": builder["id"], "eventKind": "user_request"}], room_id=meta["roomId"]
    )
    if message is None:
        raise AppsRefused("conversation_closed", "The app's conversation is closed.")
    return message


async def _reserve(
    caller: BuilderCaller, name: str, origin_room_id: str | None, requester: str | None
) -> tuple[dict, bool]:
    """The app record in state ``requested``, once per name: a second start finds the first's. How it was
    got: "made" by this call, "found" already there, or "raced" (a concurrent start made it first)."""
    how = "found"
    owner_id = caller.owner_id
    app = await apps_repo.get_by_name(owner_id, name)
    if app is None:
        try:
            app = await apps_repo.create(
                owner_id=owner_id,
                name=name,
                room_id=origin_room_id,
                origin_room_id=origin_room_id,
                created_by=requester,
                actor_id=caller.actor_id,
                actor_kind=caller.actor_kind,
                origin={"kind": "asked"},
                services=[],
                open_to="room" if origin_room_id else "workspace",
                budget_usd=0,
                display_name=svc._display({"name": name}),
                state="requested",
            )
            how = "made"
        except apps_repo.NameTaken:
            app = await apps_repo.claimed_by(owner_id, name)  # a start racing this one won the name
            if app is None:
                raise
            how = "raced"
    app = plain(app)
    if app.get("state") == "destroyed":
        raise AppsRefused("app_destroyed", f"{name} was removed.", "pick another name")
    if app.get("state") != "requested":
        raise AppsRefused(
            "app_exists",
            f"{name} is already an app in this workspace.",
            "pick another name, or ask its builder in the app's conversation in Canvas",
        )
    return app, how


_HEARD_FOR = timedelta(minutes=10)


async def _builder_busy(owner_id: str, room_id: str, text: str) -> bool:
    """A run in the conversation still beating, or this same request posted there in the last 10 minutes
    (a run can take a while to open: a quick retry must not post it twice)."""
    now = datetime.now(UTC)
    if any(svc._working(r, now) for r in await messaging_repo.list_open_runs_for_room(owner_id, room_id)):
        return True
    since = (now - _HEARD_FOR).isoformat()
    return any(
        (m.get("author") or {}).get("kind") == "system" and m.get("text") == text and str(m.get("createdAt")) >= since
        for m in await messaging_repo.list_messages_before(room_id, 10**18, 50)
    )


async def start(
    caller: BuilderCaller,
    name: str,
    request_text: str,
    *,
    origin_room_id: str | None = None,
    requested_by: str | None = None,
    via: str = "via CLI",
) -> dict:
    """Hand an app request to its own conversation: reserve the app, open its conversation, post the
    request there as from the person, and wake the builder there. The room is free at once."""
    owner_id = caller.owner_id
    svc._check_name(name)
    request = await _request_of(owner_id, origin_room_id)
    if request:  # started by a request: the app comes from where it was asked, for the person who asked
        origin_room_id, requested_by = request[1], request[0].get("ownerUserId")
    if origin_room_id:
        await svc._check_room(owner_id, origin_room_id)
        if ((await messaging_repo.get_room(origin_room_id)) or {}).get("app"):
            raise AppsRefused(
                "room_is_app",
                "That room is an app's own conversation, not a room to start an app from.",
                "work on that app there, or start a new app from a room or with no --room",
            )
    if caller.actor_kind == "person":
        person = caller.actor_id
    elif request:
        person = requested_by  # the request's own row says who asked
    else:
        person = await _requester(owner_id, requested_by, origin_room_id)
    builder = await _roster_agent(owner_id, caller.actor_id) if caller.actor_kind == "agent" else None
    builder = builder or await _default_builder({"owner_id": owner_id}, origin_room_id)
    if builder is None:
        raise _NO_AGENT
    app, how = await _reserve(caller, name, origin_room_id, person)
    if request:  # Canvas lists it as a creation of this request (the first request that made it stays)
        await tasks_repo.put_creation(owner_id, {"capId": app["app_id"], "kind": "app", "taskId": request[0]["taskId"]})
    if person and not app.get("created_by"):  # reserved before anyone asked: the first person who did made it
        if await apps_repo.patch(app["app_id"], {"created_by": person}, expect={"created_by": None}):
            app["created_by"] = person
    meta = await _ensure(app, builder)
    if person:
        await _seat(owner_id, meta, person)
    # The builder is told which app this conversation builds: unnamed, it once answered from memory with another app.
    text = await _attributed(caller, request_text, person, origin_room_id, via)
    text += f"\n\nThis conversation builds {svc._display(app)}, named {app['name']}."
    # The receipt is this request (who and what): a retry replays it, another person's request is its own line.
    key = f"apps-start:{app['app_id']}:{hashlib.sha256(text.encode()).hexdigest()[:16]}"
    # Found idle: say it again, which wakes the builder. Never on a race: the winner's line is being posted now.
    if how == "found" and not app.get("sources") and not await _builder_busy(owner_id, meta["roomId"], text):
        key += f":{uuid.uuid4()}"
    message = await _relay(app, meta, key, text)
    # Watched from the request, not the first upload: a builder that never uploads still ends in "stopped".
    svc.watch(owner_id, app["app_id"])
    display, canvas_url = svc._display(app), svc._canvas_url(app["app_id"])
    line = f"Started {display}. It's being built in its own conversation in Canvas: {canvas_url}"
    if request:  # a Worker: its result is the request's answer, posted for it
        said = line if how == "made" else f"{display} was already started. It's being built in Canvas: {canvas_url}"
        next_ = f'answer with this line as your result, then end: "{said}"'
    elif how != "made":
        next_ = "this app was already started; post nothing in the room"
    elif origin_room_id:
        next_ = f'post this line in the room as written, then end your turn: "{line}"'
    else:
        next_ = f"the builder is working on it in the app's conversation: {canvas_url}"
    return {
        "app_id": app["app_id"],
        "conversation_id": meta["roomId"],
        "canvas_url": canvas_url,
        "next": next_,
        "relayed": not message.get("replayed"),  # whether the builder heard a new request line just now
    }


_FILLER = {
    "a",
    "an",
    "the",
    "for",
    "our",
    "my",
    "us",
    "we",
    "to",
    "of",
    "and",
    "with",
    "that",
    "where",
    "can",
    "build",
    "make",
    "create",
    "me",
    "please",
    "page",
    "simple",
    "small",
    "new",
    "app",
    "website",
    "site",
    "tool",
}


def name_from_request(text: str) -> str:
    """A DNS-label name from the request's first meaningful words: "Build a sign-up form for our pottery
    workshop" → "sign-up-form-pottery". Falls back to "app"."""
    words = [w for w in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", text.lower()) if w not in _FILLER]
    name = "-".join(words[:4])[:40].strip("-")
    return name or "app"


async def start_as_member(auth: AuthContext, request_text: str) -> dict:
    """Canvas's "What should we build?": the person asks with no room. Never lands on another app: a name
    already taken gets -2, -3, …"""
    owner_id = resolve_owner_id(auth)
    base = name_from_request(request_text)
    name, n = base, 1
    while await apps_repo.get_by_name(owner_id, name):
        n += 1
        name = f"{base[: 40 - len(str(n)) - 1]}-{n}"
    caller = BuilderCaller(owner_id=owner_id, actor_id=auth.user_id, actor_kind="person")
    return await start(caller, name, request_text, via="in Canvas")


async def ask(
    caller: BuilderCaller,
    app_id: str,
    text: str,
    *,
    origin: dict | None = None,
    client_message_id: str | None = None,
) -> dict:
    """Relay a message to the app's builder in its conversation, attributed. ``origin``: ``room_id`` and
    ``requested_by`` when an agent passes on what a person said in a room. ``client_message_id`` (the
    source message's id) makes a resend a no-op."""
    owner_id = caller.owner_id
    app = await svc._owned(owner_id, app_id)
    await require_opens(owner_id, app, caller)
    room_id = (origin or {}).get("room_id")
    request = await _request_of(owner_id, room_id)
    if request:  # passed on from a request: where and by whom it was asked, from the request's row
        room_id = request[1]
    if room_id:
        await svc._check_room(owner_id, room_id)
    if caller.actor_kind == "person":
        person = caller.actor_id
    elif request:
        person = request[0].get("ownerUserId")
    else:
        person = await _requester(owner_id, (origin or {}).get("requested_by"), room_id)
    meta = await _ensure(app)  # nobody is seated: the line says who asked, and opening decides who may read it
    said = await _attributed(caller, text, person, room_id)
    message = await _relay(app, meta, f"apps-ask:{app_id}:{client_message_id or uuid.uuid4()}", said)
    return {"conversation_id": meta["roomId"], "message_id": message["messageId"]}


async def ask_as_member(auth: AuthContext, app_id: str, text: str, client_message_id: uuid.UUID | None = None) -> dict:
    """Canvas: the person says it in the app's conversation themselves, addressed to its builder."""
    owner_id = resolve_owner_id(auth)
    room_id = (await open_conversation(auth, app_id))["room_id"]
    app = await svc._owned(owner_id, app_id)
    builder = await apps_room._builder(app, room_id)
    try:
        message = await messaging.create_human_message(
            auth,
            room_id,
            messaging.CreateMessageRequest(
                client_message_id=client_message_id or uuid.uuid4(),
                text=text,
                mentions=[messaging.MemberRef(kind="agent", id=builder["id"])] if builder else [],
                reply_to_message_id=None,
                thread_root_message_id=None,
            ),
            via="apps",
        )
    except messaging.ApiProblem as e:
        raise AppsRefused("not_a_member", str(e)) from None
    return {"conversation_id": room_id, "message_id": str(message.message_id)}
