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

import base64
import binascii
import logging
import time
import uuid
from typing import Literal

from botocore.exceptions import ClientError
from pydantic import Field, field_validator, model_validator

from core.repositories import local_sessions_repo as repo, messaging_repo as store
from core.services import local_sessions as sessions, session_presence
from core.services.messaging import ContractModel

logger = logging.getLogger(__name__)

VIEWER_TTL_S = 45
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
    await local_session_relay.publish_resources_changed(row["ownerId"], row["humanUserId"], session_id=row["sessionId"])


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


MAX_RAW_BYTES = 11 * 1024
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


# Remote input (owner ruling 2026-10-03): an admitted person types into the live terminal. Their
# keystrokes act as the owner on the owner's Mac. Nothing they type is stored or logged here:
# the audit row carries who, when and how many bytes, never the bytes.
INPUT_MAX_RAW = 4096


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
