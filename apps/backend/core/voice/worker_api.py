"""The voice worker's auth seam, shared by Phone, Meetings and Talk: the worker secret and the per-room token."""

import hmac
from typing import Annotated

from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel, Field

from core.config import settings
from core.services import messaging
from core.voice import livekit_huddles


_Id = Annotated[str, Field(min_length=1, max_length=128)]


def _problem(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code})


def _require_worker(x_lightbulb_huddle_secret: Annotated[str, Header()] = "") -> None:
    expected = settings.HUDDLE_WORKER_SECRET
    # An unset secret must never match an empty header.
    if not expected or not hmac.compare_digest(x_lightbulb_huddle_secret.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="invalid huddle worker secret")


_Worker = Depends(_require_worker)


def _require_room_token(presented: str, room_id: str, owner_id: str) -> None:
    if not hmac.compare_digest(presented.encode(), livekit_huddles.room_token(room_id, owner_id).encode()):
        raise HTTPException(status_code=401, detail="invalid huddle room token")


def _room_id(room_id: str) -> str:
    # One canonical (lowercase) id per channel: an uppercase path must not open a second LiveKit room.
    return messaging.validate_uuid(room_id, label="room ID")


class RoomBody(BaseModel):
    ownerId: _Id
    roomToken: _Id
