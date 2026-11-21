"""Spark voice: GPT-Live WebRTC sessions on the company key, and the sideband that meters and ends them.

The Mac sends its SDP offer; `create_live` creates the session with OpenAI and the Mac's audio then flows
directly to OpenAI. Each session is one containers-table row (`__osvoice__#<id>`) on the sparse
`status-index` while active; the person's users row holds the set of their active session ids
(`os_voice_active`, at most MAX_ACTIVE, enforced by the condition of the write that adds one) and their
running total of voice seconds (`os_voice_seconds`).

One backend task holds each session's sideband WebSocket under a short lease on its row. Every task runs
`os_voice_sweeper`, which attaches any active session whose lease lapsed, so a replaced task's sessions are
picked up by another. The attached task closes a session whose person's account ended, that passed its
`expires_at`, or that reached MAX_SESSION_S.

Never log an SDP (host candidates carry the Mac's LAN addresses), the instructions or the input: ids, sizes
and outcomes only."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import uuid

import httpx
import websockets
from botocore.exceptions import ClientError

from core.config import settings
from core.dynamodb import get_table, is_cancelled, run_in_thread, transact
from core.repositories import container_repo

logger = logging.getLogger(__name__)
_WS_LOG = logging.getLogger(__name__ + ".ws")
_WS_LOG.setLevel(logging.WARNING)  # websockets logs frame contents (transcripts) at DEBUG

LIVE_URL = "https://api.openai.com/v1/live/sessions"
ATTACH_URL = "wss://api.openai.com/v1/live/sessions/{}/attach"
KEY_PREFIX = "__osvoice__#"
ACTIVE = "os_voice_active"
MAX_ACTIVE = 2
CREATE_TIMEOUT_S = 20.0  # under API Gateway's 30 s
RETRY_AFTER_S = 5  # OpenAI refused for capacity without a Retry-After
TICK_S = 10.0  # lease renewal, usage write and close checks while attached
LEASE_S = 30
SWEEP_S = 15
PENDING_S = 60  # a reserved session with no OpenAI id after this: its creating task died
CLOSE_WAIT_S = 30  # after our session.close, end it ourselves if session.closed never arrives
ENDED_TTL_S = 7 * 86400
# Unmeasured: OpenAI documents no WebRTC session length (2 hours is its SIP call limit). The docs say billing
# accrues through silence ("Active time includes speech, silence before the idle timeout expires", voice-latency-cost)
# but not how often session.usage.updated arrives, so usage not advancing is no evidence of a dead session; the
# reaper uses `expires_at` and this cap only.
MAX_SESSION_S = 2 * 3600
UNATTACHED_GRACE_S = 600  # past the cap with no task able to attach: end it here so its slot frees
TASK_ID = f"{os.getenv('HOSTNAME') or 'local'}-{uuid.uuid4().hex[:8]}"

# The OpenAI close reasons, as the reasons GET /os/voice-session reports.
_REASONS = {"close_requested": "client_closed", "remote_hangup": "client_closed", "expired": "expired"}
_TASKS: dict[str, asyncio.Task] = {}
_connect = websockets.connect
_transport: httpx.AsyncBaseTransport | None = None


class Upstream(Exception):
    def __init__(self, status: int, retry_after: int | None = None):
        self.status, self.retry_after = status, retry_after


def _now() -> int:
    return int(time.time())


def _table():
    return get_table("containers")


def _key(sid: str) -> dict:
    return {"owner_id": f"{KEY_PREFIX}{sid}"}


def _auth() -> dict:
    return {"Authorization": f"Bearer {settings.OPENAI_API_KEY}"}


async def create_live(body: dict) -> tuple[str, str]:
    """POST /v1/live/sessions. Returns (OpenAI session id, SDP answer)."""
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(CREATE_TIMEOUT_S, connect=5.0), transport=_transport
        ) as http:
            r = await http.post(LIVE_URL, json=body, headers=_auth())
    except httpx.TimeoutException:
        raise Upstream(504) from None
    except httpx.HTTPError:
        raise Upstream(502) from None
    if r.status_code == 429:
        after = r.headers.get("retry-after", "")
        raise Upstream(429, int(after) if after.isdigit() else RETRY_AFTER_S)
    try:
        out = r.json() if r.status_code == 201 else {}
        return out["session"]["id"], out["transport"]["sdp"]
    except (ValueError, KeyError, TypeError):
        logger.warning("os voice create refused: http %s", r.status_code)
        raise Upstream(502) from None


async def reserve(user_id: str, sid: str, kind: str, client: dict, now: int) -> bool:
    """Take one of the person's MAX_ACTIVE slots and write the session row, in one transaction."""
    users = get_table("users")
    row = {**_key(sid), "status": ACTIVE, "sessionId": sid, "userId": user_id, "kind": kind, "client": client}
    row |= {"startedAt": now, "usageSeconds": 0, "session_ttl": now + MAX_SESSION_S + ENDED_TTL_S}
    try:
        await transact(
            users,
            [
                {
                    "Update": {
                        "TableName": users.name,
                        "Key": {"user_id": user_id},
                        "UpdateExpression": "ADD os_voice_active :sid",
                        "ConditionExpression": "attribute_not_exists(os_voice_active) OR size(os_voice_active) < :max",
                        "ExpressionAttributeValues": {":sid": {sid}, ":max": MAX_ACTIVE},
                    }
                },
                {"Put": {"TableName": _table().name, "Item": row}},
            ],
        )
    except ClientError as e:
        if is_cancelled(e):
            return False
        raise
    return True


async def set_live(sid: str, live_id: str) -> None:
    await run_in_thread(
        _table().update_item,
        Key=_key(sid),
        UpdateExpression="SET liveId = :l",
        ExpressionAttributeValues={":l": live_id},
    )


async def get(sid: str) -> dict | None:
    return (await run_in_thread(_table().get_item, Key=_key(sid))).get("Item")


async def end_session(sid: str, reason: str, seconds: int) -> bool:
    """Mark the session ended, free its slot and add its seconds to the person's total, once: the row's
    condition makes a replayed close (or a second task's) a no-op. False when it had already ended."""
    row = await get(sid)
    if not row or row.get("status") != ACTIVE:
        return False
    now, users = _now(), get_table("users")
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
                            ":now": now, ":r": reason, ":s": seconds, ":ttl": now + ENDED_TTL_S, ":active": ACTIVE,
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


async def _lease(sid: str, now: int, sets: str = "", values: dict | None = None) -> bool:
    """Take or renew the session's lease; False when another task holds it or the session ended."""
    try:
        await run_in_thread(
            _table().update_item,
            Key=_key(sid),
            UpdateExpression="SET leaseOwner = :me, leaseUntil = :until" + sets,
            ConditionExpression="#st = :active AND (attribute_not_exists(leaseUntil) OR leaseUntil < :now "
            "OR leaseOwner = :me)",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":me": TASK_ID, ":until": now + LEASE_S, ":now": now, ":active": ACTIVE}
            | (values or {}),
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
    return True


async def _release(sid: str) -> None:
    with contextlib.suppress(ClientError):
        await run_in_thread(
            _table().update_item,
            Key=_key(sid),
            UpdateExpression="REMOVE leaseOwner, leaseUntil",
            ConditionExpression="leaseOwner = :me",
            ExpressionAttributeValues={":me": TASK_ID},
        )


async def _trial_ends_at(user_id: str) -> int:
    row = (await run_in_thread(get_table("users").get_item, Key={"user_id": user_id})).get("Item") or {}
    return int(row.get("os_trial_ends_at", 0))


class _Attached:
    """What the attached task knows about its session between ticks."""

    def __init__(self, row: dict, trial_ends_at: int):
        self.sid, self.user_id = row["sessionId"], row["userId"]
        self.started_at = int(row["startedAt"])
        self.seconds = int(row.get("usageSeconds", 0))
        self.expires_at = int(row["expiresAt"]) if row.get("expiresAt") else None
        self.close_reason = row.get("closeReason")
        self.close_sent_at: int | None = None
        self.trial_ends_at = trial_ends_at


async def _due(s: _Attached, now: int) -> str | None:
    """Why the backend should close this session now, if it should."""
    if now >= s.trial_ends_at:
        s.trial_ends_at = await _trial_ends_at(s.user_id)  # a referral may have extended it
        if now >= s.trial_ends_at:
            return "trial_ended"
    if (s.expires_at and now >= s.expires_at) or now >= s.started_at + MAX_SESSION_S:
        return "expired"
    return None


async def _tick(ws, s: _Attached) -> bool:
    """Renew the lease, store the latest usage, and close the session when it is due. False: stop."""
    now = _now()
    if s.close_reason is None and (reason := await _due(s, now)):
        s.close_reason = reason
    sets, values = ", usageSeconds = :s", {":s": s.seconds}
    if s.expires_at:
        sets, values = sets + ", expiresAt = :e", values | {":e": s.expires_at}
    if s.close_reason:
        sets, values = sets + ", closeReason = :c", values | {":c": s.close_reason}
    if not await _lease(s.sid, now, sets, values):
        return False
    if s.close_reason and s.close_sent_at is None:
        await ws.send(json.dumps({"type": "session.close"}))
        s.close_sent_at = now
        logger.info("os voice session %s: close sent (%s)", s.sid, s.close_reason)
    elif s.close_sent_at is not None and now - s.close_sent_at > CLOSE_WAIT_S:
        logger.warning("os voice session %s: no session.closed after close; final usage unconfirmed", s.sid)
        await end_session(s.sid, s.close_reason, s.seconds)
        return False
    return True


async def _event(s: _Attached, raw) -> bool:
    """Read one server event. True when the session closed."""
    event = json.loads(raw)
    kind = event.get("type")
    if kind == "session.usage.updated":
        s.seconds = max(s.seconds, int(event["usage"]["seconds"]))  # cumulative: never summed
    elif kind == "session.started" and event.get("session", {}).get("expires_at"):
        s.expires_at = int(event["session"]["expires_at"])
    elif kind == "session.closed":
        s.seconds = max(s.seconds, int((event.get("usage") or {}).get("seconds", 0)))
        reason = s.close_reason or _REASONS.get(event.get("reason"), "upstream")
        await end_session(s.sid, reason, s.seconds)
        return True
    elif kind == "error":
        logger.info("os voice session %s: error event %s", s.sid, (event.get("error") or {}).get("code"))
    return False


async def run_sideband(sid: str) -> None:
    """Hold this session's sideband until it closes or another task takes it. Cancelling it (shutdown) just
    stops: the lease lapses and another task's sweep re-attaches."""
    if not await _lease(sid, _now()):
        return
    row = await get(sid)
    s = _Attached(row, await _trial_ends_at(row["userId"]))
    try:
        url = ATTACH_URL.format(row["liveId"])
        async with _connect(url, additional_headers=_auth(), open_timeout=10, logger=_WS_LOG) as ws:
            loop = asyncio.get_running_loop()
            next_tick = 0.0
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
    except websockets.exceptions.InvalidStatus as e:
        if e.response.status_code in (404, 410):  # OpenAI no longer has it; its final usage is unconfirmed
            await end_session(sid, s.close_reason or "upstream", s.seconds)
            return
        logger.warning("os voice session %s: attach refused: http %s", sid, e.response.status_code)
    except (OSError, websockets.ConnectionClosed, TimeoutError):
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
    """Attach every active session nobody holds. End one whose creating task died before OpenAI answered, and
    one long past the cap that no task could attach (its final usage is unconfirmed)."""
    now = _now()
    for row in await container_repo.get_by_status(ACTIVE):
        sid = row.get("sessionId")
        if not sid or (sid in _TASKS and not _TASKS[sid].done()):
            continue
        if not row.get("liveId"):
            if int(row["startedAt"]) < now - PENDING_S:
                await end_session(sid, "upstream", 0)
        elif int(row.get("leaseUntil", 0)) < now:
            if now >= int(row["startedAt"]) + MAX_SESSION_S + UNATTACHED_GRACE_S:
                await end_session(sid, "expired", int(row.get("usageSeconds", 0)))
            else:
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
