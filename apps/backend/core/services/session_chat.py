"""Session chat: one hidden channel per shared session, where people talk beside the terminal.

Design record: PR #3807 (the spec is in git history). Membership is a projection of
session read access: whoever can read the session is seated when they open the chat, and nobody
is added or removed by hand. Nothing here reads or writes a session's control fields, so a chat
seat never lets anyone type into the terminal. Posting is the ordinary room message door.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from botocore.exceptions import ClientError

from core.auth import AuthContext, resolve_owner_id
from core.constants import SYSTEM_ACTOR_ID
from core.observability.metrics import put_metric
from core.repositories import messaging_repo
from core.services import livekit_huddles, local_sessions, messaging
from core.services.messaging import ApiProblem, RoomDetail

logger = logging.getLogger(__name__)

TITLE_MAX = 120


def session_room_id(owner_id: str, session_id: str) -> str:
    """Stable identity for one session's chat, like ``messaging.default_room_id``."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"lightbulb-session-room|{owner_id}|{session_id}"))


def _ceo_member(company: Any, roster: list[dict]) -> list[dict]:
    """The workspace's CEO on `mentions` (ruling 2026-10-07): the role-keyed starter id, else a
    legacy roster's root agent (role ``ceo``, reporting to nobody), else nobody."""
    if getattr(company, "status", None) != "active":
        return []
    starter = (getattr(company, "starter_agent_ids", None) or {}).get("ceo")
    if starter:
        ceo = next((agent for agent in roster if agent["id"] == starter), None)
    else:
        ceo = next((agent for agent in roster if agent.get("role") == "ceo" and not agent.get("reportsTo")), None)
    if ceo is None:
        return []
    return [
        {
            "kind": "agent",
            "id": ceo["id"],
            "displayName": ceo["name"],
            "mentionHandle": messaging.mention_handles(roster)[ceo["id"]],
            "role": "member",
            "listenMode": "mentions",
        }
    ]


async def _default_agents(owner_id: str) -> list[dict]:
    from core.repositories.workspace_repo import WorkspaceRepo

    company = await WorkspaceRepo().resolve_owner_row(owner_id, consistent_read=True)
    if getattr(company, "status", None) != "active":
        return []
    return _ceo_member(company, await messaging.load_agent_roster(owner_id))


def _room_name(row: dict) -> str:
    # The agent's turn carries the room name, so "Session chat" is how it knows where it is.
    title = (row.get("displayTitle") or row.get("title") or row.get("cwdName") or "Session")[:TITLE_MAX]
    return f"Session chat · {title}"


async def _create(owner_id: str, room_id: str, row: dict) -> dict:
    owner_user = row["humanUserId"]
    members = [
        {
            "kind": "human",
            "id": owner_user,
            "displayName": await messaging.human_display_name(owner_user),
            "mentionHandle": None,
            "role": "owner",
            "listenMode": None,
        },
        *await _default_agents(owner_id),
    ]
    try:
        return await messaging_repo.create_room(
            owner_id=owner_id,
            room_id=room_id,
            client_room_id=f"session-room-v1:{row['sessionId']}",
            creator_user_id=owner_user,
            name=_room_name(row),
            kind="channel",
            members=members,
            req_hash=messaging_repo.request_hash("session-room-v1", row["sessionId"]),
            extra={"hidden": True, "session": {"sessionId": row["sessionId"], "ownerId": owner_user}},
        )
    except messaging_repo.MessagingRepoError:
        # A concurrent first open created it (the META put cancels): use theirs.
        meta = await messaging_repo.get_room(room_id)
        if meta is None:
            raise
        return meta


async def _set_archived(meta: dict, archived: bool) -> dict:
    """Install the archived state. A lost revision race (a message, a seat) re-reads and tries
    again: the end and resume hooks run once, and a chat left on screen never reopens the door."""
    updates = (
        {"archivedAt": messaging.utc_now().isoformat(), "archivedBy": SYSTEM_ACTOR_ID}
        if archived
        else {"archivedAt": None, "archivedBy": None}
    )
    for attempt in range(4):
        try:
            meta = await messaging_repo.patch_room_meta(meta["roomId"], updates)
        except ClientError as exc:
            if not messaging_repo._is_cancelled(exc) or attempt == 3:
                raise
            meta = await messaging_repo.get_room(meta["roomId"]) or meta
            if archived == bool(meta.get("archivedAt")):
                return meta  # another writer installed it
            continue
        if archived:
            await livekit_huddles.close_room(meta["roomId"])  # an archived chat holds no huddle; never raises
        await messaging._publish_room_metadata(meta)
        return meta
    return meta  # pragma: no cover -- the loop returns or raises


async def _follow_lifecycle(owner_id: str, session_id: str, meta: dict) -> dict:
    """Bring the room in step with the session row AS IT IS NOW, never as a caller saw it: an
    ended session's chat is read only, a resumed one (same id, endedAt cleared) takes posts
    again, and the room carries the session's current title. Read again after each write, so a
    late end handler cannot archive a session that has since resumed (or the reverse)."""
    row = None
    for _ in range(4):
        row = await local_sessions.repo.get_session(owner_id, session_id)
        if row is None:
            return meta
        ended = row.get("endedAt") is not None
        if ended == bool(meta.get("archivedAt")):
            break
        meta = await _set_archived(meta, ended)
    name = _room_name(row)
    if meta.get("name") != name:  # the session got its title, or its owner renamed it
        try:
            meta = await messaging_repo.patch_room_meta(meta["roomId"], {"name": name})
        except ClientError as exc:
            if not messaging_repo._is_cancelled(exc):
                raise
            return meta  # lost a revision race: the next open names it
        await messaging._publish_room_metadata(meta)
    return meta


async def _active(owner_id: str, user_id: str) -> bool:
    """A current member of the workspace: their private stream is on (revoke_human_membership
    turns it off). Strongly read."""
    stream = await messaging_repo.get_user_stream(owner_id, user_id)
    return bool(stream and stream.get("active"))


async def _seat(owner_id: str, meta: dict, user_id: str, *, full: str = "This session's chat is full.") -> dict:
    """Seat one reader as a plain member. A lost revision race re-reads and retries. Also the app
    conversation's door (``app_conversation``), with its own ``full`` refusal."""
    if await messaging_repo.get_user_stream(owner_id, user_id) is None:
        # Only a person who never had a stream gets one here (create-if-absent). An inactive
        # row is a removed member: ensure_user_stream would turn it back on, so it is never
        # called for one (open_chat refused them before this).
        await messaging_repo.ensure_user_stream(owner_id, user_id)
    member = {
        "kind": "human",
        "id": user_id,
        "displayName": await messaging.human_display_name(user_id),
        "mentionHandle": None,
        "role": "member",
        "listenMode": None,
    }
    for attempt in range(3):
        try:
            meta = await messaging_repo.upsert_member(room_id=meta["roomId"], owner_id=owner_id, member=member)
        except messaging_repo.MemberLimitExceeded as exc:
            # ponytail: MAX_ROOM_MEMBERS (32) bounds a session's chat, as it bounds the vendor room.
            put_metric("messaging.session_room.capped")
            logger.warning("session chat full; %s not seated in %s", user_id, meta["roomId"])
            raise ApiProblem("MEMBER_LIMIT", full) from exc
        except ClientError as exc:
            if not messaging_repo._is_cancelled(exc) or attempt == 2:
                raise
            meta = await messaging_repo.get_room(meta["roomId"]) or meta
            if await messaging_repo.get_member(meta["roomId"], "human", user_id) is not None:
                return meta
            continue
        await messaging._publish_room_metadata(meta)
        return meta
    return meta  # pragma: no cover -- the loop returns or raises


async def open_chat(auth: AuthContext, session_id: str) -> RoomDetail:
    """The chat door: whoever can read the session gets its room, created on the first open."""
    row, _ = await local_sessions.authorize_session(auth, None, session_id)
    owner_id = resolve_owner_id(auth)
    session_id, owner_user = row["sessionId"], row["humanUserId"]
    gone = ApiProblem("LOCAL_SESSION_NOT_FOUND", "Local session not found.")
    # The session row outlives its person's membership: no chat about someone who left.
    if not await _active(owner_id, owner_user):
        raise gone
    # A removed member whose token has not expired yet: refused before any write. No stream row
    # at all is a member who has not opened messaging yet, and is seated.
    reader = await messaging_repo.get_user_stream(owner_id, auth.user_id)
    if reader is not None and not reader.get("active"):
        raise gone
    room_id = session_room_id(owner_id, session_id)
    meta = await messaging_repo.get_room(room_id) or await _create(owner_id, room_id, row)
    meta = await _follow_lifecycle(owner_id, session_id, meta)
    if await messaging_repo.get_member(room_id, "human", auth.user_id) is None:
        meta = await _seat(owner_id, meta, auth.user_id)
    await _fence(auth, owner_id, session_id, owner_user, room_id)
    return await messaging._room_detail(meta, auth.user_id)


async def _fence(auth: AuthContext, owner_id: str, session_id: str, owner_user: str, room_id: str) -> None:
    """The door's closing check, on every path (room created, reader seated, or already a
    member), before the room and its delivery key are returned. Everything the door read may be
    stale by now: a pause, or a removal from the workspace, can silence the room just before a
    seat (or the create, which seats the session's person) lands. Read it all again; whoever
    lost the session loses the seat, with the rotation."""
    try:
        await local_sessions.authorize_session(auth, None, session_id)
    except ApiProblem:
        await silence(owner_id, session_id, keep=owner_user)
        raise
    gone = ApiProblem("LOCAL_SESSION_NOT_FOUND", "Local session not found.")
    if not await _active(owner_id, owner_user):
        await silence(owner_id, session_id, keep=None)
        raise gone
    if not await _active(owner_id, auth.user_id):
        await _unseat(room_id, auth.user_id)
        raise gone


async def _unseat(room_id: str, user_id: str) -> bool:
    """Remove one human with a delivery-key rotation. A lost revision race reads again; a seat
    someone else already removed is the goal, not a conflict. Returns whether this call removed it."""
    for attempt in range(3):
        meta = await messaging_repo.get_room(room_id)
        if meta is None:
            return False
        try:
            await messaging._remove_member_with_notices(meta, "human", user_id)
            return True
        except ClientError as exc:
            if not messaging_repo._is_cancelled(exc):
                raise
            if await messaging_repo.get_member(room_id, "human", user_id) is None:
                return False
            if attempt == 2:
                raise
    return False  # pragma: no cover -- the loop returns or raises


async def silence(owner_id: str, session_id: str, *, keep: str | None) -> int:
    """Unshare or revoke: every human but ``keep`` leaves the chat, each with a delivery-key
    rotation, so nobody who lost the session keeps hearing it. Idempotent; returns how many left."""
    room_id = session_room_id(owner_id, session_id)
    removed = 0
    for member in await messaging_repo.list_members(room_id):
        if member.get("kind") == "human" and member["id"] != keep:
            removed += await _unseat(room_id, member["id"])
    return removed


async def follow_session(owner_id: str, session_id: str) -> None:
    """The session ended or resumed: its chat, if it has one, follows the session row."""
    meta = await messaging_repo.get_room(session_room_id(owner_id, session_id))
    if meta is not None:
        await _follow_lifecycle(owner_id, session_id, meta)


async def chat_state(owner_id: str, viewer: str, session_ids: list[str]) -> dict[str, tuple[str, int]]:
    """``{sessionId: (roomId, unread)}`` for the chats the viewer sits in. One key per session
    finds the viewer's seats; only a seated chat costs its META and read cursor on top."""
    rooms = {sid: session_room_id(owner_id, sid) for sid in session_ids}
    seat, read = messaging_repo.member_sk("human", viewer), messaging_repo.read_sk(viewer)
    if not rooms:
        return {}
    seats = await messaging_repo.batch_get([(messaging_repo.room_pk(room_id), seat) for room_id in rooms.values()])
    rows = await messaging_repo.batch_get([(pk, sk) for pk, _ in seats for sk in ("META", read)]) if seats else {}
    out: dict[str, tuple[str, int]] = {}
    for sid, room_id in rooms.items():
        pk = messaging_repo.room_pk(room_id)
        meta, member = rows.get((pk, "META")), seats.get((pk, seat))
        if meta and member and not meta.get("deletedAt"):
            out[sid] = (room_id, messaging_repo.unread_count(meta, member, rows.get((pk, read))))
    return out


def with_chat(sessions: list, state: dict[str, tuple[str, int]]) -> list:
    return [
        s.model_copy(update={"chat_room_id": state[s.session_id][0], "chat_unread": state[s.session_id][1]})
        if s.session_id in state
        else s
        for s in sessions
    ]
