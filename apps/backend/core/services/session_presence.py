"""What the owner's Mac shows in its Claude terminal about one shared session (spec
2026-10-04-session-sharing-mod §4.2): who watches, who may type, who is typing, pending join
requests, paused, and how many watch by an "Anyone" link (a count, never who). A new request and
a new viewer (by link too) are pushed at once so the band shows them; everything else (a viewer
leaving, typing) rides the heartbeat ack."""

from __future__ import annotations

import logging
import time

from core.repositories import local_sessions_repo as repo, messaging_repo as store
from core.services import local_sessions as sessions
from core.services.messaging import human_display_name

logger = logging.getLogger(__name__)

FEATURE = "sessionPresence"
PICKUP_FEATURE = "pickupRequests"
# This Mac strips ctrl+x and mouse reports from remote input, so only the owner can operate the
# band and nothing freezes while a request waits. An older presence Mac freezes remote input
# instead, so the web says so (inputPaused).
BAND_OWNER_ONLY_FEATURE = "bandOwnerOnly"
TYPING_WINDOW_S = 10
_clock = time.time


def pending_joins(row: dict) -> dict[str, str]:
    """Each person still waiting for the owner's answer, with the request id they asked under."""
    if row.get("endedAt") is not None:
        return {}
    return {u: r["requestId"] for u, r in (row.get("joinRequests") or {}).items() if r.get("state") == "pending"}


def _asked_as(row: dict, user: str) -> str | None:
    """The label an outsider's ask stored (name and email): they are not in the workspace yet."""
    return ((row.get("joinRequests") or {}).get(user) or {}).get("name")


def shown_pickups(row: dict) -> dict[str, str]:
    """Pending pick-up requests the Mac may show. Only a Mac that advertises pickupRequests gets
    them: an older one would label each "asked to join", and its Allow would share far more."""
    if PICKUP_FEATURE not in row.get("deviceFeatures", ()):
        return {}
    return sessions.pending_pickups(row)


async def _name(user: str) -> str:
    try:
        return await human_display_name(user)
    except Exception:  # noqa: BLE001 -- a name is decoration; the entry must still go out
        return "A teammate"


def _ms() -> int:
    return int(_clock() * 1000)


async def link_viewers(row: dict, now: int) -> int:
    """How many people watch on the "Anyone with the link" page right now: the unexpired seats on
    the link's CURRENT channel, so 0 the moment the link is narrowed. A number and nothing else:
    nobody, the owner included, learns anything more about a link viewer."""
    from core.services.session_links import MAX_LINK_VIEWERS, public_pk  # imports this module

    public = sessions.public_id(row.get("shareLink"), row)
    if not public or row.get("unshared") or row.get("endedAt") is not None:
        return 0
    # ponytail: one bounded read. `viewing` drops lapsed seats whenever it gives out a new one, so
    # the channel holds about the cap's worth of rows; past that this counts low, never high.
    seats = await store._query(public_pk(public), begins_with="VIEWER#", limit=MAX_LINK_VIEWERS + 1)
    return min(sum(int(r.get("expiresAt", 0)) > now for r in seats), MAX_LINK_VIEWERS)


async def entry(row: dict, at: int) -> dict:
    """`at`: server milliseconds taken before `row` was read. The Mac keeps the entry with the
    greatest `at`, so an ack and a push that arrive out of order cannot undo a newer read."""
    now = int(_clock())
    pk = repo.session_pk(row["ownerId"], row["sessionId"])
    live = [
        r
        for r in await store._query(pk, begins_with="VIEWER#")
        if int(r.get("expiresAt", 0)) > now and r["userId"] != row["humanUserId"]
    ]
    viewers = sorted(r["userId"] for r in live)
    typing = {r["userId"] for r in live if int(r.get("typedAt", 0)) > now - TYPING_WINDOW_S}
    joins = pending_joins(row)
    return {
        "sessionId": row["sessionId"],
        "generation": row["generation"],
        "at": at,
        "paused": bool(row.get("unshared")),
        "linkViewers": await link_viewers(row, now),
        "viewers": [
            {"userId": u, "name": await _name(u), "canType": sessions.may_control(row, u), "typing": u in typing}
            for u in viewers
        ],
        "joinRequests": sorted(
            [
                {
                    "userId": u,
                    "name": _asked_as(row, u) or await _name(u),
                    "requestId": rid,
                    "kind": kind,
                }
                for kind, pending in (("join", joins), ("pickup", shown_pickups(row)))
                for u, rid in pending.items()
            ],
            key=lambda r: (r["userId"], r["kind"]),
        ),
    }


async def push(row: dict) -> None:
    """Best effort, now: a join request was made or answered, sharing paused or resumed, or
    someone started watching.
    Always a full entry, an empty one included, so the Mac drops what is gone (and an older Mac
    lifts its input freeze). The row is read again here, after `at` is taken, so `at` never
    claims a newer read than it is."""
    if FEATURE not in row.get("deviceFeatures", ()):
        return
    from core.services.local_session_relay import check_envelope, get_local_session_relay  # imports this module

    try:
        at = _ms()
        fresh = await repo.get_session(row["ownerId"], row["sessionId"])
        if fresh is None:
            return
        envelope = {"type": "local.session.presence", **await entry(fresh, at)}
        check_envelope(envelope)
        await get_local_session_relay().management.send_message_async(row["connectionId"], envelope)
    except Exception as exc:  # noqa: BLE001 -- the next ack carries the same news
        logger.info("session presence push failed error=%s", type(exc).__name__)
