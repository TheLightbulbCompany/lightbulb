"""Session chat: one hidden channel per shared session, where people talk beside the terminal.

Spec docs/superpowers/specs/2026-10-07-session-chat-design.md. Membership is a projection of
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
from core.services import local_sessions, messaging
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
    from core.repositories.paperclip_repo import PaperclipRepo

    company = await PaperclipRepo(table_name="paperclip-companies").resolve_owner_row(owner_id, consistent_read=True)
    if getattr(company, "status", None) != "active":
        return []
    return _ceo_member(company, await messaging.load_agent_roster(owner_id))


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
    title = (row.get("displayTitle") or row.get("title") or row.get("cwdName") or "Session")[:TITLE_MAX]
    await messaging_repo.ensure_user_stream(owner_id, owner_user)
    try:
        return await messaging_repo.create_room(
            owner_id=owner_id,
            room_id=room_id,
            client_room_id=f"session-room-v1:{row['sessionId']}",
            creator_user_id=owner_user,
            # The agent's turn carries the room name, so "Session chat" is how it knows where it is.
            name=f"Session chat · {title}",
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
    updates = (
        {"archivedAt": messaging.utc_now().isoformat(), "archivedBy": SYSTEM_ACTOR_ID}
        if archived
        else {"archivedAt": None, "archivedBy": None}
    )
    try:
        meta = await messaging_repo.patch_room_meta(meta["roomId"], updates)
    except ClientError as exc:
        if not messaging_repo._is_cancelled(exc):
            raise
        return await messaging_repo.get_room(meta["roomId"]) or meta  # another writer moved it first
    await messaging._publish_room_metadata(meta)
    return meta


async def _follow_lifecycle(meta: dict, row: dict) -> dict:
    """Ended sessions read only; a resumed one (same id, endedAt cleared) takes posts again."""
    ended = row.get("endedAt") is not None
    if ended != bool(meta.get("archivedAt")):
        return await _set_archived(meta, ended)
    return meta


async def _seat(owner_id: str, meta: dict, user_id: str) -> dict:
    """Seat one reader as a plain member. A lost revision race re-reads and retries."""
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
            raise ApiProblem("MEMBER_LIMIT", "This session's chat is full.") from exc
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
    room_id = session_room_id(owner_id, row["sessionId"])
    meta = await messaging_repo.get_room(room_id) or await _create(owner_id, room_id, row)
    meta = await _follow_lifecycle(meta, row)
    if await messaging_repo.get_member(room_id, "human", auth.user_id) is None:
        meta = await _seat(owner_id, meta, auth.user_id)
    return await messaging._room_detail(meta, auth.user_id)
