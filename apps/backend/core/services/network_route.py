"""A signed-in person's own computers as the route onto the free ``lightbulb`` network, for
their hosted apps (Reason's model).

A hosted app (Claude app, ChatGPT, a bot) cannot run the engine. Each app connection (an OAuth
client, or an ``lbk_`` key) is PINNED to one of its person's logged-in computers, so its session
``<computer>/<app>-<person>`` and its inbox live in one place. The computer's daemon keeps one
outbound WebSocket to ``/api/v1/gateway/network`` (the machine-relay door), its hello signed with
its member key over a nonce this socket sends and carrying its sign-in proof. Over it the
backend asks only the ``app.*`` ops (and ``directory``) the workspace bridge asks.

The person picks the computer on ``/auth/network/apps``; until then the first call pins the
most recently connected online one. A pinned computer that is offline is a clear refusal:
never another computer, nothing queued here. The text of a message passes through in transit
and is never stored. A link's presence is a TTL row in the ws-connections table
(``ConnectionService.put_route``) naming the task that holds the socket; a call landing on
another task is forwarded to it (``/api/v1/network/internal/route``).

Pins are rows in the messaging table, ``NETPIN#<user>`` / ``<client id>`` (an OAuth grant is
Clerk's record, so it cannot carry the attribute itself; a key's client id is
``agentkey:<keyId>``).
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

NO_ROUTE = "None of your computers is online; open one (lightbulb login)."
OFFLINE = "{name} is offline. Open it."
ROUTE_OPS = frozenset({"directory", "app.send", "app.inbox", "app.ack", "app.unread", "app.receipt", "rules.set"})
HELLO_DEADLINE_S = 10.0
REFRESH_S = 60.0
MAX_FRAME = 1 << 20
CLOSE_BAD_PROOF = 4401
CLOSE_NOT_SIGNED_IN = 4403
CLOSE_SUPERSEDED = 4409
CLOSE_TOO_MANY = 4429
MAX_LINKS_PER_USER = 10  # a person's computers online at once
MAIL_PER_MINUTE = 30  # mail notices one link may send (each can POST a bot's webhook)


@dataclass
class Link:
    """One computer's live socket on THIS task."""

    key: str
    user_id: str
    generation: str
    send: Any  # async (dict) -> None
    org: str = ""  # the workspace its proof signed into ("": none named)
    exp: int = 0  # its proof's: the link closes when it lapses (the computer refreshes first)
    mail_window: float = 0.0
    mail_count: int = 0
    pending: dict[int, asyncio.Future] = field(default_factory=dict)
    next_id: int = 1
    signed_out: asyncio.Event = field(default_factory=asyncio.Event)  # its person signed it out


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


async def best_link(user_id: str) -> dict | None:
    """This person's most recently connected live computer link, or None."""
    rows = await _connections().routes_for_user(user_id)
    return max(rows, key=lambda r: r["connectedAt"], default=None)


def _pin_key(user_id: str, client_id: str) -> dict:
    return {"pk": f"NETPIN#{user_id}", "sk": client_id}


async def get_pin(user_id: str, client_id: str) -> dict | None:
    from core.repositories import messaging_repo

    return await messaging_repo._get(**_pin_key(user_id, client_id))


async def set_pin(user_id: str, client_id: str, route: dict, app: str, client: str) -> dict:
    from core.dynamodb import run_in_thread, utc_now_iso
    from core.repositories import messaging_repo

    item = {**_pin_key(user_id, client_id), "route": route, "app": app, "client": client, "pinnedAt": utc_now_iso()}
    await run_in_thread(messaging_repo._table().put_item, Item=item)
    return item


async def list_pins(user_id: str) -> list[dict]:
    from core.repositories import messaging_repo

    return await messaging_repo._query(f"NETPIN#{user_id}")


async def route_for(auth, client_id: str, slug: str, client: str) -> dict:
    """This app connection's pinned computer; the first call pins the most recently connected
    online one."""
    pin = await get_pin(auth.user_id, client_id)
    if pin is not None and pin["route"].get("kind") == "computer":
        return pin["route"]
    link = await best_link(auth.user_id)
    if link is None:
        raise ApiProblem("NETWORK_NO_ROUTE", NO_ROUTE)
    route = {"kind": "computer", "key": link["key"], "name": link.get("name") or "Your computer"}
    await set_pin(auth.user_id, client_id, route, slug, client)
    return route


async def computer_link(user_id: str, route: dict) -> dict:
    """A computer pin's live link, or the offline refusal (never another computer)."""
    rows = await _connections().live_routes([route["key"]])
    row = next((r for r in rows if r["userId"] == user_id), None)
    if row is None:
        raise ApiProblem("NETWORK_OFFLINE", OFFLINE.format(name=route.get("name") or "Your computer"))
    return row


async def app_call(user_id: str, slug: str, client: str, op: str, timeout_s: float, *, route: dict, **fields) -> Any:
    """One app op (``send``, ``inbox``, ``ack``, ``unread``, ``receipt``, or ``directory``) as
    ``<client> via <person>`` through the pinned computer. ``title`` rides every call, so the
    computer declares the app session as it runs it."""
    from core.services import network_member

    row = await computer_link(user_id, route)
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
    """``op`` over the socket this task holds for ``key`` (of ``generation``). ``close`` is ours,
    not the computer's: its person signed it out, so the link closes now."""
    if op == "close":
        if (link := links.get(key)) is not None and link.generation == generation:
            link.signed_out.set()
        return {}
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


async def _push_one(row: dict, jws: str) -> bool:
    try:
        await call(row, "rules.set", {"jws": jws}, 5.0)
    except Exception as exc:  # noqa: BLE001 -- an old engine answers unsupported; a closed link is gone
        logger.debug("rules.set not delivered: %s", type(exc).__name__)  # never the jws
        return False
    return True


async def _in_workspace(row: dict, org: str) -> bool:
    """A link belongs to the workspace its proof named; one from before that was recorded belongs to
    its person's only workspace (several: unknown, so none)."""
    if row.get("org"):
        return row["org"] == org
    from core.services import clerk_admin

    orgs = await clerk_admin.list_user_organizations(row["userId"], limit=100)
    return len(orgs) == 1 and orgs[0]["id"] == org


async def push_rules(org: str, jws: str) -> int:
    """``rules.set`` on every connected link of a person in workspace ``org``; the links reached.
    Only links signed into ``org`` (an engine pins the first workspace it is told)."""
    from core.services import clerk_admin

    # ponytail: one Clerk call per save and a routes_for_user read per member; an org-keyed
    # route index when workspaces hold hundreds of people
    members = await clerk_admin.list_organization_members(org)
    if not members:  # no Clerk key (dev), or Clerk answered nothing: the laptops get it on connect
        logger.info("a2a rules push: no members listed; nothing pushed", extra={"org": org})
        return 0
    rows = []
    for member in members:
        rows += await _connections().routes_for_user(member["user_id"])
    rows = [r for r in rows if await _in_workspace(r, org)]
    return sum(await asyncio.gather(*(_push_one(r, jws) for r in rows)))


async def push_current_rules(link: Link, org: str | None) -> None:
    """The stored (else default) rules of this link's workspace, once, to this link only. The
    workspace is the proof's ``org``, else the person's only one; ambiguous: nothing is pushed."""
    from core.services import a2a_tasks, clerk_admin, task_address

    try:
        if org is None:
            orgs = await clerk_admin.list_user_organizations(link.user_id, limit=100)
            if len(orgs) != 1:
                return
            org = orgs[0]["id"]
        row = {"key": link.key, "generation": link.generation, "taskAddress": task_address.own_address()}
        await _push_one(row, await a2a_tasks.current_rules_jws(org))
    except Exception:  # noqa: BLE001 -- the next connect or the next save pushes again
        logger.debug("rules push on connect failed", exc_info=True)


async def sign_out(user_id: str, key: str) -> None:
    """Close this person's computer ``key``'s route link now, on whichever task holds it."""
    for row in await _connections().live_routes([key]):
        if row["userId"] == user_id:
            with contextlib.suppress(Exception):  # gone already: nothing to close
                await call(row, "close", {}, 5.0)


# ---- the socket -------------------------------------------------------------------------------


async def serve(websocket) -> None:
    """One computer's route link, for its life: challenge, the hello (its member key's signature
    over the nonce, and its sign-in proof), ``ready``, then answers to our ops and its mail
    notices. Closes 4401 on a bad signature, 4403 for no proof (or one expired, or for another
    computer), 4409 when a newer link of it took over."""
    from core.services import network_login, network_member, network_proof

    await websocket.accept()
    nonce = secrets.token_urlsafe(32)
    await websocket.send_text(json.dumps({"type": "challenge", "challenge": nonce}))
    try:
        hello = json.loads((await asyncio.wait_for(websocket.receive_text(), HELLO_DEADLINE_S))[:MAX_FRAME])
    except Exception:  # noqa: BLE001 -- a timeout, a disconnect or junk: no link
        await _close(websocket, CLOSE_BAD_PROOF)
        return
    hello = hello if isinstance(hello, dict) else {}
    key, sig = hello.get("key"), hello.get("sig")
    if not isinstance(key, str) or not isinstance(sig, str) or not network_login.verify(key, "route", nonce, sig):
        await _close(websocket, CLOSE_BAD_PROOF)
        return
    claims = network_proof.verify(hello.get("proof") or "", key)
    if claims is None or await network_login.is_revoked(claims["sub"], key):
        await _close(websocket, CLOSE_NOT_SIGNED_IN)
        return
    user_id = claims["sub"]
    await network_login.register_key(user_id, key)
    connections = _connections()
    online = await connections.routes_for_user(user_id)
    if len(online) >= MAX_LINKS_PER_USER and all(r["key"] != key for r in online):
        await _close(websocket, CLOSE_TOO_MANY)
        return
    generation = str(uuid.uuid4())

    async def send(frame: dict) -> None:
        await websocket.send_text(json.dumps(frame))

    link = Link(key, user_id, generation, send, org=claims.get("org") or "", exp=claims["exp"])
    old = links.get(key)
    links[key] = link
    if old is not None:
        _fail(old)
    name = hello.get("computer") if isinstance(hello.get("computer"), str) else ""
    name = "".join(c for c in name if c.isprintable())[:64]
    await connections.put_route(key=key, user_id=user_id, generation=generation, name=name, org=link.org)
    logger.info("network route link up user=%s", user_id)  # never the key
    await send({"type": "ready", "person": network_member.app_person(user_id)})
    code = 1000
    try:
        reader = asyncio.create_task(_read(websocket, link))
        keeper = asyncio.create_task(_keep(connections, link))
        pusher = asyncio.create_task(push_current_rules(link, link.org or None))
        done, _ = await asyncio.wait({reader, keeper}, return_when=asyncio.FIRST_COMPLETED)
        if keeper in done and not keeper.cancelled() and keeper.exception() is None:
            code = keeper.result()
        for task in (reader, keeper, pusher):
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

            if _mail_allowed(link):
                asyncio.create_task(app_wake.notify(link.user_id, frame["session"]))
            continue
        future = link.pending.get(frame.get("id")) if isinstance(frame.get("id"), int) else None
        if future is not None and not future.done():
            future.set_result(frame)


async def _keep(connections, link: Link) -> int:
    """Every ``REFRESH_S``: the presence row (gone: a newer link took over) and the proof's
    ``exp`` (lapsed: the computer reconnects with its renewed one). Returns the close code.
    A logout is the computer's: it drops this link itself."""
    import time

    while True:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(link.signed_out.wait(), REFRESH_S)
        if link.signed_out.is_set():
            return CLOSE_NOT_SIGNED_IN
        if link.exp and time.time() > link.exp:
            return CLOSE_NOT_SIGNED_IN
        try:
            if not await connections.refresh_route(link.key, link.generation):
                return CLOSE_SUPERSEDED
        except Exception:  # noqa: BLE001 -- a DynamoDB hiccup: the next tick tries again
            logger.warning("network route refresh failed", exc_info=True)


def _mail_allowed(link: Link) -> bool:
    """At most ``MAIL_PER_MINUTE`` notices per link per minute; the rest are dropped (the bot
    still reads its mail with check_inbox)."""
    import time

    now = time.monotonic()
    if now - link.mail_window >= 60:
        link.mail_window, link.mail_count = now, 0
    link.mail_count += 1
    return link.mail_count <= MAIL_PER_MINUTE


def _fail(link: Link) -> None:
    for future in link.pending.values():
        if not future.done():
            future.set_exception(_refused("UNAVAILABLE", "your computer's link closed; retry"))


async def _close(websocket, code: int) -> None:
    with contextlib.suppress(Exception):
        await websocket.close(code=code)
