# BYO OAuth App (Integrations) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an owner connect Google (gog) using their own OAuth app, so a non-Bedrock / ChatGPT-OAuth owner can use Google integrations the way direct-OpenClaw users do.

**Architecture:** Extend the existing integrations machinery — no new rail. A per-owner BYO app config (`client_id`/`client_secret`) lives in Secrets Manager under the already-granted `integrations/` prefix; the OAuth dance's single resolution point `_oauth_client` reads the owner's app when present, else Isol8's; and the **four** sites that gate gog (`internal` + `requires_bedrock`, applied at grant/list/callback) carve out when the owner has a stored + acknowledged BYO app. Generic mechanism via a registry flag; only `google` is enabled.

**Tech Stack:** Python 3.12, FastAPI, boto3 Secrets Manager, Pydantic, pytest + httpx (backend); Next.js 16 / React 19 / vitest (frontend).

## Global Constraints

- **No marketplace coupling** — must not change marketplace deploy/hire. `test_catalog_service` + `test_marketplace_listings` must stay green untouched. (Verified invariant.)
- **Managed flow untouched** — with no BYO app configured, every gate and the "Switch to Bedrock" upsell behave exactly as today.
- **Generic mechanism, Google-only enabled** — `allow_byo_app` is a per-provider flag; set it on `google` only.
- **Secret path stays under `isol8/<env>/integrations/*`** — the only RW-granted prefix (`service-stack.ts:453-455`). Do NOT use `oauth-apps/*` (read-only wildcard → AccessDenied on write).
- **No backend token refresh for google** — `refresh_owner: actor`; the in-container gog CLI self-refreshes from the BYO-seeded keyring. Do not add a refresh path.
- **Backend env guard for tests:** every backend test module sets `os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")` before importing app code (match existing tests).
- **Frontend tests:** run TARGETED single-file vitest only (`pnpm test -- --run <file>`); never the full suite (RAM-heavy). Frontend CI has no vitest — run locally.
- **Commit message trailer:** `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

## File Structure

**Backend — Create:**
- `apps/backend/core/services/credentials/oauth_app_store.py` — per-owner BYO OAuth-app config store (put/get/delete), Secrets Manager under the `integrations/` prefix.

**Backend — Modify:**
- `apps/backend/core/services/credentials/registry.py` — add `allow_byo_app` + `byo_setup_url` to the `Provider` model.
- `apps/backend/core/services/credentials/providers.yaml` — set the flags on `google`.
- `apps/backend/core/services/credentials/oauth_dance.py` — thread `owner_id` into `_oauth_client`; resolve BYO-vs-Isol8 app.
- `apps/backend/routers/connections.py` — `_byo_active` predicate; carve the 4 gate sites; `PUT`/`DELETE /connections/{provider}/byo-app`; expose `byo_capable` / `byo_configured` in the list payload.

**Frontend — Modify:**
- `apps/frontend/src/components/.../ConnectButton.tsx` — a BYO branch + paste/setup affordance + acknowledgment.
- `apps/frontend/src/hooks/useConnections.ts` (+ the `Connection` type) — surface `byo_capable` / `byo_configured`.

**Tests — Create/Modify:**
- `apps/backend/tests/unit/services/credentials/test_oauth_app_store.py`
- `apps/backend/tests/unit/services/credentials/test_oauth_dance.py` (add cases)
- `apps/backend/tests/unit/routers/test_connections.py` (add cases)
- targeted frontend vitest for the ConnectButton BYO branch.

---

## Task 1: Registry flags

**Files:**
- Modify: `apps/backend/core/services/credentials/registry.py` (the `Provider` model)
- Modify: `apps/backend/core/services/credentials/providers.yaml` (`google`)
- Test: `apps/backend/tests/unit/services/credentials/test_registry_byo.py` (create)

**Interfaces:**
- Produces: `Provider.allow_byo_app: bool` (default `False`), `Provider.byo_setup_url: str | None` (default `None`).

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/services/credentials/test_registry_byo.py`:

```python
import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services.credentials import registry  # noqa: E402


def test_google_allows_byo_app():
    google = registry.get_provider("google")
    assert google.allow_byo_app is True
    assert google.byo_setup_url  # a help link is set


def test_byo_defaults_false_for_other_providers():
    assert registry.get_provider("stripe").allow_byo_app is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/credentials/test_registry_byo.py -v --no-cov`
Expected: FAIL — `Provider` has no attribute `allow_byo_app` (or validation error).

- [ ] **Step 3: Add the fields to the Provider model**

In `registry.py`, in the `Provider(BaseModel)` class (near `internal` / `requires_bedrock`), add:

```python
    # BYO OAuth app: when True, an owner may connect this provider with their
    # OWN OAuth app (client_id/secret), which lifts the internal + requires_bedrock
    # gates for that owner. Generic flag; only enabled for google today.
    allow_byo_app: bool = False
    # Help link shown in the BYO setup wizard (e.g. the Google Cloud OAuth guide).
    byo_setup_url: str | None = None
```

- [ ] **Step 4: Set the flags on google**

In `providers.yaml`, under the `google:` entry, add:

```yaml
  allow_byo_app: true
  byo_setup_url: "https://console.cloud.google.com/apis/credentials"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/credentials/test_registry_byo.py -v --no-cov`
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/credentials/registry.py \
        apps/backend/core/services/credentials/providers.yaml \
        apps/backend/tests/unit/services/credentials/test_registry_byo.py
git commit -m "feat(integrations): add allow_byo_app + byo_setup_url provider flags"
```

---

## Task 2: BYO OAuth-app secret store

**Files:**
- Create: `apps/backend/core/services/credentials/oauth_app_store.py`
- Test: `apps/backend/tests/unit/services/credentials/test_oauth_app_store.py` (create)

**Interfaces:**
- Produces:
  - `oauth_app_store._secret_name(owner_id: str, provider: str) -> str` → `isol8/<env>/integrations/<owner>/oauth-app/<provider>`
  - `async oauth_app_store.put(owner_id, provider, *, client_id, client_secret, acknowledged_at) -> str`
  - `async oauth_app_store.get(owner_id, provider) -> dict | None` (keys: `client_id`, `client_secret`, `acknowledged_at`)
  - `async oauth_app_store.delete(owner_id, provider) -> None`

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/services/credentials/test_oauth_app_store.py`:

```python
import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import pytest  # noqa: E402

from core.services.credentials import oauth_app_store  # noqa: E402


def test_secret_name_under_integrations_prefix():
    name = oauth_app_store._secret_name("user_1", "google")
    # MUST be under the RW-granted integrations/ prefix (service-stack.ts:453-455).
    assert "/integrations/user_1/oauth-app/google" in name


@pytest.mark.asyncio
@patch("core.services.credentials.oauth_app_store._client")
@patch("core.services.credentials.oauth_app_store.create_or_update_secret")
async def test_put_then_get_round_trips(mock_cou, mock_client):
    sm = MagicMock()
    mock_client.return_value = sm
    sm.describe_secret.return_value = {"ARN": "arn:aws:...:secret"}
    await oauth_app_store.put(
        "user_1", "google", client_id="cid", client_secret="csec", acknowledged_at="2026-06-18T00:00:00Z"
    )
    # create_or_update_secret received a JSON payload with the three fields.
    payload = mock_cou.call_args.args[2]
    import json

    data = json.loads(payload)
    assert data == {"client_id": "cid", "client_secret": "csec", "acknowledged_at": "2026-06-18T00:00:00Z"}

    sm.get_secret_value.return_value = {"SecretString": payload}
    got = await oauth_app_store.get("user_1", "google")
    assert got["client_id"] == "cid"


@pytest.mark.asyncio
@patch("core.services.credentials.oauth_app_store._client")
async def test_get_missing_returns_none(mock_client):
    sm = MagicMock()
    mock_client.return_value = sm
    sm.exceptions.ResourceNotFoundException = Exception
    sm.get_secret_value.side_effect = sm.exceptions.ResourceNotFoundException()
    assert await oauth_app_store.get("user_1", "google") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/credentials/test_oauth_app_store.py -v --no-cov`
Expected: FAIL — module `oauth_app_store` does not exist.

- [ ] **Step 3: Implement the store**

Create `apps/backend/core/services/credentials/oauth_app_store.py` (mirrors `integration_secret_store.py`):

```python
"""Per-owner BYO OAuth-app config (client_id/secret) in AWS Secrets Manager.

Stored at ``isol8/<env>/integrations/<owner>/oauth-app/<provider>`` — under the
``integrations/`` prefix the backend task role has READ-WRITE on
(``service-stack.ts:453-455``). NOT ``oauth-apps/*`` (that wildcard is read-only
and a write would AccessDenied).

This holds ONLY the app config, never a token — for google the token lives in
the in-container gog keyring (``refresh_owner: actor``); ``_oauth_client``
re-reads this app config on every connect/reconnect.
"""

from __future__ import annotations

import json
from typing import Any

import boto3

from core.config import settings
from core.dynamodb import run_in_thread
from core.secrets_manager import create_or_update_secret


def _secret_name(owner_id: str, provider: str) -> str:
    env = settings.ENVIRONMENT or "dev"
    return f"isol8/{env}/integrations/{owner_id}/oauth-app/{provider}"


def _client():
    return boto3.client("secretsmanager", region_name=settings.AWS_REGION)


async def put(owner_id: str, provider: str, *, client_id: str, client_secret: str, acknowledged_at: str) -> str:
    name = _secret_name(owner_id, provider)
    payload = json.dumps(
        {"client_id": client_id, "client_secret": client_secret, "acknowledged_at": acknowledged_at}
    )

    def _put() -> str:
        sm = _client()
        create_or_update_secret(sm, name, payload)
        return sm.describe_secret(SecretId=name)["ARN"]

    return await run_in_thread(_put)


async def get(owner_id: str, provider: str) -> dict[str, Any] | None:
    name = _secret_name(owner_id, provider)

    def _get() -> dict[str, Any] | None:
        sm = _client()
        try:
            resp = sm.get_secret_value(SecretId=name)
        except sm.exceptions.ResourceNotFoundException:
            return None
        return json.loads(resp["SecretString"])

    return await run_in_thread(_get)


async def delete(owner_id: str, provider: str) -> None:
    name = _secret_name(owner_id, provider)

    def _delete() -> None:
        sm = _client()
        try:
            sm.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)
        except sm.exceptions.ResourceNotFoundException:
            pass

    await run_in_thread(_delete)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/credentials/test_oauth_app_store.py -v --no-cov`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/credentials/oauth_app_store.py \
        apps/backend/tests/unit/services/credentials/test_oauth_app_store.py
git commit -m "feat(integrations): per-owner BYO OAuth-app secret store"
```

---

## Task 3: Dance resolves the owner's app

**Files:**
- Modify: `apps/backend/core/services/credentials/oauth_dance.py`
- Test: `apps/backend/tests/unit/services/credentials/test_oauth_dance.py` (create or extend)

**Interfaces:**
- Consumes: `oauth_app_store.get(owner_id, provider)` (Task 2).
- Produces: `async _oauth_client(owner_id: str, provider_key: str) -> tuple[str, str]` — returns the owner's BYO `(client_id, client_secret)` when stored, else Isol8's app from Secrets Manager. `start(owner_id, provider_key, *, redirect_uri)` and `exchange(state, code, *, redirect_uri)` use it.

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/services/credentials/test_oauth_dance.py`:

```python
import os
from unittest.mock import AsyncMock, patch

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import pytest  # noqa: E402

from core.services.credentials import oauth_dance  # noqa: E402


@pytest.mark.asyncio
@patch("core.services.credentials.oauth_dance.oauth_app_store.get", new_callable=AsyncMock)
@patch("core.services.credentials.oauth_dance._isol8_oauth_client", new_callable=AsyncMock)
async def test_oauth_client_prefers_owner_byo_app(mock_isol8, mock_byo_get):
    mock_byo_get.return_value = {"client_id": "owner_cid", "client_secret": "owner_sec"}
    cid, sec = await oauth_dance._oauth_client("user_1", "google")
    assert (cid, sec) == ("owner_cid", "owner_sec")
    mock_isol8.assert_not_called()


@pytest.mark.asyncio
@patch("core.services.credentials.oauth_dance.oauth_app_store.get", new_callable=AsyncMock)
@patch("core.services.credentials.oauth_dance._isol8_oauth_client", new_callable=AsyncMock)
async def test_oauth_client_falls_back_to_isol8(mock_isol8, mock_byo_get):
    mock_byo_get.return_value = None
    mock_isol8.return_value = ("isol8_cid", "isol8_sec")
    cid, sec = await oauth_dance._oauth_client("user_1", "google")
    assert (cid, sec) == ("isol8_cid", "isol8_sec")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/credentials/test_oauth_dance.py -v --no-cov`
Expected: FAIL — `_isol8_oauth_client` doesn't exist / `_oauth_client` takes one arg.

- [ ] **Step 3: Rename the current resolver + add the BYO-aware resolver**

In `oauth_dance.py`, add the import at top with the others:

```python
from core.services.credentials import oauth_app_store
```

Rename the existing `_oauth_client(provider_key)` body to `_isol8_oauth_client(provider_key)` (the Secrets Manager fetch — unchanged), and add the new resolver:

```python
async def _oauth_client(owner_id: str, provider_key: str) -> tuple[str, str]:
    """The OAuth app to use for this owner: their BYO app if configured, else
    Isol8's shared app. The single resolution point — start/exchange both call
    it, so refresh-by-stored-creds is automatic on the actor (gog) side."""
    byo = await oauth_app_store.get(owner_id, provider_key)
    if byo:
        return byo["client_id"], byo["client_secret"]
    return await _isol8_oauth_client(provider_key)
```

- [ ] **Step 4: Thread owner_id at both call sites**

In `start(owner_id, provider_key, *, redirect_uri)` change `client_id, _ = await _oauth_client(provider_key)` to:

```python
    client_id, _ = await _oauth_client(owner_id, provider_key)
```

In `exchange(...)`, `rec` carries `owner_id`; change `client_id, client_secret = await _oauth_client(rec["provider"])` to:

```python
    client_id, client_secret = await _oauth_client(rec["owner_id"], rec["provider"])
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/credentials/test_oauth_dance.py -v --no-cov`
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/credentials/oauth_dance.py \
        apps/backend/tests/unit/services/credentials/test_oauth_dance.py
git commit -m "feat(integrations): dance resolves owner BYO app, else Isol8's"
```

---

## Task 4: `_byo_active` carve predicate

**Files:**
- Modify: `apps/backend/routers/connections.py`
- Test: `apps/backend/tests/unit/routers/test_connections.py` (extend)

**Interfaces:**
- Consumes: `registry.get_provider(...).allow_byo_app` (Task 1), `oauth_app_store.get` (Task 2).
- Produces: `async _byo_active(owner_id: str, provider_key: str) -> bool` — True iff the provider allows BYO AND the owner has a stored BYO app with a truthy `acknowledged_at`.

- [ ] **Step 1: Write the failing test**

Append to `apps/backend/tests/unit/routers/test_connections.py` (match its existing import header):

```python
@pytest.mark.asyncio
@patch("routers.connections.oauth_app_store.get", new_callable=AsyncMock)
async def test_byo_active_true_when_allowed_and_acknowledged(mock_get):
    from routers import connections

    mock_get.return_value = {"client_id": "x", "client_secret": "y", "acknowledged_at": "2026-06-18T00:00:00Z"}
    assert await connections._byo_active("user_1", "google") is True


@pytest.mark.asyncio
@patch("routers.connections.oauth_app_store.get", new_callable=AsyncMock)
async def test_byo_active_false_without_app(mock_get):
    from routers import connections

    mock_get.return_value = None
    assert await connections._byo_active("user_1", "google") is False


@pytest.mark.asyncio
@patch("routers.connections.oauth_app_store.get", new_callable=AsyncMock)
async def test_byo_active_false_for_non_byo_provider(mock_get):
    from routers import connections

    mock_get.return_value = {"client_id": "x", "client_secret": "y", "acknowledged_at": "t"}
    # stripe has allow_byo_app False → predicate short-circuits, never True
    assert await connections._byo_active("user_1", "stripe") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_connections.py -v --no-cov -k byo_active`
Expected: FAIL — `_byo_active` not defined.

- [ ] **Step 3: Implement the predicate**

In `connections.py`, add the import (with the credentials imports) and the predicate:

```python
from core.services.credentials import oauth_app_store


async def _byo_active(owner_id: str, provider_key: str) -> bool:
    """True when this owner may use their OWN OAuth app for the provider — the
    single condition under which the internal + requires_bedrock gates lift."""
    prov = registry.all_providers().get(provider_key)
    if not prov or not prov.allow_byo_app:
        return False
    app = await oauth_app_store.get(owner_id, provider_key)
    return bool(app and app.get("acknowledged_at"))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_connections.py -v --no-cov -k byo_active`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/routers/connections.py apps/backend/tests/unit/routers/test_connections.py
git commit -m "feat(integrations): _byo_active carve predicate"
```

---

## Task 5: Carve the four gate sites

**Files:**
- Modify: `apps/backend/routers/connections.py` (the 4 sites — confirm exact lines by reading the file; the spec lists `:88`, `:131`, `:209`, `:382`)
- Test: `apps/backend/tests/unit/routers/test_connections.py` (extend)

**Interfaces:**
- Consumes: `_byo_active` (Task 4).

> Before editing, read `connections.py` and confirm each site. The four are:
> (1) `_require_grantable_provider` (`internal` 404), (2) `_require_bedrock_for`
> (403 upsell), (3) the LIST/catalog filter (`if allowlisted or not v.internal or connected...`),
> (4) the OAuth-callback bedrock re-check (`?error=requires_bedrock` redirect).

- [ ] **Step 1: Write the failing tests**

Append to `test_connections.py` (mock `_byo_active` so each site is exercised in isolation; match the file's existing client/auth fixtures):

```python
@pytest.mark.asyncio
@patch("routers.connections._byo_active", new_callable=AsyncMock)
async def test_list_includes_google_for_byo_owner(mock_byo, client, auth_override):
    """The LIST filter must include an internal provider once BYO is active —
    else the UI never renders a Connect row (the dead-code trap)."""
    mock_byo.return_value = True
    with auth_override("user_1", "u@example.com"):
        resp = client.get("/api/v1/connections")
    assert resp.status_code == 200
    keys = {c["provider"] for c in resp.json()["connections"]}
    assert "google" in keys


@pytest.mark.asyncio
@patch("routers.connections._byo_active", new_callable=AsyncMock)
async def test_list_excludes_google_for_non_byo_non_allowlisted(mock_byo, client, auth_override):
    mock_byo.return_value = False
    with auth_override("user_1", "u@example.com"):
        resp = client.get("/api/v1/connections")
    keys = {c["provider"] for c in resp.json()["connections"]}
    assert "google" not in keys  # managed behavior unchanged
```

(Add equivalent cases for the grant 404 and the bedrock 403/callback once the
exact route handlers are confirmed in `connections.py`. Each asserts: BYO active
→ no block; BYO inactive → existing block.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_connections.py -v --no-cov -k "byo_owner or non_byo"`
Expected: FAIL — google still filtered out / still gated.

- [ ] **Step 3: Carve site 1 — `_require_grantable_provider` (`internal` 404)**

Make the function async-aware of BYO (it currently raises when `p.internal and owner not in allowlist`):

```python
    if p.internal and owner_id not in settings.internal_integration_owners and not await _byo_active(owner_id, provider):
        raise HTTPException(status_code=404, detail=f"unknown integration provider: {provider}")
```

(If the function is currently sync, make it `async` and `await` it at call sites — confirm callers.)

- [ ] **Step 4: Carve site 2 — `_require_bedrock_for` (403 upsell)**

```python
    if not p.requires_bedrock:
        return
    if await _byo_active(owner_id, p.key):
        return  # owner's own app + own compliance — bedrock gate does not apply
    if not await _owner_on_bedrock(owner_id):
        raise HTTPException(status_code=403, detail=_bedrock_lock())
```

- [ ] **Step 5: Carve site 3 — the LIST/catalog filter**

At the list filter (`if allowlisted or not v.internal or connected.get(k) == "connected"`), include BYO-active providers while KEEPING the `connected` survivor clause:

```python
    byo = await _byo_active(owner_id, k)
    if allowlisted or not v.internal or byo or connected.get(k) == "connected":
        # include v in the response
```

(Resolve `_byo_active` per provider in the loop; the loop is already per-provider.)

- [ ] **Step 6: Carve site 4 — the OAuth-callback bedrock re-check**

At the callback re-check (`if p.requires_bedrock and not await _owner_on_bedrock(owner_id): RedirectResponse(...requires_bedrock)`):

```python
    if p.requires_bedrock and not await _byo_active(owner_id, p.key) and not await _owner_on_bedrock(owner_id):
        return RedirectResponse(f"{settings.FRONTEND_URL}/settings/connections?error=requires_bedrock")
```

- [ ] **Step 7: Run tests to verify they pass + no regressions**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_connections.py -v --no-cov`
Expected: all green (new BYO cases + existing managed-flow cases unchanged).

- [ ] **Step 8: No-marketplace-coupling guard**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_catalog_service.py tests/unit/routers/test_marketplace_listings.py -q --no-cov`
Expected: all green, untouched. (If anything moves, STOP and reconcile.)

- [ ] **Step 9: Commit**

```bash
git add apps/backend/routers/connections.py apps/backend/tests/unit/routers/test_connections.py
git commit -m "feat(integrations): carve the four gog gate-sites when BYO app active"
```

---

## Task 6: BYO-app endpoints + list payload exposure

**Files:**
- Modify: `apps/backend/routers/connections.py` (endpoints + payload)
- Test: `apps/backend/tests/unit/routers/test_connections.py` (extend)

**Interfaces:**
- Consumes: `oauth_app_store.put/delete` (Task 2), `_byo_active` (Task 4).
- Produces: `PUT /connections/{provider}/byo-app` (body `{client_id, client_secret, acknowledge: true}`); `DELETE /connections/{provider}/byo-app`; list items gain `byo_capable: bool` + `byo_configured: bool`.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
@patch("routers.connections.oauth_app_store.put", new_callable=AsyncMock)
async def test_put_byo_app_requires_ack_and_stores(mock_put, client, auth_override):
    with auth_override("user_1", "u@example.com"):
        ok = client.put(
            "/api/v1/connections/google/byo-app",
            json={"client_id": "cid", "client_secret": "csec", "acknowledge": True},
        )
        assert ok.status_code == 200
        mock_put.assert_awaited_once()
        # acknowledgment without consent is rejected
        bad = client.put(
            "/api/v1/connections/google/byo-app",
            json={"client_id": "cid", "client_secret": "csec", "acknowledge": False},
        )
        assert bad.status_code == 400


@pytest.mark.asyncio
async def test_put_byo_app_rejected_for_non_byo_provider(client, auth_override):
    with auth_override("user_1", "u@example.com"):
        resp = client.put(
            "/api/v1/connections/stripe/byo-app",
            json={"client_id": "c", "client_secret": "s", "acknowledge": True},
        )
        assert resp.status_code in (400, 404)  # stripe is not allow_byo_app
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_connections.py -v --no-cov -k byo_app`
Expected: FAIL — routes 404.

- [ ] **Step 3: Implement the endpoints**

Add to `connections.py` (use the file's existing `AuthContext`/`get_current_user` + `_dt` import for the timestamp):

```python
class ByoAppRequest(BaseModel):
    client_id: str = Field(min_length=1, max_length=512)
    client_secret: str = Field(min_length=1, max_length=512)
    acknowledge: bool = False


@router.put("/connections/{provider}/byo-app")
async def put_byo_app(
    provider: str,
    body: ByoAppRequest,
    auth: Annotated[AuthContext, Depends(get_current_user)],
):
    p = _get_provider_or_404(provider)
    if not p.allow_byo_app:
        raise HTTPException(status_code=400, detail=f"{provider} does not support a bring-your-own app")
    if not body.acknowledge:
        raise HTTPException(status_code=400, detail="acknowledgment required")
    await oauth_app_store.put(
        auth.user_id,
        provider,
        client_id=body.client_id,
        client_secret=body.client_secret,
        acknowledged_at=_dt.datetime.utcnow().isoformat() + "Z",
    )
    return {"status": "stored"}


@router.delete("/connections/{provider}/byo-app")
async def delete_byo_app(
    provider: str,
    auth: Annotated[AuthContext, Depends(get_current_user)],
):
    await oauth_app_store.delete(auth.user_id, provider)
    return {"status": "cleared"}
```

- [ ] **Step 4: Expose `byo_capable` / `byo_configured` in the list payload**

In the list handler, for each provider item add (resolve `_byo_active` / the stored app in the loop):

```python
        item["byo_capable"] = bool(v.allow_byo_app)
        item["byo_configured"] = await _byo_active(owner_id, k)
        item["byo_setup_url"] = v.byo_setup_url
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_connections.py -v --no-cov`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/routers/connections.py apps/backend/tests/unit/routers/test_connections.py
git commit -m "feat(integrations): PUT/DELETE byo-app endpoints + list exposure"
```

---

## Task 7: Frontend — BYO connect affordance

**Files:**
- Modify: `apps/frontend/src/hooks/useConnections.ts` (+ the `Connection` type)
- Modify: `apps/frontend/src/components/.../ConnectButton.tsx`
- Test: a targeted vitest beside `ConnectButton`

> Read the existing `ConnectButton.tsx` (it branches connected / locked /
> `direct_oauth` / `paste_token`; the inline-paste UI ~`:175-216` is the template)
> and `useConnections.ts` (the `Connection` type + `requires_bedrock` exposure)
> before editing. Match existing styling (cream/green tokens).

- [ ] **Step 1: Extend the `Connection` type**

In `useConnections.ts`, add to the `Connection` type:

```typescript
  byo_capable?: boolean;
  byo_configured?: boolean;
  byo_setup_url?: string | null;
```

- [ ] **Step 2: Write the failing test**

Create `ConnectButton.byo.test.tsx` beside the component:

```tsx
import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { ConnectButton } from "./ConnectButton";

vi.mock("@/lib/api", () => ({ useApi: () => ({ put: vi.fn(), del: vi.fn() }) }));

describe("ConnectButton BYO branch", () => {
  it("offers 'Use your own Google app' for a byo_capable locked provider", () => {
    render(
      <ConnectButton
        connection={{ provider: "google", status: "disconnected", requires_bedrock: true, byo_capable: true, byo_configured: false }}
      />,
    );
    expect(screen.getByText(/use your own/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd apps/frontend && pnpm test -- --run ConnectButton.byo`
Expected: FAIL — no "Use your own" affordance.

- [ ] **Step 4: Add the BYO branch + paste/setup affordance**

In `ConnectButton.tsx`, before the existing locked/upsell branch, add: if `connection.byo_capable && !connection.byo_configured` and the provider is otherwise locked, render a "Use your own Google app" button that opens a small panel with: the `byo_setup_url` link, the **redirect URI to register** (the connections callback URL — read it from config / the existing OAuth-return handling), `client_id`/`client_secret` inputs, the **Limited-Use acknowledgment checkbox**, and a Save that `PUT`s `/connections/{provider}/byo-app` with `{client_id, client_secret, acknowledge}`. After save, the normal Connect (OAuth) flow runs.

> The acknowledgment copy is a founder decision — use this placeholder constant
> and leave a comment for the founder to finalize:
> ```tsx
> const BYO_ACK_COPY = "I understand I'm using my own Google app and accept responsibility for its compliance with Google's Limited Use policy."; // FOUNDER: finalize wording
> ```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd apps/frontend && pnpm test -- --run ConnectButton.byo`
Expected: PASS. (Targeted single file only — do NOT run the full suite.)

- [ ] **Step 6: Commit**

```bash
git add apps/frontend/src/hooks/useConnections.ts "apps/frontend/src/components" 
git commit -m "feat(integrations): BYO Google app connect affordance"
```

---

## Final verification

- [ ] Backend: `cd apps/backend && uv run pytest tests/unit/services/credentials/ tests/unit/routers/test_connections.py -q --no-cov` → green.
- [ ] No-coupling guard: `uv run pytest tests/unit/services/test_catalog_service.py tests/unit/routers/test_marketplace_listings.py -q --no-cov` → green, untouched.
- [ ] `uv run ruff check . && uv run ruff format --check .` (changed files) → clean.
- [ ] Frontend: targeted vitest for ConnectButton BYO → green. (Never the full suite.)
- [ ] Manual (dev): a non-Bedrock owner stores a BYO Google app, sees Google appear + connectable, completes OAuth against their app, gog materializes + self-refreshes.

## Spec coverage check

- Registry flags → Task 1 ✓
- BYO secret store under integrations/ prefix → Task 2 ✓
- `owner_id` into `_oauth_client` (BYO-vs-Isol8) → Task 3 ✓
- carve predicate → Task 4 ✓
- four carve-sites (incl. LIST filter + callback re-check + survivor clause) → Task 5 ✓
- endpoints + payload exposure → Task 6 ✓
- frontend affordance + acknowledgment → Task 7 ✓
- no-marketplace-coupling invariant → Task 5 step 8 + Final verification ✓
- materializer unchanged (verified) → no task needed ✓
- Limited-Use disclaimer wording → placeholder in Task 7 (founder finalizes) ✓
- no infra change (secret nests under granted prefix) → Task 2 ✓
