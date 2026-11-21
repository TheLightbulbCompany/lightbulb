"""A voice in a room, shared by Phone, Meetings and Talk: the room's agents and team, who a spoken run is for, the
plan gate a call start passes, and the runtime prewake."""

from __future__ import annotations

import logging

from core.billing import voice_billing
from core.config import settings
from core.engine import api
from core.engine.api import _background, _fire  # noqa: F401 -- meetings fire their background work here
from core.repositories import billing_repo, messaging_repo
from core.services import messaging
from core.voice.voice_calls import _problem

logger = logging.getLogger(__name__)


async def room_agents(room_id: str) -> list[dict]:
    """Every agent member. Listen modes are a chat-traffic setting; Live ignores them (owner ruling 2026-10-02)."""
    return [
        {"id": m["id"], "name": m.get("displayName") or "Agent"}
        for m in await messaging_repo.list_members(room_id)
        if m.get("kind") == "agent"
    ]


async def _room_team(owner_id: str, user_id: str, room_id: str, *, need_team: bool = True) -> tuple[dict, list[dict]]:
    """The room's agents, any listen mode. Any room kind; a DM with an agent is a one-agent team.
    need_team=False (Live join) returns an empty team for a people-only room instead of 409."""
    meta, _ = await messaging.require_room_member(owner_id, user_id, room_id)
    if meta.get("ownerId") != owner_id:  # a shared-channel guest: the team is another workspace's agents
        raise _problem(404, code="room_not_found")
    if meta.get("archivedAt"):
        raise _problem(409, code="room_archived")
    team = await room_agents(room_id)
    if need_team and not team:
        raise _problem(409, code="no_agents")
    return meta, team


def _voice_requester(members: list[dict], owner_id: str, speaker_id: str) -> str | None:
    """Who a voice run's local-session grant is FOR: the speaker, only when they are a human member of
    this workspace seated in the room (the C8 rule). A Meet speaker (``meet:<room>`` or a label) is
    never a member, so its grant names nobody (it reads only workspace-wide rooms); never the inviting
    owner."""
    member = next((m for m in members if m.get("kind") == "human" and m["id"] == speaker_id), None)
    return speaker_id if member and messaging_repo.member_home(member, owner_id) == owner_id else None


def prewake_owner(owner_id: str, team: list[dict] | None = None, room_id: str | None = None) -> None:
    """Fire-and-forget wake of the workspace runtime and of every agent of `team` (`core.engine.api.warm`)."""
    api.warm(owner_id, [member["id"] for member in team or ()], room_id)


def live_configured() -> bool:
    # CDK seeds the secret with a random non-sk- value; that counts as unset (voice stays dark).
    return settings.OPENAI_API_KEY.startswith("sk-")


async def _gate(owner_id: str) -> tuple[str, int]:
    """Plan + free-budget gates shared by every call start. Returns (billing mode, free budget seconds)."""
    if not live_configured():
        raise _problem(503, code="voice_unavailable")
    mode = voice_billing.billing_mode(await billing_repo.get_by_owner_id(owner_id))
    if mode is None:
        raise _problem(402, code="voice_limit", reason="plan_inactive")
    budget = 0
    if mode == "free":
        budget = await voice_billing.budget_seconds(owner_id, mode)
        if budget < voice_billing.MIN_CALL_SECONDS:
            raise _problem(402, code="voice_limit", reason="free_minutes_used")
    return mode, budget
