"""Mac live view: the task holding a Mac's node socket drives its agent tab's cast and republishes
the frames to AppSync.

One `run` per room-run CDP channel (started by `mac_cdp.serve`). It turns the viewers' heartbeats
(`mac_view_at` / `mac_thumb_at`) into `browser.cast.set`, re-sent every `SET_EVERY_S` while wanted
so the Mac's own TTL keeps it alive, and publishes each tier's latest frame: view frames on the
room's `frames` sibling channel, thumbs on the room channel. `input` routes a viewer's input to
the live run's channel.

Never log a frame or a payload: owner ids and exception class names only.
"""

from __future__ import annotations

import asyncio
import logging
import time

from core.repositories import container_repo
from core.services import live_events
from core.services.messaging import make_live_event
from core.services.node_broker import CdpClosed, CommandNotAllowed, NodeCapacity

logger = logging.getLogger(__name__)

FRAME_POLL_S = 1.0
SET_EVERY_S = 10.0
WANTED_S = 45.0  # a viewer stamp younger than this wants its tier
_TIERS = ("view", "thumb")
_clock = time.time  # the stamps are epoch seconds
_live: dict[tuple[str, str], tuple] = {}  # (ownerId, runId) -> (session, channel)


async def _watch(owner: str, session, channel) -> None:
    sent, sent_at = (False, False), 0.0
    while True:
        try:
            stamps = [await container_repo.get_browser_input_at(owner, f"mac_{tier}_at") for tier in _TIERS]
        except Exception as exc:  # noqa: BLE001 — a DynamoDB blip: keep the last state, retry next tick
            logger.warning("mac live stamp read failed owner=%s error=%s", owner, type(exc).__name__)
        else:
            now = _clock()
            want = tuple(at is not None and now - at < WANTED_S for at in stamps)
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


async def run(claims: dict, session, channel) -> None:
    """Until the channel closes; room runs on a cast-capable Mac only."""
    if claims.get("family") != "room" or not claims.get("roomId") or not session.can_cast:
        return
    key = (claims["ownerId"], claims["runId"])
    _live[key] = (session, channel)
    tasks = {
        asyncio.create_task(_watch(claims["ownerId"], session, channel)),
        asyncio.create_task(_publish(claims, channel)),
        asyncio.create_task(channel.closed.wait()),
    }
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:  # not awaited, as in mac_cdp.serve; the callback retrieves any exception
            task.cancel()
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
        if _live.get(key) == (session, channel):
            del _live[key]


async def input(owner: str, run_id: str, events: list[dict]) -> None:  # noqa: A001 — the spec's name
    """Raises LookupError when the run has no live channel on this task; otherwise whatever
    `cast_input` raises (CommandNotAllowed / CdpClosed / NodeCapacity)."""
    live = _live.get((owner, run_id))
    if live is None:
        raise LookupError(run_id)
    session, channel = live
    session.cast_input(channel, events)
