"""Spark voice: GPT-Live WebRTC sessions on the company key, and the sideband that meters and ends them.

The Mac sends its SDP offer; `open_session` creates the session with OpenAI and the Mac's audio then flows
directly to OpenAI. Each session is one containers-table row (`__osvoice__#<id>`) on the sparse
`status-index` while active; the person's users row holds the set of their active session ids
(`os_voice_active`: one per person, newest wins, the older one is marked `closeRequested` in the same
write) and their running total of voice seconds (`os_voice_seconds`).

One task holds each session's sideband under a lease on its row; every task's sweep re-attaches lapsed ones.

With `SPARK_TRANSCRIPTS_ENABLED` the sideband also saves the text transcript, one `spark-transcripts` item per
turn (what the person said, what Spark said; never audio or screen content), kept 90 days by TTL.

Never log an SDP (host candidates carry the Mac's LAN addresses), the instructions, the input, a transcript or an
OpenAI error message (it can quote the request): ids, sizes, statuses and outcomes only."""

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
from core.observability import spark_events, spark_store
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
TRANSCRIPTS = "spark-transcripts"
TRANSCRIPT_TTL_S = 90 * 86400
TURN_PAUSE_S = 3.0  # GPT-Live sends no end-of-turn event: a side silent this long has finished its turn
_ROLES = {"session.input_transcript.delta": "user", "session.output_transcript.delta": "spark"}
TASK_ID = f"{os.getenv('HOSTNAME') or 'local'}-{uuid.uuid4().hex[:8]}"

# The OpenAI close reasons, as the reasons GET /os/voice-session reports.
_REASONS = {"close_requested": "client_closed", "remote_hangup": "client_closed", "expired": "expired"}
_TASKS: dict[str, asyncio.Task] = {}
_BACKGROUND: set[asyncio.Task] = set()  # fire-and-forget work: the event loop keeps only a weak reference
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


def spawn(coro) -> None:
    """Run `coro` in the background, held until it finishes. A failure is logged (the type only) and never raised."""

    async def guarded() -> None:
        try:
            await coro
        except Exception as e:  # noqa: BLE001 -- the type only
            logger.warning("os voice background task failed: %s", type(e).__name__)

    task = asyncio.create_task(guarded())
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


async def reserve(user_id: str, sid: str, kind: str, client: dict, started_at: int, model: str | None = None) -> None:
    """Make `sid` the person's one session, in one transaction: write its row, set their active set to
    exactly {sid}, and mark every previous one `closeRequested: replaced` (its sideband then closes it).
    The write is conditional on the set being what was read, so of two concurrent opens the later one
    replaces the earlier: exactly one stays active."""
    users = get_table("users")
    row = {**_key(sid), "status": ACTIVE, "sessionId": sid, "userId": user_id, "kind": kind, "client": client}
    if model:
        row["model"] = model
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
    await reserve(user_id, sid, kind, client, started_at, model)
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


async def request_close(sid: str, reason: str = "client_closed") -> None:
    """The app will not use this session: mark it `closeRequested: <reason>` (never over another reason)
    and let its sideband close it on the next tick. With no live sideband, attach one here now."""
    try:
        row = (
            await run_in_thread(
                _table().update_item,
                Key=_key(sid),
                UpdateExpression="SET closeRequested = :r",
                ConditionExpression="#st = :active AND attribute_not_exists(closeRequested)",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={":r": reason, ":active": ACTIVE},
                ReturnValues="ALL_NEW",
            )
        )["Attributes"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return  # ended already, or already being closed
        raise
    if row.get("liveId") and int(row.get("leaseUntil", 0)) < now():
        start_sideband(sid)


CLOSE_POLL_S = 0.5
CLOSE_SETTLE_S = 6.0  # three ticks: the sideband sends session.close, OpenAI answers session.closed


async def close_and_wait(sid: str, reason: str) -> None:
    """Close the session upstream through its sideband (`request_close`) and wait for it to end. A session
    with no OpenAI id has nothing upstream to close and ends at once; one still active after the wait is
    ended here (`os_voice.delete_close_timeout`), whatever OpenAI is still doing."""
    await request_close(sid, reason)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + CLOSE_SETTLE_S
    while (row := await get(sid)) and row.get("status") == ACTIVE and row.get("liveId"):
        if loop.time() >= deadline:
            put_metric("os_voice.delete_close_timeout")
            break
        await asyncio.sleep(CLOSE_POLL_S)
    await end_session(sid, reason, 0)  # a no-op when the sideband ended it


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
    client = row.get("client") or {}
    props = {"seconds": seconds, "kind": row.get("kind"), "ended_reason": reason}
    for prop, value in (
        ("brain", client.get("brain")),
        ("platform", client.get("platform")),
        ("full_access", client.get("fullAccess")),
        ("model", row.get("model")),
    ):
        if value is not None:
            props[prop] = value
    version = client.get("appVersion")
    if isinstance(version, str) and spark_events.VERSION.fullmatch(version):
        props["app_version"] = version
    spark_events.server_event(row["userId"], "voice_session_ended", sid, props)
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


async def _release(sid: str) -> None:
    """Free the lease so the next sweep re-attaches."""
    with contextlib.suppress(ClientError):
        await run_in_thread(
            _table().update_item,
            Key=_key(sid),
            UpdateExpression="REMOVE leaseOwner, leaseUntil",
            ConditionExpression="leaseOwner = :me",
            ExpressionAttributeValues={":me": TASK_ID},
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
    if (row := await _lease(s["sessionId"], at, expression, values)) is None:
        return False
    s["closeRequested"] = row.get("closeRequested")
    s["noTranscript"] = row.get("noTranscript")
    # The latest task started in this session, deliberately never cleared: the person's reaction after a task is
    # tagged with it.
    s["taskId"] = row.get("currentTaskId")
    if s["noTranscript"]:
        s.pop("turns", None)  # opted out: what is held is dropped unsaved
    await _flush(s, idle=TURN_PAUSE_S)
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


def _saves(s: dict) -> bool:
    """Whether this session may keep a transcript: not when the app opened it with usage data off, and not
    once the person turned it off mid-session (`stop_transcripts` sets `noTranscript` on the row)."""
    return (s.get("client") or {}).get("analytics") is not False and not s.get("noTranscript")


async def _save_turn(s: dict, role: str, text: str, start_ms: int) -> None:
    """Write one turn, only while the session is active and neither it (`noTranscript`) nor the person
    (`os_analytics_off`) has opted out, checked in the write's own transaction, so a sideband holding a stale
    copy cannot save after a delete. An ended session (closed for an account deletion, say) saves nothing more. Turns sort by when they started (ms into the session), so a reply that began before
    the request's last words arrived still sorts after the request. A failure is counted and logged without
    its text; it never touches the session."""
    at = now()
    sk = f"{int(s['startedAt'])}#{s['sessionId']}#{start_ms:012d}#{role}"
    item = {"user_id": s["userId"], "sk": sk, "role": role}
    item |= {"text": text, "at": at, "sessionId": s["sessionId"], "ttl": at + TRANSCRIPT_TTL_S}
    try:
        table = get_table(TRANSCRIPTS)
        active = {
            "TableName": _table().name,
            "Key": _key(s["sessionId"]),
            "ConditionExpression": "#st = :active AND attribute_not_exists(noTranscript)",
            "ExpressionAttributeNames": {"#st": "status"},
            "ExpressionAttributeValues": {":active": ACTIVE},
        }
        # The person's own opt-out: it also covers a session whose open was in flight when they switched off.
        person = {
            "TableName": get_table("users").name,
            "Key": {"user_id": s["userId"]},
            "ConditionExpression": "attribute_not_exists(os_analytics_off)",
        }
        puts = [{"ConditionCheck": active}, {"ConditionCheck": person}]
        await transact(table, [*puts, {"Put": {"TableName": table.name, "Item": item}}])
    except ClientError as e:
        if not is_cancelled(e):
            put_metric("os_voice.transcript.write_failed")
            logger.warning("os voice session %s: transcript write failed: %s", s["sessionId"], type(e).__name__)
    except Exception as e:  # noqa: BLE001 -- the type only: the text is never logged
        put_metric("os_voice.transcript.write_failed")
        logger.warning("os voice session %s: transcript write failed: %s", s["sessionId"], type(e).__name__)
    else:  # saved: the store's copy is separate, so a bug there is not a failed transcript write
        try:
            _store_turn(s, role, text, start_ms)
        except Exception as e:  # noqa: BLE001 -- the type only
            put_metric("spark_store.put_failed")
            logger.warning("os voice session %s: store hand-off failed: %s", s["sessionId"], type(e).__name__)


def _store_turn(s: dict, role: str, text: str, start_ms: int) -> None:
    """Also write the turn it just saved to the data store, in the background (it never delays or fails the save).
    The save's transaction already proved the person has not opted out."""
    if not (settings.SPARK_STORE_ENABLED and _saves(s)):
        return
    client = s.get("client") or {}
    row = {
        "row_id": str(uuid.uuid5(spark_events.NS, f"{s['sessionId']}:{start_ms}:{role}")),
        "ts": (int(s["startedAt"]) * 1000 + start_ms) * 1000,  # epoch microseconds; `put` writes it as a string
        "user_id": s["userId"],
        "session_id": s["sessionId"],
        "task_id": s.get("taskId"),
        "role": "person" if role == "user" else "spark",
        "source": "voice",
        "text": spark_store.scrub(text)[:8000],
        "platform": client.get("platform"),
        "app_version": client.get("appVersion"),
    }
    checked_at = time.monotonic()  # the save's transaction has just succeeded
    spawn(spark_store.put("turns", s["userId"], f"{s['sessionId']}_{start_ms}_{role}", [row], checked_at))


async def _flush(s: dict, idle: float = 0.0) -> None:
    """Save each held turn whose side has been silent `idle` seconds (0: every held turn, at session end)."""
    if not _saves(s):
        s.pop("turns", None)
        return
    turns = s.get("turns") or {}
    for role in tuple(turns):
        parts, last, start_ms = turns[role]
        if time.monotonic() - last >= idle:
            del turns[role]
            if text := "".join(parts).strip():
                await _save_turn(s, role, text, start_ms)


async def _transcribe(s: dict, role: str, delta: str, start_ms) -> None:
    """Add a delta to its side's turn. A turn ends only after its own side's silence (or the session's end):
    the other side speaking never ends or splits it. Spark often starts replying before the person's last
    words arrive, and the person's whole request matters more than splitting a rapid back-and-forth."""
    if type(start_ms) is not int:  # no timing on the event: when it arrived, in ms into the session
        start_ms = max(0, time.time_ns() // 1_000_000 - int(s["startedAt"]) * 1000)
    turns = s.setdefault("turns", {})
    turn = turns.setdefault(role, [[], 0.0, start_ms])
    turn[0].append(delta)
    turn[1] = time.monotonic()


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
    elif kind in _ROLES:
        if settings.SPARK_TRANSCRIPTS_ENABLED and _saves(s) and isinstance(event.get("delta"), str):
            await _transcribe(s, _ROLES[kind], event["delta"], event.get("start_ms"))
    elif kind == "session.closed":
        await _flush(s)
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
                await end_session(
                    sid, s.get("closeReason") or await _due(s, now()) or "upstream", int(s["usageSeconds"])
                )
                return
            else:
                put_metric("os_voice.sideband.attach_failed", dimensions={"status": _status_class(e)})
                logger.warning("os voice session %s: attach failed (%s)", sid, _status_class(e))
                if s.get("closeReason") or await _due(s, now()):  # due, and this attempt to close it failed
                    await _reap_unconfirmed(sid, int(s["usageSeconds"]))
                else:
                    await _release(sid)
                return
        except websockets.ConnectionClosed:
            logger.info("os voice session %s: sideband dropped", sid)
        except Exception as e:  # noqa: BLE001 -- the type only: an event's text is never logged
            logger.warning("os voice session %s: sideband failed: %s", sid, type(e).__name__)
        await _release(sid)
    finally:
        await _flush(s)  # a dropped or cancelled sideband keeps the turns it heard


async def _active_sessions(user_id: str, sessions) -> set[str]:
    """The person's active session ids: `sessions` (their users row's active set) and every active session of
    theirs on the status index. A replaced session left the set but stays active until its sideband closes it,
    and the index can lag a new one, hence the union."""
    rows = await container_repo.get_by_status(ACTIVE)
    return set(sessions or ()) | {r["sessionId"] for r in rows if r.get("userId") == user_id and r.get("sessionId")}


async def forget(user_id: str, sessions) -> None:
    """Close the person's open sessions, then delete their saved transcripts. A closed session's sideband can
    save no more turns (`_save_turn` writes only while the session is active)."""
    for sid in await _active_sessions(user_id, sessions):
        await close_and_wait(sid, "signed_out")  # OpenAI gets session.close before the cleanup
    await delete_transcripts(user_id)
    await spark_store.forget_account(user_id)  # a failure raises: the caller keeps the row and retries


async def _set_opt_out(user_id: str, off: bool, at: int) -> bool:
    """Mark (or unmark) the person's users row `os_analytics_off`; a person with no row gets none. `at` is when
    the request arrived (ms): the row keeps the newest choice in `os_analytics_at`, and a write older than it
    is ignored, so two backend tasks applying an off and an on out of order still end on the later one.
    Returns whether the person's choice is "off" now or this write applied (the caller deletes only then)."""
    try:
        await run_in_thread(
            get_table("users").update_item,
            Key={"user_id": user_id},
            UpdateExpression="SET os_analytics_at = :at, os_analytics_off = :t"
            if off
            else "SET os_analytics_at = :at REMOVE os_analytics_off",
            ConditionExpression="attribute_exists(user_id)"
            " AND (attribute_not_exists(os_analytics_at) OR os_analytics_at < :at)",
            ExpressionAttributeValues={":at": at} | ({":t": True} if off else {}),
            ReturnValuesOnConditionCheckFailure="ALL_OLD",
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code != "ConditionalCheckFailedException":
            put_metric("os_voice.opt_out_write_failed")
            logger.warning("os voice opt-out write failed: %s", code)
            return False
        item = e.response.get("Item")
        if not item:  # no users row: nothing to order against
            return True
        return off and bool(item.get("os_analytics_off"))  # a newer choice landed: this one is stale
    return True


async def clear_opt_out(user_id: str, at: int) -> None:
    """The person turned usage data back on: their sessions may save turns again."""
    await _set_opt_out(user_id, False, at)


async def stop_transcripts(user_id: str, at: int) -> None:
    """The person turned usage data off: their active sessions save no more turns and the turns already saved
    are deleted. The sessions stay open; each sideband sees `noTranscript` on its next tick."""
    if not await _set_opt_out(user_id, True, at):  # first: a session still opening then saves nothing
        return  # a newer "on" won, or the write failed: delete nothing
    person = (await run_in_thread(get_table("users").get_item, Key={"user_id": user_id}, ConsistentRead=True)).get(
        "Item"
    ) or {}
    for sid in await _active_sessions(user_id, person.get(ACTIVE)):
        with contextlib.suppress(ClientError):  # ended meanwhile: nothing to stop
            await run_in_thread(
                _table().update_item,
                Key=_key(sid),
                UpdateExpression="SET noTranscript = :t",
                ConditionExpression="#st = :active",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={":t": True, ":active": ACTIVE},
            )
    await delete_transcripts(user_id)
    spark_store.forget(user_id)


async def mark_task(user_id: str, task_id: str) -> None:
    """The person's app started a task: tag their active sessions with it, so the turns saved from now on carry
    it (the sideband copies `currentTaskId` into its state each tick)."""
    person = (
        await run_in_thread(get_table("users").get_item, Key={"user_id": user_id}, ProjectionExpression=ACTIVE)
    ).get("Item") or {}
    for sid in person.get(ACTIVE) or ():
        with contextlib.suppress(ClientError):  # ended meanwhile: nothing to tag
            await run_in_thread(
                _table().update_item,
                Key=_key(sid),
                UpdateExpression="SET currentTaskId = :t",
                ConditionExpression="#st = :active",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={":t": task_id, ":active": ACTIVE},
            )


async def delete_transcripts(user_id: str) -> None:
    """Delete every saved transcript turn of this person: their Spark account is being deleted, or they turned
    usage data off."""

    def delete() -> None:
        table, start = get_table(TRANSCRIPTS), {}
        while True:
            page = table.query(
                KeyConditionExpression="user_id = :u",
                ExpressionAttributeValues={":u": user_id},
                ProjectionExpression="user_id, sk",
                ConsistentRead=True,  # includes the turn a closing session saved just before
                **start,
            )
            with table.batch_writer() as batch:
                for item in page.get("Items", []):
                    batch.delete_item(Key=item)
            if "LastEvaluatedKey" not in page:
                return
            start = {"ExclusiveStartKey": page["LastEvaluatedKey"]}

    await run_in_thread(delete)


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
