"""Mac live view: the task holding a Mac's node socket drives its agent tab's cast and republishes
the frames to AppSync.

One `run` per room-run CDP channel (started by `mac_cdp.serve`). Its canonical run row holds the
viewer heartbeats across channel reconnects and turns them into `browser.cast.set`, re-sent every `SET_EVERY_S` while wanted
so the Mac's own TTL keeps it alive, and publishes each tier's latest frame: view frames on the
room's `frames` sibling channel, thumbs on the room channel. `input` routes a viewer's input to
the live run's channel, or to a shared session's browser (`live`, held by session_browser).

Never log a frame or a payload: owner ids and exception class names only.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from core.repositories import messaging_repo
from core.services import live_events
from core.services.messaging import ApiProblem, make_live_event, require_room_member
from core.services.node_broker import CdpClosed, CommandNotAllowed, NodeCapacity

logger = logging.getLogger(__name__)

FRAME_POLL_S = 1.0
SET_EVERY_S = 10.0
WANTED_S = 45.0  # a viewer stamp younger than this wants its tier
_TIERS = ("view", "thumb")
_clock = time.time  # the stamps are epoch seconds
_live: dict[tuple[str, str], tuple] = {}  # (ownerId, runId) -> (session, channel, roomId)
MAX_INPUT_EVENTS = 32
RUN_ID_PATTERN = r"^[A-Za-z0-9:_-]{1,256}$"  # a room run id (BrowserInput.runId's shape)

# Viewer input (spec §3). In the frame's CSS px; the Mac clamps to the frame. Modifiers are CDP
# bits: Alt=1, Ctrl=2, Meta=4, Shift=8.
_Coord = Annotated[float, Field(ge=0, le=100_000, allow_inf_nan=False)]


class _Event(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Pointer(_Event):
    type: Literal["down", "move", "up"]
    x: _Coord
    y: _Coord
    button: Literal["left", "middle", "right"] = "left"
    clicks: int = Field(default=1, ge=1, le=3)


class _Wheel(_Event):
    type: Literal["wheel"]
    x: _Coord
    y: _Coord
    deltaY: float = Field(ge=-5000, le=5000, allow_inf_nan=False)


class _Key(_Event):
    type: Literal["key"]
    key: str = Field(min_length=1, max_length=32)
    modifiers: int = Field(default=0, ge=0, le=15)


class _Text(_Event):
    type: Literal["text", "paste"]
    text: str = Field(min_length=1, max_length=1000)


MacInputEvent = Annotated[_Pointer | _Wheel | _Key | _Text, Field(discriminator="type")]
MacInputEvents = Annotated[list[MacInputEvent], Field(min_length=1, max_length=MAX_INPUT_EVENTS)]
_EVENTS = TypeAdapter(MacInputEvents)


def parse_events(raw) -> list[dict]:
    """Validate viewer input; the plain dicts `cast_input` sends. Raises pydantic.ValidationError."""
    return dump_events(_EVENTS.validate_python(raw))


def dump_events(events: list) -> list[dict]:
    return [event.model_dump(exclude_none=True) for event in events]


async def _watch(owner: str, run_id: str, session, channel) -> None:
    sent, sent_at = (False, False), 0.0
    while True:
        try:
            row = await messaging_repo.get_run(owner, run_id)
        except Exception as exc:  # noqa: BLE001 — a DynamoDB blip: keep the last state, retry next tick
            logger.warning("mac live stamp read failed owner=%s error=%s", owner, type(exc).__name__)
        else:
            now = _clock()
            stamps = [(row or {}).get(f"mac{tier.title()}At") for tier in _TIERS]
            want = (
                tuple(at is not None and now - int(at) < WANTED_S for at in stamps)
                if row and row.get("status") != "ended"
                else (False, False)
            )
            if want != sent or (any(want) and now - sent_at >= SET_EVERY_S):
                try:
                    session.cast_set(channel, *want)
                    sent, sent_at = want, now
                except NodeCapacity:
                    pass  # the node socket is backed up: retried next tick
                except (CdpClosed, CommandNotAllowed):
                    return
        await asyncio.sleep(FRAME_POLL_S)


async def _publish(claims: dict, channel) -> None:
    room = claims["roomId"]
    publisher = live_events.get_live_event_publisher()
    while True:
        await channel.cast_ready.wait()
        channel.cast_ready.clear()  # clear, THEN pop: a frame landing mid-drain re-sets it
        frames = [(tier, channel.cast.pop(tier, None)) for tier in _TIERS]
        for tier, frame in frames:
            if frame is None:
                continue
            payload = {"roomId": room, "runId": claims["runId"], "agentId": claims["agentId"], **frame, "tier": tier}
            try:
                event = make_live_event("room.browser.frame", payload, room_id=room)
                await publisher.publish_room(room, event, frames=tier == "view")
            except Exception as exc:  # noqa: BLE001 — skip this frame; the next one follows shortly
                logger.warning("mac live publish failed owner=%s error=%s", claims["ownerId"], type(exc).__name__)


@contextlib.contextmanager
def live(owner: str, run_id: str, session, channel, room_id: str | None = None):
    """`run_id`'s live channel on this task while the block runs: where `input` delivers. A room run's
    (`room_id`), or a shared session's browser (None: session_browser.may_type decides who types)."""
    key, entry = (owner, run_id), (session, channel, room_id)
    _live[key] = entry
    try:
        yield
    finally:
        if _live.get(key) is entry:
            del _live[key]


async def run(claims: dict, session, channel) -> None:
    """Until the channel closes; room runs on a cast-capable Mac only."""
    if claims.get("family") != "room" or not claims.get("roomId") or not session.can_cast:
        return
    tasks = {
        asyncio.create_task(_watch(claims["ownerId"], claims["runId"], session, channel)),
        asyncio.create_task(_publish(claims, channel)),
        asyncio.create_task(channel.closed.wait()),
    }
    # The door's room, never a caller's.
    with live(claims["ownerId"], claims["runId"], session, channel, claims["roomId"]):
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:  # not awaited, as in mac_cdp.serve; the callback retrieves any exception
                task.cancel()
                task.add_done_callback(lambda t: t.cancelled() or t.exception())


async def input(owner: str, user: str, run_id: str, events: list[dict], *, viewer_owner: str | None = None) -> None:  # noqa: A001 — the spec's name
    """Only a current human member of the run's room (who can see its frames) may drive it; for a
    shared session's browser (an entry with no room), only a person who may type into the session.
    A guest's membership is keyed under their own workspace, while the channel belongs to the room
    owner's workspace.

    Raises LookupError when the run has no live channel on this task, PermissionError when `user`
    may not drive it, otherwise whatever `cast_input` raises (CommandNotAllowed / CdpClosed /
    NodeCapacity)."""
    entry = _live.get((owner, run_id))
    if entry is None:
        raise LookupError(run_id)
    session, channel, room_id = entry
    if room_id is None:
        from core.services import session_browser  # it imports this module

        if not await session_browser.may_type(owner, user, run_id):
            raise PermissionError(run_id)
    else:
        try:
            await require_room_member(viewer_owner or owner, user, room_id)
        except ApiProblem:
            raise PermissionError(run_id) from None
    session.cast_input(channel, events)


async def heartbeat(
    owner: str, user: str, run_id: str, tier: Literal["view", "thumb"], *, viewer_owner: str | None = None
) -> None:
    """Only a current room member can keep this live run's cast tier on."""
    row = await messaging_repo.get_run(owner, run_id)
    if row is None or row.get("status") == "ended":
        raise LookupError(run_id)
    try:
        await require_room_member(viewer_owner or owner, user, row["roomId"])
    except ApiProblem:
        raise PermissionError(run_id) from None
    if not await messaging_repo.touch_mac_viewer(owner, run_id, tier, int(_clock())):
        raise LookupError(run_id)
