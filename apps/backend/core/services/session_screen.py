"""Watching a running terminal (spec §5.5, rulings 2026-10-03 §4-5).

LOCALSESSION#<owner>#<sid> / VIEWER#<user>          who gets this session's screen frames;
                                                    `typedAt` is when they last typed
LOCALUSER#<owner>#<human>  / WATCH#<device>#<sid>   what that Mac's heartbeat ack lists
LOCALSESSION#<owner>#<sid> / TYPED#<user>#<start>   one admitted person's typing burst (who,
                                                    when, byte count; never the bytes), 30 days

Both expire 45 s after the viewer's last POST (they post every 15 s). `since` is when the
newest viewer arrived, or mounted again inside its lease: the Mac sends history and a fresh
snapshot when it moves. Screen frames themselves are published ONCE, to the session's frames
channel `/ws/<ws>/session-frames/<sid>`, and stored nowhere (see `forward`). Who may hear them
is decided when they subscribe: the AppSync handler requires a current workspace member and a
shared session. A removed member's client drops its subscriptions (`workspace.access.removed`).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Literal

from botocore.exceptions import ClientError
from pydantic import Field, field_validator, model_validator

from core.auth import AuthContext
from core.observability.metrics import put_metric
from core.repositories import local_sessions_repo as repo, messaging_repo as store
from core.services import live_events, local_sessions as sessions, session_presence
from core.services.messaging import ContractModel, make_live_event

logger = logging.getLogger(__name__)

VIEWER_TTL_S = 45
ACK_CAP = 24
# ponytail: `forward` signs one publish per viewer per frame, so a session's fan-out is
# O(viewers); dev measured frame p95 3.7 s at 25 viewers and a stalled backend at 50. Drop the
# cap when frames get one channel per session (design note in #3903). Two new viewers racing
# for the last seat can both get in: a soft ceiling, not a lock.
MAX_SESSION_VIEWERS = 20
_clock = time.time


def _viewer_key(row: dict, user: str) -> dict:
    return {"pk": repo.session_pk(row["ownerId"], row["sessionId"]), "sk": f"VIEWER#{user}"}


def _watch_key(row: dict) -> dict:
    return {
        "pk": repo.user_pk(row["ownerId"], row["humanUserId"]),
        "sk": f"WATCH#{row['deviceId']}#{row['sessionId']}",
    }


async def _still_a_member(row: dict, user: str) -> None:
    """A teammate must be in the workspace NOW: removal deactivates their user stream, while an
    already-issued Clerk JWT stays valid until it expires. The same fence as resume and offline
    sends. The owner's own Mac session is theirs."""
    if user == row["humanUserId"]:
        return
    stream = await store.get_user_stream(row["ownerId"], user)
    if not stream or not stream.get("active"):
        raise sessions.ApiProblem("LOCAL_SESSION_FORBIDDEN", "You are no longer in this workspace.")


async def watch(auth: sessions.Caller, session_id: str, mounted: bool = False) -> dict:
    """A person has this running session's terminal on screen. Humans who can read it only:
    `authorize_session` refuses agents on the roomless path, and no room id is accepted here.

    `mounted`: the view just opened and has no screen (a page refresh inside the 45 s lease
    looks like a renewal otherwise), so it counts as a new viewer: `since` moves and the Mac is
    nudged. The Mac's budget bounds what a client that always says so can cost."""
    row, _ = await sessions.authorize_session(auth, None, session_id)
    await _still_a_member(row, auth.user_id)
    now = int(_clock())
    kept = sessions.screen_available(row, now)  # an ended held session's final screen (spec 2026-10-07 §5.4)
    if row.get("unshared") or (not kept and (row.get("endedAt") is not None or int(row.get("onlineUntil", 0)) <= now)):
        # Only a lapsed lease is gone. A paused session's screen is still on the Mac.
        if not kept and row.get("endedAt") is not None and row.get("screenUntil") is not None:
            await _screen_gone(row)
        raise sessions.ApiProblem("LOCAL_SESSION_OFFLINE", "This session is not running.")
    if sessions.session_terminal(row) is None:
        raise sessions.ApiProblem("LOCAL_SESSION_UNSUPPORTED", "This session has no live terminal.")
    key = _viewer_key(row, auth.user_id)
    expires = now + VIEWER_TTL_S
    table = store._table()
    values = {":u": auth.user_id, ":exp": expires, ":now": now}
    # One conditional write, never read-then-put: a refresh of a live lease leaves `since` and
    # `typedAt` as they are (a keystroke marked meanwhile is kept); a new lease, or a view that
    # just mounted, starts both over.
    fresh = mounted
    if not fresh:
        try:
            await store.run_in_thread(
                table.update_item,
                Key=key,
                UpdateExpression="SET userId = :u, expiresAt = :exp, since = if_not_exists(since, :now)",
                ConditionExpression="attribute_exists(pk) AND expiresAt > :now",
                ExpressionAttributeValues=values,
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            fresh = True
    if fresh and auth.user_id != row["humanUserId"]:
        # Counted as `forward` counts whom it publishes to. Never evicts; never refuses the sharer.
        live = {
            r["userId"]
            for r in await store._query(key["pk"], begins_with="VIEWER#")
            if int(r.get("expiresAt", 0)) > now
        }
        if auth.user_id not in live and len(live) >= MAX_SESSION_VIEWERS:
            raise sessions.ApiProblem(
                "LOCAL_SESSION_VIEWERS_FULL", f"This session already has {MAX_SESSION_VIEWERS} live viewers."
            )
    if fresh:
        await store.run_in_thread(
            table.update_item,
            Key=key,
            UpdateExpression="SET userId = :u, expiresAt = :exp, since = :now REMOVE typedAt",
            ExpressionAttributeValues=values,
        )
    # The marker the frames channel's authorizer reads, for a session shared before put_session
    # wrote it (checked shared just above). On every watch, renewals too: a view whose lease is
    # still live renews instead of starting fresh (Codex on #3916). Idempotent; one write per 15 s.
    await store.run_in_thread(
        table.put_item,
        Item={
            "pk": store.workspace_pk(await store.ensure_workspace_id(row["ownerId"])),
            "sk": f"SESSION#{row['sessionId']}",
            "ownerId": row["ownerId"],
        },
    )
    await tell_mac(row, now, fresh)
    # The owner's band names the new viewer now, not at the next heartbeat ack (up to 10 s later).
    # After the nudge: the picture matters more than the name. The owner is never listed.
    if fresh and row.get("endedAt") is None and auth.user_id != row["humanUserId"]:
        await session_presence.push(row)
    # What the view says about its audience, while people watch by link: counts only. The owner
    # is not counted, as on the band. No link viewer (nearly every session): no read, and zeros.
    via_link = await session_presence.link_viewers(row, now)
    members = {
        r["userId"]
        for r in (await store._query(key["pk"], begins_with="VIEWER#") if via_link else ())
        if int(r.get("expiresAt", 0)) > now and r["userId"] != row["humanUserId"]
    }
    return {"watching": len(members), "viaLink": via_link}


async def tell_mac(row: dict, now: int, fresh: bool) -> None:
    """Someone watches this session for the next 45 s: its Mac's heartbeat ack lists it, and a
    viewer with no screen yet (`fresh`) gets history and a snapshot now. Shared by workspace
    viewers (`watch`) and link viewers (`session_links.viewing`)."""
    await store.run_in_thread(
        store._table().update_item,
        Key=_watch_key(row),
        UpdateExpression="SET sessionId = :sid, expiresAt = :exp, since = "
        + (":now" if fresh else "if_not_exists(since, :now)"),
        ExpressionAttributeValues={":sid": row["sessionId"], ":exp": now + VIEWER_TTL_S, ":now": now},
    )
    if fresh and "liveTerminal" in row.get("deviceFeatures", ()):
        await _nudge(row, now)


async def _screen_gone(row: dict) -> None:
    """A final screen's lease lapsed (the app quit or closed that holder) and nothing says so: open
    panels refetch now, once. The lease is removed so the next refused ping tells nobody again."""
    from core.services import local_session_relay  # it imports this module

    try:
        await store.run_in_thread(
            store._table().update_item,
            Key={"pk": row["pk"], "sk": "META"},
            UpdateExpression="REMOVE screenUntil",
            ConditionExpression="screenUntil = :u",
            ExpressionAttributeValues={":u": row["screenUntil"]},
        )
    except ClientError:
        return  # renewed meanwhile, or another ping already told them
    await local_session_relay.publish_resources_changed(row["ownerId"], row["humanUserId"])


async def _nudge(row: dict, since: int) -> None:
    """Tell the Mac now, not at its next heartbeat ack (up to 10 s). Only a Mac that advertised
    `liveTerminal`: an older desktop drops its connection over a frame it does not know. The
    Mac never answers; the ack's `watching` stays the backstop and the stop signal.

    `since` is the WATCH row's own, so the ack that lists this viewer next is not news to the
    Mac and asks for no second history burst. A desktop that predates the key ignores it (it
    reads `sessionId` and `generation` out of the JSON by name)."""
    from core.services.local_session_relay import check_envelope, get_local_session_relay  # imports this module

    envelope = {
        "type": "local.session.watch",
        "sessionId": row["sessionId"],
        "generation": row["generation"],
        "since": since,
    }
    # An ended session's final screen is on the connection that renews it.
    cid = row.get("screenConnectionId") if row.get("endedAt") is not None else row["connectionId"]
    try:
        check_envelope(envelope)
        if not await get_local_session_relay().management.send_message_async(cid, envelope):
            logger.info("session watch nudge not delivered")
    except Exception as exc:  # noqa: BLE001 -- best effort; the next ack carries the same news
        logger.info("session watch nudge failed error=%s", type(exc).__name__)


async def ack_with_watching(ack: dict, owner: str, human: str, device: str) -> dict:
    """Every session on this Mac someone watches now; the Mac streams exactly these. Always
    present when read (an empty list stops every relay); absent only when the read failed."""
    try:
        now = int(_clock())
        rows = await store._query(repo.user_pk(owner, human), begins_with=f"WATCH#{device}#")
        watching = []
        # Expired rows first: their TTL delete lags up to 48 h and must not push live ones out.
        live = [r for r in rows if int(r.get("expiresAt", 0)) > now]
        for r in sorted(live, key=lambda r: int(r.get("since", 0)))[-ACK_CAP:]:
            meta = await repo.get_session(owner, r["sessionId"])
            if meta is None or meta.get("unshared") or meta.get("removedAt") is not None:
                continue
            if meta.get("endedAt") is not None and not sessions.screen_available(meta, now):
                continue
            watching.append({"sessionId": r["sessionId"], "since": int(r["since"])})
    except Exception:
        logger.warning("Watched sessions unavailable")  # never costs the heartbeat
        return ack
    while True:
        candidate = {**ack, "watching": watching}
        if len(json.dumps(candidate, separators=(",", ":")).encode()) <= sessions.MAX_ENVELOPE_BYTES or not watching:
            return candidate
        watching = watching[1:]  # oldest viewer set first


async def watch_on_register(row: dict) -> dict | None:
    """Someone watches the session that just registered: the nudge, on its own registration ack.

    After a reconnect the Mac heard of its viewers only from its first heartbeat ack, sent after
    every session had registered again (25 s in prod, 2026-10-07), and a nudge sent while the
    connection was down reached nobody. Shaped like the nudge, with the WATCH row's own `since`.
    Never costs the registration."""
    try:
        if row.get("unshared"):
            return None
        watch = (await store.run_in_thread(store._table().get_item, Key=_watch_key(row))).get("Item")
        if not watch or int(watch.get("expiresAt", 0)) <= int(_clock()):
            return None
        return {"sessionId": row["sessionId"], "generation": row["generation"], "since": int(watch["since"])}
    except Exception:
        logger.warning("Watched session unavailable at registration")
        return None


async def keep_screens(owner: str, human: str, device: str, cid: str, sids: list[str]) -> None:
    """The Mac's ended held sessions whose holder still shows the final screen (spec 2026-10-07
    §5.4): each stays watchable for one more lease. Only this Mac's own; never raises (a lost
    renewal lapses the screen 30 s later, nothing worse). A screen that was not there (the end
    came before the holder had it, or its lease lapsed in a heartbeat gap) is news: its viewers'
    Mac relay is asked for the picture, and open panels refetch, once."""
    from core.services.local_session_relay import LIVENESS_SECONDS, publish_resources_changed  # imports this module

    now = int(_clock())
    for sid in sids:
        try:
            row = await repo.get_session(owner, sid)
            if (
                row is None
                or row.get("humanUserId") != human
                or row.get("deviceId") != device
                or row.get("endedAt") is None
                or sessions.session_terminal(row) != "held"
            ):
                continue
            # ponytail: a write per kept screen per heartbeat (10 s); skip it while the lease is fresh if that ever shows.
            await store.run_in_thread(
                store._table().update_item,
                Key={"pk": repo.session_pk(owner, sid), "sk": "META"},
                UpdateExpression="SET screenUntil = :u, screenConnectionId = :c",
                ConditionExpression="attribute_exists(endedAt) AND deviceId = :d",
                ExpressionAttributeValues={":u": now + LIVENESS_SECONDS, ":c": cid, ":d": device},
            )
            if not sessions.screen_available(row, now):
                # Whoever watched it through the end has no picture of it: the frames the Mac sent
                # between the end and this report were dropped (`forward`). Ask the Mac for it now,
                # as for a new viewer, before any page has heard and asked for itself.
                watched = await watch_on_register(row)
                if watched and "liveTerminal" in row.get("deviceFeatures", ()):
                    await _nudge({**row, "screenConnectionId": cid}, watched["since"])
                await publish_resources_changed(owner, human)
        except Exception as exc:  # noqa: BLE001 -- see docstring
            logger.info("ended screen not kept error=%s", type(exc).__name__)


MAX_RAW_BYTES = 11 * 1024
# How long the public watch stays quiet after the owner's End was admitted: End has 30 s to reach
# the Mac, and the provider a few more to leave.
END_QUIET_S = 60
BUDGET_BYTES_PER_MINUTE = 2 * 1024 * 1024
# ponytail: per backend task, so N tasks allow N x 2 MiB/min per session; a shared counter would
# cost a write per frame. Move to a per-minute DynamoDB counter if a session is seen abusing it.
_spent: dict[str, tuple[int, int]] = {}


class Screen(ContractModel):
    type: Literal["local.session.screen"]
    session_id: uuid.UUID
    generation: sessions.OpaqueId
    epoch: int = Field(ge=0, lt=2**53)
    seq: int = Field(ge=0, lt=2**53)
    cols: int = Field(ge=1, le=1000)
    rows: int = Field(ge=1, le=500)
    # history: the holder's recent output, before a snapshot, for viewers with no screen yet;
    # ping: no data, proves a quiet stream is alive (spec §5.2).
    kind: Literal["snapshot", "output", "history", "ping"]
    # A snapshot over MAX_RAW_BYTES: this many consecutive seqs from this one make the picture
    # (the rest are "output"). The viewer writes them in one write. Old Macs omit it. <=16 = the
    # viewer's HELD.
    parts: int | None = Field(default=None, ge=2, le=16)
    data: str = Field(max_length=4 * ((MAX_RAW_BYTES + 2) // 3))

    @field_validator("data")
    @classmethod
    def _base64(cls, value: str) -> str:
        try:
            base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("data is not base64") from exc
        return value

    @model_validator(mode="after")
    def _ping_is_empty(self) -> Screen:
        if self.kind == "ping" and self.data:
            raise ValueError("a ping carries no data")
        if self.parts is not None and self.kind != "snapshot":
            raise ValueError("only a snapshot has parts")
        return self


def _spent_up(sid: str, size: int, now: float) -> bool:
    """This frame would not fit this minute's budget: drop it before it costs any read."""
    at, spent = _spent.get(sid, (-1, 0))
    return at == int(now // 60) and spent + size > BUDGET_BYTES_PER_MINUTE


def _spend(sid: str, size: int, now: float) -> bool:
    minute = int(now // 60)
    if len(_spent) > 10_000:
        for key in [k for k, (m, _) in _spent.items() if m != minute]:
            del _spent[key]
    at, spent = _spent.get(sid, (minute, 0))
    spent = spent if at == minute else 0
    if spent + size > BUDGET_BYTES_PER_MINUTE:
        return False
    _spent[sid] = (minute, spent + size)
    return True


async def forward(relay, cid: str, body: dict) -> None:
    """One `local.session.screen` frame from a Mac to the people watching that session.

    Never answers and never raises: the Mac waits for no reply, and an error frame would end
    its relay connection. Snapshots come on join, resize and loss; a quiet Mac pings. There is no
    periodic redraw, so a dropped frame costs a viewer the wait for the next snapshot.
    Checks the row like any session frame; never writes it, nor anything else. One AppSync
    publish per frame, whatever the number of viewers."""
    try:
        frame = Screen.model_validate(body)
        size = len(base64.b64decode(frame.data))
        # Only drops: the real spend is below, after the frame is proven to be this Mac's.
        if _spent_up(str(frame.session_id), size, _clock()):
            put_metric("session.screen.frame.dropped")
            return
        connection, _ = await relay.connect(cid)
        owner = connection.get("org_id") or connection["user_id"]
        sid = str(frame.session_id)
        row = await repo.get_session(owner, sid)
        if (
            row is None
            or row.get("humanUserId") != connection["user_id"]
            or row.get("deviceId") != connection["device_id"]
            or row.get("generation") != frame.generation
            or row.get("unshared")
        ):
            return
        now = _clock()
        if row.get("endedAt") is None:
            if row.get("connectionId") != cid:
                return
        elif not sessions.screen_available(row, int(now)) or row.get("screenConnectionId") != cid:
            return  # an ended session streams only its kept final screen, from the connection that keeps it
        seats = [
            r
            for r in await store._query(repo.session_pk(owner, sid), begins_with="VIEWER#")
            if int(r.get("expiresAt", 0)) > now
        ]
        viewers = [r["userId"] for r in seats]
        # An "Anyone with the link" link's watchers: read on every frame, like the row above, so a
        # link that stops being public, loses its last watcher, or whose session ends gets no
        # further frame (an ended session's kept final screen is for the workspace). One item,
        # written by `session_links.viewing`. A failure here never costs the workspace its frame.
        # Nor while the owner's End is on its way (`endAskedAt`): what the provider prints as it
        # leaves (`claude --resume <id>`) is not for the link. A refused End frees it after a minute.
        ending = int(row.get("endAskedAt") or 0) > now - END_QUIET_S
        public = sessions.public_id(row.get("shareLink"), row) if row.get("endedAt") is None and not ending else None
        by_link = False
        if public:
            try:
                watched = await store._get(f"PUBWATCH#{public}", "WATCHED")
                by_link = int((watched or {}).get("until", 0)) > now
            except Exception as exc:  # noqa: BLE001 -- see above
                logger.info("session link watchers unread error=%s", type(exc).__name__)
        if not viewers and not by_link:
            return
        if not _spend(sid, size, now):
            put_metric("session.screen.frame.dropped")
            return
        payload = frame.model_dump(mode="json", by_alias=True, exclude={"type"}, exclude_none=True)
        typist = await _cursor_typist(row, seats, now)
        if typist:
            payload["typist"] = typist
        publisher = live_events.get_live_event_publisher()

        async def to_workspace() -> None:
            workspace = await store.ensure_workspace_id(owner)  # minted long before any session: a read
            await publisher.publish_session_frames(workspace, make_live_event("workspace.session.screen", payload))

        async def to_link() -> None:
            # The publicId stands in for the session's id and generation: a link viewer learns neither.
            # Nor the typist's user id: their name and colour only.
            shown = {**payload, "sessionId": public, "generation": public}
            if typist:
                shown["typist"] = {
                    "name": typist["name"],
                    "color": _person_color(typist["userId"]),
                    "forMs": typist["forMs"],
                }
            await publisher.publish_link_frames(public, make_live_event("workspace.session.screen", shown))

        # Each audience on its own, at the same time: neither publish waits on, or fails with, the
        # other, and the public one goes out as soon after the row was read as it can. Revocation
        # is cooperative (owner ruling 2026-10-08): the frame already on its way when a link is
        # narrowed may still be delivered; the next frame reads the row again.
        results = await asyncio.gather(
            *([to_workspace()] if viewers else []), *([to_link()] if by_link else []), return_exceptions=True
        )
        failed = sum(isinstance(r, BaseException) for r in results)
        for _ in range(failed):
            put_metric("session.screen.publish.failed")  # metered; the Mac is never told
        if failed == len(results):
            return
        put_metric("session.screen.frame.forwarded")
        put_metric("session.screen.bytes", size, "Bytes")
        put_metric("session.screen.viewers", len(viewers))
    except Exception as exc:  # noqa: BLE001 -- see docstring
        logger.info("session screen frame dropped error=%s", type(exc).__name__)


TYPIST_SHOWN_S = 4
_PERSON_COLORS = ("cyan", "magenta", "green", "yellow", "blue", "blueBright")


def _person_color(user: str) -> str:
    """`personColor` in @lightbulb/shared (FNV-1a over the user id, the same palette order): the
    colour a link viewer gets in place of the id. Keep the two in step."""
    h = 0x811C9DC5
    for ch in user:
        h = ((h ^ ord(ch)) * 0x01000193) & 0xFFFFFFFF
    return _PERSON_COLORS[h % len(_PERSON_COLORS)]


async def _cursor_typist(row: dict, seats: list[dict], now: float) -> dict | None:
    """Who typed into this terminal last, for about TYPIST_SHOWN_S after (S8): every view draws the
    cursor in their colour with their name. From the `typedAt` marks on the viewer rows this frame
    already read. The owner typing last (a tie included) shows nobody: the plain cursor."""
    last = max(seats, key=lambda r: (int(r.get("typedAt", 0)), r["userId"] == row["humanUserId"]), default=None)
    left = int(last.get("typedAt", 0)) + TYPIST_SHOWN_S - now if last else 0
    if left <= 0 or last["userId"] == row["humanUserId"]:
        return None
    user = last["userId"]
    name = await session_presence._name(user)
    return {"userId": user, "name": "A teammate" if name == user else name, "forMs": int(left * 1000)}


# Remote input (owner ruling 2026-10-03): an admitted person types into the live terminal. Their
# keystrokes act as the owner on the owner's Mac. Nothing they type is stored or logged here:
# the audit row carries who, when and how many bytes, never the bytes.
INPUT_MAX_RAW = 4096
INPUT_CALLS_PER_S = 20
INPUT_BURST_CALLS = 40
INPUT_BYTES_PER_MINUTE = 64 * 1024
TYPED_BURST_GAP_S = 10
TYPED_TTL_S = 30 * 24 * 3600


REMOTE_RESIZE_FEATURE = sessions.REMOTE_RESIZE_FEATURE


class SessionSize(ContractModel):
    """The app panel's terminal size. The session takes the size of whoever used it last, as
    tmux's `window-size latest`: `claim` says the panel is used now (opened, shown or typed into);
    without it the size applies only while the panel is already the latest user (the holder decides)."""

    cols: int = Field(ge=40, le=300)
    rows: int = Field(ge=10, le=120)
    claim: bool = False


class SessionInput(ContractModel):
    # Empty only with a `size`: a resize alone.
    data: str = Field(default="", max_length=4 * ((INPUT_MAX_RAW + 2) // 3))
    epoch: int = Field(ge=0, lt=2**53)
    size: SessionSize | None = None

    @field_validator("size", mode="wrap")
    @classmethod
    def _tolerant(cls, value, handler):
        # A size this backend cannot read never costs the keys beside it: it is dropped. (A size
        # alone then carries nothing, which `_something` refuses.)
        try:
            return handler(value)
        except ValueError:
            return None

    @model_validator(mode="after")
    def _something(self) -> SessionInput:
        if not self.data and self.size is None:
            raise ValueError("data or size is required")
        return self

    @field_validator("data")
    @classmethod
    def _base64(cls, value: str) -> str:
        if value == "":
            return value
        try:
            raw = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("data is not base64") from exc
        if not 1 <= len(raw) <= INPUT_MAX_RAW:
            raise ValueError(f"data must be 1 to {INPUT_MAX_RAW} bytes")
        return value


@dataclass
class _Typist:
    at: float  # last token refill
    tokens: float = INPUT_BURST_CALLS
    minute: int = 0
    spent: int = 0
    burst: int = 0  # this person's current burst start (its audit row), 0 = none yet
    last: float = 0.0  # their last accepted call


# ponytail: per backend task, so N tasks allow N x (20 calls/s, 64 KiB/min) per (session, person),
# and a burst that hops tasks splits into two audit rows. A shared counter would cost a write per
# key; move the bucket to DynamoDB if someone is seen abusing it.
_typists: dict[tuple[str, str], _Typist] = {}


def _admit(typist: _Typist, size: int, now: float) -> bool:
    typist.tokens = min(INPUT_BURST_CALLS, typist.tokens + (now - typist.at) * INPUT_CALLS_PER_S)
    typist.at = now
    minute = int(now // 60)
    if minute != typist.minute:
        typist.minute, typist.spent = minute, 0
    if typist.tokens < 1 or typist.spent + size > INPUT_BYTES_PER_MINUTE:
        return False
    typist.tokens -= 1
    typist.spent += size
    return True


def _typist(sid: str, user: str, now: float) -> _Typist:
    if len(_typists) > 10_000:
        for key in [k for k, t in _typists.items() if now - t.at > 60]:
            del _typists[key]
    return _typists.setdefault((sid, user), _Typist(at=now))


async def _mark_typing(row: dict, user: str, now: int) -> None:
    """`typedAt` on the typist's live VIEWER row, so presence reads typing from the rows it
    already reads. Never creates or revives a row: someone typing without watching is not shown.
    Best effort: a failure here never costs the keystrokes."""
    key = _viewer_key(row, user)
    try:
        await store.run_in_thread(
            store._table().update_item,
            Key=key,
            UpdateExpression="SET typedAt = :now",
            ConditionExpression="attribute_exists(pk) AND expiresAt > :now",
            ExpressionAttributeValues={":now": now},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            logger.info("session typing mark failed error=%s", exc.response["Error"]["Code"])
    except Exception as exc:  # noqa: BLE001 -- the label is a courtesy; the keys still go
        logger.info("session typing mark failed error=%s", type(exc).__name__)


async def type_into(auth: AuthContext, session_id: str, body: SessionInput) -> None:
    """One batch of keystrokes from an admitted person into a running held session's terminal.

    Admission is re-read on every call (one META read inside `authorize_session`, nothing
    cached): the owner, a person they let in, or anyone while team control is on, through the
    same `may_control` that gates follow-ups. Agents are refused on the roomless path, no room id
    is accepted, and the route takes a Clerk session JWT only, so MCP's opaque tokens never
    reach it. A refused call sends nothing to the Mac."""
    row, _ = await sessions.authorize_session(auth, None, session_id, owner_only=True)
    await _still_a_member(row, auth.user_id)
    now = _clock()
    if row.get("endedAt") is not None or row.get("unshared") or int(row.get("onlineUntil", 0)) <= now:
        raise sessions.ApiProblem("LOCAL_SESSION_OFFLINE", "This session is not running.")
    if sessions.session_terminal(row) is None:
        raise sessions.ApiProblem("LOCAL_SESSION_UNSUPPORTED", "This session has no live terminal.")
    if "remoteInput" not in row.get("deviceFeatures", ()):
        raise sessions.ApiProblem("LOCAL_SESSION_UNSUPPORTED", "This Mac cannot take typing from elsewhere yet.")
    size = len(base64.b64decode(body.data))
    # A Mac too old to take the panel's size (or a relay before the feature) gets the keys alone.
    resize = body.size if REMOTE_RESIZE_FEATURE in row.get("deviceFeatures", ()) else None
    if not size and resize is None:
        return
    sid, user = row["sessionId"], auth.user_id
    typist = _typist(sid, user, now)
    if not _admit(typist, size, now):
        raise sessions.ApiProblem("RATE_LIMITED", "Typing too fast.", retryable=True, retry_after=1)
    from core.services.local_session_relay import check_envelope, get_local_session_relay  # imports this module

    envelope = {
        "type": "local.session.input",
        "sessionId": sid,
        "generation": row["generation"],
        "epoch": body.epoch,
        "data": body.data,
        # The session's owner: their Mac passes these keys untouched. Anyone else's lose ctrl+x
        # and mouse reports there, so only the owner can operate the terminal's Claude band.
        "fromOwner": user == row["humanUserId"],
    }
    if resize is not None:
        envelope["size"] = resize.model_dump()
    if size and user != row["humanUserId"]:
        # Who typed: the Mac's Claude terminal labels a prompt a teammate submitted with their
        # name. `_name` keeps its own short cache, so nothing is kept here: a failed lookup (the
        # bare id, or its "A teammate" fallback) sends this frame without a name and the next asks again.
        name = await session_presence._name(user)
        if name not in (user, "A teammate"):
            envelope["actor"] = {"userId": user, "name": name}
    check_envelope(envelope)
    if size:
        await _audit_typing(row, user, typist, size, now)
    try:
        sent = await get_local_session_relay().management.send_message_async(row["connectionId"], envelope)
    except Exception as exc:  # noqa: BLE001 -- reported as offline; the content is never logged
        logger.info("session input not delivered error=%s", type(exc).__name__)
        sent = False
    if not sent:
        raise sessions.ApiProblem("LOCAL_SESSION_OFFLINE", "The desktop session is offline.", retryable=True)


async def _audit_typing(row: dict, user: str, typist: _Typist, size: int, now: float) -> None:
    """Audited before it is sent: a keystroke that reached the Mac always has its row. A resize
    alone types nothing, so it has none and marks nobody typing."""
    new_burst = not typist.burst or now - typist.last > TYPED_BURST_GAP_S
    if new_burst:
        typist.burst = int(now)
    typist.last = now
    await store.run_in_thread(
        store._table().update_item,
        Key={"pk": repo.session_pk(row["ownerId"], row["sessionId"]), "sk": f"TYPED#{user}#{typist.burst}"},
        UpdateExpression="SET userId = :u, startedAt = if_not_exists(startedAt, :s), lastAt = :t, "
        "expiresAt = :e ADD #b :n",
        ExpressionAttributeNames={"#b": "bytes"},
        ExpressionAttributeValues={
            ":u": user,
            ":s": typist.burst,
            ":t": int(now),
            ":e": int(now) + TYPED_TTL_S,
            ":n": size,
        },
    )
    # The owner's mark too: their keys put the plain cursor back on every view (`_cursor_typist`).
    await _mark_typing(row, user, int(now))
