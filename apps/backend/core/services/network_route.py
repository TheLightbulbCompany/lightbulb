"""A signed-in person's routes onto the free ``lightbulb`` network, for their hosted apps.

A hosted app (Claude app, ChatGPT, a bot) cannot run the engine. The hosted MCP hands each of
its network calls to one of its person's online routes, preferred first:

1. their workspace, when it is on a network an admin added it to (``sessions.json``'s
   ``joined``): the existing ``network.app`` bridge path (``network_member.app_call``);
2. else their most recently connected logged-in computer, over its route link: one outbound
   WebSocket the daemon keeps to ``/api/v1/gateway/network`` (the machine-relay door), signed
   with its member key over a nonce this socket sends. Over it the backend asks only the
   ``app.*`` ops (and ``directory``) the workspace bridge asks, for the app sessions
   ``<computer>/<app>-<person>`` of that computer's own person.

No route online is a clear refusal: nothing is queued here. The text of a message passes
through in transit and is never stored. A link's presence is a TTL row in the ws-connections
table (``ConnectionService.put_route``) naming the task that holds the socket; a call landing on
another task is forwarded to it (``/api/v1/network/internal/route``, the internal secret).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from typing import Any

from core.services.messaging import ApiProblem

logger = logging.getLogger(__name__)

NO_ROUTE = "None of your computers is online; open one (lightbulb login) or add a workspace."
ROUTE_OPS = frozenset({"directory", "app.send", "app.inbox", "app.ack", "app.unread", "app.receipt"})
HELLO_DEADLINE_S = 10.0
REFRESH_S = 60.0
MAX_FRAME = 1 << 20
CLOSE_BAD_PROOF = 4401
CLOSE_NOT_SIGNED_IN = 4403
CLOSE_SUPERSEDED = 4409


@dataclass
class Link:
    """One computer's live socket on THIS task."""

    key: str
    user_id: str
    generation: str
    send: Any  # async (dict) -> None
    pending: dict[int, asyncio.Future] = field(default_factory=dict)
    next_id: int = 1


links: dict[str, Link] = {}


def _connections():
    from core.services.connection_service import ConnectionService

    return ConnectionService()


def _refused(code: str, message: str, reason: str | None = None) -> RuntimeError:
    """The same RuntimeError shape ``container_rpc`` raises for a member's refusal."""
    error = {"code": code, "message": message}
    if reason:
        error["reason"] = reason
    return RuntimeError(json.dumps(error))


# ---- choosing a route -------------------------------------------------------------------------


async def workspace_for(auth) -> str | None:
    """The workspace that routes this person's apps: their selected one (or, for a free
    account's grant, any of theirs) that is on a network. None: route through a computer."""
    from core.services import network_member

    owners = [auth.org_id] if auth.org_id else await network_member.workspaces_of(auth.user_id)
    for owner_id in owners:
        doc = await network_member._read_sessions(owner_id)
        if doc and doc.get("joined"):
            return owner_id
    return None


async def best_link(user_id: str) -> dict | None:
    """This person's most recently connected live computer link, or None."""
    from core.repositories import network_keys_repo

    keys = await network_keys_repo.keys_for_users([user_id])
    rows = [r for r in await _connections().live_routes(keys) if r["userId"] == user_id] if keys else []
    return max(rows, key=lambda r: r["connectedAt"], default=None)


async def app_call(user_id: str, slug: str, client: str, op: str, timeout_s: float, **fields) -> Any:
    """One app op (``send``, ``inbox``, ``ack``, ``unread``, ``receipt``, or ``directory``) as
    ``<client> via <person>`` through this person's computer. ``title`` rides every call, so
    the computer declares the app session as it runs it."""
    from core.services import network_member

    row = await best_link(user_id)
    if row is None:
        raise ApiProblem("NETWORK_NO_ROUTE", NO_ROUTE)
    params = {
        **fields,
        "app": slug,
        "person": network_member.app_person(user_id),
        "title": await network_member.app_title(user_id, client),
    }
    return await call(row, "directory" if op == "directory" else f"app.{op}", params, timeout_s)


async def call(row: dict, op: str, params: dict, timeout_s: float) -> Any:
    """``op`` on the computer of ``row`` (a ``live_routes`` row), here or on the task holding it."""
    from core.services import task_address

    if row["taskAddress"] == task_address.own_address():
        return await local_call(row["key"], row["generation"], op, params, timeout_s)
    return await _forward(row, op, params, timeout_s)


async def _forward(row: dict, op: str, params: dict, timeout_s: float) -> Any:
    import httpx

    from core.config import settings

    body = {"key": row["key"], "generation": row["generation"], "op": op, "params": params, "timeoutS": timeout_s}
    try:
        async with httpx.AsyncClient(timeout=timeout_s + 2, follow_redirects=False) as client:
            response = await client.post(
                f"{row['taskAddress']}/api/v1/network/internal/route",
                json=body,
                headers={"x-lightbulb-ws-internal": settings.WS_INTERNAL_SECRET},
            )
    except httpx.TimeoutException as exc:
        raise asyncio.TimeoutError() from exc
    except httpx.HTTPError as exc:
        raise _refused("UNAVAILABLE", "your computer's link is unavailable") from exc
    answer = response.json() if response.status_code == 200 else {}
    if answer.get("ok") is True:
        return answer.get("result")
    error = answer.get("error") if isinstance(answer.get("error"), dict) else {}
    if error.get("code") == "TIMEOUT":
        raise asyncio.TimeoutError()
    raise _refused(error.get("code") or "UNAVAILABLE", error.get("message") or "refused", error.get("reason"))


async def local_call(key: str, generation: str, op: str, params: dict, timeout_s: float) -> Any:
    """``op`` over the socket this task holds for ``key`` (of ``generation``)."""
    if op not in ROUTE_OPS:
        raise _refused("INVALID_REQUEST", "not a route op")
    link = links.get(key)
    if link is None or link.generation != generation:
        raise _refused("UNAVAILABLE", "your computer's link closed; retry")
    frame_id = link.next_id
    link.next_id += 1
    future = asyncio.get_running_loop().create_future()
    link.pending[frame_id] = future
    try:
        await link.send({**params, "id": frame_id, "op": op})
        answer = await asyncio.wait_for(future, timeout_s)
    finally:
        link.pending.pop(frame_id, None)
    if answer.get("ok") is True:
        return answer.get("result")
    code = str(answer.get("error") or "internal")
    raise _refused(code, f"{op} was refused ({code})", answer.get("reason"))


# ---- the socket -------------------------------------------------------------------------------


async def serve(websocket) -> None:
    """One computer's route link, for its life: challenge, the signed hello, ``ready``, then
    answers to our ops and its mail notices. Closes 4401 on a bad proof, 4403 for a computer
    nobody signed in on (or one since signed out), 4409 when a newer link of it took over."""
    from core.repositories import network_keys_repo
    from core.services import network_login, network_member

    await websocket.accept()
    nonce = secrets.token_urlsafe(32)
    await websocket.send_text(json.dumps({"type": "challenge", "challenge": nonce}))
    try:
        hello = json.loads((await asyncio.wait_for(websocket.receive_text(), HELLO_DEADLINE_S))[:MAX_FRAME])
    except Exception:  # noqa: BLE001 -- a timeout, a disconnect or junk: no link
        await _close(websocket, CLOSE_BAD_PROOF)
        return
    key, sig = (hello.get("key"), hello.get("sig")) if isinstance(hello, dict) else (None, None)
    if not isinstance(key, str) or not isinstance(sig, str) or not network_login.verify(key, "route", nonce, sig):
        await _close(websocket, CLOSE_BAD_PROOF)
        return
    bound = await network_keys_repo.get(key)
    if not bound:
        await _close(websocket, CLOSE_NOT_SIGNED_IN)
        return
    user_id = bound["user_id"]
    generation = str(uuid.uuid4())

    async def send(frame: dict) -> None:
        await websocket.send_text(json.dumps(frame))

    link = Link(key, user_id, generation, send)
    connections = _connections()
    old = links.get(key)
    links[key] = link
    if old is not None:
        _fail(old)
    await connections.put_route(key=key, user_id=user_id, generation=generation)
    logger.info("network route link up user=%s", user_id)  # never the key
    await send({"type": "ready", "person": network_member.app_person(user_id)})
    code = 1000
    try:
        reader = asyncio.create_task(_read(websocket, link))
        keeper = asyncio.create_task(_keep(connections, link))
        done, _ = await asyncio.wait({reader, keeper}, return_when=asyncio.FIRST_COMPLETED)
        if keeper in done and not keeper.cancelled() and keeper.exception() is None:
            code = keeper.result()
        for task in (reader, keeper):
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
    finally:
        if links.get(key) is link:
            links.pop(key, None)
        _fail(link)
        with contextlib.suppress(Exception):
            await connections.delete_route(key, generation)
        await _close(websocket, code)
        logger.info("network route link down user=%s code=%s", user_id, code)


async def _read(websocket, link: Link) -> None:
    while True:
        text = await websocket.receive_text()
        if len(text) > MAX_FRAME:
            continue
        try:
            frame = json.loads(text)
        except ValueError:
            continue
        if not isinstance(frame, dict):
            continue
        if frame.get("type") == "mail" and isinstance(frame.get("session"), str):
            # New mail for one of this computer's app sessions: wake its bot, if it asked.
            from core.services import app_wake

            asyncio.create_task(app_wake.notify(link.user_id, frame["session"]))
            continue
        future = link.pending.get(frame.get("id")) if isinstance(frame.get("id"), int) else None
        if future is not None and not future.done():
            future.set_result(frame)


async def _keep(connections, link: Link) -> int:
    """Every ``REFRESH_S``: the presence row (gone: a newer link took over) and the binding
    (gone or moved: the person signed this computer out). Returns the close code."""
    from core.repositories import network_keys_repo

    while True:
        await asyncio.sleep(REFRESH_S)
        try:
            if not await connections.refresh_route(link.key, link.generation):
                return CLOSE_SUPERSEDED
            bound = await network_keys_repo.get(link.key)
        except Exception:  # noqa: BLE001 -- a DynamoDB hiccup: the next tick tries again
            logger.warning("network route refresh failed", exc_info=True)
            continue
        if not bound or bound.get("user_id") != link.user_id:
            return CLOSE_NOT_SIGNED_IN


def _fail(link: Link) -> None:
    for future in link.pending.values():
        if not future.done():
            future.set_exception(_refused("UNAVAILABLE", "your computer's link closed; retry"))


async def _close(websocket, code: int) -> None:
    with contextlib.suppress(Exception):
        await websocket.close(code=code)
