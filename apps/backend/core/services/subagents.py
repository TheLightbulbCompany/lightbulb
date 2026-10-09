"""Background subagents in rooms (docs/superpowers/specs/2026-10-09-background-subagents-design.md).

The bridge relays Hermes's `subagent.*` events here; this module publishes them on the room's
CURRENT live channel (a removed member's rotated key stays silent), and serves the product
routes' snapshot/tail/steer/stop through one bridge RPC each. Nothing is stored: AppSync is the
fanout and the runtime's own registry is the truth while children live.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, Field

from core.repositories import messaging_repo
from core.services.messaging import ApiProblem, ContractModel, make_live_event
from core.services.session_keys import parse_lightbulb_session_key

logger = logging.getLogger(__name__)

GOAL_CAP = 500
STRING_CAP = 220
EventType = Literal["subagent.start", "subagent.progress", "subagent.tool", "subagent.thinking", "subagent.complete"]


class SubagentEvent(BaseModel):
    type: EventType
    seq: int
    payload: dict[str, Any]


class SubagentEventsRequest(ContractModel):
    session_key: str = Field(min_length=1, max_length=512)
    events: list[SubagentEvent] = Field(min_length=1, max_length=25)


def _clip(value: Any, cap: int) -> Any:
    if isinstance(value, str):
        return value[:cap]
    if isinstance(value, list):
        return [_clip(v, cap) for v in value[:50]]
    if isinstance(value, dict):
        return {k: _clip(v, cap) for k, v in list(value.items())[:50]}
    return value


def _clipped(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: _clip(v, GOAL_CAP if k == "goal" else STRING_CAP) for k, v in payload.items()}


async def _publish(room_id: str, event: dict) -> bool:
    from core.services.live_events import get_live_event_publisher

    return await get_live_event_publisher().publish_room(room_id, event)


async def room_and_agent(owner_id: str, session_key: str) -> tuple[str, str]:
    """The room and agent a session key names, only if the room is this owner's and the agent is in it."""
    parsed = parse_lightbulb_session_key(session_key)
    if parsed is None:
        raise ApiProblem("INVALID_REQUEST", "sessionKey is not a room session.")
    room_id, agent_id = parsed["room_id"], parsed["agent_id"]
    meta = await messaging_repo.get_room(room_id)
    if not meta or meta.get("ownerId") != owner_id:
        raise ApiProblem("ROOM_NOT_FOUND", "Room not found.")
    if await messaging_repo.get_member(room_id, "agent", agent_id) is None:
        raise ApiProblem("RUN_NOT_ACTIVE", "The agent is no longer in this room.")
    return room_id, agent_id


async def publish_events(owner_id: str, body: SubagentEventsRequest) -> dict:
    room_id, agent_id = await room_and_agent(owner_id, body.session_key)
    events = [{"type": e.type, "seq": e.seq, "payload": _clipped(e.payload)} for e in body.events]
    event = make_live_event(
        "room.subagent.event", {"roomId": room_id, "agentId": agent_id, "events": events}, room_id=room_id
    )
    published = await _publish(room_id, event)
    return {"published": len(events) if published else 0}
