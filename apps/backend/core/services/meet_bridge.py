"""Recall's per-person Meet audio into the meeting's LiveKit room: each Meet person becomes their own
LiveKit participant (``meet:<roomId>:<participantId>``, their Meet name, one ``mic`` track), so the
voice worker hears who speaks the way it does in a Live call. One ``MeetBridge`` per Recall socket.

Recall's frames are JSON text: ``{"event", "data": {"data": {"buffer": b64 PCM s16le 16 kHz mono,
"timestamp", "participant": {"id", "name", ...}}, "bot", ...}}`` (Recall's sample app,
bot_separate_audio_streams_per_participant_realtime_write_to_audio_files). Audio and names are
never logged or stored.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging

from livekit import rtc

from core.config import settings
from core.observability.metrics import put_metric
from core.services import livekit_huddles, meeting_bots

logger = logging.getLogger(__name__)
MAX_SEATS = 16  # Recall separates the 16 loudest concurrent speakers on Meet
_RATE, _CHANNELS = 16000, 1
_NAME_MAX = 80
_Seat = tuple["rtc.Room", "rtc.AudioSource"] | None  # None: the connect failed; the person stays unbridged


class MeetBridge:
    def __init__(self, room_id: str):
        self.room_id = room_id
        self._seats: dict[str, asyncio.Task[_Seat]] = {}
        self._heard: dict[str, int] = {}  # pid -> the order of their latest audio
        self._left: set[str] = set()  # gone from the Meet: their last packets may still be on the wire
        self._tick = 0
        self._seated = 0
        self._capped = False
        put_metric("meeting.bridge.connected")
        logger.info("meet bridge connected room=%s", room_id)

    async def feed(self, frame: str) -> None:
        """One Recall frame. Unknown events and malformed frames are dropped; never raises."""
        try:
            msg = json.loads(frame)
            event = msg.get("event")
            data = msg["data"]["data"]
            who = data["participant"]
            pid = str(who["id"])
        except (ValueError, TypeError, KeyError, AttributeError):
            return
        name = " ".join(str(who.get("name") or "").split())
        if name.endswith(meeting_bots._BOT_SUFFIX.strip()) or who.get("is_bot"):
            return  # the bot's own seat is the page (meetbot:), never a person
        try:
            if event == "participant_events.leave":
                self._left.add(pid)
                await self._unseat(pid)
            elif event == "participant_events.join":
                self._left.discard(pid)
                self._seat(pid, name)  # seated muted too; the connect runs beside the socket
            elif event == "audio_separate_raw.data":
                pcm = base64.b64decode(data.get("buffer") or "", validate=True)
                pcm = pcm[: len(pcm) // 2 * 2]
                if not pcm or pid in self._left:
                    # an unmuted silent person sends empty packets; a packet behind its sender's leave would
                    # seat them again as someone nobody can remove (10-04 prod Meet: one person, two seats)
                    return
                self._tick += 1
                self._heard[pid] = self._tick
                if pid not in self._seats and len(self._seats) >= MAX_SEATS:
                    # A full room seats who speaks, from whoever has been silent longest (a join alone
                    # holds a seat only while there is room). ponytail: 17+ people talking in turn
                    # reconnect a seat per switch; hold seats for a minimum time if that ever shows up.
                    await self._unseat(min(self._seats, key=lambda p: self._heard.get(p, 0)))
                task = self._seat(pid, name)
                seat = await task if task else None
                if seat:
                    await seat[1].capture_frame(rtc.AudioFrame(pcm, _RATE, _CHANNELS, len(pcm) // 2))
        except (binascii.Error, ValueError):
            return
        except Exception:  # noqa: BLE001 -- one person's seat never stops the others
            put_metric("meeting.bridge.error", dimensions={"why": "audio"})
            logger.warning("meet bridge audio failed room=%s", self.room_id, exc_info=True)

    def _seat(self, pid: str, name: str) -> asyncio.Task[_Seat] | None:
        task = self._seats.get(pid)
        if task is None:
            if len(self._seats) >= MAX_SEATS:
                if not self._capped:
                    self._capped = True
                    put_metric("meeting.bridge.error", dimensions={"why": "capped"})
                return None
            task = self._seats[pid] = asyncio.create_task(self._connect(pid, name[:_NAME_MAX] or "Guest"))
            self._seated += 1
        return task

    async def _connect(self, pid: str, name: str) -> _Seat:
        room = rtc.Room()
        try:
            token = livekit_huddles.mint_meet_participant_token(self.room_id, pid, name)
            await room.connect(settings.LIVEKIT_URL, token, rtc.RoomOptions(auto_subscribe=False))
            source = rtc.AudioSource(_RATE, _CHANNELS)
            track = rtc.LocalAudioTrack.create_audio_track("mic", source)
            await room.local_participant.publish_track(
                track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
            )
            return room, source
        except Exception:  # noqa: BLE001
            # ponytail: a failed seat stays failed for this socket (no retry storm at 50 packets/s);
            # Recall's reconnect builds a new bridge. Retry with backoff if this metric shows up.
            put_metric("meeting.bridge.error", dimensions={"why": "connect"})
            logger.warning("meet bridge seat failed room=%s", self.room_id, exc_info=True)
            await _quietly(room.disconnect())
            return None

    async def _unseat(self, pid: str) -> None:
        task = self._seats.pop(pid, None)
        self._heard.pop(pid, None)
        if task is None:
            return
        seat = await task
        if seat:
            await _quietly(seat[1].aclose())
            await _quietly(seat[0].disconnect())

    async def drain(self) -> None:
        """Waits for every seat still connecting."""
        await asyncio.gather(*self._seats.values(), return_exceptions=True)

    async def close(self) -> None:
        await self.drain()
        await asyncio.gather(*(self._unseat(pid) for pid in list(self._seats)), return_exceptions=True)
        put_metric("meeting.bridge.participants", self._seated)
        logger.info("meet bridge closed room=%s seated=%d capped=%s", self.room_id, self._seated, self._capped)


async def _quietly(coro) -> None:
    try:
        await coro
    except Exception:  # noqa: BLE001 -- teardown of one seat never stops the rest
        logger.debug("meet bridge teardown step failed", exc_info=True)
