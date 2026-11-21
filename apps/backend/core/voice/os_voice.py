"""Spark voice: GPT-Live WebRTC sessions on the company key, and the sideband that meters and ends them.

The Mac sends its SDP offer; `open_session` creates the session with OpenAI and the Mac's audio then flows
directly to OpenAI. Each session is one containers-table row (`__osvoice__#<id>`) on the sparse
`status-index` while active; the person's users row holds the set of their active session ids
(`os_voice_active`: one per person, newest wins, the older one is marked `closeRequested` in the same
write) and their running total of voice seconds (`os_voice_seconds`).

One task holds each session's sideband under a lease on its row; every task's sweep re-attaches lapsed ones.

Never log an SDP (host candidates carry the Mac's LAN addresses), the instructions, the input or an OpenAI
error message (it can quote the request): ids, sizes, statuses and outcomes only."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time
import uuid

import httpx
import websockets
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from core.config import settings
from core.dynamodb import get_table, is_cancelled, run_in_thread, transact
from core.observability.metrics import put_metric
from core.repositories import container_repo

logger = logging.getLogger(__name__)
_WS_LOG = logging.getLogger(__name__ + ".ws")
_WS_LOG.setLevel(logging.WARNING)  # websockets logs frame contents (transcripts) at DEBUG

LIVE_URL = "https://api.openai.com/v1/live/sessions"
ATTACH_URL = "wss://api.openai.com/v1/live/sessions/{}/attach"
KEY_PREFIX = "__osvoice__#"
ACTIVE = "os_voice_active"
ALLOWED_CLIENT_EVENTS = [
    "session.input_audio.mute",
    "session.input_audio.unmute",
    "session.instructions.append",
    "session.thinking.append",
    "session.commentary.append",
    "session.close",
]
CREATE_TIMEOUT_S = 20.0  # under API Gateway's 30 s
RETRY_AFTER_S = 5  # OpenAI refused for capacity without a Retry-After
TICK_S = 2.0  # lease renewal (which reads closeRequested), usage write and close checks while attached
LEASE_S = 30
SWEEP_S = 15
PENDING_S = 60  # a reserved session with no OpenAI id after this: its creating task died
CLOSE_WAIT_S = 30  # after our session.close, end it ourselves if session.closed never arrives
ENDED_TTL_S = 7 * 86400
MAX_SESSION_S = 2 * 3600  # unmeasured: OpenAI documents no WebRTC length (2 h is its SIP limit)
TASK_ID = f"{os.getenv('HOSTNAME') or 'local'}-{uuid.uuid4().hex[:8]}"

# The OpenAI close reasons, as the reasons GET /os/voice-session reports.
_REASONS = {"close_requested": "client_closed", "remote_hangup": "client_closed", "expired": "expired"}
_TASKS: dict[str, asyncio.Task] = {}
_connect = websockets.connect
_transport: httpx.AsyncBaseTransport | None = None  # tests swap in httpx.MockTransport


class _Upstream(Exception):
    def __init__(self, status: int, detail: str = "upstream", retry_after: int | None = None):
        self.status, self.detail, self.retry_after = status, detail, retry_after


def now() -> int:
    return int(time.time())


def _table():
    return get_table("containers")


def _key(sid: str) -> dict:
    return {"owner_id": f"{KEY_PREFIX}{sid}"}


def _auth() -> dict:
    return {"Authorization": f"Bearer {settings.OPENAI_API_KEY}"}


async def _create_live(body: dict) -> tuple[str, str]:
    """POST /v1/live/sessions. Returns (OpenAI session id, SDP answer)."""
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(CREATE_TIMEOUT_S, connect=5.0), transport=_transport
        ) as http:
            r = await http.post(LIVE_URL, json=body, headers=_auth())
    except httpx.TimeoutException:
        raise _Upstream(504) from None
    except httpx.HTTPError:
        raise _Upstream(502) from None
    if r.status_code == 201:
        with contextlib.suppress(ValueError, KeyError, TypeError):
            out = r.json()
            return out["session"]["id"], out["transport"]["sdp"]
    error = {}
    with contextlib.suppress(ValueError, AttributeError):
        error = r.json().get("error") or {}
    error = error if isinstance(error, dict) else {}
    logger.warning("os voice create refused: http %s %s %s", r.status_code, error.get("type"), error.get("code"))
    if r.status_code == 429:
        after = r.headers.get("retry-after", "")
        raise _Upstream(429, "capacity", int(after) if after.isdigit() else RETRY_AFTER_S)
    if r.status_code in (400, 422):  # ours to get right, or a limit only OpenAI measures (instruction tokens)
        raise _Upstream(400, "invalid_config")
    raise _Upstream(502)


async def reserve(user_id: str, sid: str, kind: str, client: dict, started_at: int) -> None:
    """Make `sid` the person's one session, in one transaction: write its row, set their active set to
    exactly {sid}, and mark every previous one `closeRequested: replaced` (its sideband then closes it).
    The write is conditional on the set being what was read, so of two concurrent opens the later one
    replaces the earlier: exactly one stays active."""
    users = get_table("users")
    row = {**_key(sid), "status": ACTIVE, "sessionId": sid, "userId": user_id, "kind": kind, "client": client}
    row |= {"startedAt": started_at, "usageSeconds": 0, "session_ttl": started_at + MAX_SESSION_S + ENDED_TTL_S}
    for _ in range(5):
        person = (await run_in_thread(users.get_item, Key={"user_id": user_id}, ConsistentRead=True)).get("Item") or {}
        previous = person.get("os_voice_active") or set()
        replaced = [
            {
                "Update": {
                    "TableName": _table().name,
                    "Key": _key(old),
                    "UpdateExpression": "SET closeRequested = :r",
                    "ConditionExpression": "#st = :active",
                    "ExpressionAttributeNames": {"#st": "status"},
                    "ExpressionAttributeValues": {":r": "replaced", ":active": ACTIVE},
                }
            }
            for old in previous
        ]
        set_new = {
            "TableName": users.name,
            "Key": {"user_id": user_id},
            "UpdateExpression": "SET os_voice_active = :sid",
            "ConditionExpression": "os_voice_active = :prev" if previous else "attribute_not_exists(os_voice_active)",
            "ExpressionAttributeValues": {":sid": {sid}} | ({":prev": previous} if previous else {}),
        }
        try:
            await transact(users, [{"Update": set_new}, {"Put": {"TableName": _table().name, "Item": row}}, *replaced])
            return
        except ClientError as e:
            if not is_cancelled(e):
                raise
    raise HTTPException(status_code=503, detail="voice_unavailable")  # the set kept changing under us


async def open_session(
    user_id: str, kind: str, client: dict, model: str, config: dict, sdp: str
) -> dict | JSONResponse:
    """Make this the person's one session (replacing any other), create the Live session with the
    backend-owned settings, and start its sideband. Returns the 201 body; raises the contract's error, or
    returns a JSONResponse for `capacity`."""
    started_at, sid = now(), f"vs_{uuid.uuid4().hex}"
    await reserve(user_id, sid, kind, client, started_at)
    config |= {
        "model": model,
        "store": False,
        "client": {"data_channel": {"allowed_client_events": ALLOWED_CLIENT_EVENTS}},
        "safety_identifier": hashlib.sha256(user_id.encode()).hexdigest(),  # OpenAI's per-user abuse signal
    }
    transport = {"type": "webrtc", "sdp": sdp, "dcid": 4}  # dcid in the body: OpenAI rejects query parameters
    try:
        live_id, answer = await _create_live({"session": config, "transport": transport})
    except _Upstream as e:
        await end_session(sid, "upstream", 0)
        if e.detail == "capacity":
            return JSONResponse(
                status_code=429,
                content={"detail": "capacity", "retryAfterS": e.retry_after},
                headers={"Retry-After": str(e.retry_after)},
            )
        raise HTTPException(status_code=e.status, detail=e.detail) from None
    try:
        await run_in_thread(
            _table().update_item,
            Key=_key(sid),
            UpdateExpression="SET liveId = :l",
            ExpressionAttributeValues={":l": live_id},
        )
    except (ClientError, BotoCoreError):  # OpenAI's session exists, but nothing can attach to it
        await _reap_unconfirmed(sid, 0)
        raise HTTPException(status_code=502, detail="upstream") from None
    start_sideband(sid)
    return {"sessionId": sid, "sdp": answer, "expiresAt": started_at + MAX_SESSION_S}


async def get(sid: str) -> dict | None:
    return (await run_in_thread(_table().get_item, Key=_key(sid), ConsistentRead=True)).get("Item")


async def request_close(sid: str) -> None:
    """The app will not use this session: mark it `closeRequested: client_closed` (never over another reason)
    and let its sideband close it on the next tick. With no live sideband, attach one here now."""
    try:
        row = (
            await run_in_thread(
                _table().update_item,
                Key=_key(sid),
                UpdateExpression="SET closeRequested = :r",
                ConditionExpression="#st = :active AND attribute_not_exists(closeRequested)",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={":r": "client_closed", ":active": ACTIVE},
                ReturnValues="ALL_NEW",
            )
        )["Attributes"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return  # ended already, or already being closed
        raise
    if row.get("liveId") and int(row.get("leaseUntil", 0)) < now():
        start_sideband(sid)


async def end_session(sid: str, reason: str, seconds: int) -> bool:
    """Mark the session ended, free its slot and add its seconds to the person's total, once: the row's
    condition makes a replayed close (or a second task's) a no-op. False when it had already ended."""
    row = await get(sid)
    if not row or row.get("status") != ACTIVE:
        return False
    at, users = now(), get_table("users")
    seconds = max(int(seconds), int(row.get("usageSeconds", 0)))
    try:
        await transact(
            users,
            [
                {
                    "Update": {
                        "TableName": _table().name,
                        "Key": _key(sid),
                        "UpdateExpression": "SET endedAt = :now, endedReason = :r, usageSeconds = :s, session_ttl = :ttl "
                        "REMOVE #st, leaseOwner, leaseUntil",
                        "ConditionExpression": "#st = :active",
                        "ExpressionAttributeNames": {"#st": "status"},
                        "ExpressionAttributeValues": {
                            ":now": at, ":r": reason, ":s": seconds, ":ttl": at + ENDED_TTL_S, ":active": ACTIVE,
                        },
                    }
                },
                {
                    "Update": {
                        "TableName": users.name,
                        "Key": {"user_id": row["userId"]},
                        "UpdateExpression": "DELETE os_voice_active :sid ADD os_voice_seconds :s",
                        "ExpressionAttributeValues": {":sid": {sid}, ":s": seconds},
                    }
                },
            ],
        )  # fmt: skip
    except ClientError as e:
        if is_cancelled(e):
            return False
        raise
    logger.info("os voice session %s ended: %s after %ss", sid, reason, seconds)
    return True


async def _reap_unconfirmed(sid: str, seconds: int) -> None:
    """End a session OpenAI never confirmed closing: it may still be running untracked, so it is counted."""
    if await end_session(sid, "upstream", seconds):
        put_metric("os_voice.session.reaped_unconfirmed")
        logger.warning("os voice session %s reaped without session.closed; final usage unconfirmed", sid)


async def _lease(sid: str, at: int, expression: str = "", values: dict | None = None) -> dict | None:
    """Take or renew the session's lease and return the row; None when another task holds it or the
    session ended."""
    try:
        out = await run_in_thread(
            _table().update_item,
            Key=_key(sid),
            UpdateExpression="SET leaseOwner = :me, leaseUntil = :until" + expression,
            ConditionExpression="#st = :active AND (attribute_not_exists(leaseUntil) OR leaseUntil < :now "
            "OR leaseOwner = :me)",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":me": TASK_ID, ":until": at + LEASE_S, ":now": at, ":active": ACTIVE}
            | (values or {}),
            ReturnValues="ALL_NEW",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise
    return out["Attributes"]


async def _release(sid: str, attach_failed: bool = False) -> None:
    """Free the lease so the next sweep re-attaches; an attach failure is counted on the row."""
    with contextlib.suppress(ClientError):
        await run_in_thread(
            _table().update_item,
            Key=_key(sid),
            UpdateExpression="REMOVE leaseOwner, leaseUntil" + (" ADD attachFailures :one" if attach_failed else ""),
            ConditionExpression="leaseOwner = :me",
            ExpressionAttributeValues={":me": TASK_ID} | ({":one": 1} if attach_failed else {}),
        )


async def _trial_ends_at(user_id: str) -> int | None:
    """The person's trial end, read consistently: it decides whether to close a live session. None (no row,
    no trial attribute, or a live Spark subscription) is never an ended trial."""
    from core.services import os_billing

    row = (await run_in_thread(get_table("users").get_item, Key={"user_id": user_id}, ConsistentRead=True)).get(
        "Item"
    ) or {}
    ends = row.get("os_trial_ends_at")
    return None if ends is None or os_billing.status(row, now()) == "active" else int(ends)


async def _due(s: dict, at: int) -> str | None:
    """Why the backend should close this session now, if it should."""
    if s.get("closeRequested"):  # replaced by a newer session, or client_closed by the app
        return s["closeRequested"]
    if s["trialEndsAt"] is not None and at >= s["trialEndsAt"]:
        s["trialEndsAt"] = await _trial_ends_at(s["userId"])  # a referral may have extended it
        if s["trialEndsAt"] is not None and at >= s["trialEndsAt"]:
            return "trial_ended"
    if (s.get("expiresAt") and at >= int(s["expiresAt"])) or at >= int(s["startedAt"]) + MAX_SESSION_S:
        return "expired"
    return None


async def _tick(ws, s: dict) -> bool:
    """Renew the lease, store the latest usage, and close the session when it is due. False: stop."""
    at = now()
    expression, values = ", usageSeconds = :s", {":s": s["usageSeconds"]}
    for field in ("expiresAt", "closeReason"):
        if s.get(field):
            expression, values = f"{expression}, {field} = :{field}", values | {f":{field}": s[field]}
    if s.pop("attachFailures", None):
        expression += " REMOVE attachFailures"
    if (row := await _lease(s["sessionId"], at, expression, values)) is None:
        return False
    s["closeRequested"] = row.get("closeRequested")
    if not s.get("closeReason") and (reason := await _due(s, at)):
        s["closeReason"] = reason
    if s.get("closeReason") and not s.get("closeSentAt"):
        await ws.send(json.dumps({"type": "session.close"}))
        s["closeSentAt"] = at
        logger.info("os voice session %s: close sent (%s)", s["sessionId"], s["closeReason"])
    elif s.get("closeSentAt") and at - s["closeSentAt"] > CLOSE_WAIT_S:
        await _reap_unconfirmed(s["sessionId"], s["usageSeconds"])
        return False
    return True


def _seconds(event: dict) -> int | None:
    usage = event.get("usage")
    seconds = usage.get("seconds") if isinstance(usage, dict) else None
    return seconds if type(seconds) is int else None


async def _event(s: dict, raw) -> bool:
    """Read one server event. True when the session closed. An event of an unexpected shape is skipped
    and counted; it never drops the socket."""
    try:
        event = json.loads(raw)
        kind = event.get("type")
    except (ValueError, TypeError, AttributeError):
        put_metric("os_voice.sideband.bad_event")
        return False
    if kind == "session.usage.updated":
        if (seconds := _seconds(event)) is None:
            put_metric("os_voice.sideband.bad_event")
        else:
            s["usageSeconds"] = max(int(s["usageSeconds"]), seconds)  # cumulative: never summed
    elif kind == "session.started":
        session = event.get("session")
        if isinstance(session, dict) and type(session.get("expires_at")) is int:
            s["expiresAt"] = session["expires_at"]
    elif kind == "session.closed":
        seconds = max(int(s["usageSeconds"]), _seconds(event) or 0)
        reason = s.get("closeReason") or _REASONS.get(event.get("reason"), "upstream")
        await end_session(s["sessionId"], reason, seconds)
        return True
    elif kind == "error":
        error = event.get("error") if isinstance(event.get("error"), dict) else {}
        logger.info("os voice session %s: error event %s", s["sessionId"], error.get("code"))
    return False


def _status_class(e: Exception) -> str:
    if isinstance(e, websockets.exceptions.InvalidStatus):
        return f"{e.response.status_code // 100}xx"
    return "network"


async def run_sideband(sid: str) -> None:
    """Hold this session's sideband until it closes or another task takes it. Cancelling it (shutdown) just
    stops: the lease lapses and another task's sweep re-attaches."""
    if not await _lease(sid, now()):
        return
    s = await get(sid)
    s["trialEndsAt"] = await _trial_ends_at(s["userId"])
    connected = False
    try:
        url = ATTACH_URL.format(s["liveId"])
        async with _connect(url, additional_headers=_auth(), open_timeout=10, logger=_WS_LOG) as ws:
            connected, loop, next_tick = True, asyncio.get_running_loop(), 0.0
            while True:
                if loop.time() >= next_tick:
                    if not await _tick(ws, s):
                        return
                    next_tick = loop.time() + TICK_S
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.0, next_tick - loop.time()))
                except TimeoutError:
                    continue
                if await _event(s, raw):
                    return
    except (OSError, TimeoutError, websockets.exceptions.InvalidStatus) as e:
        if connected:
            logger.info("os voice session %s: sideband dropped", sid)
        elif isinstance(e, websockets.exceptions.InvalidStatus) and e.response.status_code in (404, 410):
            # OpenAI no longer has it: it is over
            await end_session(sid, s.get("closeReason") or await _due(s, now()) or "upstream", int(s["usageSeconds"]))
            return
        else:
            put_metric("os_voice.sideband.attach_failed", dimensions={"status": _status_class(e)})
            logger.warning("os voice session %s: attach failed (%s)", sid, _status_class(e))
            if s.get("closeReason") or await _due(s, now()):  # due, and this attempt to close it failed
                await _reap_unconfirmed(sid, int(s["usageSeconds"]))
            else:
                await _release(sid, attach_failed=True)
            return
    except websockets.ConnectionClosed:
        logger.info("os voice session %s: sideband dropped", sid)
    except Exception as e:  # noqa: BLE001 -- the type only: an event's text is never logged
        logger.warning("os voice session %s: sideband failed: %s", sid, type(e).__name__)
    await _release(sid)


def start_sideband(sid: str) -> None:
    if sid in _TASKS and not _TASKS[sid].done():
        return
    task = asyncio.create_task(run_sideband(sid))
    _TASKS[sid] = task
    task.add_done_callback(lambda t: _TASKS.pop(sid) if _TASKS.get(sid) is t else None)


async def sweep_once() -> None:
    """Attach every active session nobody holds; the attach closes one that is due, and retires it as
    unconfirmed only if that attempt fails. A session whose creating task died before recording OpenAI's id
    cannot be attached, so it is ended as unconfirmed."""
    at = now()
    for row in await container_repo.get_by_status(ACTIVE):
        sid = row.get("sessionId")
        if not sid or (sid in _TASKS and not _TASKS[sid].done()):
            continue
        if not row.get("liveId"):
            if int(row["startedAt"]) < at - PENDING_S:
                await _reap_unconfirmed(sid, 0)
        elif int(row.get("leaseUntil", 0)) < at:
            start_sideband(sid)


async def os_voice_sweeper(stop: asyncio.Event) -> None:
    try:
        while not stop.is_set():
            try:
                await sweep_once()
            except Exception:  # noqa: BLE001
                logger.warning("os voice sweep failed", exc_info=True)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=SWEEP_S)
    finally:
        for task in list(_TASKS.values()):
            task.cancel()
