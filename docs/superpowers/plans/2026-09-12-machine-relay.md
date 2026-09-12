# Machine Relay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move desktop node hosts and Paperclip operators off the API Gateway WebSocket relay onto a raw WebSocket pipe on the existing `bv-` door, delete the in-process relay maps, and run the backend at `desiredCount: 2` in prod.

**Architecture:** `routers/gateway_relay.py` accepts `wss://bv[-env]/api/v1/gateway/ws?token=…&clientId=…`, authenticates the upgrade in-process (`core/services/machine_auth.py`, a port of the authorizer Lambda's two verifiers), resolves the container, writes a TTL presence row in `ws-connections`, and pipes frames verbatim to `ws://<ip>:18789` — the `browser_view.py` pattern. Two frame hooks (operator wake gate; NOT_PAIRED self-heal) and the node side effects run inside the handler. The local-session relay keeps API Gateway (it needs the Management API). `node_proxy.py`, `operator_proxy.py`, `node_connection.py` and `operator_rpc.py` are deleted.

**Tech Stack:** FastAPI WebSocket routes, `websockets` (client), PyJWT + `cryptography` (EdDSA), DynamoDB `ws-connections` (`by-owner-id` GSI, `ttl`), CDK (ALB listener rule, CfnOutput/env, ECS scaling), Tauri desktop build env, Paperclip admin API.

**Spec:** `docs/superpowers/specs/2026-09-12-machine-relay-design.md` (execute only after the owner confirms its §2 decisions and A and B have shipped).

## Global Constraints

- No backward compatibility, no feature flags, fix-forward. Old desktop builds get a clean 403 from the narrowed authorizer until the updater ships the new build.
- A socket the backend must hold is pinned to the instance that accepted it and carries no cross-request state; cross-instance facts are `ws-connections` rows with `ttl`; every per-connection side effect runs in the handler that owns the socket.
- Door: `wss://bv[-env].flickmeon.ai/api/v1/gateway/ws?token=<jwt>&clientId=<kind>`; `clientId` ∈ {`node-host`, `openclaw-macos`} needs a device token, `paperclip-operator` needs a Paperclip service token; anything else closes 1008. Owner and user come from the token, never from the client.
- Device token: EdDSA, `kind=device`, claims `exp`, `iat`, `deviceId`, `owner` required, `exp - iat ≤ 300`, verified against `containers[owner].paired_devices[deviceId].publicKey` (urlsafe base64, pad `==`); `userId = entry.userId`, `orgId = "" if owner == userId else owner`. Service token: `service_token.verify` (HS256, `kind=paperclip_service`, `sub` present); `userId = sub`, `orgId = ""`.
- Presence row: `connectionId = "relay#<uuid4>"`, `userId`, `orgId`, `ownerId = orgId or userId`, `connectionType` `node`|`operator`, `authKind`, `clientId`, `deviceId` (device only), `connectedAt`, `ttl = now + 120`; refreshed every 60 s with `attribute_exists(connectionId)`; deleted on close; a failed refresh closes the socket 1008.
- The relay parses only text frames under 64 KiB and only for the two hooks; every frame is forwarded verbatim (except a gate-refused request, which is dropped and answered).
- Wake-gated methods on operator connections: `chat.send`, `sessions.send`, `sessions.steer`, `agent`; refusal payload exactly as `websocket_chat._wake_refusal_payload` today.
- Never log or emit message text, handles, or emails; metrics never carry owner/user/room/member/device ids as dimensions.
- `desiredCount: 2` + scalable target 2–4 at CPU 60 % in prod; dev the same.
- Never push, merge, deploy, run the backfill with `--apply`, or touch AWS without the owner's explicit approval.

---

## File structure

- `apps/backend/core/services/machine_auth.py` (new) — `verify_upgrade(token, client_id) -> MachineIdentity | None`.
- `apps/backend/routers/gateway_relay.py` (new) — the WebSocket route and its hooks; mounted at `/api/v1/gateway`.
- `apps/backend/core/services/connection_service.py` — `put_relay_presence`, `refresh_relay_presence`, `count_relay_nodes`; `delete_connection` (existing).
- `apps/backend/core/services/relay_hooks.py` (new, ~80 lines) — `maybe_disarm_expired_trial` (moved), `wake_refusal_payload` + `admit_operator_wake` (moved), `on_node_ready`, `on_node_closed` (the deny-list edges + invalidation, moved from `node_proxy`).
- `apps/backend/routers/websocket_chat.py` — local-session-only.
- Deleted: `core/services/node_proxy.py`, `core/services/operator_proxy.py`, `core/gateway/node_connection.py`, `core/gateway/operator_rpc.py`, their tests.
- `apps/backend/core/config.py` — `GATEWAY_RELAY_URL`; consumers `routers/desktop_pair.py`, `core/services/paperclip_provisioning.py`, `core/services/company_deploy_service.py`, `core/services/paperclip_adapter_config.py`.
- `apps/backend/scripts/backfill_adapter_gateway_urls.py` (new, one-shot).
- `apps/infra/lib/stacks/network-stack.ts` (listener rule), `api-stack.ts` (`gatewayRelayUrl` property + output), `lightbulb-stage.ts` + `service-stack.ts` (env), `apps/infra/lambda/websocket-authorizer/index.py` + `lambda/ws-connect/index.py` (narrowed), `service-stack.ts` (desired count + scaling), `.github/workflows/desktop-build.yml`, `apps/desktop/src-tauri/src/lib.rs` defaults, `CLAUDE.md`.

---

### Task 1: `machine_auth`

**Files:**
- Create: `apps/backend/core/services/machine_auth.py`
- Test: `apps/backend/tests/unit/services/test_machine_auth.py`

**Interfaces:**
- Consumes: `container_repo.get_by_owner_id(owner_id, consistent=True)` (row field `paired_devices: {deviceId: {publicKey, userId, ...}}`), `service_token.verify(token)`.
- Produces: `MachineIdentity(user_id, org_id, owner_id, kind: Literal["node","operator"], auth_kind, client_id, device_id: str | None)`; `async verify_upgrade(token: str, client_id: str) -> MachineIdentity | None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/services/test_machine_auth.py
import base64
import time
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.services import machine_auth

KEY = Ed25519PrivateKey.generate()
PUB = base64.urlsafe_b64encode(KEY.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode().rstrip("=")
DEVICE = "a" * 64
ROW = {"owner_id": "org_1", "paired_devices": {DEVICE: {"publicKey": PUB, "userId": "user_1"}}}


def _device_token(**overrides):
    now = int(time.time())
    claims = {"kind": "device", "deviceId": DEVICE, "owner": "org_1", "iat": now, "exp": now + 90, **overrides}
    return jwt.encode(claims, KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()), algorithm="EdDSA")


@pytest.mark.asyncio
async def test_device_token_yields_a_node_identity():
    with patch("core.services.machine_auth.container_repo.get_by_owner_id", new_callable=AsyncMock, return_value=ROW):
        ident = await machine_auth.verify_upgrade(_device_token(), "openclaw-macos")
    assert ident is not None
    assert (ident.kind, ident.owner_id, ident.user_id, ident.org_id, ident.device_id, ident.client_id) == ("node", "org_1", "user_1", "org_1", DEVICE, "openclaw-macos")


@pytest.mark.asyncio
@pytest.mark.parametrize("token,client_id", [
    (lambda: _device_token(exp=int(time.time()) + 900), "node-host"),   # lifetime ceiling
    (lambda: _device_token(deviceId="b" * 64), "node-host"),            # unknown device
    (lambda: _device_token(), "paperclip-operator"),                    # wrong kind for the clientId
    (lambda: _device_token(), "browser"),                               # unknown clientId
])
async def test_device_token_rejections(token, client_id):
    with patch("core.services.machine_auth.container_repo.get_by_owner_id", new_callable=AsyncMock, return_value=ROW):
        assert await machine_auth.verify_upgrade(token(), client_id) is None


@pytest.mark.asyncio
async def test_service_token_yields_an_operator_identity():
    with patch("core.services.machine_auth.service_token.verify", return_value={"kind": "paperclip_service", "sub": "org_1"}):
        ident = await machine_auth.verify_upgrade("svc", "paperclip-operator")
        assert ident is not None and (ident.kind, ident.owner_id, ident.user_id, ident.org_id, ident.device_id) == ("operator", "org_1", "org_1", "", None)
        assert await machine_auth.verify_upgrade("svc", "node-host") is None
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_machine_auth.py -q --no-cov`
Expected: FAIL (no module).

- [ ] **Step 3: Implement**

```python
# core/services/machine_auth.py
"""Upgrade-time authentication for the gateway relay: the authorizer Lambda's two verifiers, in-process."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Literal

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from core.repositories import container_repo
from core.services import service_token

DEVICE_TOKEN_MAX_LIFETIME_S = 300
NODE_CLIENT_IDS = frozenset({"node-host", "openclaw-macos"})
OPERATOR_CLIENT_ID = "paperclip-operator"


@dataclass(frozen=True)
class MachineIdentity:
    user_id: str
    org_id: str
    owner_id: str
    kind: Literal["node", "operator"]
    auth_kind: str
    client_id: str
    device_id: str | None


async def _device(token: str, client_id: str) -> MachineIdentity | None:
    try:
        unverified = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return None
    device_id, owner_id = unverified.get("deviceId"), unverified.get("owner")
    if not isinstance(device_id, str) or not isinstance(owner_id, str) or not device_id or not owner_id:
        return None
    row = await container_repo.get_by_owner_id(owner_id, consistent=True)
    entry = ((row or {}).get("paired_devices") or {}).get(device_id)
    if not entry or not entry.get("publicKey"):
        return None  # not paired / revoked
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.urlsafe_b64decode(entry["publicKey"] + "=="))
        claims = jwt.decode(token, key=pub, algorithms=["EdDSA"], options={"require": ["exp", "iat", "deviceId", "owner"]})
    except (jwt.PyJWTError, ValueError):
        return None
    if claims.get("kind") != "device" or claims["exp"] - claims["iat"] > DEVICE_TOKEN_MAX_LIFETIME_S:
        return None
    user_id = entry.get("userId") or owner_id
    return MachineIdentity(user_id=user_id, org_id="" if owner_id == user_id else owner_id, owner_id=owner_id, kind="node", auth_kind="device", client_id=client_id, device_id=device_id)


def _operator(token: str, client_id: str) -> MachineIdentity | None:
    try:
        claims = service_token.verify(token)
    except jwt.PyJWTError:
        return None
    return MachineIdentity(user_id=claims["sub"], org_id="", owner_id=claims["sub"], kind="operator", auth_kind="paperclip_service", client_id=client_id, device_id=None)


async def verify_upgrade(token: str, client_id: str) -> MachineIdentity | None:
    """None on any failure; the caller closes 1008. Never raises."""
    if not token:
        return None
    if client_id in NODE_CLIENT_IDS:
        return await _device(token, client_id)
    if client_id == OPERATOR_CLIENT_ID:
        return _operator(token, client_id)
    return None
```

Confirm the `paired_devices[deviceId]` entry really carries `userId` (`core/services/desktop_pair.py` writes it; the authorizer reads `entry.get("userId")`); the `org_id` rule mirrors the Lambda's.

- [ ] **Step 4: Run the tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_machine_auth.py -q --no-cov && uv run ruff check core/services/machine_auth.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/machine_auth.py apps/backend/tests/unit/services/test_machine_auth.py
git commit -m "feat(relay): in-process machine authentication for the gateway relay"
```

---

### Task 2: presence rows and the relay hooks

**Files:**
- Modify: `apps/backend/core/services/connection_service.py` (after `delete_connection`)
- Create: `apps/backend/core/services/relay_hooks.py` (moves from `operator_proxy.py:115-150`, `routers/websocket_chat.py:36,214-245`, `node_proxy.py:59-75,334-342,380-420` deny-list edges)
- Test: `apps/backend/tests/unit/services/test_relay_presence.py`, `apps/backend/tests/unit/services/test_relay_hooks.py`

**Interfaces:**
- Produces: `ConnectionService.put_relay_presence(*, connection_id, identity: MachineIdentity) -> None`, `refresh_relay_presence(connection_id) -> bool` (False when the row is gone), `count_relay_nodes(owner_id) -> int`; `relay_hooks.WAKE_GATED_METHODS`, `wake_refusal_payload(gate, *, req_id)`, `async admit_operator_wake(*, user_id, owner_id, body) -> dict | None`, `async maybe_disarm_expired_trial(owner_id)`, `async on_node_ready(owner_id, user_id, device_id)`, `async on_node_closed(owner_id, user_id)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/services/test_relay_presence.py
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from core.services.connection_service import ConnectionService
from core.services.machine_auth import MachineIdentity

IDENT = MachineIdentity(user_id="user_1", org_id="org_1", owner_id="org_1", kind="node", auth_kind="device", client_id="openclaw-macos", device_id="a" * 64)


def _svc():
    svc = ConnectionService(table_name="t")
    svc._client = MagicMock()
    return svc


@pytest.mark.asyncio
async def test_put_writes_the_presence_row_with_a_ttl():
    svc = _svc()
    with patch("core.services.connection_service.time.time", return_value=1_000):
        await svc.put_relay_presence(connection_id="relay#1", identity=IDENT)
    item = svc._client.put_item.call_args.kwargs["Item"]
    assert item["connectionId"] == {"S": "relay#1"} and item["ownerId"] == {"S": "org_1"} and item["connectionType"] == {"S": "node"}
    assert item["deviceId"] == {"S": "a" * 64} and item["ttl"] == {"N": "1120"}


@pytest.mark.asyncio
async def test_refresh_reports_a_deleted_row():
    svc = _svc()
    assert await svc.refresh_relay_presence("relay#1") is True
    assert svc._client.update_item.call_args.kwargs["ConditionExpression"] == "attribute_exists(connectionId)"
    svc._client.update_item.side_effect = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
    assert await svc.refresh_relay_presence("relay#1") is False


@pytest.mark.asyncio
async def test_count_relay_nodes_counts_node_rows_only():
    svc = _svc()
    with patch.object(svc, "query_by_owner", return_value=[("c1", "u", "node"), ("c2", "u", "operator"), ("c3", "u", "node")]):
        assert await svc.count_relay_nodes("org_1") == 2
```

```python
# tests/unit/services/test_relay_hooks.py
from unittest.mock import AsyncMock, patch

import pytest

from core.services import relay_hooks


@pytest.mark.asyncio
async def test_operator_wake_gate_refuses_only_gated_methods():
    with patch("core.services.relay_hooks.chat_gate.gate_chat", new_callable=AsyncMock, return_value={"blocked": True, "code": "TRIAL_EXPIRED", "message": "Trial over"}):
        refusal = await relay_hooks.admit_operator_wake(user_id="u", owner_id="o", body={"type": "req", "id": "7", "method": "agent", "params": {"sessionKey": "agent:main:main"}})
        assert refusal == {"type": "res", "id": "7", "ok": False, "error": {"code": "TRIAL_EXPIRED", "message": "Trial over", "details": {"reason": "wake_admission_gate", "code": "TRIAL_EXPIRED"}}}
        assert await relay_hooks.admit_operator_wake(user_id="u", owner_id="o", body={"type": "req", "id": "8", "method": "sessions.list"}) is None


@pytest.mark.asyncio
async def test_node_ready_and_closed_toggle_the_deny_list_at_the_edges():
    with (
        patch("core.services.relay_hooks.approve_pending_node_surface", new_callable=AsyncMock),
        patch("core.services.relay_hooks.ConnectionService") as svc_cls,
        patch("core.services.relay_hooks.patch_openclaw_config", new_callable=AsyncMock) as patch_cfg,
        patch("core.services.relay_hooks.publish_node_invalidation", new_callable=AsyncMock),
    ):
        svc_cls.return_value.count_relay_nodes = AsyncMock(side_effect=[1, 2, 1, 0])
        await relay_hooks.on_node_ready("o", "u", "d")   # first node → enable node tools
        await relay_hooks.on_node_ready("o", "u", "d")   # second node → no patch
        await relay_hooks.on_node_closed("o", "u")       # one left → no patch
        await relay_hooks.on_node_closed("o", "u")       # none left → deny nodes again
    assert [c.args[1]["tools"]["deny"][-1] for c in patch_cfg.await_args_list] == ["dashboard", "nodes"]
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_relay_presence.py tests/unit/services/test_relay_hooks.py -q --no-cov`
Expected: FAIL.

- [ ] **Step 3: Implement**

`connection_service.py` (mirror `mark_gateway_connected`'s typed-attribute `put_item`; `time` is already imported there or add it):

```python
    RELAY_TTL_SECONDS = 120

    async def put_relay_presence(self, *, connection_id: str, identity) -> None:
        now = int(time.time())
        item = {
            "connectionId": {"S": connection_id},
            "userId": {"S": identity.user_id},
            "orgId": {"S": identity.org_id},
            "ownerId": {"S": identity.owner_id},
            "connectionType": {"S": identity.kind},
            "authKind": {"S": identity.auth_kind},
            "clientId": {"S": identity.client_id},
            "connectedAt": {"S": utc_now_iso()},
            "ttl": {"N": str(now + self.RELAY_TTL_SECONDS)},
        }
        if identity.device_id:
            item["deviceId"] = {"S": identity.device_id}
        await asyncio.to_thread(self._client.put_item, TableName=self.table_name, Item=item)

    async def refresh_relay_presence(self, connection_id: str) -> bool:
        """False when the row is gone (revoked or expired): the relay must close."""
        try:
            await asyncio.to_thread(
                self._client.update_item,
                TableName=self.table_name,
                Key={"connectionId": {"S": connection_id}},
                UpdateExpression="SET #ttl = :ttl",
                ConditionExpression="attribute_exists(connectionId)",
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={":ttl": {"N": str(int(time.time()) + self.RELAY_TTL_SECONDS)}},
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    async def count_relay_nodes(self, owner_id: str) -> int:
        return sum(1 for _c, _u, kind in await self.query_by_owner(owner_id) if kind == "node")
```

`relay_hooks.py`:

```python
"""Per-connection side effects of the gateway relay; every function is safe to call from the handler that owns the socket."""

from __future__ import annotations

import logging
from typing import Any

from core.containers.config import TOOL_DENY_BASE, TOOL_DENY_WITH_NODES
from core.observability.metrics import put_metric
from core.services import chat_gate
from core.services.config_patcher import patch_openclaw_config
from core.services.connection_service import ConnectionService
from core.services.desktop_pair_live import approve_pending_node_surface
from core.services.live_events import get_live_event_publisher
from core.services.messaging import make_live_event

logger = logging.getLogger(__name__)
WAKE_GATED_METHODS = frozenset({"chat.send", "sessions.send", "sessions.steer", "agent"})


def wake_refusal_payload(gate: dict, *, req_id: Any) -> dict:
    ...verbatim from routers/websocket_chat.py...


async def admit_operator_wake(*, user_id: str, owner_id: str, body: dict[str, Any]) -> dict | None:
    ...verbatim from routers/websocket_chat.py, with `chat_gate.gate_chat(...)`...


async def maybe_disarm_expired_trial(owner_id: str) -> None:
    ...verbatim from operator_proxy._maybe_disarm_expired_trial...


async def publish_node_invalidation(owner_id: str, user_id: str) -> None:
    ...verbatim from node_proxy._publish_node_invalidation...


async def on_node_ready(owner_id: str, user_id: str, device_id: str) -> None:
    await approve_pending_node_surface(owner_id=owner_id, device_id=device_id)
    if await ConnectionService().count_relay_nodes(owner_id) == 1:
        # First live node: drop "nodes" from the deny list (wholesale replace, so carry the base).
        await patch_openclaw_config(owner_id, {"tools": {"deny": list(TOOL_DENY_BASE)}})
    await publish_node_invalidation(owner_id, user_id)


async def on_node_closed(owner_id: str, user_id: str) -> None:
    if await ConnectionService().count_relay_nodes(owner_id) == 0:
        await patch_openclaw_config(owner_id, {"tools": {"deny": list(TOOL_DENY_WITH_NODES)}})
    await publish_node_invalidation(owner_id, user_id)
```

Read the three source blocks and move them; do not re-type them from memory. `on_node_closed` is called after the row is deleted, so the count excludes the closing connection.

- [ ] **Step 4: Run the tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_relay_presence.py tests/unit/services/test_relay_hooks.py tests/unit/services/test_connection_service*.py -q --no-cov && uv run ruff check core/services/connection_service.py core/services/relay_hooks.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/connection_service.py apps/backend/core/services/relay_hooks.py apps/backend/tests/unit/services/test_relay_presence.py apps/backend/tests/unit/services/test_relay_hooks.py
git commit -m "feat(relay): presence rows with TTL and the per-connection hooks"
```

---

### Task 3: the relay route

**Files:**
- Create: `apps/backend/routers/gateway_relay.py`
- Modify: `apps/backend/main.py` (mount next to `browser_view`: `app.include_router(gateway_relay.router, prefix="/api/v1/gateway")` — copy the browser-view mount's exact form)
- Test: `apps/backend/tests/unit/routers/test_gateway_relay.py`

**Interfaces:**
- Consumes: `machine_auth.verify_upgrade`, `get_ecs_manager().resolve_running_container`, `container_wake.request_wake_silent(owner_id, door="relay")`, `ConnectionService.{put_relay_presence, refresh_relay_presence, delete_connection}`, `relay_hooks.*`, `desktop_pair_live.heal_desktop_device_not_paired(owner_id=..., device_id=..., request_id=...)`, `paperclip_openclaw_pairing.approve_pending_device_from_rejection(owner_id=..., device_id=..., request_id=...)`.
- Produces: `GET /api/v1/gateway/ws` WebSocket; close codes 1008 (auth/revoked), 1013 (container not running), 1011 (relay error).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/routers/test_gateway_relay.py
"""Relay contract with a fake upstream: auth, wake, presence, verbatim piping, the two hooks."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from core.services.machine_auth import MachineIdentity
from routers import gateway_relay

NODE = MachineIdentity(user_id="user_1", org_id="org_1", owner_id="org_1", kind="node", auth_kind="device", client_id="openclaw-macos", device_id="a" * 64)
OPERATOR = MachineIdentity(user_id="org_1", org_id="", owner_id="org_1", kind="operator", auth_kind="paperclip_service", client_id="paperclip-operator", device_id=None)


class FakeUpstream:
    """Minimal async-iterable websocket: `send` records, `feed` pushes frames to the relay."""

    def __init__(self):
        self.sent, self._queue = [], asyncio.Queue()

    async def send(self, frame):
        self.sent.append(frame)

    def feed(self, frame):
        self._queue.put_nowait(frame)

    def __aiter__(self):
        return self

    async def __anext__(self):
        frame = await self._queue.get()
        if frame is None:
            raise StopAsyncIteration
        return frame

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _app():
    app = FastAPI()
    app.include_router(gateway_relay.router, prefix="/api/v1/gateway")
    return app


def _patches(identity, upstream, container=({"gateway_token": "gw"}, "10.0.0.9")):
    manager = MagicMock()
    manager.resolve_running_container = AsyncMock(return_value=container)
    svc = MagicMock()
    svc.put_relay_presence = AsyncMock()
    svc.refresh_relay_presence = AsyncMock(return_value=True)
    svc.delete_connection = MagicMock()
    return (
        patch("routers.gateway_relay.machine_auth.verify_upgrade", new_callable=AsyncMock, return_value=identity),
        patch("routers.gateway_relay.get_ecs_manager", return_value=manager),
        patch("routers.gateway_relay.ConnectionService", return_value=svc),
        patch("routers.gateway_relay.ws_connect", return_value=upstream),
        patch("routers.gateway_relay.relay_hooks.on_node_ready", new_callable=AsyncMock),
        patch("routers.gateway_relay.relay_hooks.on_node_closed", new_callable=AsyncMock),
        patch("routers.gateway_relay.request_wake_silent", new_callable=AsyncMock, return_value="started"),
    ), svc


def test_bad_token_closes_1008():
    with patch("routers.gateway_relay.machine_auth.verify_upgrade", new_callable=AsyncMock, return_value=None):
        with pytest.raises(WebSocketDisconnect) as exc:
            with TestClient(_app()).websocket_connect("/api/v1/gateway/ws?token=x&clientId=node-host"):
                pass
    assert exc.value.code == 1008


def test_stopped_container_wakes_and_closes_1013():
    patches, _ = _patches(OPERATOR, FakeUpstream(), container=(None, None))
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6] as wake:
        with pytest.raises(WebSocketDisconnect) as exc:
            with TestClient(_app()).websocket_connect("/api/v1/gateway/ws?token=x&clientId=paperclip-operator"):
                pass
    assert exc.value.code == 1013
    wake.assert_awaited_once_with("org_1", door="relay")


def test_frames_are_piped_verbatim_and_presence_is_written_then_deleted():
    upstream = FakeUpstream()
    patches, svc = _patches(NODE, upstream)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
        with TestClient(_app()).websocket_connect("/api/v1/gateway/ws?token=x&clientId=openclaw-macos") as ws:
            ws.send_text('{"type":"req","id":"1","method":"connect","params":{"role":"node"}}')
            upstream.feed('{"type":"res","id":"1","ok":true,"payload":{"type":"hello-ok"}}')
            assert json.loads(ws.receive_text())["payload"]["type"] == "hello-ok"
            upstream.feed(b"\x00\x01")
            assert ws.receive_bytes() == b"\x00\x01"
            upstream.feed(None)
    assert upstream.sent[0] == '{"type":"req","id":"1","method":"connect","params":{"role":"node"}}'
    row = svc.put_relay_presence.await_args.kwargs
    assert row["identity"] == NODE and row["connection_id"].startswith("relay#")
    svc.delete_connection.assert_called_once_with(row["connection_id"])


def test_operator_wake_gate_answers_the_client_and_drops_the_frame():
    upstream = FakeUpstream()
    patches, _ = _patches(OPERATOR, upstream)
    refusal = {"type": "res", "id": "9", "ok": False, "error": {"code": "TRIAL_EXPIRED", "message": "x", "details": {"reason": "wake_admission_gate", "code": "TRIAL_EXPIRED"}}}
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patch("routers.gateway_relay.relay_hooks.admit_operator_wake", new_callable=AsyncMock, return_value=refusal):
        with TestClient(_app()).websocket_connect("/api/v1/gateway/ws?token=x&clientId=paperclip-operator") as ws:
            ws.send_text('{"type":"req","id":"9","method":"agent","params":{}}')
            assert json.loads(ws.receive_text()) == refusal
            upstream.feed(None)
    assert upstream.sent == []


def test_not_paired_from_the_container_triggers_the_self_heal():
    upstream = FakeUpstream()
    patches, _ = _patches(NODE, upstream)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patch("routers.gateway_relay.heal_desktop_device_not_paired", new_callable=AsyncMock, return_value=True) as heal:
        with TestClient(_app()).websocket_connect("/api/v1/gateway/ws?token=x&clientId=openclaw-macos") as ws:
            upstream.feed('{"type":"res","id":"1","ok":false,"error":{"code":"NOT_PAIRED","details":{"requestId":"req-1","deviceId":"' + "a" * 64 + '"}}}')
            assert json.loads(ws.receive_text())["error"]["code"] == "NOT_PAIRED"  # still forwarded
            upstream.feed(None)
    heal.assert_awaited_once_with(owner_id="org_1", device_id="a" * 64, request_id="req-1")
```

If `TestClient`'s thread model makes the `FakeUpstream` queue awkward (the relay runs on the test client's event loop), switch the relay tests to `httpx-ws`/`starlette.testclient` patterns already used by `test_browser_view.py` — read it first and keep the same harness.

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_gateway_relay.py -q --no-cov`
Expected: FAIL.

- [ ] **Step 3: Implement**

```python
# routers/gateway_relay.py
"""Raw WebSocket relay for machine clients (desktop node hosts, Paperclip operators) → the owner's OpenClaw gateway.

One backend task owns both sockets for the connection's life; nothing about the connection
lives anywhere else except a TTL presence row. The OpenClaw handshake is the client's and
the container's business — frames are forwarded verbatim except the two hooks below.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect
from websockets import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from core.containers import get_ecs_manager
from core.observability.metrics import put_metric
from core.services import machine_auth, relay_hooks
from core.services.connection_service import ConnectionService
from core.services.container_wake import request_wake_silent
from core.services.desktop_pair_live import heal_desktop_device_not_paired
from core.services.paperclip_openclaw_pairing import approve_pending_device_from_rejection

logger = logging.getLogger(__name__)
router = APIRouter()

GATEWAY_PORT = 18789
_WS_MAX_FRAME_SIZE = 10 * 1024 * 1024
_PARSE_LIMIT = 64 * 1024
_REFRESH_SECONDS = 60


def _parse(frame: str) -> dict | None:
    if len(frame) > _PARSE_LIMIT:
        return None
    try:
        body = json.loads(frame)
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


@router.websocket("/ws")
async def gateway_relay_ws(websocket: WebSocket) -> None:
    token = websocket.query_params.get("token", "")
    client_id = websocket.query_params.get("clientId", "")
    identity = await machine_auth.verify_upgrade(token, client_id)
    if identity is None:
        put_metric("relay.connect", dimensions={"kind": "unknown", "outcome": "unauthorized"})
        await websocket.close(code=1008, reason="Unauthorized")
        return
    owner_id, kind = identity.owner_id, identity.kind

    container, ip = await get_ecs_manager().resolve_running_container(owner_id)
    if container is None or ip is None:
        outcome = await request_wake_silent(owner_id, door="relay")
        if kind == "operator":
            await relay_hooks.maybe_disarm_expired_trial(owner_id)
        put_metric("relay.connect", dimensions={"kind": kind, "outcome": f"wake_{outcome}"})
        await websocket.close(code=1013, reason="Container not running")
        return

    connections = ConnectionService()
    connection_id = f"relay#{uuid.uuid4()}"
    try:
        await connections.put_relay_presence(connection_id=connection_id, identity=identity)
    except Exception:  # noqa: BLE001 — presence is best-effort; the relay still serves
        logger.warning("relay presence write failed kind=%s", kind, exc_info=True)
        put_metric("relay.presence", dimensions={"outcome": "put_error"})

    close_code = 1000
    try:
        async with ws_connect(f"ws://{ip}:{GATEWAY_PORT}", max_size=_WS_MAX_FRAME_SIZE, open_timeout=10, close_timeout=5) as upstream:
            await websocket.accept()
            put_metric("relay.connect", dimensions={"kind": kind, "outcome": "ok"})
            node_ready = False

            async def client_to_upstream() -> None:
                try:
                    while True:
                        msg = await websocket.receive()
                        if msg.get("type") == "websocket.disconnect":
                            return
                        if msg.get("bytes") is not None:
                            await upstream.send(msg["bytes"])
                            continue
                        text = msg.get("text")
                        if text is None:
                            continue
                        if kind == "operator":
                            body = _parse(text)
                            if body and body.get("type") == "req" and body.get("method") in relay_hooks.WAKE_GATED_METHODS:
                                refusal = await relay_hooks.admit_operator_wake(user_id=identity.user_id, owner_id=owner_id, body=body)
                                if refusal is not None:
                                    await websocket.send_text(json.dumps(refusal))
                                    continue
                        await upstream.send(text)
                except (WebSocketDisconnect, ConnectionClosed):
                    return

            async def upstream_to_client() -> None:
                nonlocal node_ready
                try:
                    async for frame in upstream:
                        if isinstance(frame, bytes):
                            await websocket.send_bytes(frame)
                            continue
                        await websocket.send_text(frame)
                        body = _parse(frame)
                        if not body or body.get("type") != "res":
                            continue
                        error = body.get("error") if isinstance(body.get("error"), dict) else None
                        if error and error.get("code") == "NOT_PAIRED":
                            details = error.get("details") if isinstance(error.get("details"), dict) else {}
                            request_id = details.get("requestId") if isinstance(details.get("requestId"), str) else None
                            device_id = identity.device_id or (details.get("deviceId") if isinstance(details.get("deviceId"), str) else None)
                            if device_id:
                                await _self_heal(kind, owner_id, device_id, request_id)
                        elif kind == "node" and not node_ready and body.get("ok") is True and isinstance(body.get("payload"), dict) and body["payload"].get("type") == "hello-ok":
                            node_ready = True
                            await relay_hooks.on_node_ready(owner_id, identity.user_id, identity.device_id or "")
                except (WebSocketDisconnect, ConnectionClosed):
                    return

            async def refresh_presence() -> None:
                while True:
                    await asyncio.sleep(_REFRESH_SECONDS)
                    try:
                        alive = await connections.refresh_relay_presence(connection_id)
                    except Exception:  # noqa: BLE001
                        put_metric("relay.presence", dimensions={"outcome": "refresh_error"})
                        continue
                    if not alive:
                        put_metric("relay.presence", dimensions={"outcome": "revoked"})
                        return  # ends the wait below → both legs cancelled → close 1008

            legs = {asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())}
            refresher = asyncio.create_task(refresh_presence())
            done, pending = await asyncio.wait(legs | {refresher}, return_when=asyncio.FIRST_COMPLETED)
            if refresher in done:
                close_code = 1008
            for task in pending:
                task.cancel()
            await asyncio.gather(*legs, refresher, return_exceptions=True)
    except Exception:  # noqa: BLE001 — never let a relay error take the worker down
        logger.warning("relay error kind=%s", kind, exc_info=True)
        close_code = 1011
    finally:
        try:
            await asyncio.to_thread(connections.delete_connection, connection_id)
        except Exception:  # noqa: BLE001
            put_metric("relay.presence", dimensions={"outcome": "delete_error"})
        if kind == "node" and node_ready:
            try:
                await relay_hooks.on_node_closed(owner_id, identity.user_id)
            except Exception:  # noqa: BLE001
                logger.warning("relay node-closed hook failed", exc_info=True)
        put_metric("relay.close", dimensions={"kind": kind, "code": str(close_code)})
        try:
            await websocket.close(code=close_code)
        except Exception:  # noqa: BLE001 — already closed by the client
            pass


async def _self_heal(kind: str, owner_id: str, device_id: str, request_id: str | None) -> None:
    try:
        if kind == "node":
            healed = await heal_desktop_device_not_paired(owner_id=owner_id, device_id=device_id, request_id=request_id)
        else:
            healed = await approve_pending_device_from_rejection(owner_id=owner_id, device_id=device_id, request_id=request_id)
        put_metric("relay.not_paired_selfheal", dimensions={"kind": kind, "outcome": "healed" if healed else "failed"})
    except Exception:  # noqa: BLE001
        put_metric("relay.not_paired_selfheal", dimensions={"kind": kind, "outcome": "error"})
        logger.warning("relay NOT_PAIRED self-heal failed kind=%s", kind, exc_info=True)
```

Note `node_ready` must be declared before the closures and read in `finally` — hoist it above the `try`. Check `approve_pending_device_from_rejection`'s B-era signature and `heal_desktop_device_not_paired`'s (Task 6 of the B plan) and match them.

- [ ] **Step 4: Run the tests**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_gateway_relay.py tests/unit/routers/test_browser_view.py -q --no-cov && uv run ruff check routers/gateway_relay.py main.py && uv run python -c "import main"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/routers/gateway_relay.py apps/backend/main.py apps/backend/tests/unit/routers/test_gateway_relay.py
git commit -m "feat(relay): raw WebSocket pipe from machine clients to the owner's gateway"
```

---

### Task 4: delete the API Gateway relays; settings and URL consumers

**Files:**
- Delete: `apps/backend/core/services/node_proxy.py`, `core/services/operator_proxy.py`, `core/gateway/node_connection.py`, `core/gateway/operator_rpc.py`, `tests/unit/services/test_node_proxy.py`, `tests/unit/services/test_operator_proxy.py`, `tests/unit/gateway/test_node_connection.py` (or wherever it lives), `tests/unit/routers/test_websocket_chat.py` node/operator cases
- Modify: `apps/backend/routers/websocket_chat.py` (local-session only), `core/services/paperclip_openclaw_pairing.py` (`_probe_pairing_request` and the probe step in `ensure_operator_device_paired` deleted; the pairing-window comment rewritten: "the adapter's first connect through the relay opens the window; the relay approves it"), `core/services/desktop_pair_live.py` + `routers/desktop_pair.py` (`force_close_device` → delete the device's presence rows: `ConnectionService.delete_relay_rows_for_device(device_id)`, a `by-owner-id` query filtered on `deviceId` then `delete_connection` each), `core/config.py` (`GATEWAY_RELAY_URL: str = "wss://bv-dev.flickmeon.ai/api/v1/gateway/ws"`), `routers/desktop_pair.py:94` (`gateway_ws_url=settings.GATEWAY_RELAY_URL`), `core/services/paperclip_provisioning.py:202-215` (`_ws_gateway_url` returns `settings.GATEWAY_RELAY_URL` in deployed envs), `core/services/paperclip_adapter_config.py:60-77` (`build_gateway_url_re(settings.GATEWAY_RELAY_URL)`: the hosted branch becomes `wss://bv(?:-[a-z]+)?\.{apex}/api/v1/gateway/ws`), `core/services/company_deploy_service.py:88-90` (unchanged: it calls `_ws_gateway_url`)
- Test: `tests/unit/services/test_paperclip_adapter_config.py` (allowlist cases), `tests/unit/routers/test_desktop_pair_router.py` (URL), `tests/unit/services/test_paperclip_openclaw_pairing.py` (probe tests deleted), `tests/unit/routers/test_websocket_chat.py` (trimmed), `tests/unit/routers/test_websocket_chat_internal_auth.py` (unchanged)

**Interfaces:**
- Produces: `websocket_chat.ws_message` serves only `connection_type == "local-session"` (403 otherwise); `ws_connect` kicks off only the local-session `connect`; `ConnectionService.delete_relay_rows_for_device(device_id) -> int`.

- [ ] **Step 1: Trim and delete**

`routers/websocket_chat.py`: delete the `operator_proxy`/`node_proxy` imports, `_kick_off_node_connect`, `_kick_off_operator_connect`, `_WAKE_GATED_METHODS`, `_wake_refusal_payload`, `_admit_operator_wake`, `_send_relay_unavailable`, and in `ws_connect` / `ws_disconnect` / `ws_message` every branch for `node` and `operator` (a non-`local-session` row is a 403 "Machine connection required"). `_assert_machine_connection` keeps only the local-session arm. Then `git rm` the four modules and their tests; rewrite `test_websocket_chat.py` to the local-session cases that remain.

`paperclip_openclaw_pairing.py`: delete `_probe_pairing_request`; in `ensure_operator_device_paired` delete the probe/approve step (`:817-835`) and its `gateway_token`/`ip` parameters; update its callers (`routers/teams/agents.py`, `runtime_rpc._create_agent`, provisioning) to drop those two arguments. Delete the `perform_signed_connect` import.

`desktop_pair_live.py`: `force_close_device(device_id)` → `await ConnectionService().delete_relay_rows_for_device(device_id)` (add that method: `query_by_owner` needs the owner — take `owner_id` too: `delete_relay_rows_for_device(owner_id, device_id)`; the revoke route knows the owner).

- [ ] **Step 2: Settings and URL consumers**

Apply the `GATEWAY_RELAY_URL` edits listed under Files. In `test_paperclip_adapter_config.py` replace the `wss://ws-dev.…` allowlist cases with `wss://bv-dev.flickmeon.ai/api/v1/gateway/ws` (accepted) and `wss://ws-dev.flickmeon.ai` (rejected); keep `ws://localhost:<port>`.

- [ ] **Step 3: Gates**

Run: `cd apps/backend && git grep -n "node_proxy\|operator_proxy\|node_connection\|operator_rpc\|perform_signed_connect\|_probe_pairing_request\|WS_GATEWAY_URL" -- core routers main.py scripts`
Expected: `WS_GATEWAY_URL` remains only where the local-session enrollment URL is built (`local_sessions.py` / `local_session_relay.py` — read them; if they read `settings.WS_GATEWAY_URL` that is correct and stays); nothing else.

Run: `cd apps/backend && uv run pytest tests/unit/routers tests/unit/services -q --no-cov -x && uv run ruff check core routers && uv run python -c "import main"`
Expected: PASS (report pre-existing reds, do not fix).

- [ ] **Step 4: Commit**

```bash
git add -A apps/backend
git commit -m "feat(relay)!: node hosts and Paperclip operators leave the API Gateway relay"
```

---

### Task 5: infra, authorizer, desktop build

**Files:**
- Modify: `apps/infra/lib/stacks/network-stack.ts:194-197` (path patterns), `apps/infra/lib/stacks/api-stack.ts` (`public readonly gatewayRelayUrl: string` set next to the browser-view output: `wss://${browserViewDomain}/api/v1/gateway/ws`; a `CfnOutput` `lightbulb-${env}-gateway-relay-url`), `apps/infra/lib/lightbulb-stage.ts` (thread `gatewayRelayUrl: api.gatewayRelayUrl` into the service stack props) and `apps/infra/lib/stacks/service-stack.ts` (`GATEWAY_RELAY_URL: props.gatewayRelayUrl` in the env block; `desiredCount: 2`; after the service: `service.autoScaleTaskCount({ minCapacity: 2, maxCapacity: 4 }).scaleOnCpuUtilization("Cpu", { targetUtilizationPercent: 60 })`; delete the "NOT autoscaled" comment), `apps/infra/lambda/websocket-authorizer/index.py` (delete `_try_service_token` and its key loading; the device branch admits only `clientId == "lightbulb-local-sessions"` — read how the authorizer sees the clientId, or leave the clientId check to `ws-connect` where it already lives), `apps/infra/lambda/ws-connect/index.py:53-66` (`node-host`/`openclaw-macos`/`paperclip-operator` removed: a device token with any other `clientId` than `lightbulb-local-sessions` is refused; the `paperclip_service` branch deleted), `.github/workflows/desktop-build.yml:216,411` (`LIGHTBULB_WS_URL: wss://bv-dev.flickmeon.ai/api/v1/gateway/ws` / `wss://bv.flickmeon.ai/api/v1/gateway/ws`), `apps/desktop/src-tauri/src/lib.rs:220,708` (defaults likewise), `CLAUDE.md` (WebSocket Architecture items 1–3 rewritten for the local-session-only door; item 5 gains the relay path; `service-stack.ts` row: autoscaled 2–4; Desktop App bullet on `LIGHTBULB_WS_URL`)
- Test: `apps/infra/test/network-stack.test.ts` / `api-stack.test.ts` / `service-stack.test.ts` (whichever assert the listener rule, outputs, desired count — grep `desiredCount` and `browser-view` in `apps/infra/test`), `apps/infra/lambda/websocket-authorizer/test_*.py` and `lambda/ws-connect/test_*.py` if present

- [ ] **Step 1: Write/adjust the infra tests first** — the listener rule carries both path patterns; the service's `DesiredCount` is 2 and a `ScalableTarget` with min 2 / max 4 exists; the `GATEWAY_RELAY_URL` env is present; the authorizer refuses a service token; `ws-connect` refuses `clientId=node-host`.

- [ ] **Step 2: Implement** the edits listed under Files.

- [ ] **Step 3: Run**

Run: `cd apps/infra && pnpm test && npx cdk synth > /dev/null && git status --short .github/workflows/deploy.yml; cd lambda/websocket-authorizer && python3 -m pytest -q 2>/dev/null || true`
Expected: PASS; commit the regenerated `deploy.yml` if it changed.

- [ ] **Step 4: Commit**

```bash
git add apps/infra .github/workflows/deploy.yml .github/workflows/desktop-build.yml apps/desktop/src-tauri/src/lib.rs CLAUDE.md
git commit -m "feat(infra): gateway relay path on the bv door; backend autoscaled 2-4; API Gateway door narrowed to local sessions"
```

---

### Task 6: Paperclip adapter URL backfill

**Files:**
- Create: `apps/backend/scripts/backfill_adapter_gateway_urls.py`
- Test: `apps/backend/tests/unit/scripts/test_backfill_adapter_gateway_urls.py`

**Interfaces:**
- Consumes: `PaperclipRepo(table_name="paperclip-companies")._table().scan` (rows with `company_id`), `company_deploy_service._open_admin_session()` → `(http, admin, cookie)`, `admin.list_agents(cookie, company_id) -> {"agents": [...]}` (confirm the key by reading the method), `admin.patch_agent(cookie, agent_id, {"adapterConfig": {...}})`, `settings.GATEWAY_RELAY_URL`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/scripts/test_backfill_adapter_gateway_urls.py
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scripts import backfill_adapter_gateway_urls as script

AGENTS = {"agents": [
    {"id": "ag-1", "adapterType": "openclaw_gateway", "adapterConfig": {"url": "wss://ws-dev.flickmeon.ai/?token=t&clientId=paperclip-operator", "agentId": "x"}},
    {"id": "ag-2", "adapterType": "openclaw_gateway", "adapterConfig": {"url": "wss://bv-dev.flickmeon.ai/api/v1/gateway/ws?token=t&clientId=paperclip-operator"}},
    {"id": "ag-3", "adapterType": "other", "adapterConfig": {}},
]}


@pytest.mark.asyncio
@pytest.mark.parametrize("apply", [False, True])
async def test_rewrites_only_stale_openclaw_adapters(apply):
    admin = MagicMock()
    admin.list_agents = AsyncMock(return_value=AGENTS)
    admin.patch_agent = AsyncMock()
    http = MagicMock()
    http.aclose = AsyncMock()
    with (
        patch("scripts.backfill_adapter_gateway_urls._companies", new_callable=AsyncMock, return_value=["co-1"]),
        patch("scripts.backfill_adapter_gateway_urls._open_admin_session", new_callable=AsyncMock, return_value=(http, admin, "cookie")),
        patch("scripts.backfill_adapter_gateway_urls.settings") as settings,
    ):
        settings.GATEWAY_RELAY_URL = "wss://bv-dev.flickmeon.ai/api/v1/gateway/ws"
        changed = await script.main(apply=apply)
    assert changed == 1
    if apply:
        admin.patch_agent.assert_awaited_once()
        assert admin.patch_agent.await_args.args[2]["adapterConfig"]["url"] == "wss://bv-dev.flickmeon.ai/api/v1/gateway/ws?token=t&clientId=paperclip-operator"
        assert admin.patch_agent.await_args.args[2]["adapterConfig"]["agentId"] == "x"
    else:
        admin.patch_agent.assert_not_awaited()
```

- [ ] **Step 2: Implement**

```python
"""ONE-SHOT: point every openclaw_gateway adapter at the gateway relay URL.

Run once per environment after that environment's relay deploy, then DELETE this file.
Dry run by default; ``--apply`` writes.

    cd apps/backend && AWS_PROFILE=isol8-admin uv run python scripts/backfill_adapter_gateway_urls.py [--apply]
"""

import argparse
import asyncio
import sys
from urllib.parse import urlsplit, urlunsplit

sys.path.insert(0, ".")

from core.config import settings  # noqa: E402
from core.dynamodb import run_in_thread  # noqa: E402
from core.repositories.paperclip_repo import PaperclipRepo  # noqa: E402
from core.services.company_deploy_service import _open_admin_session  # noqa: E402


async def _companies() -> list[str]:
    table = PaperclipRepo(table_name="paperclip-companies")._table()
    ids, kwargs = [], {"ProjectionExpression": "company_id"}
    while True:
        page = await run_in_thread(table.scan, **kwargs)
        ids.extend(row["company_id"] for row in page.get("Items", []) if row.get("company_id"))
        if "LastEvaluatedKey" not in page:
            return sorted(set(ids))
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _relay_url(old: str, base: str) -> str:
    """Keep the query (token + clientId); replace scheme/host/path with the relay's."""
    query = urlsplit(old).query
    b = urlsplit(base)
    return urlunsplit((b.scheme, b.netloc, b.path, query, ""))


async def main(apply: bool) -> int:
    base = settings.GATEWAY_RELAY_URL
    http, admin, cookie = await _open_admin_session()
    changed = 0
    try:
        for company_id in await _companies():
            agents = (await admin.list_agents(cookie, company_id)).get("agents") or []
            for agent in agents:
                if agent.get("adapterType") != "openclaw_gateway":
                    continue
                cfg = agent.get("adapterConfig") or {}
                url = cfg.get("url") or ""
                if url.startswith(base):
                    continue
                changed += 1
                print(f"{'PATCH' if apply else 'would patch'} company={company_id} agent={agent['id']}")
                if apply:
                    await admin.patch_agent(cookie, agent["id"], {"adapterConfig": {**cfg, "url": _relay_url(url, base)}})
    finally:
        await http.aclose()
    print(f"{'patched' if apply else 'stale'}: {changed}")
    return changed


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    raise SystemExit(0 if asyncio.run(main(parser.parse_args().apply)) >= 0 else 1)
```

Confirm `list_agents`'s return shape (`{"agents": [...]}` or a bare list) by reading the method and adjust. `tests/unit/scripts/` may need an `__init__.py` — mirror how `backfill_workspace_ids`'s test was placed.

- [ ] **Step 3: Run and commit**

Run: `cd apps/backend && uv run pytest tests/unit/scripts/test_backfill_adapter_gateway_urls.py -q --no-cov && uv run ruff check scripts/backfill_adapter_gateway_urls.py`

```bash
git add apps/backend/scripts/backfill_adapter_gateway_urls.py apps/backend/tests/unit/scripts
git commit -m "chore(relay): one-shot backfill of Paperclip adapter gateway URLs"
```

---

### Task 7: Cutover

Owner approval before every push, merge, deploy, `--apply`, or AWS mutation.

- [ ] **Step 1:** Rebase onto `main`, full backend suite + `apps/infra` tests, PR, required check + Codex, merge. Dev deploys.
- [ ] **Step 2 (dev):** run the backfill dry-run then `--apply`; build the dev desktop (`desktop-build.yml` dev job) and pair a Mac; spec §8 checks at `desiredCount: 2` (both instances serve relay connections — ALB target request counts on the :8081 target group; an `exec` through the node; a Paperclip heartbeat run completes and its usage lands once; revoke closes a live node within 60 s; a rolling deploy mid-run reconnects; trial idle holds under an operator connection).
- [ ] **Step 3 (prod):** dispatch `deploy.yml`, run the backfill `--apply` on prod, promote the desktop build; watch `relay.connect` / `relay.close` / `wake.admission` and the P12 canary for a day.
- [ ] **Step 4:** delete the backfill script, this plan and the spec in a follow-up PR; record the state in the session memory file.
