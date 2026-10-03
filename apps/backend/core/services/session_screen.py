"""Watching a running terminal (spec §5.5, rulings 2026-10-03 §4-5).

LOCALSESSION#<owner>#<sid> / VIEWER#<user>          who gets this session's screen frames
LOCALUSER#<owner>#<human>  / WATCH#<device>#<sid>   what that Mac's heartbeat ack lists
LOCALSESSION#<owner>#<sid> / TYPED#<user>#<start>   one admitted person's typing burst (who,
                                                    when, byte count; never the bytes), 30 days

Both expire 45 s after the viewer's last POST (they post every 15 s). `since` is when the
newest viewer arrived: the Mac sends a fresh snapshot when it moves. Screen frames themselves
are published to each viewer's own user channel and stored nowhere (see `forward`).
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

from pydantic import Field, field_validator

from core.auth import AuthContext
from core.repositories import local_sessions_repo as repo, messaging_repo as store
from core.services import live_events, local_sessions as sessions
from core.services.messaging import ContractModel, make_live_event

logger = logging.getLogger(__name__)

VIEWER_TTL_S = 45
ACK_CAP = 24
_clock = time.time


def _viewer_key(row: dict, user: str) -> dict:
    return {"pk": repo.session_pk(row["ownerId"], row["sessionId"]), "sk": f"VIEWER#{user}"}


def _watch_key(row: dict) -> dict:
    return {
        "pk": repo.user_pk(row["ownerId"], row["humanUserId"]),
        "sk": f"WATCH#{row['deviceId']}#{row['sessionId']}",
    }


async def watch(auth: sessions.Caller, session_id: str) -> None:
    """A person has this running session's terminal on screen. Humans who can read it only:
    `authorize_session` refuses agents on the roomless path, and no room id is accepted here."""
    row, _ = await sessions.authorize_session(auth, None, session_id)
    now = int(_clock())
    if row.get("endedAt") is not None or row.get("unshared") or int(row.get("onlineUntil", 0)) <= now:
        raise sessions.ApiProblem("LOCAL_SESSION_OFFLINE", "This session is not running.")
    key = _viewer_key(row, auth.user_id)
    viewer = await store._get(key["pk"], key["sk"])
    fresh = viewer is None or int(viewer.get("expiresAt", 0)) <= now
    expires = now + VIEWER_TTL_S
    table = store._table()
    await store.run_in_thread(
        table.put_item,
        Item={**key, "userId": auth.user_id, "since": now if fresh else int(viewer["since"]), "expiresAt": expires},
    )
    await store.run_in_thread(
        table.update_item,
        Key=_watch_key(row),
        UpdateExpression="SET sessionId = :sid, expiresAt = :exp, since = "
        + (":now" if fresh else "if_not_exists(since, :now)"),
        ExpressionAttributeValues={":sid": row["sessionId"], ":exp": expires, ":now": now},
    )
    if fresh and "liveTerminal" in row.get("deviceFeatures", ()):
        await _nudge(row)


async def _nudge(row: dict) -> None:
    """Tell the Mac now, not at its next heartbeat ack (up to 10 s). Only a Mac that advertised
    `liveTerminal`: an older desktop drops its connection over a frame it does not know. The
    Mac never answers; the ack's `watching` stays the backstop and the stop signal."""
    from core.services.local_session_relay import check_envelope, get_local_session_relay  # imports this module

    envelope = {"type": "local.session.watch", "sessionId": row["sessionId"], "generation": row["generation"]}
    try:
        check_envelope(envelope)
        if not await get_local_session_relay().management.send_message_async(row["connectionId"], envelope):
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
        for r in sorted(rows, key=lambda r: int(r.get("since", 0)))[-ACK_CAP:]:
            if int(r.get("expiresAt", 0)) <= now:
                continue
            meta = await repo.get_session(owner, r["sessionId"])
            if meta is None or meta.get("endedAt") is not None or meta.get("unshared"):
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
    kind: Literal["snapshot", "output"]
    data: str = Field(max_length=4 * ((MAX_RAW_BYTES + 2) // 3))

    @field_validator("data")
    @classmethod
    def _base64(cls, value: str) -> str:
        try:
            base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("data is not base64") from exc
        return value


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
    its relay connection. A dropped frame costs a viewer one snapshot interval (at most 10 s).
    Checks the row like any session frame; never writes it, nor anything else."""
    try:
        frame = Screen.model_validate(body)
        connection, _ = await relay.connect(cid)
        owner = connection.get("org_id") or connection["user_id"]
        sid = str(frame.session_id)
        row = await repo.get_session(owner, sid)
        if (
            row is None
            or row.get("humanUserId") != connection["user_id"]
            or row.get("deviceId") != connection["device_id"]
            or row.get("connectionId") != cid
            or row.get("generation") != frame.generation
            or row.get("endedAt") is not None
            or row.get("unshared")
        ):
            return
        now = _clock()
        viewers = [
            r["userId"]
            for r in await store._query(repo.session_pk(owner, sid), begins_with="VIEWER#")
            if int(r.get("expiresAt", 0)) > now
        ]
        if not viewers or not _spend(sid, len(base64.b64decode(frame.data)), now):
            return
        event = make_live_event(
            "workspace.session.screen", frame.model_dump(mode="json", by_alias=True, exclude={"type"})
        )
        publisher = live_events.get_live_event_publisher()
        # publish_user finds no channel for a removed member: they get nothing.
        await asyncio.gather(*(publisher.publish_user(owner, user, event) for user in viewers), return_exceptions=True)
    except Exception as exc:  # noqa: BLE001 -- see docstring
        logger.info("session screen frame dropped error=%s", type(exc).__name__)


# Remote input (owner ruling 2026-10-03): an admitted person types into the live terminal. Their
# keystrokes act as the owner on the owner's Mac. Nothing they type is stored or logged here:
# the audit row carries who, when and how many bytes, never the bytes.
INPUT_MAX_RAW = 4096
INPUT_CALLS_PER_S = 20
INPUT_BURST_CALLS = 40
INPUT_BYTES_PER_MINUTE = 64 * 1024
TYPED_BURST_GAP_S = 10
TYPED_TTL_S = 30 * 24 * 3600
TYPING_REPUBLISH_S = 5


class SessionInput(ContractModel):
    data: str = Field(min_length=1, max_length=4 * ((INPUT_MAX_RAW + 2) // 3))
    epoch: int = Field(ge=0, lt=2**53)

    @field_validator("data")
    @classmethod
    def _base64(cls, value: str) -> str:
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
    told: float = 0.0  # when the owner last heard "is typing"


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


async def type_into(auth: AuthContext, session_id: str, body: SessionInput) -> None:
    """One batch of keystrokes from an admitted person into a running held session's terminal.

    Admission is re-read on every call (one META read inside `authorize_session`, nothing
    cached): the owner, a person they let in, or anyone while team control is on, through the
    same `may_control` that gates follow-ups. Agents are refused on the roomless path, no room id
    is accepted, and the route takes a Clerk session JWT only, so MCP's opaque tokens never
    reach it. A refused call sends nothing to the Mac."""
    row, _ = await sessions.authorize_session(auth, None, session_id, owner_only=True)
    now = _clock()
    if row.get("endedAt") is not None or row.get("unshared") or int(row.get("onlineUntil", 0)) <= now:
        raise sessions.ApiProblem("LOCAL_SESSION_OFFLINE", "This session is not running.")
    if "remoteInput" not in row.get("deviceFeatures", ()):
        raise sessions.ApiProblem("LOCAL_SESSION_UNSUPPORTED", "This Mac cannot take typing from elsewhere yet.")
    size = len(base64.b64decode(body.data))
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
    }
    check_envelope(envelope)
    new_burst = not typist.burst or now - typist.last > TYPED_BURST_GAP_S
    if new_burst:
        typist.burst = int(now)
    typist.last = now
    # Audited before it is sent: a keystroke that reached the Mac always has its row.
    await store.run_in_thread(
        store._table().update_item,
        Key={"pk": repo.session_pk(row["ownerId"], sid), "sk": f"TYPED#{user}#{typist.burst}"},
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
    try:
        sent = await get_local_session_relay().management.send_message_async(row["connectionId"], envelope)
    except Exception as exc:  # noqa: BLE001 -- reported as offline; the content is never logged
        logger.info("session input not delivered error=%s", type(exc).__name__)
        sent = False
    if not sent:
        raise sessions.ApiProblem("LOCAL_SESSION_OFFLINE", "The desktop session is offline.", retryable=True)
    if user != row["humanUserId"] and (new_burst or now - typist.told >= TYPING_REPUBLISH_S):
        typist.told = now
        try:
            await live_events.get_live_event_publisher().publish_user(
                row["ownerId"],
                row["humanUserId"],
                make_live_event("workspace.session.typing", {"sessionId": sid, "userId": user}),
            )
        except Exception as exc:  # noqa: BLE001 -- the label is a courtesy; the keys already went
            logger.info("session typing notice failed error=%s", type(exc).__name__)
