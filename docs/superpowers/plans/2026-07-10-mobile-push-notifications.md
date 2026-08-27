# Mobile Push Notifications v1 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a run the user started completes, their phone gets a push notification (agent name + reply preview) that deep-links into that agent's chat — even with the app closed.

**Architecture:** Backend stores Expo push tokens on the `users` row (member-scoped, org-correct), a `run_ended` terminal in `connection_pool.py` fires a fire-and-forget push via the Expo Push API, and a small teardown guard keeps the container connection alive while a run is active so a phone-only send-then-close still delivers. Mobile registers the token after sign-in, shows/suppresses the banner via a notification handler, and routes taps to the agent's chat.

**Tech Stack:** Python/FastAPI + DynamoDB (backend), AWS CDK (infra secret), Expo SDK 57 / React Native 0.86 / React 19.2 / expo-router (mobile), Expo Push API (transport), pytest (backend tests), jest-expo + @testing-library/react-native (mobile tests, set up in Task 7), vitest (packages/shared).

## Global Constraints

- **Worktree:** all work happens in `.claude/worktrees/mobile-push-notifications` on branch `feat/mobile-push-notifications` (off `origin/main`). Backend/infra paths are under `apps/backend`, `apps/infra`; mobile under `apps/mobile`.
- **Audience rule (org-correct):** push target = `target_member or self.owner_id`, and **only if it starts with `user_`** (a Clerk user id). Org sessions carry `member_id`; personal sessions use `agent:<id>:main` (no member_id) so fall back to the owner (== the user). An unattributed org session never pushes in v1.
- **v1 fires only on `state == "final"`** (a completed reply). Error/aborted terminals do not push in v1.
- **No APNs credentials in our infra** — Expo/EAS manages the APNs key. The only backend secret is `EXPO_ACCESS_TOKEN` (Expo access token), read from the env var of the same name.
- **Expo project id** (for `getExpoPushTokenAsync`): `2def259d-5e79-462b-9b0f-3d01d666ba66` (from `app.json` `extra.eas.projectId`; read at runtime via `expo-constants`).
- **iOS ids:** bundle `co.isol8.mobile` (→ `ai.flickmeon.mobile` at the flag-day flip), Apple team `WZX4U3C22Y`.
- **Mobile authenticated REST pattern (mandatory):** pin Clerk's `getToken` behind a ref, build the client once — `const getTokenRef = useRef(getToken); getTokenRef.current = getToken; const config = useMemo(() => makeAdapter(() => getTokenRef.current()), []); const api = useMemo(() => createApiClient(config), [config]);` A `[getToken]` dep is an infinite-loop trap.
- **Secret naming:** the Expo secret is `isol8/${env}/expo_access_token`; the existing `isol8/${env}/*` IAM grants cover read+decrypt, so **no new IAM**.
- **Do not run full `pnpm test`** across the monorepo (RAM-heavy). Run per-package filtered test commands only.
- Commit after every task (frequent commits).

---

## File Structure

**Backend**
- `apps/backend/core/repositories/user_repo.py` — MODIFY: add `set_push_token`, `clear_push_token` (scoped `update_item`).
- `apps/backend/schemas/user_schemas.py` — MODIFY: add `DeviceRegisterRequest`, `DeviceRegisterResponse`.
- `apps/backend/routers/devices.py` — CREATE: `POST /api/v1/devices`, `DELETE /api/v1/devices/{device_id}`.
- `apps/backend/main.py` — MODIFY: register the devices router.
- `apps/backend/core/services/push_service.py` — CREATE: Expo Push API sender + dead-token prune.
- `apps/backend/core/gateway/connection_pool.py` — MODIFY: push dispatch hook on `run_ended`; agent-name resolver; teardown guard.

**Infra**
- `apps/infra/lib/stacks/auth-stack.ts` — MODIFY: declare the Expo secret.
- `apps/infra/lib/stacks/service-stack.ts` — MODIFY: `SecretNames` member + `secrets:` entry.
- `apps/infra/lib/stages/lightbulb-stage.ts`, `apps/infra/lib/stages/local-stage.ts` — MODIFY: pass `expoAccessToken` name.

**Mobile**
- `apps/mobile/babel.config.js`, `apps/mobile/jest.config.js`, `apps/mobile/jest-setup.ts` — CREATE (Task 7).
- `apps/mobile/package.json`, `apps/mobile/app.json` — MODIFY: test runner + expo-notifications deps/config.
- `apps/mobile/src/lib/deepLink.ts` (+ `.test.ts`) — CREATE: parse `lightbulbai://agent/<id>`.
- `apps/mobile/src/lib/notifications.ts` (+ `.test.ts`) — CREATE: foreground-suppression decision + handler config.
- `apps/mobile/src/components/push/PushRegistration.tsx` — CREATE: permission + token registration + tap routing.
- `apps/mobile/src/app/(app)/_layout.tsx` — MODIFY: mount `<PushRegistration/>`.
- `.github/workflows/mobile-ci.yml` — MODIFY: add a mobile test step.

**Docs**
- `apps/mobile/README.md`, `docs/superpowers/plans/2026-07-05-mobile-app-expo-ios.md` — MODIFY (Task 12).

---

## Task 1: Backend — `user_repo` push-token methods

**Files:**
- Modify: `apps/backend/core/repositories/user_repo.py`
- Test: `apps/backend/tests/unit/repositories/test_user_repo_push_tokens.py`

**Interfaces:**
- Produces: `async set_push_token(user_id: str, device_id: str, entry: dict) -> None`, `async clear_push_token(user_id: str, device_id: str) -> None`. Row attribute `push_tokens: { <deviceId>: {"token": str, "platform": str, "updatedAt": str} }`.

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/unit/repositories/test_user_repo_push_tokens.py
"""Unit tests for user_repo push-token scoped map writes."""

import pytest
from unittest.mock import MagicMock, patch


@pytest.mark.asyncio
async def test_set_push_token_scoped_update():
    fake_table = MagicMock()
    with patch("core.repositories.user_repo._get_table", return_value=fake_table):
        from core.repositories import user_repo

        entry = {"token": "ExponentPushToken[abc]", "platform": "ios", "updatedAt": "2026-07-10T00:00:00Z"}
        await user_repo.set_push_token("user_1", "dev_1", entry)

    # Two calls: init-map-if-absent, then scoped set of the single key.
    assert fake_table.update_item.call_count == 2
    init_kwargs = fake_table.update_item.call_args_list[0].kwargs
    set_kwargs = fake_table.update_item.call_args_list[1].kwargs
    assert init_kwargs["Key"] == {"user_id": "user_1"}
    assert init_kwargs["UpdateExpression"] == "SET #pt = if_not_exists(#pt, :empty)"
    assert set_kwargs["UpdateExpression"] == "SET #pt.#did = :entry"
    assert set_kwargs["ExpressionAttributeNames"] == {"#pt": "push_tokens", "#did": "dev_1"}
    assert set_kwargs["ExpressionAttributeValues"] == {":entry": entry}


@pytest.mark.asyncio
async def test_clear_push_token_scoped_remove():
    fake_table = MagicMock()
    with patch("core.repositories.user_repo._get_table", return_value=fake_table):
        from core.repositories import user_repo

        await user_repo.clear_push_token("user_1", "dev_1")

    fake_table.update_item.assert_called_once()
    kwargs = fake_table.update_item.call_args.kwargs
    assert kwargs["UpdateExpression"] == "REMOVE #pt.#did"
    assert kwargs["ExpressionAttributeNames"] == {"#pt": "push_tokens", "#did": "dev_1"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/repositories/test_user_repo_push_tokens.py -v`
Expected: FAIL — `AttributeError: module 'core.repositories.user_repo' has no attribute 'set_push_token'`.

- [ ] **Step 3: Implement the methods**

Add to the top imports of `apps/backend/core/repositories/user_repo.py` (the module currently imports only from `core.dynamodb`):

```python
from botocore.exceptions import ClientError
```

Append these functions to `apps/backend/core/repositories/user_repo.py`:

```python
async def set_push_token(user_id: str, device_id: str, entry: dict) -> None:
    """Write one Expo push token into the user's ``push_tokens`` map.

    Scoped, server-atomic UpdateItem touching only its one map key (mirrors
    container_repo._put_paired_device) so a concurrent write of another device
    or another attribute can't clobber it. ``entry`` = {token, platform, updatedAt}.
    Requires the users row to exist (it always does for a signed-in user who
    hit /users/sync); no-op if it doesn't.
    """
    table = _get_table()
    # 1. Ensure the map exists — a nested SET fails if its parent map is absent.
    try:
        await run_in_thread(
            table.update_item,
            Key={"user_id": user_id},
            UpdateExpression="SET #pt = if_not_exists(#pt, :empty)",
            ConditionExpression="attribute_exists(user_id)",
            ExpressionAttributeNames={"#pt": "push_tokens"},
            ExpressionAttributeValues={":empty": {}},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return  # no users row — nothing to write onto
        raise
    # 2. Scoped set of the single device key.
    try:
        await run_in_thread(
            table.update_item,
            Key={"user_id": user_id},
            UpdateExpression="SET #pt.#did = :entry",
            ConditionExpression="attribute_exists(user_id)",
            ExpressionAttributeNames={"#pt": "push_tokens", "#did": device_id},
            ExpressionAttributeValues={":entry": entry},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return  # row deleted mid-write — no-op
        raise


async def clear_push_token(user_id: str, device_id: str) -> None:
    """Remove one device's push token (sign-out, or Expo DeviceNotRegistered prune).

    Scoped REMOVE of the single map key so a racing writer of other attributes
    cannot resurrect the removed entry. No-op if the row is absent.
    """
    table = _get_table()
    try:
        await run_in_thread(
            table.update_item,
            Key={"user_id": user_id},
            UpdateExpression="REMOVE #pt.#did",
            ConditionExpression="attribute_exists(user_id)",
            ExpressionAttributeNames={"#pt": "push_tokens", "#did": device_id},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return  # no users row — nothing to remove
        raise
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/repositories/test_user_repo_push_tokens.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/repositories/user_repo.py apps/backend/tests/unit/repositories/test_user_repo_push_tokens.py
git commit -m "feat(backend): user_repo push-token scoped map read/write"
```

---

## Task 2: Backend — device registration endpoint

**Files:**
- Modify: `apps/backend/schemas/user_schemas.py`
- Create: `apps/backend/routers/devices.py`
- Modify: `apps/backend/main.py` (register router near the other `include_router` calls, e.g. after the `users.router` line)
- Test: `apps/backend/tests/unit/routers/test_devices.py`

**Interfaces:**
- Consumes: `user_repo.set_push_token` / `clear_push_token` (Task 1); `get_current_user`, `AuthContext` (`core/auth.py`).
- Produces: `POST /api/v1/devices` body `{token, platform, deviceId}` → `{status: "registered"}`; `DELETE /api/v1/devices/{device_id}` → `{status: "cleared"}`.

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/unit/routers/test_devices.py
"""Unit tests for the device-registration router."""

import pytest
from unittest.mock import AsyncMock, patch


class TestRegisterDevice:
    @pytest.mark.asyncio
    @patch("routers.devices.user_repo")
    async def test_register_writes_token(self, mock_repo, async_client):
        mock_repo.set_push_token = AsyncMock(return_value=None)

        resp = await async_client.post(
            "/api/v1/devices",
            json={"token": "ExponentPushToken[abc]", "platform": "ios", "deviceId": "dev_1"},
        )

        assert resp.status_code == 200
        assert resp.json()["status"] == "registered"
        mock_repo.set_push_token.assert_awaited_once()
        args = mock_repo.set_push_token.await_args
        assert args.args[0] == "user_test_123"  # auth.user_id from conftest
        assert args.args[1] == "dev_1"
        assert args.args[2]["token"] == "ExponentPushToken[abc]"
        assert args.args[2]["platform"] == "ios"
        assert "updatedAt" in args.args[2]

    @pytest.mark.asyncio
    @patch("routers.devices.user_repo")
    async def test_register_rejects_bad_token(self, mock_repo, async_client):
        mock_repo.set_push_token = AsyncMock()
        resp = await async_client.post(
            "/api/v1/devices",
            json={"token": "not-a-token", "platform": "ios", "deviceId": "dev_1"},
        )
        assert resp.status_code == 422
        mock_repo.set_push_token.assert_not_called()

    @pytest.mark.asyncio
    async def test_register_requires_auth(self, unauthenticated_async_client):
        resp = await unauthenticated_async_client.post(
            "/api/v1/devices",
            json={"token": "ExponentPushToken[abc]", "platform": "ios", "deviceId": "dev_1"},
        )
        assert resp.status_code in [401, 403]

    @pytest.mark.asyncio
    @patch("routers.devices.user_repo")
    async def test_delete_clears_token(self, mock_repo, async_client):
        mock_repo.clear_push_token = AsyncMock(return_value=None)
        resp = await async_client.delete("/api/v1/devices/dev_1")
        assert resp.status_code == 200
        assert resp.json()["status"] == "cleared"
        mock_repo.clear_push_token.assert_awaited_once_with("user_test_123", "dev_1")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_devices.py -v`
Expected: FAIL — import error / 404 (router not registered yet).

- [ ] **Step 3: Add the schemas**

Append to `apps/backend/schemas/user_schemas.py`:

```python
class DeviceRegisterRequest(BaseModel):
    """Body for POST /api/v1/devices — one device's Expo push token."""

    token: str = Field(..., description="Expo push token, ExponentPushToken[...]")
    platform: Literal["ios"] = Field(default="ios", description="Device platform")
    deviceId: str = Field(..., min_length=1, description="Stable per-install device id")

    @field_validator("token")
    @classmethod
    def _looks_like_expo_token(cls, v: str) -> str:
        if not (v.startswith("ExponentPushToken[") or v.startswith("ExpoPushToken[")):
            raise ValueError("not an Expo push token")
        return v


class DeviceRegisterResponse(BaseModel):
    status: str = Field(..., description="'registered'")
```

Add `field_validator` to the pydantic import at the top of the file:

```python
from pydantic import BaseModel, Field, field_validator
```

- [ ] **Step 4: Create the router**

```python
# apps/backend/routers/devices.py
"""Device (push-token) registration endpoints."""

import logging

from fastapi import APIRouter, Depends

from core.auth import AuthContext, get_current_user
from core.dynamodb import utc_now_iso
from core.repositories import user_repo
from schemas.user_schemas import DeviceRegisterRequest, DeviceRegisterResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("", response_model=DeviceRegisterResponse, operation_id="register_device")
async def register_device(
    body: DeviceRegisterRequest,
    auth: AuthContext = Depends(get_current_user),
):
    """Register (or refresh) this device's Expo push token for the caller."""
    entry = {"token": body.token, "platform": body.platform, "updatedAt": utc_now_iso()}
    await user_repo.set_push_token(auth.user_id, body.deviceId, entry)
    return {"status": "registered"}


@router.delete("/{device_id}", operation_id="unregister_device")
async def unregister_device(
    device_id: str,
    auth: AuthContext = Depends(get_current_user),
):
    """Remove this device's push token (sign-out)."""
    await user_repo.clear_push_token(auth.user_id, device_id)
    return {"status": "cleared"}
```

- [ ] **Step 5: Register the router**

In `apps/backend/main.py`, add near the existing `app.include_router(users.router, prefix="/api/v1/users", tags=["users"])` line:

```python
app.include_router(devices.router, prefix="/api/v1/devices", tags=["devices"])
```

Add `devices` to the routers import at the top of `main.py` (match the existing `from routers import (...)` grouping — add `devices` alongside `users`).

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_devices.py -v`
Expected: PASS (4 passed).

- [ ] **Step 7: Commit**

```bash
git add apps/backend/routers/devices.py apps/backend/schemas/user_schemas.py apps/backend/main.py apps/backend/tests/unit/routers/test_devices.py
git commit -m "feat(backend): POST/DELETE /api/v1/devices push-token registration"
```

---

## Task 3: Backend — Expo Push API service

**Files:**
- Create: `apps/backend/core/services/push_service.py`
- Test: `apps/backend/tests/unit/services/test_push_service.py`

**Interfaces:**
- Consumes: `user_repo.get`, `user_repo.clear_push_token`.
- Produces: `async send_push_to_user(user_id: str, title: str, body: str, data: dict | None = None) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/unit/services/test_push_service.py
"""Unit tests for the Expo push sender."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _resp(json_body):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value=json_body)
    return r


@pytest.mark.asyncio
async def test_sends_to_all_device_tokens():
    row = {"push_tokens": {
        "d1": {"token": "ExponentPushToken[a]", "platform": "ios"},
        "d2": {"token": "ExponentPushToken[b]", "platform": "ios"},
    }}
    post = AsyncMock(return_value=_resp({"data": [{"status": "ok"}, {"status": "ok"}]}))
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=MagicMock(post=post))
    client.__aexit__ = AsyncMock(return_value=False)

    with patch("core.services.push_service.user_repo.get", AsyncMock(return_value=row)), \
         patch("core.services.push_service.httpx.AsyncClient", return_value=client):
        from core.services import push_service
        await push_service.send_push_to_user("user_1", "Friday", "hello", {"agentId": "a1"})

    sent = post.await_args.kwargs["json"]
    assert {m["to"] for m in sent} == {"ExponentPushToken[a]", "ExponentPushToken[b]"}
    assert sent[0]["title"] == "Friday"
    assert sent[0]["body"] == "hello"
    assert sent[0]["data"] == {"agentId": "a1"}


@pytest.mark.asyncio
async def test_prunes_device_not_registered():
    row = {"push_tokens": {"d1": {"token": "ExponentPushToken[dead]", "platform": "ios"}}}
    post = AsyncMock(return_value=_resp(
        {"data": [{"status": "error", "details": {"error": "DeviceNotRegistered"}}]}
    ))
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=MagicMock(post=post))
    client.__aexit__ = AsyncMock(return_value=False)
    clear = AsyncMock()

    with patch("core.services.push_service.user_repo.get", AsyncMock(return_value=row)), \
         patch("core.services.push_service.user_repo.clear_push_token", clear), \
         patch("core.services.push_service.httpx.AsyncClient", return_value=client):
        from core.services import push_service
        await push_service.send_push_to_user("user_1", "Friday", "hi", {})

    clear.assert_awaited_once_with("user_1", "d1")


@pytest.mark.asyncio
async def test_no_tokens_is_noop():
    with patch("core.services.push_service.user_repo.get", AsyncMock(return_value={})):
        from core.services import push_service
        # Must not raise and must not construct a client.
        with patch("core.services.push_service.httpx.AsyncClient") as client_ctor:
            await push_service.send_push_to_user("user_1", "t", "b", {})
            client_ctor.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_push_service.py -v`
Expected: FAIL — module `core.services.push_service` does not exist.

- [ ] **Step 3: Implement the service**

```python
# apps/backend/core/services/push_service.py
"""Send agent-completion push notifications via the Expo Push API.

Best-effort: never raises into the caller (the gateway reader loop). Expo
fronts APNs/FCM, so we hold no APNs key — only an optional EXPO_ACCESS_TOKEN
bearer (injected as a container secret by infra) for send authentication.
"""

import logging
import os

import httpx

from core.repositories import user_repo

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
_TIMEOUT_S = 10.0


async def send_push_to_user(user_id: str, title: str, body: str, data: dict | None = None) -> None:
    """Deliver a push to every registered device of *user_id*. No-op if none."""
    row = await user_repo.get(user_id)
    tokens_map = (row or {}).get("push_tokens") or {}
    entries = [
        (device_id, t["token"])
        for device_id, t in tokens_map.items()
        if isinstance(t, dict) and t.get("token")
    ]
    if not entries:
        return

    messages = [
        {"to": token, "title": title, "body": body, "data": data or {}, "sound": "default"}
        for _, token in entries
    ]
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    access_token = os.environ.get("EXPO_ACCESS_TOKEN")
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            resp = await client.post(EXPO_PUSH_URL, json=messages, headers=headers)
            resp.raise_for_status()
            tickets = resp.json().get("data", [])
    except Exception:
        logger.exception("Expo push send failed for user %s", user_id)
        return

    # Prune tokens Expo reports as permanently invalid.
    for (device_id, _token), ticket in zip(entries, tickets):
        if isinstance(ticket, dict) and ticket.get("status") == "error":
            if (ticket.get("details") or {}).get("error") == "DeviceNotRegistered":
                try:
                    await user_repo.clear_push_token(user_id, device_id)
                except Exception:
                    logger.exception("Failed to prune dead token %s for user %s", device_id, user_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_push_service.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/push_service.py apps/backend/tests/unit/services/test_push_service.py
git commit -m "feat(backend): Expo Push API sender with DeviceNotRegistered prune"
```

---

## Task 4: Backend — push dispatch hook + agent-name resolver in `connection_pool`

**Files:**
- Modify: `apps/backend/core/gateway/connection_pool.py`
- Test: `apps/backend/tests/unit/gateway/test_push_dispatch.py`

**Interfaces:**
- Consumes: `push_service.send_push_to_user` (Task 3); existing `_extract_assistant_text`, `send_rpc`, `self._active_runs`, `self.owner_id`.
- Produces: `GatewayConnection._dispatch_push_notification(payload, target_member, agent_id)`, `GatewayConnection._resolve_agent_name(agent_id)`, `GatewayConnection.has_active_runs()`.

- [ ] **Step 0: Verify the `agents.list` response shape (small unknown)**

The agent-name title needs the shape of the `agents.list` RPC result. Before coding the resolver, confirm it. Read `apps/backend/routers/container_rpc.py` / any existing `agents.list` caller, or inspect a live response. The defensive parser below reads `id`/`agent_id` and `name`/`displayName`; adjust the key names in Step 3 if the real shape differs. If `agents.list` proves unavailable from within the connection, fall back to `title = "New reply"` and open a follow-up for name enrichment (do not block the rest of the task).

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/unit/gateway/test_push_dispatch.py
"""Unit tests for the run-completion push dispatch."""

import asyncio
import pytest
from unittest.mock import AsyncMock, patch

from core.gateway.connection_pool import GatewayConnection


def _make_conn(owner_id="user_owner"):
    # Construct without running __init__'s socket setup: build a bare instance
    # and set only the attributes the dispatch reads.
    conn = GatewayConnection.__new__(GatewayConnection)
    conn.owner_id = owner_id
    conn.user_id = owner_id
    conn._push_tasks = set()
    conn._agent_names = None
    conn._agent_names_at = 0.0
    conn.send_rpc = AsyncMock(return_value={"agents": [{"id": "a1", "name": "Friday"}]})
    return conn


@pytest.mark.asyncio
async def test_dispatch_pushes_to_member_with_preview():
    conn = _make_conn()
    payload = {"sessionKey": "agent:a1:user_member:s1",
               "message": {"content": [{"type": "text", "text": "here is the summary"}]}}
    sent = AsyncMock()
    with patch("core.services.push_service.send_push_to_user", sent):
        conn._dispatch_push_notification(payload, target_member="user_member", agent_id="a1")
        # drain the fire-and-forget task
        await asyncio.gather(*list(conn._push_tasks))
    sent.assert_awaited_once()
    args = sent.await_args.args
    assert args[0] == "user_member"          # audience = the member
    assert args[1] == "Friday"               # title = agent name
    assert args[2] == "here is the summary"  # body = preview
    assert args[3]["agentId"] == "a1"


@pytest.mark.asyncio
async def test_personal_session_targets_owner():
    conn = _make_conn(owner_id="user_owner")
    payload = {"sessionKey": "agent:a1:main",
               "message": {"content": [{"type": "text", "text": "done"}]}}
    sent = AsyncMock()
    with patch("core.services.push_service.send_push_to_user", sent):
        conn._dispatch_push_notification(payload, target_member=None, agent_id="a1")
        await asyncio.gather(*list(conn._push_tasks))
    assert sent.await_args.args[0] == "user_owner"


@pytest.mark.asyncio
async def test_org_owner_without_member_does_not_push():
    conn = _make_conn(owner_id="org_123")
    payload = {"sessionKey": "agent:a1:cron:c1", "message": {"content": []}}
    sent = AsyncMock()
    with patch("core.services.push_service.send_push_to_user", sent):
        conn._dispatch_push_notification(payload, target_member=None, agent_id="a1")
        await asyncio.gather(*list(conn._push_tasks))
    sent.assert_not_called()


def test_has_active_runs():
    conn = GatewayConnection.__new__(GatewayConnection)
    conn._active_runs = {}
    assert conn.has_active_runs() is False
    conn._active_runs = {"agent:a1:main": {}}
    assert conn.has_active_runs() is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/gateway/test_push_dispatch.py -v`
Expected: FAIL — `AttributeError: ... has no attribute '_dispatch_push_notification'`.

- [ ] **Step 3: Add init state, the resolver, the dispatch, and `has_active_runs`**

In `GatewayConnection.__init__`, next to `self._billing_tasks` (currently initialized at line 164), add:

```python
        self._push_tasks: set = set()
        self._agent_names: dict | None = None  # agent_id -> display name (TTL cache)
        self._agent_names_at: float = 0.0
```

Add the module constant near the other timing constants (e.g. next to `_RUN_LONG_IDLE_S = 300` at line 62):

```python
_AGENT_NAME_TTL_S = 60.0
```

Add these methods to `GatewayConnection` (near `_record_usage_from_session`, ~line 867):

```python
    def has_active_runs(self) -> bool:
        """True while any run is in flight (the teardown-guard signal)."""
        return bool(self._active_runs)

    async def _resolve_agent_name(self, agent_id: str | None) -> str:
        """Best-effort display name for *agent_id* via a TTL-cached agents.list.

        Returns "" on any failure — the push falls back to a generic title.
        """
        if not agent_id:
            return ""
        now = time.monotonic()
        if self._agent_names is not None and (now - self._agent_names_at) < _AGENT_NAME_TTL_S:
            return self._agent_names.get(agent_id, "")
        try:
            result = await self.send_rpc("agents.list", {})
            agents = result.get("agents", []) if isinstance(result, dict) else (result or [])
            names: dict = {}
            for a in agents:
                if isinstance(a, dict):
                    aid = a.get("id") or a.get("agent_id")
                    nm = a.get("name") or a.get("displayName")
                    if aid and nm:
                        names[aid] = nm
            self._agent_names = names
            self._agent_names_at = now
            return names.get(agent_id, "")
        except Exception:
            logger.warning("agents.list for push-name resolution failed", exc_info=True)
            return ""

    def _dispatch_push_notification(
        self, payload: dict, target_member: str | None, agent_id: str | None
    ) -> None:
        """Fire-and-forget push on a completed run (mirrors _record_usage_from_session).

        Audience = the member who owns the session (org), or the owner (personal,
        whose key is agent:<id>:main with no member_id). Only pushes to a Clerk
        user id; an unattributed org session (org_ owner) silently no-ops.
        """
        push_user_id = target_member or self.owner_id
        if not push_user_id or not push_user_id.startswith("user_"):
            return
        preview = self._extract_assistant_text(payload)
        if not preview:
            return
        session_key = payload.get("sessionKey")

        async def _send():
            try:
                agent_name = await self._resolve_agent_name(agent_id)
                body = preview if len(preview) <= 120 else preview[:117] + "…"
                data: dict = {}
                if agent_id:
                    data["agentId"] = agent_id
                if session_key:
                    data["sessionKey"] = session_key
                from core.services.push_service import send_push_to_user
                await send_push_to_user(push_user_id, agent_name or "New reply", body, data)
            except Exception:
                logger.exception("Push dispatch failed for user %s", push_user_id)

        task = asyncio.create_task(_send())
        self._push_tasks.add(task)
        task.add_done_callback(self._push_tasks.discard)
```

- [ ] **Step 4: Wire the hook into the `run_ended` block**

In `_handle_message`, immediately after the `run_ended` forward (the `await self._forward_to_frontends(run_ended, target_member)` at line 1395), add:

```python
                    # Push a notification for a completed reply (fire-and-forget,
                    # off the reader-loop hot path — mirrors billing at line 1204).
                    if state == "final":
                        self._dispatch_push_notification(payload, target_member, event_agent_id)
```

(It sits inside the existing `if is_true_terminal:` block, so it only fires once per genuine termination.)

- [ ] **Step 5: Add close-time drain (parity with `_billing_tasks`)**

Find `close()` where `_billing_tasks` are awaited on shutdown and add `_push_tasks` to the same gather (search `_billing_tasks` in the file; add `*self._push_tasks` alongside). If the drain uses a single set, this is a one-line addition; keep pushes from being cancelled mid-send on teardown.

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/gateway/test_push_dispatch.py -v`
Expected: PASS (4 passed).

- [ ] **Step 7: Commit**

```bash
git add apps/backend/core/gateway/connection_pool.py apps/backend/tests/unit/gateway/test_push_dispatch.py
git commit -m "feat(backend): push dispatch on run_ended (member-scoped, agent-name title)"
```

---

## Task 5: Backend — teardown guard (keep connection alive during active runs)

**Files:**
- Modify: `apps/backend/core/gateway/connection_pool.py` (`_grace_close`, ~line 2002; constants near line 37)
- Test: `apps/backend/tests/unit/gateway/test_grace_close_guard.py`

**Interfaces:**
- Consumes: `GatewayConnection.has_active_runs()` (Task 4).

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/unit/gateway/test_grace_close_guard.py
"""The grace-close teardown must not fire while a run is active."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.gateway.connection_pool import GatewayConnectionPool


def _pool_with_active_run(active: bool):
    pool = GatewayConnectionPool.__new__(GatewayConnectionPool)
    pool._frontend_connections = {}          # no clients connected
    conn = MagicMock()
    conn.has_active_runs = MagicMock(return_value=active)
    pool._connections = {"user_1": conn}
    pool.close_user = AsyncMock()
    return pool


@pytest.mark.asyncio
async def test_closes_when_no_active_run():
    pool = _pool_with_active_run(active=False)
    # Patch sleeps so the test doesn't wait real seconds.
    with patch("core.gateway.connection_pool.asyncio.sleep", AsyncMock()):
        await pool._grace_close("user_1")
    pool.close_user.assert_awaited_once_with("user_1")


@pytest.mark.asyncio
async def test_defers_while_run_active_then_closes():
    pool = _pool_with_active_run(active=True)
    # Flip to inactive after the first poll so the loop exits promptly.
    calls = {"n": 0}

    def flip():
        calls["n"] += 1
        pool._connections["user_1"].has_active_runs.return_value = calls["n"] < 1

    pool._connections["user_1"].has_active_runs.side_effect = lambda: (flip() or calls["n"] < 1)
    with patch("core.gateway.connection_pool.asyncio.sleep", AsyncMock()):
        await pool._grace_close("user_1")
    # Still closes once the run clears and no clients returned.
    pool.close_user.assert_awaited_once_with("user_1")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/gateway/test_grace_close_guard.py -v`
Expected: FAIL — current `_grace_close` closes immediately regardless of active runs (second test fails because it closes without honoring the active run, or the guard attributes don't exist).

- [ ] **Step 3: Add constants**

Near `_GRACE_PERIOD = 30` (line 37) add:

```python
_PUSH_GRACE_CEILING_S = 300  # max extra hold while a run is active (bounded by parked watchdog)
_ACTIVE_RUN_POLL_S = 5       # re-check cadence for the active-run hold
```

- [ ] **Step 4: Replace `_grace_close`**

Replace the body of `_grace_close` (lines 2002-2010) with:

```python
    async def _grace_close(self, user_id: str) -> None:
        """Wait the grace period, then close the gateway connection if still idle.

        Guard: while a run is still in flight for this owner, HOLD the connection
        open (bounded by _PUSH_GRACE_CEILING_S) so the reader loop still observes
        run_ended and can fire the completion push — even with no client attached
        (e.g. a phone that sent then closed). Clients re-appearing cancels this
        task via remove/add_frontend_connection, so the loop also exits when a
        client returns.
        """
        try:
            await asyncio.sleep(_GRACE_PERIOD)
            waited = 0.0
            while (
                not self._frontend_connections.get(user_id)
                and waited < _PUSH_GRACE_CEILING_S
            ):
                conn = self._connections.get(user_id)
                if conn is None or not conn.has_active_runs():
                    break
                await asyncio.sleep(_ACTIVE_RUN_POLL_S)
                waited += _ACTIVE_RUN_POLL_S
            if not self._frontend_connections.get(user_id):
                await self.close_user(user_id)
        except asyncio.CancelledError:
            pass
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/gateway/test_grace_close_guard.py -v`
Expected: PASS (2 passed).

- [ ] **Step 6: Run the full gateway test module to check for regressions**

Run: `cd apps/backend && uv run pytest tests/unit/gateway/ -q`
Expected: PASS (no regressions in existing connection-pool tests).

- [ ] **Step 7: Commit**

```bash
git add apps/backend/core/gateway/connection_pool.py apps/backend/tests/unit/gateway/test_grace_close_guard.py
git commit -m "feat(backend): hold container connection during active runs so closed-app push delivers"
```

---

## Task 6: Infra — `EXPO_ACCESS_TOKEN` secret

**Files:**
- Modify: `apps/infra/lib/stacks/auth-stack.ts`, `apps/infra/lib/stacks/service-stack.ts`, `apps/infra/lib/stages/lightbulb-stage.ts`, `apps/infra/lib/stages/local-stage.ts`

No new IAM (secret is under the `isol8/${env}/*` prefix already granted). This is a config task — commit as one unit; validated by `cdk synth`.

- [ ] **Step 1: Declare the secret in `auth-stack.ts`**

Add the public field next to `public readonly resendApiKey` (line 36):

```typescript
  public readonly expoAccessToken: secretsmanager.Secret;
```

Add the assignment after the `this.resendApiKey = createSecret(...)` block (after line 168):

```typescript
    // Expo access token for the Expo Push API (mobile push notifications).
    // CDK generates a placeholder; operator overwrites out-of-band with the
    // real Expo access token via `aws secretsmanager put-secret-value`. Same
    // isol8/{env}/* CMK + wildcard read as the other secrets — no extra IAM.
    this.expoAccessToken = createSecret(
      "ExpoAccessToken",
      "expo_access_token",
      { passwordLength: 48, excludePunctuation: true },
      "Expo access token for the Expo Push API. Overwritten out-of-band with the real token from expo.dev.",
    );
```

- [ ] **Step 2: Add the `SecretNames` member in `service-stack.ts`**

Alongside `resendApiKey: string;` (line 82):

```typescript
  /** Expo access-token secret name (isol8/{env}/expo_access_token). */
  expoAccessToken: string;
```

- [ ] **Step 3: Inject the secret into the backend container**

Inside the `secrets: { ... }` block, before its closing `},` (line 1353):

```typescript
        EXPO_ACCESS_TOKEN: ecs.Secret.fromSecretsManager(
          secretsmanager.Secret.fromSecretNameV2(this, "ImportExpoAccessToken", props.secretNames.expoAccessToken),
        ),
```

- [ ] **Step 4: Wire both stages**

In `lightbulb-stage.ts` (in the `secretNames: { ... }` object, after `wsInternalSecret: auth.wsInternalSecret.secretName,` line 122):

```typescript
        expoAccessToken: auth.expoAccessToken.secretName,
```

Identical addition in `local-stage.ts` (after its `wsInternalSecret:` line 123).

- [ ] **Step 5: Synthesize to verify**

Run: `cd apps/infra && pnpm cdk synth 2>&1 | tail -5`
Expected: synth succeeds (no unresolved-token / missing-property errors). Also regenerate `deploy.yml` if the repo's convention requires committing it with CDK changes (see the deploy.yml drift check).

- [ ] **Step 6: Commit**

```bash
git add apps/infra/lib/stacks/auth-stack.ts apps/infra/lib/stacks/service-stack.ts apps/infra/lib/stages/lightbulb-stage.ts apps/infra/lib/stages/local-stage.ts
git commit -m "feat(infra): EXPO_ACCESS_TOKEN secret for the Expo Push API"
```

---

## Task 7: Mobile — set up the jest-expo test runner

**Files:**
- Create: `apps/mobile/babel.config.js`, `apps/mobile/jest.config.js`, `apps/mobile/jest-setup.ts`
- Modify: `apps/mobile/package.json` (devDeps + `test` script)
- Create: `apps/mobile/src/lib/__tests__/smoke.test.ts`
- Modify: `.github/workflows/mobile-ci.yml`

**Interfaces:** Produces a working `pnpm --filter @lightbulb/mobile run test` (jest-expo). Later mobile tasks add tests against it.

- [ ] **Step 1: Install the SDK-matched dev deps**

Run (from repo root; `expo install` pins SDK-57-compatible versions):
```bash
cd apps/mobile && npx expo install --dev jest-expo jest @testing-library/react-native react-test-renderer && cd -
```
Then pin `react-test-renderer` to the React version if `expo install` didn't: it must equal `react` (`19.2.3`). Verify in `apps/mobile/package.json` devDependencies.

- [ ] **Step 2: Create `babel.config.js`** (jest-expo transforms via babel; Expo apps otherwise rely on metro's internal preset)

```javascript
// apps/mobile/babel.config.js
module.exports = function (api) {
  api.cache(true);
  return { presets: ["babel-preset-expo"] };
};
```

- [ ] **Step 3: Create `jest.config.js`**

```javascript
// apps/mobile/jest.config.js
module.exports = {
  preset: "jest-expo",
  setupFilesAfterEnv: ["<rootDir>/jest-setup.ts"],
  transformIgnorePatterns: [
    "node_modules/(?!((jest-)?react-native|@react-native(-community)?|expo(nent)?|@expo(nent)?/.*|@expo-google-fonts/.*|react-navigation|@react-navigation/.*|@unimodules/.*|unimodules|sentry-expo|native-base|react-native-svg|@clerk/.*))",
  ],
};
```

- [ ] **Step 4: Create `jest-setup.ts`** (mock the native modules feature tests touch)

```typescript
// apps/mobile/jest-setup.ts
jest.mock("expo-secure-store", () => ({
  getItemAsync: jest.fn(async () => null),
  setItemAsync: jest.fn(async () => {}),
  deleteItemAsync: jest.fn(async () => {}),
}));

jest.mock("expo-notifications", () => ({
  getPermissionsAsync: jest.fn(async () => ({ status: "granted" })),
  requestPermissionsAsync: jest.fn(async () => ({ status: "granted" })),
  getExpoPushTokenAsync: jest.fn(async () => ({ data: "ExponentPushToken[test]" })),
  setNotificationHandler: jest.fn(),
  addNotificationResponseReceivedListener: jest.fn(() => ({ remove: jest.fn() })),
}));

jest.mock("expo-device", () => ({ isDevice: true }));
```

- [ ] **Step 5: Add the `test` script** to `apps/mobile/package.json` scripts:

```json
    "test": "jest --ci"
```

- [ ] **Step 6: Add a smoke test to prove the harness**

```typescript
// apps/mobile/src/lib/__tests__/smoke.test.ts
describe("jest-expo harness", () => {
  it("runs a trivial assertion", () => {
    expect(1 + 1).toBe(2);
  });
});
```

- [ ] **Step 7: Run it**

Run: `pnpm --filter @lightbulb/mobile run test`
Expected: PASS (1 test). If jest-expo fights the SDK-57/React-19 stack (transform/renderer errors), resolve versions with `npx expo install --dev --check`; if component rendering (not pure logic) is the blocker, keep this pure-logic harness green and note component tests as manual for now.

- [ ] **Step 8: Wire CI** — in `.github/workflows/mobile-ci.yml`, in the `check-and-bundle` job, add after the `Mobile lint` step (before `Mobile iOS bundle`):

```yaml
      - name: Mobile tests
        run: pnpm --filter @lightbulb/mobile run test
```

- [ ] **Step 9: Commit**

```bash
git add apps/mobile/babel.config.js apps/mobile/jest.config.js apps/mobile/jest-setup.ts apps/mobile/package.json apps/mobile/src/lib/__tests__/smoke.test.ts .github/workflows/mobile-ci.yml pnpm-lock.yaml
git commit -m "test(mobile): set up jest-expo + RNTL runner and CI step"
```

---

## Task 8: Mobile — expo-notifications dependency + iOS config

**Files:**
- Modify: `apps/mobile/package.json`, `apps/mobile/app.json`

- [ ] **Step 1: Install deps** (SDK-matched)

```bash
cd apps/mobile && npx expo install expo-notifications expo-device && cd -
```

- [ ] **Step 2: Add the config plugin + entitlement in `app.json`**

Add `expo-notifications` to `plugins` (after the `expo-splash-screen` entry):

```json
      "expo-notifications"
```

Add the APNs entitlement inside `ios` (alongside `infoPlist`):

```json
      "entitlements": {
        "aps-environment": "production"
      }
```

(EAS maps `production` to the correct APNs environment per build profile.)

- [ ] **Step 3: Verify the Metro bundle still resolves**

Run: `pnpm --filter @lightbulb/mobile run export:ios 2>&1 | tail -5`
Expected: export succeeds (expo-notifications resolves).

- [ ] **Step 4: Commit**

```bash
git add apps/mobile/package.json apps/mobile/app.json pnpm-lock.yaml
git commit -m "feat(mobile): add expo-notifications + aps-environment entitlement"
```

---

## Task 9: Mobile — deep-link parser

**Files:**
- Create: `apps/mobile/src/lib/deepLink.ts`, `apps/mobile/src/lib/deepLink.test.ts`

**Interfaces:** Produces `parseAgentDeepLink(url: string): { agentId: string } | null`.

- [ ] **Step 1: Write the failing test**

```typescript
// apps/mobile/src/lib/deepLink.test.ts
import { parseAgentDeepLink } from "./deepLink";

describe("parseAgentDeepLink", () => {
  it("parses an agent deep link", () => {
    expect(parseAgentDeepLink("lightbulbai://agent/agent_123")).toEqual({ agentId: "agent_123" });
  });
  it("parses with a query string", () => {
    expect(parseAgentDeepLink("lightbulbai://agent/agent_123?session=s1")).toEqual({ agentId: "agent_123" });
  });
  it("returns null for the oauth callback", () => {
    expect(parseAgentDeepLink("lightbulbai://oauth-native-callback")).toBeNull();
  });
  it("returns null for junk", () => {
    expect(parseAgentDeepLink("https://example.com")).toBeNull();
    expect(parseAgentDeepLink("lightbulbai://agent/")).toBeNull();
  });
});
```

- [ ] **Step 2: Run it (fails)**

Run: `pnpm --filter @lightbulb/mobile run test -- deepLink`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```typescript
// apps/mobile/src/lib/deepLink.ts
/**
 * Parse an `lightbulbai://agent/<agentId>` deep link (optionally with a query string).
 * Returns null for any other scheme/host (e.g. the oauth-native-callback link).
 */
export function parseAgentDeepLink(url: string): { agentId: string } | null {
  const match = /^lightbulbai:\/\/agent\/([^/?#]+)/.exec(url);
  if (!match) return null;
  const agentId = decodeURIComponent(match[1]);
  return agentId ? { agentId } : null;
}
```

- [ ] **Step 4: Run it (passes)**

Run: `pnpm --filter @lightbulb/mobile run test -- deepLink`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/mobile/src/lib/deepLink.ts apps/mobile/src/lib/deepLink.test.ts
git commit -m "feat(mobile): lightbulbai://agent deep-link parser"
```

---

## Task 10: Mobile — foreground-suppression handler

**Files:**
- Create: `apps/mobile/src/lib/notifications.ts`, `apps/mobile/src/lib/notifications.test.ts`

**Interfaces:** Produces `shouldSuppressBanner(currentAgentId: string | null, notifAgentId: string | undefined): boolean` and `configureNotificationHandler(getCurrentAgentId: () => string | null): void`.

- [ ] **Step 1: Write the failing test** (pure decision only)

```typescript
// apps/mobile/src/lib/notifications.test.ts
import { shouldSuppressBanner } from "./notifications";

describe("shouldSuppressBanner", () => {
  it("suppresses when viewing the same agent's chat", () => {
    expect(shouldSuppressBanner("agent_1", "agent_1")).toBe(true);
  });
  it("shows when on a different agent", () => {
    expect(shouldSuppressBanner("agent_2", "agent_1")).toBe(false);
  });
  it("shows when not on any agent chat", () => {
    expect(shouldSuppressBanner(null, "agent_1")).toBe(false);
  });
  it("shows when the notif carries no agent id", () => {
    expect(shouldSuppressBanner("agent_1", undefined)).toBe(false);
  });
});
```

- [ ] **Step 2: Run it (fails)**

Run: `pnpm --filter @lightbulb/mobile run test -- notifications`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```typescript
// apps/mobile/src/lib/notifications.ts
import * as Notifications from "expo-notifications";

/**
 * Suppress the in-app banner only when the app is foregrounded on the exact
 * agent chat the notification is about (the reply already streamed live).
 */
export function shouldSuppressBanner(
  currentAgentId: string | null,
  notifAgentId: string | undefined,
): boolean {
  return !!notifAgentId && currentAgentId === notifAgentId;
}

/**
 * Install the notification handler. `getCurrentAgentId` returns the agentId of
 * the chat currently on screen (or null), so the handler can suppress the
 * banner for the active session while showing it everywhere else.
 */
export function configureNotificationHandler(getCurrentAgentId: () => string | null): void {
  Notifications.setNotificationHandler({
    handleNotification: async (notification) => {
      const notifAgentId = notification.request.content.data?.agentId as string | undefined;
      const suppress = shouldSuppressBanner(getCurrentAgentId(), notifAgentId);
      return {
        shouldShowBanner: !suppress,
        shouldShowList: true,
        shouldPlaySound: !suppress,
        shouldSetBadge: false,
      };
    },
  });
}
```

- [ ] **Step 4: Run it (passes)**

Run: `pnpm --filter @lightbulb/mobile run test -- notifications`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/mobile/src/lib/notifications.ts apps/mobile/src/lib/notifications.test.ts
git commit -m "feat(mobile): notification handler with foreground suppression"
```

---

## Task 11: Mobile — `<PushRegistration/>` component + mount + tap routing

**Files:**
- Create: `apps/mobile/src/components/push/PushRegistration.tsx`
- Modify: `apps/mobile/src/app/(app)/_layout.tsx` (mount inside `<GatewayProvider>`)
- Test: `apps/mobile/src/components/push/PushRegistration.test.tsx`

**Interfaces:** Consumes `parseAgentDeepLink` (Task 9), `configureNotificationHandler` (Task 10), `makeAdapter`/`createApiClient`, `POST /devices` (Task 2).

- [ ] **Step 1: Write the failing test** (registration POST + token cache)

```tsx
// apps/mobile/src/components/push/PushRegistration.test.tsx
import { render, waitFor } from "@testing-library/react-native";

const post = jest.fn(async () => ({}));
jest.mock("@lightbulb/shared", () => ({
  createApiClient: () => ({ post }),
}));
jest.mock("../../lib/adapter", () => ({ makeAdapter: () => ({}) }));
jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ getToken: async () => "tok" }),
}));
jest.mock("expo-constants", () => ({
  expoConfig: { extra: { eas: { projectId: "proj_1" } } },
}));
jest.mock("expo-router", () => ({ router: { push: jest.fn() } }));

import { PushRegistration } from "./PushRegistration";

it("requests a token and POSTs it once", async () => {
  render(<PushRegistration getCurrentAgentId={() => null} />);
  await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
  const [path, body] = post.mock.calls[0];
  expect(path).toBe("/devices");
  expect(body.token).toBe("ExponentPushToken[test]");
  expect(body.platform).toBe("ios");
  expect(typeof body.deviceId).toBe("string");
});
```

- [ ] **Step 2: Run it (fails)**

Run: `pnpm --filter @lightbulb/mobile run test -- PushRegistration`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the component**

```tsx
// apps/mobile/src/components/push/PushRegistration.tsx
import { useAuth } from "@clerk/clerk-expo";
import { createApiClient } from "@lightbulb/shared";
import Constants from "expo-constants";
import { randomUUID } from "expo-crypto";
import * as Notifications from "expo-notifications";
import * as Device from "expo-device";
import { router } from "expo-router";
import * as SecureStore from "expo-secure-store";
import { useEffect, useMemo, useRef } from "react";

import { parseAgentDeepLink } from "../../lib/deepLink";
import { configureNotificationHandler } from "../../lib/notifications";
import { makeAdapter } from "../../lib/adapter";

const DEVICE_ID_KEY = "isol8.push.deviceId"; // storage keys ride the bundle-id change, not this rebrand PR
const LAST_TOKEN_KEY = "isol8.push.lastToken";

async function getDeviceId(): Promise<string> {
  const existing = await SecureStore.getItemAsync(DEVICE_ID_KEY).catch(() => null);
  if (existing) return existing;
  const id = randomUUID();
  await SecureStore.setItemAsync(DEVICE_ID_KEY, id).catch(() => {});
  return id;
}

/**
 * Mounted inside the signed-in subtree (GatewayProvider). Requests notification
 * permission, registers the Expo push token with the backend (once per change),
 * installs the foreground-suppression handler, and routes notification taps to
 * the right agent chat.
 */
export function PushRegistration({
  getCurrentAgentId,
}: {
  getCurrentAgentId: () => string | null;
}) {
  const { getToken } = useAuth();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const api = useMemo(() => createApiClient(makeAdapter(() => getTokenRef.current())), []);

  // Register token (permission → getExpoPushToken → POST /devices, de-duped).
  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!Device.isDevice) return; // simulators can't get a real APNs token
      const settings = await Notifications.getPermissionsAsync();
      let status = settings.status;
      if (status !== "granted") {
        status = (await Notifications.requestPermissionsAsync()).status;
      }
      if (status !== "granted" || cancelled) return;

      const projectId = Constants.expoConfig?.extra?.eas?.projectId as string | undefined;
      const { data: token } = await Notifications.getExpoPushTokenAsync(
        projectId ? { projectId } : undefined,
      );
      if (cancelled || !token) return;

      const last = await SecureStore.getItemAsync(LAST_TOKEN_KEY).catch(() => null);
      if (last === token) return; // already registered this exact token
      const deviceId = await getDeviceId();
      await api.post("/devices", { token, platform: "ios", deviceId });
      await SecureStore.setItemAsync(LAST_TOKEN_KEY, token).catch(() => {});
    })().catch(() => {
      // Best-effort: never crash the app over push registration.
    });
    return () => {
      cancelled = true;
    };
  }, [api]);

  // Foreground suppression + tap routing.
  useEffect(() => {
    configureNotificationHandler(getCurrentAgentId);
    const sub = Notifications.addNotificationResponseReceivedListener((response) => {
      const data = response.notification.request.content.data as
        | { agentId?: string }
        | undefined;
      const agentId = data?.agentId;
      if (agentId) {
        router.push(`/agent/${agentId}`);
      } else {
        // Fallback: a deep-link URL may arrive via the notification data.
        const url = (data as { url?: string } | undefined)?.url;
        const parsed = url ? parseAgentDeepLink(url) : null;
        if (parsed) router.push(`/agent/${parsed.agentId}`);
      }
    });
    return () => sub.remove();
  }, [getCurrentAgentId]);

  return null;
}
```

- [ ] **Step 4: Mount it** in `apps/mobile/src/app/(app)/_layout.tsx` — add the import and render it inside `<GatewayProvider>`, before `<ProvisionGate>`:

```tsx
import { PushRegistration } from "../../components/push/PushRegistration";
```

```tsx
    <GatewayProvider>
      <PushRegistration getCurrentAgentId={() => null} />
      <ProvisionGate>
```

(For v1, `getCurrentAgentId` returns `null` — the handler shows all banners. A follow-up can thread the active-agent id from the chat screen via a small context to enable true active-session suppression; the pure `shouldSuppressBanner` logic is already in place.)

- [ ] **Step 5: Run the component test**

Run: `pnpm --filter @lightbulb/mobile run test -- PushRegistration`
Expected: PASS. If RNTL rendering fights the stack, keep the pure-logic tests (Tasks 9/10) green and verify this component on-device instead (note it in the PR).

- [ ] **Step 6: Typecheck + bundle**

Run: `pnpm --filter @lightbulb/mobile run typecheck && pnpm --filter @lightbulb/mobile run export:ios 2>&1 | tail -3`
Expected: both succeed.

- [ ] **Step 7: Commit**

```bash
git add apps/mobile/src/components/push/PushRegistration.tsx "apps/mobile/src/app/(app)/_layout.tsx" apps/mobile/src/components/push/PushRegistration.test.tsx
git commit -m "feat(mobile): PushRegistration — permission, token registration, tap routing"
```

---

## Task 12: Docs + on-device exit gate

**Files:**
- Modify: `apps/mobile/README.md`, `docs/superpowers/plans/2026-07-05-mobile-app-expo-ios.md`

- [ ] **Step 1: Update the mobile README** — add a "Push notifications" section: the new native-build requirement (expo-notifications + entitlement = a store/TestFlight build, not OTA), the `jest-expo` test runner (`pnpm --filter @lightbulb/mobile test`), and that the Expo access token secret is set out-of-band in Secrets Manager.

- [ ] **Step 2: Reconcile the Phase 7 plan** — in `docs/superpowers/plans/2026-07-05-mobile-app-expo-ios.md`, mark Phase 7 Task 1/2 as specced/implemented here; record the Expo-Push-API (not bare APNs) decision and the `users`-row token store.

- [ ] **Step 3: Commit**

```bash
git add apps/mobile/README.md docs/superpowers/plans/2026-07-05-mobile-app-expo-ios.md
git commit -m "docs(mobile): push notifications setup + Phase 7 reconciliation"
```

- [ ] **Step 4: On-device exit gate (manual)** — after an EAS `preview` build with the real Expo access token set:
  1. Sign in on a physical device; grant notification permission.
  2. Send a message to an agent; background the app; confirm a push arrives with the agent name + reply preview.
  3. Tap it → lands on that agent's chat.
  4. Repeat with the desktop/web closed (phone-only) to exercise the teardown guard.
  5. With the app foregrounded on that agent's chat, confirm no duplicate banner (once `getCurrentAgentId` is threaded).

---

## Self-Review

**Spec coverage:** device-token store (T1) ✓; registration endpoint (T2) ✓; Expo sender + prune (T3) ✓; `run_ended` dispatch, member-scoped, agent-name title, reply preview (T4) ✓; teardown guard (T5) ✓; infra secret (T6) ✓; deep-link (T9), suppression (T10), registration + mount + tap routing (T11) ✓; docs + on-device gate (T12) ✓. Test runner (T7) added per user request; expo-notifications config (T8) ✓.

**Known deviations to flag at handoff:**
1. **Agent-name title** depends on the `agents.list` RPC shape (T4 Step 0 verifies it; defensive fallback to `"New reply"`).
2. **Active-session suppression** ships with `getCurrentAgentId → null` in v1 (shows all banners); the pure decision + handler are in place, threading the active agent id is a small follow-up.
3. **Endpoint path** is `/api/v1/devices` (dedicated router) — the spec wrote `POST /api/v1/devices`; matches.

**Type consistency:** `set_push_token(user_id, device_id, entry)` / `clear_push_token(user_id, device_id)` used identically in T1/T2/T3; `push_tokens` map attribute + entry `{token, platform, updatedAt}` consistent across T1/T2/T3; `_dispatch_push_notification(payload, target_member, agent_id)` and `has_active_runs()` defined in T4 and consumed in T4/T5; `parseAgentDeepLink`/`shouldSuppressBanner`/`configureNotificationHandler` consistent across T9/T10/T11.
