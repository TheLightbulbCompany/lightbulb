"""The ticketed door from a runtime to a paired Mac's agent Chrome (spec §5).

Acquire mints a single-use ticket bound to the task holding the Mac's native socket; the runtime
dials `ws://<that task>:8000/api/v1/internal/mac-cdp/<ticket>` and gets a CDP byte pipe over the
node socket's `browser.cdp.*` channel. Authority is re-checked every `RECHECK_SECONDS`.

Never log the ticket, a frame, or any claim other than the owner.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import time
import uuid
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken

from core.observability.metrics import put_metric
from core.services import connection_service, desktop_execution, mac_live, task_address
from core.services.desktop_execution import DesktopError, valid_auth
from core.services.node_broker import CDP_COMMAND, CdpClosed, CommandNotAllowed, NodeCapacity

logger = logging.getLogger(__name__)

TICKET_SECONDS = 60
RECHECK_SECONDS = 30
RECHECK_FAILURES = 3
AGENT_MAX_SECONDS = 9 * 3600
PATH = "/api/v1/internal/mac-cdp/"
SKEW_SECONDS = 5
PURPOSE = "mac-cdp-v1"
_STR_CLAIMS = (
    "purpose", "use", "family", "ownerId", "memberId", "deviceId", "generation",
    "address", "profileId", "runId", "agentId", "roomId", "nonce",
)  # fmt: skip
_CLAIMS = frozenset(_STR_CLAIMS) | {"iat", "exp"}
_RUN_FIELDS = ("family", "memberId", "runId", "agentId", "roomId")
#: The Mac slept, reconnected or moved task: the bridge re-acquires (4409), never "access ended".
_TASK_CODES = {"desktop_generation_stale", "desktop_task_moved", "desktop_not_ready"}
_clock = time.time
_redeemed: dict[str, int] = {}  # nonce -> exp; this task only (the ticket is bound to it)


class TicketError(Exception):
    def __init__(self, code: str):  # "invalid" | "expired" | "replayed" | "wrong_task"
        super().__init__(code)
        self.code = code


def _cipher() -> Fernet:
    from core.config import settings

    secret = settings.WS_INTERNAL_SECRET
    if not secret:  # an empty secret would make every ticket forgeable
        raise DesktopError("desktop_not_configured", 503)
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("mac-cdp-ticket-v1:" + secret).encode()).digest()))


def mint(*, target: dict, profile_id: str, auth: dict, use: str = "agent") -> str:
    """`target` is the NATIVE device's resolve_desktop_target() dict; `auth` is exactly what
    authorize_run returned. Room `memberId` is the run's human (== the pairer). `use` "session": a shared session's relay
    to the cloud browser (session_browser): the door joins the runtime to the channel its Mac offered."""
    if not valid_auth(auth) or auth["ownerId"] != target["ownerId"]:
        raise ValueError("mac-cdp ticket auth invalid")
    if use == "session" and auth["family"] != "session":
        raise ValueError("mac-cdp relay ticket is for a session run only")
    now = int(_clock())
    claims = {
        "purpose": PURPOSE,
        "use": use,
        "ownerId": target["ownerId"],
        "deviceId": target["deviceId"],
        "generation": target["generation"],
        "address": target["taskAddress"],
        "profileId": profile_id,
        "nonce": uuid.uuid4().hex,
        "iat": now,
        "exp": now + TICKET_SECONDS,
    } | {field: auth.get(field, "") for field in _RUN_FIELDS}
    return _cipher().encrypt(json.dumps(claims, separators=(",", ":")).encode()).decode()


def door_url(target: dict, ticket: str) -> str:
    return "ws://" + urlsplit(target["taskAddress"]).netloc + PATH + ticket


def _well_formed(claims) -> bool:
    if not isinstance(claims, dict) or set(claims) != _CLAIMS:
        return False
    if any(not isinstance(claims[k], str) for k in _STR_CLAIMS):
        return False
    if any(type(claims[k]) is not int for k in ("iat", "exp")) or claims["exp"] != claims["iat"] + TICKET_SECONDS:
        return False
    if claims["purpose"] != PURPOSE or not all(claims[k] for k in ("ownerId", "deviceId", "generation", "profileId")):
        return False
    use, family = claims["use"], claims["family"]
    return (
        (use == "agent" and family in {"room", "call", "session"}) or (use == "session" and family == "session")
    ) and valid_auth(auth_from_claims(claims))


def redeem(ticket: str) -> dict:
    try:
        claims = json.loads(_cipher().decrypt(ticket.encode()))
    except (DesktopError, InvalidToken, ValueError, UnicodeError):
        raise TicketError("invalid") from None
    if not _well_formed(claims):
        raise TicketError("invalid")
    now = int(_clock())
    if claims["iat"] > now + SKEW_SECONDS:
        raise TicketError("invalid")
    if now > claims["exp"] + SKEW_SECONDS:
        raise TicketError("expired")
    if claims["address"] != task_address.own_address():
        raise TicketError("wrong_task")
    for nonce, exp in list(_redeemed.items()):
        if exp + SKEW_SECONDS < now:
            del _redeemed[nonce]
    if claims["nonce"] in _redeemed:
        raise TicketError("replayed")
    _redeemed[claims["nonce"]] = claims["exp"]
    return claims


def auth_from_claims(claims: dict) -> dict:
    """The exact authorize_run dict for the claim's family."""
    return {key: claims[key] for key in desktop_execution.AUTH_FIELDS}


async def check(claims: dict) -> dict:
    """Re-resolve the native target, the run, and (not for a relay) that the Mac's profile still
    holds this workspace's sign-ins. Raises DesktopError."""
    owner = claims["ownerId"]
    if not desktop_execution.submissions_enabled(owner):
        raise DesktopError("desktop_disabled", 403)
    auth = auth_from_claims(claims)
    member = auth["memberId"]
    relay = claims["use"] == "session"
    try:
        # A relay to the cloud browser drives nothing on the Mac: no browser grant, no profile.
        target = await connection_service.resolve_desktop_target(
            owner, member, claims["deviceId"], claims["generation"], None if relay else CDP_COMMAND
        )
    except connection_service.DesktopTargetError as exc:
        raise DesktopError(exc.code, 409 if exc.code == "desktop_generation_stale" else 403) from None
    if target["taskAddress"] != claims["address"]:
        raise DesktopError("desktop_task_moved", 409)
    await desktop_execution._reauthorize(auth)
    if relay:
        return target
    from core.services import mac_browser  # it imports this module

    await mac_browser.check_holder(owner, claims["deviceId"], claims["profileId"])
    return target


def _meter(outcome: str) -> None:
    with contextlib.suppress(Exception):
        put_metric("mac_cdp.door", dimensions={"outcome": outcome})


async def _close(websocket, code: int, reason: str = "") -> None:
    with contextlib.suppress(Exception):  # the client may already be gone
        await websocket.close(code=code, reason=reason)


async def _upstream(websocket, session, channel) -> tuple:
    """Client text -> Mac."""
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return None, "", "ok"
        data = message.get("text")
        if data is None:
            return 1003, "TEXT_ONLY", "error"
        try:
            session.cdp_send(channel, data)
        except CdpClosed as exc:
            return 4502, exc.code, "lost"
        except NodeCapacity:
            return 4503, "CAPACITY", "refused"


async def _downstream(websocket, session, channel, target) -> tuple:
    """Mac -> client. Only the Mac's own close{CLOSED} is a normal close."""
    while (data := await channel.next()) is not None:
        try:
            await websocket.send_text(data)
        except Exception:  # noqa: BLE001 — the client left mid-send
            return None, "", "ok"
    code = channel.close_code or "CLOSED"
    if code != "CLOSED":
        return 4502, code, "lost"
    try:  # fail_pending also ends channels CLOSED: a dropped socket is not a normal close
        if desktop_execution._session(target) is session:
            return 1000, "", "ok"
    except DesktopError:
        pass
    return 4409, "", "lost"


async def _recheck(claims: dict) -> tuple:
    """Authority gone closes at once (4403); a Mac that slept or moved closes 4409 so the bridge
    re-acquires; a transient error (Clerk, DynamoDB) retries next tick and closes only
    after RECHECK_FAILURES in a row."""
    failures = 0
    while True:
        await asyncio.sleep(RECHECK_SECONDS)
        try:
            await check(claims)
            failures = 0
        except DesktopError as exc:
            if exc.code in _TASK_CODES:
                return 4409, "", "lost"
            return 4403, "", "forbidden"
        except Exception as exc:  # noqa: BLE001
            failures += 1
            logger.warning("mac-cdp recheck failed owner=%s error=%s", claims["ownerId"], type(exc).__name__)
            if failures >= RECHECK_FAILURES:  # authority unverifiable: an internal error, fail closed
                return 1011, "", "error"


async def serve(websocket, ticket: str) -> None:
    await websocket.accept()
    try:
        claims = redeem(ticket)
    except TicketError as exc:
        _meter("bad_ticket")
        return await _close(websocket, 4409 if exc.code == "wrong_task" else 4401)
    owner = claims["ownerId"]
    session = channel = None
    try:
        try:
            target = await check(claims)
        except DesktopError as exc:
            _meter("forbidden")
            return await _close(websocket, 4409 if exc.code in _TASK_CODES else 4403)
        try:
            session = desktop_execution._session(target)
        except DesktopError:
            _meter("lost")
            return await _close(websocket, 4409)
        try:
            if claims["use"] == "session":
                from core.services import session_browser  # it imports this module

                channel = session_browser.relay(claims)
                if channel is None:
                    _meter("lost")
                    return await _close(websocket, 4404)
                await session.cdp_accept(channel, {"relay": True})
            else:
                channel = await session.cdp_open(claims["profileId"])
        except CdpClosed as exc:
            _meter("refused")
            return await _close(websocket, 4502, exc.code)
        except NodeCapacity:
            _meter("refused")
            return await _close(websocket, 4503, "CAPACITY")
        except CommandNotAllowed:
            _meter("forbidden")
            return await _close(websocket, 4403)
        tasks = {
            asyncio.create_task(_upstream(websocket, session, channel)),
            asyncio.create_task(_downstream(websocket, session, channel, target)),
            asyncio.create_task(_recheck(claims)),
        }
        # The Mac live view rides beside the pumps, outside the FIRST_COMPLETED set: it returns at
        # once for a non-castable run, and must never be the task that closes the door.
        live = asyncio.create_task(mac_live.run(claims, session, channel))
        try:
            done, _ = await asyncio.wait(tasks, timeout=AGENT_MAX_SECONDS, return_when=asyncio.FIRST_COMPLETED)
        finally:
            # Not awaited: a gather here, hit by the server cancelling this connection, leaks the
            # CancelledError past the ASGI cancel scope. A cancelled pump ends on its next loop turn;
            # the callback retrieves a losing pump's exception so asyncio never logs it.
            for task in (*tasks, live):
                task.cancel()
                task.add_done_callback(lambda t: t.cancelled() or t.exception())
        winner = next((t for t in done if t.exception() is None), next(iter(done), None))
        code, reason, outcome = winner.result() if winner else (1000, "MAX_AGE", "ok")
        _meter(outcome)
        if code is not None:
            await _close(websocket, code, reason)
    except Exception as exc:  # noqa: BLE001
        logger.warning("mac-cdp door failed owner=%s error=%s", owner, type(exc).__name__)
        _meter("error")
        await _close(websocket, 1011)
    finally:
        if channel is not None:
            with contextlib.suppress(Exception):
                session.cdp_close(channel)
