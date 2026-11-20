"""The runtime's hook-token check, and the reader for the first narrow runtime grant: one canonical room
run's events."""

import hmac
import logging
import os
import time
import uuid

import jwt
from fastapi import HTTPException, Request

from core.observability.metrics import put_metric
from core.repositories import container_repo, messaging_repo

logger = logging.getLogger(__name__)

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
        or "abortRequestedAt" in run
        or type(run.get("deadlineAt")) is not int
        or run["deadlineAt"] <= int(time.time())
    ):
        raise HTTPException(403, detail="hook grant run unavailable")
    from core.services import messaging  # lazy: messaging imports the routers' world

    room = await messaging_repo.get_room(claims["roomId"])
    if (
        not room
        or room.get("ownerId") != owner_id
        # The feed's agents are authors, never seated: room_agent stands a roster agent in.
        or not await messaging.room_agent(claims["roomId"], claims["agentId"], room)
    ):
        raise HTTPException(403, detail="hook grant room unavailable")


def _count_hook_auth_failure(owner_id: str) -> None:
    """Counted so that silence pages; the owner goes to the log (never a metric dimension)."""
    logger.warning("internal hook auth failed owner=%s", owner_id)
    put_metric("internal_hook.auth_failed", dimensions={"route": "other"})


async def require_hook_token(owner_id: str, request: Request, *, run_event_id: str | None = None) -> dict:
    """The owner's hook token (or, with ``run_event_id``, a run-event grant); returns the containers row."""
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        _count_hook_auth_failure(owner_id)
        raise HTTPException(status_code=401, detail="missing hook token")
    presented = authorization.removeprefix("Bearer ")
    container = await container_repo.get_by_owner_id(owner_id, consistent=True)
    expected = (container or {}).get("hook_token") or ""
    if expected and hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        put_metric("internal_hook.auth_accepted", dimensions={"source": "legacy"})
        return container
    if run_event_id is not None:
        try:
            await require_run_event(presented, owner_id, container or {}, run_event_id)
        except HTTPException:
            _count_hook_auth_failure(owner_id)
            raise
        put_metric("internal_hook.auth_accepted", dimensions={"source": "run_grant"})
        return container
    _count_hook_auth_failure(owner_id)
    # Internal hook auth intentionally remains 401 at the router boundary.
    raise HTTPException(status_code=401, detail="invalid hook token")
