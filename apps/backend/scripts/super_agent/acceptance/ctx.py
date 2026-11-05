"""G1: ``Ctx`` and ``Result`` for the wave-2 acceptance scenarios (plans/wave2-README.md G1, spec 09 §5.5).

Dev only, three times over: ``settings.ENVIRONMENT`` must be ``dev``, the API host must be ``api-dev``
(``runner.dev_only``) and the tasks table must be ``lightbulb-dev-*``. The fixture users (Sam, Priya, Ada) sign in
through Clerk sign-in tickets minted with the dev Clerk secret from Secrets Manager (``runner.Clerk``, AWS profile
``isol8-admin``). No token or secret is ever printed: evidence is ids, row keys and timings.

The names every lane's scenario body uses (O13 #4764, Q11, A13, M10, V17, S9):
``ctx.ws`` / ``ctx.users[label]`` / ``ctx.fixtures[name]`` (ids), ``ctx.state`` (carried across scenarios),
``ctx.api(user)`` (httpx.AsyncClient rooted at the API host), ``await ctx.tasks_rows(ws=ctx.ws)``,
``ctx.appsync(ws)`` (async iterator of live events on ``/ws/{wsChannel}/tasks``),
``await ctx.wait_for(pred, timeout_s)`` (polls ``tasks_rows``; the first truthy ``pred(rows)``, ``None`` on timeout),
``await ctx.sleep(s)``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar
from urllib.parse import urlsplit

import httpx

from core import tasks_schema as ts
from core.config import settings
from core.dynamodb import get_table, plain, run_in_thread, table_name
from scripts.load.fixtures import APPSYNC_TEMPLATE
from scripts.multiparty_e2e.runner import API, UA, Clerk, Session, dev_only

T = TypeVar("T")

#: Fixture workspaces (spec 09 §7.2, wave2-README): Clerk org name -> the fixture users seated in it; the first
#: user creates the org and so is its admin (Q11: Sam admin, Priya member).
FIXTURES: dict[str, tuple[str, ...]] = {
    "sa-dev-on": ("sam", "priya"),
    "sa-dev-off": ("sam", "priya"),
    "sa-dev-chatgpt": ("sam", "priya"),
    "sa-dev-acme": ("ada",),
}
API_ROOT = "{0.scheme}://{0.netloc}".format(urlsplit(API))
DEV_TABLE_PREFIX = "lightbulb-dev-"
POLL_S = 0.5


def user_email(fixture: str, label: str) -> str:
    """One Clerk user per (fixture, label), so every fixture workspace tears down on its own:
    ``sa-dev-on-sam+clerk_test@example.com`` (``+clerk_test`` never receives mail, as scripts/load/fixtures.py)."""
    return f"{fixture}-{label}+clerk_test@example.com"


@dataclass(frozen=True)
class Result:
    """One scenario's verdict. Bool-valued ``evidence`` keys are its checks; the rest is what it saw (ids,
    timings); never message text that could carry a secret."""

    passed: bool
    evidence: dict[str, Any] = field(default_factory=dict)


def not_implemented(reason: str = "not implemented") -> Result:
    return Result(False, {"reason": reason})


def guard() -> None:
    """Refuse anything but dev: the env, the API host and the table prefix each on their own."""
    if settings.ENVIRONMENT != "dev":
        raise SystemExit(f"refusing: ENVIRONMENT={settings.ENVIRONMENT!r}; the acceptance harness runs in dev only")
    dev_only(API)
    if not table_name("tasks").startswith(DEV_TABLE_PREFIX):
        raise SystemExit(f"refusing: tasks table {table_name('tasks')!r} is not a {DEV_TABLE_PREFIX}* table")


class _TicketAuth(httpx.Auth):
    """Bearer from the user's ticket session; ``Session.token`` re-mints its 60 s JWT off the event loop."""

    def __init__(self, session: Session):
        self.session = session

    async def async_auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = f"Bearer {await asyncio.to_thread(self.session.token)}"
        yield request


class Ctx:
    def __init__(
        self,
        *,
        ws: str,
        users: dict[str, str],
        fixtures: dict[str, str],
        ws_name: str = "sa-dev-on",
        clerk: Clerk | None = None,
    ):
        self.ws, self.users, self.fixtures, self.ws_name = ws, users, fixtures, ws_name
        self.state: dict[str, Any] = {}
        self._clerk = clerk
        self._sessions: dict[tuple[str, str], Session] = {}
        self._clients: dict[tuple[str, str], httpx.AsyncClient] = {}

    # ------------------------------------------------------------------ construction
    @classmethod
    def connect(cls, *, ws_name: str = "sa-dev-on") -> Ctx:
        """Resolve the fixture ids from dev Clerk (reads only) and bind ``ws`` / ``users`` to ``ws_name``."""
        guard()
        clerk = Clerk()
        fixtures = resolve_fixtures(clerk)
        missing = sorted(set(FIXTURES) - set(fixtures))
        if missing:
            raise SystemExit(f"fixture workspaces missing in dev Clerk: {missing}; run `setup --apply` first")
        return cls(
            ws=fixtures[ws_name], users=resolve_users(clerk, ws_name), fixtures=fixtures, ws_name=ws_name, clerk=clerk
        )

    def for_ws(self, ws_name: str) -> Ctx:
        """Another fixture workspace with its own users and a fresh ``state``, sharing the Clerk client and every
        session so far (the gate runs A1/A2 on ``sa-dev-chatgpt`` for the VM evidence)."""
        if self._clerk is None:
            raise RuntimeError("for_ws needs a Clerk client")
        other = Ctx(
            ws=self.fixtures[ws_name],
            users=resolve_users(self._clerk, ws_name),
            fixtures=self.fixtures,
            ws_name=ws_name,
            clerk=self._clerk,
        )
        other._sessions, other._clients = self._sessions, self._clients
        return other

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()

    # ------------------------------------------------------------------ the API as a fixture user
    def _session(self, user: str, ws: str) -> Session:
        key = (user, ws)
        if key not in self._sessions:
            if self._clerk is None:
                raise RuntimeError("Ctx has no Clerk client (tests pass their own api())")
            self._sessions[key] = self._clerk.session(self.users[user], ws)
        return self._sessions[key]

    def api(self, user: str, ws: str | None = None) -> httpx.AsyncClient:
        """An httpx.AsyncClient rooted at the API host, signed in as the fixture user with ``ws`` active
        (default ``ctx.ws``). Paths are full, e.g. ``/api/v1/messaging/rooms``."""
        key = (user, ws or self.ws)
        if key not in self._clients:
            self._clients[key] = httpx.AsyncClient(
                base_url=API_ROOT, auth=_TicketAuth(self._session(*key)), headers=UA, timeout=30
            )
        return self._clients[key]

    # ------------------------------------------------------------------ dev-only reads
    async def tasks_rows(self, ws: str | None = None) -> list[dict[str, Any]]:
        """Every row under ``WS#{ws}`` in the dev tasks table (strongly consistent, paginated)."""
        table = get_table("tasks")
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": "pk = :pk",
            "ExpressionAttributeValues": {":pk": ts.ws_pk(ws or self.ws)},
            "ConsistentRead": True,
        }
        rows: list[dict[str, Any]] = []
        while True:
            page = await run_in_thread(table.query, **kwargs)
            rows.extend(plain(item) for item in page.get("Items", []))
            if not page.get("LastEvaluatedKey"):
                return rows
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    async def wait_for(
        self, pred: Callable[[list[dict[str, Any]]], T | None], timeout_s: float, *, ws: str | None = None
    ) -> T | None:
        """Poll ``tasks_rows(ws)`` every 0.5 s until ``pred(rows)`` is truthy; that value, or ``None`` at
        ``timeout_s`` (never raises: the scenario turns ``None`` into a failed check)."""
        deadline = time.monotonic() + timeout_s
        while True:
            value = pred(await self.tasks_rows(ws))
            if value:
                return value
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(POLL_S)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    # ------------------------------------------------------------------ A12: each user's DM with @lightbulb
    async def ensure_dm_rooms(self) -> dict[str, str]:
        """``ctx.state["samDmRoomId"]`` / ``["priyaDmRoomId"]`` (one per fixture user): the user's DM with the
        front agent, found as the single ``@lightbulb`` member of ``#general`` (O7 seating) and opened through
        ``POST /messaging/rooms`` ``kind=dm`` (the route returns the existing DM, so this is idempotent)."""
        out: dict[str, str] = {}
        for label, user_id in self.users.items():
            key = f"{label}DmRoomId"
            if key not in self.state:
                api = self.api(label)
                rooms = (await api.get("/api/v1/messaging/rooms")).raise_for_status().json().get("rooms") or []
                general = next((r for r in rooms if (r.get("name") or "").lower() == "general"), None)
                if general is None:
                    raise RuntimeError(f"{self.ws}: no #general for {label}")
                detail = (await api.get(f"/api/v1/messaging/rooms/{general['roomId']}")).raise_for_status().json()
                front = next(
                    (
                        m
                        for m in detail.get("members", [])
                        if m.get("kind") == "agent" and m.get("mentionHandle") == "lightbulb"
                    ),
                    None,
                )
                if front is None:
                    raise RuntimeError(f"{self.ws}: #general has no @lightbulb member (switch off?)")
                body = {
                    "clientRoomId": str(
                        uuid.uuid5(uuid.NAMESPACE_URL, f"sa-acceptance-dm|{self.ws}|{user_id}|{front['id']}")
                    ),
                    "name": None,
                    "kind": "dm",
                    "members": [{"kind": "agent", "id": front["id"], "role": "member", "listenMode": "active"}],
                }
                dm = (await api.post("/api/v1/messaging/rooms", json=body)).raise_for_status().json()
                self.state[key] = dm["roomId"]
            out[label] = self.state[key]
        return out

    # ------------------------------------------------------------------ live events
    async def appsync(self, ws: str | None = None, *, user: str = "sam") -> AsyncIterator[dict[str, Any]]:
        """Live events on the workspace's ``/ws/{wsChannel}/tasks`` channel (O9 ``task.changed``), as published:
        ``{"type": "task.changed", "payload": {"taskId", "part", "version"}, ...}``. The channel and endpoints come
        from ``GET /messaging/live-config`` as ``user`` with ``ws`` active; the AppSync JWT is the user's
        ``lightbulb-appsync`` template token (never logged)."""
        import websockets  # a backend dependency; imported here so the pure helpers never need it

        ws = ws or self.ws
        resp = await self.api(user, ws).get("/api/v1/messaging/live-config")
        resp.raise_for_status()
        cfg = resp.json()
        jwt = await asyncio.to_thread(self._session(user, ws).token, APPSYNC_TEMPLATE)
        channel = f"/ws/{cfg['workspaceId']}/tasks"
        header = json.dumps({"Authorization": jwt, "host": urlsplit(cfg["httpEndpoint"]).netloc})
        protocols = [f"header-{base64.urlsafe_b64encode(header.encode()).decode().rstrip('=')}", "aws-appsync-event-ws"]
        async with websockets.connect(cfg["realtimeEndpoint"], subprotocols=protocols, max_size=256 * 1024) as sock:
            await sock.send(json.dumps({"type": "connection_init"}))
            sub_id = str(uuid.uuid4())
            async for raw in sock:
                msg = json.loads(raw)
                if msg.get("type") == "connection_ack":
                    await sock.send(
                        json.dumps(
                            {
                                "type": "subscribe",
                                "id": sub_id,
                                "channel": channel,
                                "authorization": {"Authorization": jwt},
                            }
                        )
                    )
                elif msg.get("type") == "data" and msg.get("id") == sub_id:
                    for event in parse_appsync_data(msg):
                        yield event
                elif msg.get("type") in ("subscribe_error", "connection_error", "error"):
                    raise RuntimeError(f"appsync {msg.get('type')}: {msg.get('errors') or msg.get('message')}")


def parse_appsync_data(msg: dict[str, Any]) -> list[dict[str, Any]]:
    """The events in one AppSync ``data`` frame (``event`` is a JSON string, or a list of them)."""
    raw = msg.get("event")
    items = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    out = []
    for item in items:
        try:
            event = json.loads(item) if isinstance(item, str) else item
        except ValueError:
            continue
        if isinstance(event, dict):
            out.append(event)
    return out


# ---------------------------------------------------------------------- fixture lookups (dev Clerk, reads only)
def resolve_users(clerk: Clerk, fixture: str) -> dict[str, str]:
    """label -> Clerk user id for the fixture's users; refuses when one is missing (run ``setup --apply``)."""
    found = {label: clerk.user(user_email(fixture, label)) for label in FIXTURES[fixture]}
    missing = sorted(label for label, user in found.items() if not user)
    if missing:
        raise SystemExit(f"{fixture}: fixture users missing in dev Clerk: {missing}; run `setup --apply` first")
    return {label: user["id"] for label, user in found.items()}


def resolve_fixtures(clerk: Clerk) -> dict[str, str]:
    """fixture name -> Clerk org id (= the workspace owner id) for every fixture org that exists."""
    orgs = clerk.http.get("/organizations", params={"query": "sa-dev-", "limit": 100}).raise_for_status().json()
    by_name = {o["name"]: o["id"] for o in orgs.get("data", [])}
    return {name: by_name[name] for name in FIXTURES if name in by_name}


def aws_profile_default() -> None:
    """Dev reads (tasks table, Secrets Manager) go through the ``isol8-admin`` profile unless the caller set one."""
    os.environ.setdefault("AWS_PROFILE", "isol8-admin")
    os.environ.setdefault("AWS_REGION", "us-east-1")
