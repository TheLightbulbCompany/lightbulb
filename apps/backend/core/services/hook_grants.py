"""Reader for the first narrow runtime grant: one canonical room run's events."""

import os
import time
import uuid

import jwt
from fastapi import HTTPException

from core.repositories import messaging_repo

PURPOSE = "lightbulb_hook_run"
AUDIENCE = "lightbulb:hook:run-events"
MAX_LIFETIME = 300


async def require_run_event(token: str, owner_id: str, container: dict, run_id: str) -> None:
    """A grant never authorizes another route, run, agent, room, or container row."""
    try:
        key = os.environ["HOOK_GRANT_SIGNING_KEY"]
        if not key or not 1 <= len(token) <= 4096:
            raise ValueError()
        if jwt.get_unverified_header(token) != {"alg": "HS256", "kid": "1", "typ": "JWT"}:
            raise ValueError()
        claims = jwt.decode(token, key, algorithms=["HS256"], audience=AUDIENCE)
        if set(claims) != {
            "kind",
            "aud",
            "ownerId",
            "rowId",
            "agentId",
            "runId",
            "roomId",
            "actions",
            "iat",
            "exp",
            "jti",
        }:
            raise ValueError()
        now = int(time.time())
        if (
            claims["kind"] != PURPOSE
            or claims["aud"] != AUDIENCE
            or claims["ownerId"] != owner_id
            or claims["rowId"] != container.get("id")
            or claims["runId"] != run_id
            or claims["actions"] != ["run.event"]
            or type(claims["iat"]) is not int
            or type(claims["exp"]) is not int
            or not claims["iat"] <= now < claims["exp"] <= claims["iat"] + MAX_LIFETIME
        ):
            raise ValueError()
        for field in ("rowId", "agentId", "runId", "roomId", "jti"):
            if not isinstance(claims[field], str) or str(uuid.UUID(claims[field])) != claims[field]:
                raise ValueError()
    except (KeyError, ValueError, TypeError, jwt.InvalidTokenError):
        raise HTTPException(401, detail="invalid hook grant") from None

    run = await messaging_repo.get_run(owner_id, run_id)
    if (
        not run
        or run.get("ownerId") != owner_id
        or run.get("runId") != run_id
        or run.get("agentId") != claims["agentId"]
        or run.get("roomId") != claims["roomId"]
        or run.get("status") not in {"dispatched", "started"}
    ):
        raise HTTPException(403, detail="hook grant run unavailable")
    room = await messaging_repo.get_room(claims["roomId"])
    if (
        not room
        or room.get("ownerId") != owner_id
        or not await messaging_repo.get_member(claims["roomId"], "agent", claims["agentId"])
    ):
        raise HTTPException(403, detail="hook grant room unavailable")
