"""G1: ``Ctx`` and ``Result`` for the wave-2 acceptance scenarios (plans/wave2-README.md G1, spec 09 §5.5).

Dev only, three times over: ``settings.ENVIRONMENT`` must be ``dev``, the API host must be ``api-dev``
(``runner.dev_only``) and the tasks table must be ``lightbulb-dev-*``. Everything runs in ONE existing workspace
(the dev "Super Agent Test" org): ``sam`` is its owner, ``priya`` a second Clerk test user added as a member. They sign in
through Clerk sign-in tickets minted with the dev Clerk secret from Secrets Manager (``runner.Clerk``, AWS profile
``isol8-admin``). No token or secret is ever printed: evidence is ids, row keys and timings.

The names every lane's scenario body uses (O13 #4764, Q11, A13, M10, V17, S9):
``ctx.ws`` / ``ctx.users[label]`` (ids), ``ctx.state`` (carried across scenarios),
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
from scripts.load.fixtures import APPSYNC_TEMPLATE, _add_member, _bapi
from scripts.multiparty_e2e.runner import API, UA, Clerk, Session, dev_only

T = TypeVar("T")

#: The dev test workspace the founder keeps ("Super Agent Test"): its Clerk org id is the workspace owner id.
DEFAULT_WORKSPACE = "org_3LgB9twuJbYCjGILfOL5JjgQV0p"
#: The second person (Q11, A12: a member beside the owner); ``+clerk_test`` never receives mail.
SECOND_USER_EMAIL = "sa-test-2+clerk_test@thelightbulb.company"
API_ROOT = "{0.scheme}://{0.netloc}".format(urlsplit(API))
DEV_TABLE_PREFIX = "lightbulb-dev-"
POLL_S = 0.5


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
    def __init__(self, *, ws: str, users: dict[str, str], clerk: Clerk | None = None):
        self.ws, self.users = ws, users
        self.state: dict[str, Any] = {}
        self._clerk = clerk
        self._sessions: dict[tuple[str, str], Session] = {}
        self._clients: dict[tuple[str, str], httpx.AsyncClient] = {}

    @classmethod
    def connect(cls, *, ws: str = DEFAULT_WORKSPACE) -> Ctx:
        """Bind to the existing workspace ``ws``: ``sam`` is its Clerk org creator (the owner); ``priya`` is
        ``SECOND_USER_EMAIL``, created and added as a MEMBER when missing (idempotent: the only Clerk writes)."""
        guard()
        clerk = Clerk()
        org = clerk.http.get(f"/organizations/{ws}")
        if org.status_code != 200:
            raise SystemExit(f"workspace {ws} not found in dev Clerk (HTTP {org.status_code})")
        second = clerk.user(SECOND_USER_EMAIL) or _bapi(
            clerk,
            "/users",
            {
                "email_address": [SECOND_USER_EMAIL],
                "first_name": "Priya",
                "last_name": "Test",
                "skip_password_requirement": True,
            },
        )
        _add_member(clerk, ws, second["id"])
        return cls(ws=ws, users={"sam": org.json()["created_by"], "priya": second["id"]}, clerk=clerk)

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()

    # ------------------------------------------------------------------ the API as a user
    def _session(self, user: str, ws: str) -> Session:
        key = (user, ws)
        if key not in self._sessions:
            if self._clerk is None:
                raise RuntimeError("Ctx has no Clerk client (tests pass their own api())")
            self._sessions[key] = self._clerk.session(self.users[user], ws)
        return self._sessions[key]

    def api(self, user: str, ws: str | None = None) -> httpx.AsyncClient:
        """An httpx.AsyncClient rooted at the API host, signed in as ``user`` with ``ws`` active
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
        """``ctx.state["samDmRoomId"]`` / ``["priyaDmRoomId"]`` (one per user): the user's DM with the
        front agent, found as the single ``@lightbulb`` member of ``#general`` (O7 seating) and opened through
        ``POST /messaging/rooms`` ``kind=dm`` (the route returns the existing DM, so this is idempotent)."""
        out: dict[str, str] = {}
        for label, user_id in self.users.items():
            key = f"{label}DmRoomId"
            if key not in self.state:
                api = self.api(label)
                front = await self._front_agent(api)
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

    async def _front_agent(self, api: httpx.AsyncClient) -> dict:
        """Lightbulb, the workspace's single front agent: the Lightbulb or CEO agent of a room this run created (A1), else of ``#general`` (dev seeds #general with the CEO handle)."""
        room_id = self.state.get("roomId")
        if room_id is None:
            rooms = (await api.get("/api/v1/messaging/rooms")).raise_for_status().json().get("rooms") or []
            general = next((r for r in rooms if (r.get("name") or "").lower() == "general"), None)
            if general is None:
                raise RuntimeError(f"{self.ws}: no #general")
            room_id = general["roomId"]
        detail = (await api.get(f"/api/v1/messaging/rooms/{room_id}")).raise_for_status().json()
        agents = [m for m in detail.get("members", []) if m.get("kind") == "agent"]
        front = next((m for m in agents if m.get("mentionHandle") in ("lightbulb", "ceo")), None)
        if front is None:
            raise RuntimeError(f"{self.ws}: no @lightbulb/CEO agent in room {room_id} (switch off?)")
        return front

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


def aws_profile_default() -> None:
    """Dev reads (tasks table, Secrets Manager) go through the ``isol8-admin`` profile unless the caller set one;
    the dev tables the scenarios read through backend services (messaging, OAuth tokens) default to ``lightbulb-dev-*``
    (the harness is dev-only; ``guard()`` refuses anything else)."""
    os.environ.setdefault("AWS_PROFILE", "isol8-admin")
    os.environ.setdefault("AWS_REGION", "us-east-1")
    settings.OAUTH_TOKENS_TABLE = settings.OAUTH_TOKENS_TABLE or "lightbulb-dev-oauth-tokens"
    settings.MESSAGING_TABLE = settings.MESSAGING_TABLE or "lightbulb-dev-messaging"


def load_backend_secrets() -> None:
    """The backend env some services need when the harness calls them directly (``token_custody`` decrypts the
    token row): ``ENCRYPTION_KEY`` from the dev Secrets Manager secret the backend task definition points at.
    Never printed; a no-op when the settings already hold it."""
    if not settings.ENCRYPTION_KEY:
        from scripts.multiparty_e2e.runner import _secret

        settings.ENCRYPTION_KEY = _secret("lightbulb/dev/encryption_key")
