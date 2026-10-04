"""What the owner's Mac shows in its Claude terminal about one shared session (spec
2026-10-04-session-sharing-mod §4.2): who watches, who may type, who is typing, pending join
requests, paused. A pending request also freezes remote typing on the Mac, so the request
being answered is pushed at once; everything else rides the heartbeat ack."""

from __future__ import annotations

import json
import logging
import time

from core.repositories import local_sessions_repo as repo, messaging_repo as store
from core.services import local_sessions as sessions
from core.services.messaging import human_display_name

logger = logging.getLogger(__name__)

FEATURE = "sessionPresence"
TYPING_WINDOW_S = 10
_clock = time.time


def pending_joins(row: dict) -> dict[str, str]:
    """Each person still waiting for the owner's answer, with the request id they asked under."""
    if row.get("endedAt") is not None:
        return {}
    return {u: r["requestId"] for u, r in (row.get("joinRequests") or {}).items() if r.get("state") == "pending"}


async def _name(user: str) -> str:
    try:
        return await human_display_name(user)
    except Exception:  # noqa: BLE001 -- a name is decoration; the entry must still go out
        return "A teammate"


async def entry(row: dict) -> dict:
    now = int(_clock())
    pk = repo.session_pk(row["ownerId"], row["sessionId"])
    viewers = sorted(
        r["userId"]
        for r in await store._query(pk, begins_with="VIEWER#")
        if int(r.get("expiresAt", 0)) > now and r["userId"] != row["humanUserId"]
    )
    # ponytail: reads every TYPED# burst row (30-day TTL); add a lastAt index if a session's typing history grows large.
    typing = {
        r["userId"]
        for r in await store._query(pk, begins_with="TYPED#")
        if int(r.get("lastAt", 0)) > now - TYPING_WINDOW_S
    }
    return {
        "sessionId": row["sessionId"],
        "generation": row["generation"],
        "paused": bool(row.get("unshared")),
        "viewers": [
            {"userId": u, "name": await _name(u), "canType": sessions.may_control(row, u), "typing": u in typing}
            for u in viewers
        ],
        "joinRequests": [
            {"userId": u, "name": await _name(u), "requestId": rid} for u, rid in sorted(pending_joins(row).items())
        ],
    }


async def ack_with_presence(ack: dict, owner: str, session_ids: list[str]) -> dict:
    """The backstop: the heartbeat ack names presence for this Mac's sessions. Never costs the heartbeat."""
    try:
        out = []
        for sid in session_ids:
            row = await repo.get_session(owner, sid)
            if row is None or row.get("endedAt") is not None:
                continue
            e = await entry(row)
            if e["viewers"] or e["joinRequests"] or e["paused"]:
                out.append(e)
    except Exception:  # noqa: BLE001
        logger.warning("Session presence unavailable")
        return ack
    while True:
        candidate = {**ack, "presence": out}
        if len(json.dumps(candidate, separators=(",", ":")).encode()) <= sessions.MAX_ENVELOPE_BYTES or not out:
            return candidate
        out = out[:-1]


async def push(row: dict) -> None:
    """Best effort, now: a join request was made or answered, or sharing paused or resumed.
    Always a full entry, an empty one included, so the Mac can lift its input freeze."""
    if FEATURE not in row.get("deviceFeatures", ()):
        return
    from core.services.local_session_relay import check_envelope, get_local_session_relay  # imports this module

    try:
        envelope = {"type": "local.session.presence", **await entry(row)}
        check_envelope(envelope)
        await get_local_session_relay().management.send_message_async(row["connectionId"], envelope)
    except Exception as exc:  # noqa: BLE001 -- the next ack carries the same news
        logger.info("session presence push failed error=%s", type(exc).__name__)
