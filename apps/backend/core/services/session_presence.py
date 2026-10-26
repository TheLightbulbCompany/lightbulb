"""What the owner's Mac shows in its Claude terminal about one shared session (spec
2026-10-04-session-sharing-mod §4.2): who watches, who may type, who is typing, pending join
requests, paused, and how many watch by an "Anyone" link (a count, never who). A new request and
a new viewer (by link too) are pushed at once so the band shows them; everything else (a viewer
leaving, typing) rides the heartbeat ack."""

from __future__ import annotations

import asyncio
import json
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


LABEL_CHARS = 64
# A held command's label, by (owner, session, request id): the label reads rooms and names, and
# presence goes out on every heartbeat while someone is asking.
# ponytail: process-local and cleared when full; a label stored on the command if it matters.
HELD_PREVIEW_CHARS = 200
_labels: dict[tuple[str, str, str], str | None] = {}


def clean_label(value: object) -> str | None:
    """A sender name as one short line: someone else's text that reaches the target agent before
    any Allow, so no control, line-break or invisible (format, bidi) characters, at most 64."""
    if not isinstance(value, str):
        return None
    text = "".join(" " if c.isspace() else c for c in value if c.isspace() or c.isprintable())
    return " ".join(text.split())[:LABEL_CHARS].strip() or None


async def _label(row: dict, command: dict) -> str | None:
    key = (row["ownerId"], row["sessionId"], command.get("requestId") or "")
    if key not in _labels:
        from core.services.local_session_relay import _sender_label  # imports this module

        try:
            label = await _sender_label(command, row["ownerId"], row.get("humanUserId"))
        except Exception:  # noqa: BLE001 -- a label is decoration; the requester's name stands in
            return None
        if len(_labels) >= 4096:
            _labels.clear()
        _labels[key] = clean_label((label or {}).get("displayName"))
    return _labels[key]


async def _held_sends(row: dict, users: list[str]) -> dict[str, dict]:
    """Each asking person's newest held session send (agent network §6) as the fields the Mac's
    notification reads: the preview and the sender as delivery labels it ("Aden's Codex"). One
    command read, and only while someone is asking. A failed read leaves them out, never the
    presence entry."""
    if not users:
        return {}
    try:
        commands = await repo.list_commands(row["ownerId"], row["sessionId"])  # sorted by commandSeq
    except Exception:  # noqa: BLE001 -- the join requests still go out, as plain asks
        logger.warning("held sends unavailable session=%s", row["sessionId"])
        return {}
    newest: dict[str, dict] = {}
    count: dict[str, int] = {}
    for command in commands:
        # Only what Allow would release (``release_held``): a held send past its deadline is not.
        if (
            command.get("heldFor") in users
            and command.get("state") == "awaiting_approval"
            and int(command.get("deliverBy") or 0) > _clock()
        ):
            newest[command["heldFor"]] = command
            count[command["heldFor"]] = count.get(command["heldFor"], 0) + 1
    out = {}
    for user, command in newest.items():
        # Allow releases every held message, so the preview says how many wait (first, so a
        # shortened preview keeps it).
        many = f"{count[user]} messages · " if count[user] > 1 else ""
        held = {"text": (many + (command.get("text") or ""))[:HELD_PREVIEW_CHARS]}
        label = await _label(row, command)
        if label:
            held["label"] = label
        out[user] = held
    return out


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
    held = await _held_sends(row, list(joins))
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
                    # A held session send waits on this ask: the Mac notifies it (Task 7b).
                    **(held.get(u, {}) if kind == "join" else {}),
                }
                for kind, pending in (("join", joins), ("pickup", shown_pickups(row)))
                for u, rid in pending.items()
            ],
            key=lambda r: (r["userId"], r["kind"]),
        ),
    }


async def ack_with_presence(ack: dict, owner: str, device: str, session_ids: list[str]) -> dict:
    """The backstop: the heartbeat ack names presence for this Mac's sessions. Never costs the heartbeat.
    Only sessions registered from `device`: presence (a held send's preview and its sender among it)
    is for the session owner's own Mac, never another Mac that names the id."""
    read_at = _ms()
    try:
        out = []
        rows = [
            r
            for r in await repo.get_sessions(owner, session_ids)
            if r and r.get("endedAt") is None and r.get("deviceId") == device
        ]
        # One frame's sessions concurrently: each entry is a bounded read or two (viewers, ask slots).
        for e in await asyncio.gather(*(entry(row, read_at) for row in rows)):
            if e["viewers"] or e["linkViewers"] or e["joinRequests"] or e["paused"]:
                out.append(e)
    except Exception:  # noqa: BLE001
        logger.warning("Session presence unavailable")
        return ack
    # A listed session the ack leaves out loses its presence on the Mac, so an entry holding a
    # join request (an input freeze on an older Mac) is never trimmed: drop quiet entries, then
    # viewer lists, and if requests alone overflow, send no presence at all (the Mac keeps what it has).
    out.sort(key=lambda e: not e["joinRequests"])

    def fits(p: list) -> bool:
        return (
            len(json.dumps({**ack, "presence": p, "presenceAt": read_at}, separators=(",", ":")).encode())
            <= sessions.MAX_ENVELOPE_BYTES
        )

    while out and not out[-1]["joinRequests"] and not fits(out):
        out.pop()
    if not fits(out):
        out = [{**e, "viewers": []} for e in out]
    # `presenceAt`: a session left out is removed on the Mac only if what it holds is older.
    full = {**ack, "presence": out, "presenceAt": read_at}
    return full if fits(out) else ack


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
