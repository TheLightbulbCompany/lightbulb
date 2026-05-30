# Integrations Panel (v1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Isol8's own integrations panel — a registry-driven credential layer that lets a customer connect third-party services (API-key paste *and* OAuth) through a no-shell web UX, delivering the credential into their OpenClaw container. No Nango, no Composio.

**Architecture:** A YAML **integration registry** (`providers.yaml`) is the single source of truth: per integration it declares `acquisition` (paste_token | direct_oauth), `actor` (cli | mcp | skill), `refresh_owner`, and `wiring` (literal `openclaw_paths` injection, or a `materializer` that writes a CLI's config file to EFS). A **wiring engine** (`apply_connection`) dispatches a fetched credential to the right delivery rail, all of which already exist in the codebase (`config_patcher.py` for openclaw.json deep-merge; `workspace.py:700-786` atomic EFS write for materializers; `key_service`/`channels` for the existing BYOK/channel rails). The **only new acquisition machinery** is the OAuth redirect dance (PKCE+state store + a generic `GET /oauth/{provider}/callback`), modeled on the existing ChatGPT device-code flow.

**Tech Stack:** FastAPI (routers under `/api/v1`), DynamoDB (plain dicts via `core/repositories/`), Fernet encryption (reuse `oauth_service`/`encryption.py`), AWS CDK (TypeScript) for the two new DDB tables, Next.js 16 + SWR + the `useApi()` hook on the frontend, pytest + vitest for tests.

**Key reused rails (verified):** `core/containers/workspace.py:700-786` (atomic 0600 EFS write + chown 1000), `core/services/oauth_service.py` (Fernet + device-code precedent), `core/services/config_patcher.py` (`set_openclaw_config_path`, `patch_openclaw_config`, `append_to_openclaw_config_list`), `core/services/key_service.py` (BYOK allowlist → Secrets Manager), `core/services/channel_link_service.py` + `routers/channels.py`, `apps/frontend/src/hooks/useChatGPTOAuth.ts`, `apps/frontend/src/components/settings/MyChannelsSection.tsx`, `core/services/catalog_service.py` (`deploy_from_artifact`), `core/services/catalog_slice.py` (`_scrub_secret_fields`).

---

## File Structure

```
apps/backend/core/services/credentials/
├── __init__.py
├── providers.yaml              # the registry (one entry per integration)
├── registry.py                 # pydantic models + loader + validation (import-time)
├── apply_connection.py         # wiring engine: credential -> delivery rail
├── oauth_dance.py              # PKCE+state + authorize-URL + code exchange
└── materializers/
    ├── __init__.py             # MATERIALIZERS registry (name -> impl)
    ├── base.py                 # Materializer protocol + shared atomic EFS write
    ├── toml_cli.py             # 0600 ~/.config/<cli>/config.toml  (PP CLIs)
    └── gog_keyring.py          # gog config.json + token import (Google)

apps/backend/core/repositories/
├── connection_repo.py          # DDB isol8-<env>-connections (owner_id, provider)
└── oauth_state_repo.py         # DDB isol8-<env>-oauth-state (state -> verifier, TTL)

apps/backend/routers/
└── connections.py              # /connections + /oauth/{provider}/callback

apps/backend/tests/credentials/
├── test_registry.py
├── test_apply_connection.py
├── test_materializer_toml_cli.py
├── test_materializer_gog_keyring.py
├── test_oauth_dance.py
├── test_connections_routes.py
└── test_marketplace_requires_credentials.py

apps/infra/lib/stacks/database-stack.ts   # MODIFY: + connections + oauth-state tables

apps/frontend/src/app/settings/connections/page.tsx          # panel page
apps/frontend/src/components/settings/ConnectionsList.tsx     # list + status
apps/frontend/src/components/settings/ConnectButton.tsx       # paste-or-OAuth connect
apps/frontend/src/hooks/useConnections.ts                     # SWR fetch + mutate
```

---

## Verified against the codebase (2026-05-29) — use these exact names

A verification pass confirmed the reused rails exist; a few names differ from the illustrative code below. **These are authoritative — prefer them wherever a code block disagrees:**

- **Fernet:** `get_fernet()` lives in **`core/encryption.py:16`** (not `oauth_service`). `core/encryption.py` also exposes `encrypt(str)->str` / `decrypt(str)->str`.
- **Owner id:** `AuthContext` (`core/auth.py:57`) has **no `owner_id`**. Use **`core.auth.resolve_owner_id(auth)`** (`:97` — `org_id` in org context, else `user_id`). Replace every `auth.owner_id` in this plan with `resolve_owner_id(auth)`.
- **DynamoDB:** `core.dynamodb.get_table(short_name)` (`:35`) takes a **short** name and auto-prepends `DYNAMODB_TABLE_PREFIX`. Call `get_table("connections")` / `get_table("oauth-state")` — **no `CONNECTIONS_TABLE`/`OAUTH_STATE_TABLE` setting or service-stack env var is needed** (only the table creation + IAM grant). This simplifies Task 2.1.
- **config_patcher is ASYNC and takes a path LIST:** `await set_openclaw_config_path(owner_id, path: list[str], value)`, `await patch_openclaw_config(...)`, `await append_to_openclaw_config_list(owner_id, path: list[str], value)`. Split dotted registry paths with `op.path.split(".")`. **`apply_connection` and every router handler that calls it must be `async` and `await`** — and the unit-test monkeypatches must be async doubles.
- **Frontend URL:** settings field is **`FRONTEND_URL`** (`core/config.py:166`), not `FRONTEND_BASE`. `PUBLIC_API_BASE` (`:41`) and `EFS_MOUNT_PATH` (`:65`) are correct as written.
- **Materializer write:** the atomic 0600 + chown-1000 pattern to mirror is inside **`workspace.py:700-786 pre_stage_auth_profile_store`** (a named function, not a generic helper); the new `materializers/base.py:atomic_write` reproduces that sequence (correct as written).
- **Slice scrub:** `catalog_slice._scrub_secret_fields` (`:89`) blanks `{apiKey, env, headers, webhookToken}` — include `webhookToken`.
- **Marketplace deploy:** `deploy_from_artifact` is an **async method on `CatalogService`** (`catalog_service.py:149`); `owner_id` is its first kwarg; insert the credential pre-check at the top of the method body (before tarball extraction ~`:200`). Define `assert_credentials_satisfied` as a module helper accordingly.
- **Router mount:** mirror `main.py:537+` — `app.include_router(connections.router, prefix="/api/v1/connections", ...)` and a separate `oauth_router` (carrying `/oauth/...`) at `prefix="/api/v1"`.

---

## M1 — Integration registry foundation

The registry is the spine; build it first so every later task reads from it.

### Task 1.1: Registry pydantic models + loader

**Files:**
- Create: `apps/backend/core/services/credentials/__init__.py`
- Create: `apps/backend/core/services/credentials/registry.py`
- Create: `apps/backend/core/services/credentials/providers.yaml`
- Test: `apps/backend/tests/credentials/test_registry.py`

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/credentials/test_registry.py
import pytest
from core.services.credentials import registry


def test_loads_known_providers():
    providers = registry.all_providers()
    assert "linear" in providers
    assert "google" in providers


def test_paste_key_provider_shape():
    p = registry.get_provider("linear")
    assert p.acquisition == "paste_token"
    assert p.actor == "mcp"
    assert p.refresh_owner == "none"
    assert p.wiring.openclaw_paths[0].path == "mcp.servers.linear.env.LINEAR_API_KEY"
    assert p.wiring.openclaw_paths[0].source == "input.token"


def test_direct_oauth_provider_shape():
    p = registry.get_provider("google")
    assert p.acquisition == "direct_oauth"
    assert p.actor == "cli"
    assert p.refresh_owner == "actor"
    assert p.oauth.token_url == "https://oauth2.googleapis.com/token"
    assert "gmail.modify" in p.oauth.scopes
    assert p.wiring.materializer == "gog_keyring"


def test_unknown_provider_raises():
    with pytest.raises(KeyError):
        registry.get_provider("does-not-exist")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/credentials/test_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: core.services.credentials.registry`

- [ ] **Step 3: Write `providers.yaml` (the seed registry)**

OAuth endpoints/scopes below are transcribed from Nango's open `packages/providers/providers.yaml` (facts) — verify each against the vendor's own OAuth docs when implementing.

```yaml
# apps/backend/core/services/credentials/providers.yaml
# Bucket A — paste-key (no OAuth, no refresh)
linear:
  display_name: Linear
  acquisition: paste_token
  actor: mcp
  refresh_owner: none
  validate: { method: GET, url: "https://api.linear.app/graphql", auth_header: Authorization }
  wiring:
    openclaw_paths:
      - { path: "mcp.servers.linear.env.LINEAR_API_KEY", source: "input.token" }

stripe:
  display_name: Stripe
  acquisition: paste_token
  actor: mcp
  refresh_owner: none
  validate: { method: GET, url: "https://api.stripe.com/v1/account", auth_header: Authorization }
  wiring:
    openclaw_paths:
      - { path: "mcp.servers.stripe.env.STRIPE_SECRET_KEY", source: "input.token" }

# Bucket B — dedicated self-refreshing CLI
google:
  display_name: Google Workspace
  acquisition: direct_oauth
  actor: cli
  refresh_owner: actor
  requires: { bins: [gog] }
  oauth:
    authorization_url: "https://accounts.google.com/o/oauth2/v2/auth"
    token_url: "https://oauth2.googleapis.com/token"
    scopes: ["https://www.googleapis.com/auth/gmail.modify", "https://www.googleapis.com/auth/calendar", "https://www.googleapis.com/auth/drive"]
    scope_separator: " "
    pkce: true
    extra_authorize_params: { access_type: offline, prompt: consent }
  wiring:
    materializer: gog_keyring
```

- [ ] **Step 4: Write the loader + models**

```python
# apps/backend/core/services/credentials/registry.py
"""Integration registry: the single source of truth for how each third-party
provider is acquired, who acts with the credential, and exactly where it lands.
Loaded + validated once at import. See docs/superpowers/specs/2026-05-29-
credential-integration-registry-design.md."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, model_validator

_YAML = Path(__file__).parent / "providers.yaml"

Acquisition = Literal["paste_token", "direct_oauth", "nango", "aws_sts", "secrets_manager"]
Actor = Literal["cli", "mcp", "skill", "nango_proxy"]
RefreshOwner = Literal["none", "actor", "isol8_rematerialize", "nango_proxy"]


class OpenclawPath(BaseModel):
    path: str          # dotted path into openclaw.json
    source: str        # "input.token" | "credentials.access_token" | dotted token-payload path


class Wiring(BaseModel):
    openclaw_paths: list[OpenclawPath] | None = None
    materializer: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "Wiring":
        if bool(self.openclaw_paths) == bool(self.materializer):
            raise ValueError("wiring must set exactly one of openclaw_paths | materializer")
        return self


class OAuthSpec(BaseModel):
    authorization_url: str
    token_url: str
    scopes: list[str]
    scope_separator: str = " "
    pkce: bool = True
    extra_authorize_params: dict[str, str] = {}


class ValidateSpec(BaseModel):
    method: str
    url: str
    auth_header: str = "Authorization"


class Requires(BaseModel):
    bins: list[str] = []
    config: list[str] = []


class Provider(BaseModel):
    key: str
    display_name: str
    acquisition: Acquisition
    actor: Actor
    refresh_owner: RefreshOwner
    wiring: Wiring
    requires: Requires = Requires()
    oauth: OAuthSpec | None = None
    validate: ValidateSpec | None = None

    @model_validator(mode="after")
    def _oauth_present_when_needed(self) -> "Provider":
        if self.acquisition == "direct_oauth" and self.oauth is None:
            raise ValueError(f"{self.key}: direct_oauth requires an `oauth` block")
        return self


@lru_cache(maxsize=1)
def _load() -> dict[str, Provider]:
    raw = yaml.safe_load(_YAML.read_text()) or {}
    return {k: Provider(key=k, **v) for k, v in raw.items()}


def all_providers() -> dict[str, Provider]:
    return _load()


def get_provider(key: str) -> Provider:
    providers = _load()
    if key not in providers:
        raise KeyError(f"unknown integration provider: {key!r}")
    return providers[key]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/credentials/test_registry.py -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/credentials/ apps/backend/tests/credentials/test_registry.py
git commit -m "feat(credentials): integration registry + providers.yaml loader"
```

### Task 1.2: CI gate — every required provider has a registry entry

**Files:**
- Test: `apps/backend/tests/credentials/test_registry.py` (add to it)

- [ ] **Step 1: Add the failing test**

```python
def test_validate_registry_is_self_consistent():
    # Each direct_oauth provider must declare scopes; each materializer must be known.
    from core.services.credentials.materializers import MATERIALIZERS
    for key, p in registry.all_providers().items():
        if p.acquisition == "direct_oauth":
            assert p.oauth and p.oauth.scopes, f"{key} missing scopes"
        if p.wiring.materializer:
            assert p.wiring.materializer in MATERIALIZERS, f"{key}: unknown materializer {p.wiring.materializer}"
```

- [ ] **Step 2:** Run it; expected FAIL (`MATERIALIZERS` not defined yet — it lands in Task 3.1). Mark this test `@pytest.mark.skip(reason="enable after M3")` for now and unskip in Task 3.1.

- [ ] **Step 3: Commit**

```bash
git add apps/backend/tests/credentials/test_registry.py
git commit -m "test(credentials): registry self-consistency gate (skipped until M3)"
```

---

## M2 — Credential vault + wiring engine

### Task 2.1: CDK — `connections` + `oauth-state` DynamoDB tables

A new `connections` table avoids migrating the existing `oauth-tokens` table (which is keyed by owner only for ChatGPT).

**Files:**
- Modify: `apps/infra/lib/stacks/database-stack.ts` (near the existing oauth-tokens table at ~line 237)

- [ ] **Step 1: Add the tables**

```typescript
// apps/infra/lib/stacks/database-stack.ts  (alongside the other tables)
// Credential vault — mirrors oauthTokensTable (database-stack.ts:237) exactly,
// + a `provider` sort key. Data is ALSO Fernet-encrypted app-side (core/encryption.py)
// → "table-read != token-decrypt"; KMS-at-rest is the separate snapshot-theft layer.
this.connectionsTable = new dynamodb.Table(this, "ConnectionsTable", {
  tableName: `isol8-${env}-connections`,
  partitionKey: { name: "owner_id", type: dynamodb.AttributeType.STRING },
  sortKey: { name: "provider", type: dynamodb.AttributeType.STRING },
  billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
  encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
  encryptionKey: props.kmsKey,
  pointInTimeRecovery: true,            // always — like every other table in this stack
  removalPolicy: config.removalPolicy,  // ENV_CONFIG: prod RETAIN / dev DESTROY
});

// Short-lived OAuth dance state (PKCE verifier + state nonce). Mirrors the
// webhook-event-dedup table (database-stack.ts:166): TTL'd, CMK, DESTROY.
this.oauthStateTable = new dynamodb.Table(this, "OauthStateTable", {
  tableName: `isol8-${env}-oauth-state`,
  partitionKey: { name: "state", type: dynamodb.AttributeType.STRING },
  billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
  timeToLiveAttribute: "ttl",           // epoch seconds; auto-expire stale flows
  encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
  encryptionKey: props.kmsKey,
  removalPolicy: cdk.RemovalPolicy.DESTROY,
});
```

- [ ] **Step 2:** In `service-stack.ts`, add both tables to `ServiceStackProps.database` and grant the task role access (mirror the `oauthTokensTable.grantReadWriteData(this.taskRole)` line at `service-stack.ts:304`): `props.database.connectionsTable.grantReadWriteData(this.taskRole)` + the same for `oauthStateTable`. **No env var / `core/config.py` setting needed** — `get_table("connections")` / `get_table("oauth-state")` derive the full name from `DYNAMODB_TABLE_PREFIX` (see the verification box).

- [ ] **Step 3: Verify synth**

Run: `cd apps/infra && pnpm cdk synth -q 2>&1 | head -5`
Expected: synth succeeds (no exceptions).

- [ ] **Step 4: Commit**

```bash
git add apps/infra/lib/stacks/database-stack.ts apps/infra/lib/stacks/service-stack.ts apps/backend/core/config.py
git commit -m "feat(infra): connections + oauth-state DynamoDB tables for integrations panel"
```

### Task 2.2: Connection repository (Fernet-encrypted credential vault)

**Files:**
- Create: `apps/backend/core/repositories/connection_repo.py`
- Test: `apps/backend/tests/credentials/test_apply_connection.py` (start the file with repo tests)

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/credentials/test_apply_connection.py
import pytest
from core.repositories import connection_repo


@pytest.fixture
def fake_table(monkeypatch):
    store = {}
    class _T:
        def put_item(self, Item): store[(Item["owner_id"], Item["provider"])] = Item
        def get_item(self, Key):
            it = store.get((Key["owner_id"], Key["provider"]))
            return {"Item": it} if it else {}
        def delete_item(self, Key): store.pop((Key["owner_id"], Key["provider"]), None)
        def query(self, **kw):
            owner = kw["KeyConditionExpression"]  # simplified in test
            return {"Items": [v for (o, _), v in store.items() if True]}
    monkeypatch.setattr(connection_repo, "_table", lambda: _T())
    return store


def test_roundtrip_encrypts_token(fake_table):
    connection_repo.put("org_1", "linear", {"token": "lin_secret"}, status="connected")
    rec = connection_repo.get("org_1", "linear")
    assert rec["status"] == "connected"
    assert connection_repo.decrypt_credential(rec) == {"token": "lin_secret"}
    # ciphertext is not the plaintext
    assert "lin_secret" not in rec["credential_enc"]


def test_delete(fake_table):
    connection_repo.put("org_1", "linear", {"token": "x"})
    connection_repo.delete("org_1", "linear")
    assert connection_repo.get("org_1", "linear") is None
```

- [ ] **Step 2:** Run → FAIL (`connection_repo` missing).

- [ ] **Step 3: Implement the repo**

```python
# apps/backend/core/repositories/connection_repo.py
"""Per-owner, per-provider connected-credential vault. Credential payloads are
Fernet-encrypted at rest (same key/helper as oauth_service). Plain DynamoDB dicts."""
from __future__ import annotations

import json
import time
from typing import Any

from boto3.dynamodb.conditions import Key

from core.dynamodb import get_table
from core.encryption import get_fernet  # reuse existing Fernet helper (NOT in oauth_service)


def _table():
    return get_table("connections")   # short name; DYNAMODB_TABLE_PREFIX auto-applied


def put(owner_id: str, provider: str, credential: dict[str, Any], *, status: str = "connected", meta: dict | None = None) -> None:
    enc = get_fernet().encrypt(json.dumps(credential).encode()).decode()
    _table().put_item(Item={
        "owner_id": owner_id,
        "provider": provider,
        "credential_enc": enc,
        "status": status,
        "updated_at": int(time.time()),
        **(meta or {}),
    })


def get(owner_id: str, provider: str) -> dict | None:
    return _table().get_item(Key={"owner_id": owner_id, "provider": provider}).get("Item")


def list_for_owner(owner_id: str) -> list[dict]:
    return _table().query(KeyConditionExpression=Key("owner_id").eq(owner_id)).get("Items", [])


def delete(owner_id: str, provider: str) -> None:
    _table().delete_item(Key={"owner_id": owner_id, "provider": provider})


def decrypt_credential(rec: dict) -> dict[str, Any]:
    return json.loads(get_fernet().decrypt(rec["credential_enc"].encode()).decode())
```

- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/repositories/connection_repo.py apps/backend/tests/credentials/test_apply_connection.py
git commit -m "feat(credentials): Fernet-encrypted connection vault repository"
```

### Task 2.3: Wiring engine (`apply_connection`)

**Files:**
- Create: `apps/backend/core/services/credentials/apply_connection.py`
- Test: `apps/backend/tests/credentials/test_apply_connection.py` (extend)

- [ ] **Step 1: Add the failing test**

```python
def test_apply_openclaw_paths_injects_literal(monkeypatch):
    from core.services.credentials import apply_connection as ac
    calls = []
    monkeypatch.setattr(ac.config_patcher, "set_openclaw_config_path",
                        lambda owner, path, value: calls.append((owner, path, value)))
    ac.apply_connection("org_1", "linear", {"token": "lin_secret"})
    assert calls == [("org_1", "mcp.servers.linear.env.LINEAR_API_KEY", "lin_secret")]


def test_apply_materializer_dispatch(monkeypatch):
    from core.services.credentials import apply_connection as ac
    seen = {}
    monkeypatch.setitem(ac.MATERIALIZERS, "gog_keyring",
                        lambda owner, provider, cred: seen.update(owner=owner, cred=cred))
    ac.apply_connection("org_1", "google", {"access_token": "a", "refresh_token": "r"})
    assert seen["owner"] == "org_1" and seen["cred"]["refresh_token"] == "r"
```

- [ ] **Step 2:** Run → FAIL.

- [ ] **Step 3: Implement the engine**

```python
# apps/backend/core/services/credentials/apply_connection.py
"""Wiring engine: take a fetched credential for (owner, provider) and deliver it
into the owner's container via the rail the registry specifies."""
from __future__ import annotations

from typing import Any

from core.services import config_patcher
from core.services.credentials import registry
from core.services.credentials.materializers import MATERIALIZERS


def _resolve_source(source: str, credential: dict[str, Any], raw_input: dict[str, Any]) -> Any:
    # "input.token" -> raw_input["token"]; "credentials.access_token" -> credential["access_token"];
    # dotted paths index nested dicts.
    root, *rest = source.split(".")
    cur: Any = raw_input if root == "input" else credential
    for part in rest:
        cur = cur[part]
    return cur


async def apply_connection(owner_id: str, provider_key: str, credential: dict[str, Any]) -> None:
    """credential is the resolved token payload (e.g. {"token": ...} for paste,
    or {"access_token","refresh_token","token_expiry","client_id","client_secret"} for OAuth).
    Async because config_patcher is async."""
    p = registry.get_provider(provider_key)
    w = p.wiring
    if w.openclaw_paths:
        for op in w.openclaw_paths:
            value = _resolve_source(op.source, credential, credential)
            await config_patcher.set_openclaw_config_path(owner_id, op.path.split("."), value)
        return
    if w.materializer:
        MATERIALIZERS[w.materializer](owner_id, provider_key, credential)  # materializers are sync file writes
        return
    raise RuntimeError(f"{provider_key}: registry wiring has no deliverable target")
```

- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/credentials/apply_connection.py apps/backend/tests/credentials/test_apply_connection.py
git commit -m "feat(credentials): apply_connection wiring engine (openclaw_paths + materializer dispatch)"
```

---

## M3 — Materializers

### Task 3.1: Materializer base + registry + atomic EFS write

**Files:**
- Create: `apps/backend/core/services/credentials/materializers/__init__.py`
- Create: `apps/backend/core/services/credentials/materializers/base.py`
- Test: `apps/backend/tests/credentials/test_materializer_toml_cli.py`

- [ ] **Step 1: Write the base + a shared atomic-write helper (generalize `workspace.py:700-786`)**

```python
# apps/backend/core/services/credentials/materializers/base.py
"""Materializers write a CLI's own credential file onto the owner's EFS workspace
(0600, owned by uid/gid 1000, atomic) so a baked CLI reads it at runtime. This
generalizes core/containers/workspace.py:700-786 (the ChatGPT auth-profile rail)."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Protocol

from core.config import settings

NODE_UID = 1000
NODE_GID = 1000


class Materializer(Protocol):
    def __call__(self, owner_id: str, provider_key: str, credential: dict) -> None: ...


def owner_efs_root(owner_id: str) -> Path:
    return Path(settings.EFS_MOUNT_PATH) / owner_id


def atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        if os.geteuid() == 0:                       # only when running as root (in-container)
            os.chown(tmp, NODE_UID, NODE_GID)
        os.replace(tmp, path)
        if os.geteuid() == 0:
            os.chown(path.parent, NODE_UID, NODE_GID)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
```

```python
# apps/backend/core/services/credentials/materializers/__init__.py
from core.services.credentials.materializers.gog_keyring import write as _gog
from core.services.credentials.materializers.toml_cli import write as _toml

MATERIALIZERS = {"toml_cli": _toml, "gog_keyring": _gog}
```

- [ ] **Step 2: Unskip the registry self-consistency test from Task 1.2** (remove the `@pytest.mark.skip`).

### Task 3.2: `toml_cli` materializer (Printing Press CLIs)

**Files:**
- Create: `apps/backend/core/services/credentials/materializers/toml_cli.py`
- Test: `apps/backend/tests/credentials/test_materializer_toml_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/credentials/test_materializer_toml_cli.py
import tomllib
from pathlib import Path

from core.services.credentials.materializers import toml_cli


def test_writes_0600_toml(tmp_path, monkeypatch):
    monkeypatch.setattr("core.config.settings.EFS_MOUNT_PATH", str(tmp_path))
    cred = {"cli": "outlook-email-pp-cli", "access_token": "a", "refresh_token": "r",
            "token_expiry": "2026-06-01T00:00:00Z", "client_id": "cid", "client_secret": "cs"}
    toml_cli.write("org_1", "outlook", cred)
    p = Path(tmp_path) / "org_1" / ".config" / "outlook-email-pp-cli" / "config.toml"
    assert p.exists()
    assert oct(p.stat().st_mode)[-3:] == "600"
    data = tomllib.loads(p.read_text())
    assert data["refresh_token"] == "r" and data["client_id"] == "cid"
```

- [ ] **Step 2:** Run → FAIL.

- [ ] **Step 3: Implement** (matches the verified PP `config.toml` shape: `access_token`/`refresh_token`/`token_expiry`/`client_id`/`client_secret`)

```python
# apps/backend/core/services/credentials/materializers/toml_cli.py
from pathlib import Path

from core.services.credentials.materializers.base import atomic_write, owner_efs_root


def _toml_escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"')


def write(owner_id: str, provider_key: str, credential: dict) -> None:
    cli = credential["cli"]                       # e.g. "outlook-email-pp-cli"
    path = owner_efs_root(owner_id) / ".config" / cli / "config.toml"
    fields = ["access_token", "refresh_token", "token_expiry", "client_id", "client_secret"]
    body = "\n".join(
        f'{k} = "{_toml_escape(str(credential[k]))}"' for k in fields if credential.get(k)
    ) + "\n"
    atomic_write(path, body.encode())
```

- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/credentials/materializers/ apps/backend/tests/credentials/test_materializer_toml_cli.py
git commit -m "feat(credentials): materializer base + toml_cli (Printing Press CLI config)"
```

### Task 3.3: `gog_keyring` materializer (Google)

> **⚠️ SUPERSEDED (2026-05-29) — see M6.** This file-write materializer **cannot work**: gog's refresh token lives in a 99designs file-keyring **JWE blob** (PBES2-HS256+A128KW + AES-256-GCM), not a hand-writable `tokens.json`. Google is provisioned by **running gog headlessly in the container** (`gog auth credentials set -` + `gog auth tokens import -`), not by writing files. Build the materializer below if useful for non-keyring CLIs, but the `google` provider wiring uses the `gog_exec` provisioner from M6, and this `gog_keyring` materializer is retired. The original text is kept for history.

`gog` stores the OAuth **client** in `~/.config/gogcli/config.json` and the **refresh token** in a keyring; the file backend reads `GOG_KEYRING_PASSWORD`. We seed both and inject `GOG_KEYRING_BACKEND=file` + `GOG_KEYRING_PASSWORD` into the container env (per-owner, via `config_patcher` into the agent's run env or the ECS task — choose the EFS-file keyring path so no task redeploy is needed).

**Files:**
- Create: `apps/backend/core/services/credentials/materializers/gog_keyring.py`
- Test: `apps/backend/tests/credentials/test_materializer_gog_keyring.py`

- [ ] **Step 1: Write the failing test**

```python
import json
from pathlib import Path
from core.services.credentials.materializers import gog_keyring


def test_writes_client_config_json(tmp_path, monkeypatch):
    monkeypatch.setattr("core.config.settings.EFS_MOUNT_PATH", str(tmp_path))
    cred = {"client_id": "cid", "client_secret": "cs", "refresh_token": "r",
            "access_token": "a", "account_email": "u@example.com"}
    gog_keyring.write("org_1", "google", cred)
    cfg = Path(tmp_path) / "org_1" / ".config" / "gogcli" / "config.json"
    assert cfg.exists()
    data = json.loads(cfg.read_text())
    assert data["installed"]["client_id"] == "cid"
```

- [ ] **Step 2:** Run → FAIL.

- [ ] **Step 3: Implement** (writes `config.json`; writes the token store for the gog `file` keyring backend. Verify the exact on-disk keyring format against `gog auth keyring file` output during the M6 smoke and adjust the token-file shape there.)

```python
# apps/backend/core/services/credentials/materializers/gog_keyring.py
import json
from pathlib import Path

from core.services.credentials.materializers.base import atomic_write, owner_efs_root


def write(owner_id: str, provider_key: str, credential: dict) -> None:
    root = owner_efs_root(owner_id) / ".config" / "gogcli"
    # 1) OAuth client config (the `gog auth credentials set` artifact).
    client = {"installed": {
        "client_id": credential["client_id"],
        "client_secret": credential.get("client_secret", ""),
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }}
    atomic_write(root / "config.json", json.dumps(client).encode())
    # 2) Refresh token for the file keyring backend. Exact format confirmed in M6 smoke.
    token = {"account": credential.get("account_email", ""),
             "refresh_token": credential["refresh_token"],
             "access_token": credential.get("access_token", "")}
    atomic_write(root / "tokens.json", json.dumps(token).encode())
```

- [ ] **Step 4:** Run → PASS. **Step 5: Commit**

```bash
git add apps/backend/core/services/credentials/materializers/gog_keyring.py apps/backend/tests/credentials/test_materializer_gog_keyring.py
git commit -m "feat(credentials): gog_keyring materializer (Google client + token seed)"
```

---

## M4 — Paste-key panel (Bucket A): backend

Ships the panel end-to-end for the lowest-risk bucket first (no OAuth).

### Task 4.1: Connections router — list / connect-paste / disconnect

**Files:**
- Create: `apps/backend/routers/connections.py`
- Modify: `apps/backend/main.py` (register the router under `/api/v1`)
- Test: `apps/backend/tests/credentials/test_connections_routes.py`

- [ ] **Step 1: Write the failing test** (use the app's existing auth override pattern from other router tests)

```python
# apps/backend/tests/credentials/test_connections_routes.py
def test_paste_connect_validates_then_applies(client, auth_org_1, monkeypatch):
    applied = {}
    monkeypatch.setattr("routers.connections.apply_connection.apply_connection",
                        lambda owner, prov, cred: applied.update(owner=owner, prov=prov, cred=cred))
    monkeypatch.setattr("routers.connections._validate_token", lambda p, token: True)
    r = client.post("/api/v1/connections/linear", json={"token": "lin_x"}, headers=auth_org_1)
    assert r.status_code == 200
    assert applied == {"owner": "org_1", "prov": "linear", "cred": {"token": "lin_x"}}


def test_list_returns_status(client, auth_org_1, monkeypatch):
    monkeypatch.setattr("routers.connections.connection_repo.list_for_owner",
                        lambda owner: [{"provider": "linear", "status": "connected"}])
    r = client.get("/api/v1/connections", headers=auth_org_1)
    assert r.status_code == 200
    assert r.json()["connections"][0]["provider"] == "linear"
```

- [ ] **Step 2:** Run → FAIL.

- [ ] **Step 3: Implement the router**

```python
# apps/backend/routers/connections.py
"""Customer-facing integrations panel API. /connections list+paste+disconnect here;
the OAuth dance routes land in M5 (same router)."""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.auth import AuthContext, get_current_user
from core.repositories import connection_repo
from core.services.credentials import apply_connection, registry

router = APIRouter(prefix="/connections", tags=["connections"])


class PasteBody(BaseModel):
    token: str


def _validate_token(p: registry.Provider, token: str) -> bool:
    if not p.validate:
        return True
    headers = {p.validate.auth_header: f"Bearer {token}"}
    try:
        resp = httpx.request(p.validate.method, p.validate.url, headers=headers, timeout=10)
        return resp.status_code < 400
    except httpx.HTTPError:
        return False


@router.get("")
def list_connections(auth: AuthContext = Depends(get_current_user)):
    rows = connection_repo.list_for_owner(auth.owner_id)
    catalog = registry.all_providers()
    connected = {r["provider"]: r.get("status", "connected") for r in rows}
    return {"connections": [
        {"provider": k, "display_name": v.display_name, "acquisition": v.acquisition,
         "status": connected.get(k, "disconnected")}
        for k, v in catalog.items()
    ]}


@router.post("/{provider}")
def connect_paste(provider: str, body: PasteBody, auth: AuthContext = Depends(get_current_user)):
    p = registry.get_provider(provider)
    if p.acquisition != "paste_token":
        raise HTTPException(400, f"{provider} is not a paste-key integration; use /authorize")
    if not _validate_token(p, body.token):
        raise HTTPException(422, "token failed validation against the provider")
    connection_repo.put(auth.owner_id, provider, {"token": body.token}, status="connected")
    apply_connection.apply_connection(auth.owner_id, provider, {"token": body.token})
    return {"status": "connected", "provider": provider}


@router.delete("/{provider}")
def disconnect(provider: str, auth: AuthContext = Depends(get_current_user)):
    connection_repo.delete(auth.owner_id, provider)
    # Best-effort: blank the injected slot so the agent stops authenticating.
    p = registry.get_provider(provider)
    if p.wiring.openclaw_paths:
        from core.services import config_patcher
        for op in p.wiring.openclaw_paths:
            config_patcher.set_openclaw_config_path(auth.owner_id, op.path, "")
    return {"status": "disconnected", "provider": provider}
```

- [ ] **Step 4:** Register in `main.py`: `app.include_router(connections.router, prefix="/api/v1")`. Run → PASS.
- [ ] **Step 5: Commit**

```bash
git add apps/backend/routers/connections.py apps/backend/main.py apps/backend/tests/credentials/test_connections_routes.py
git commit -m "feat(credentials): connections router — list/paste-connect/disconnect (Bucket A)"
```

---

## M5 — OAuth dance scaffolding (Buckets B/C)

The one genuinely new acquisition piece. PKCE+state store → authorize URL → generic callback → exchange → vault → apply.

### Task 5.1: OAuth state repo (PKCE verifier + state, TTL)

**Files:**
- Create: `apps/backend/core/repositories/oauth_state_repo.py`
- Test: `apps/backend/tests/credentials/test_oauth_dance.py`

- [ ] **Step 1: Failing test**

```python
# apps/backend/tests/credentials/test_oauth_dance.py
from core.repositories import oauth_state_repo

def test_state_roundtrip(monkeypatch):
    store = {}
    monkeypatch.setattr(oauth_state_repo, "_table",
                        lambda: type("T", (), {
                            "put_item": lambda self, Item: store.update({Item["state"]: Item}),
                            "get_item": lambda self, Key: {"Item": store.get(Key["state"])} if store.get(Key["state"]) else {},
                            "delete_item": lambda self, Key: store.pop(Key["state"], None)})())
    oauth_state_repo.put("st1", owner_id="org_1", provider="google", verifier="v")
    rec = oauth_state_repo.consume("st1")
    assert rec["owner_id"] == "org_1" and rec["verifier"] == "v"
    assert oauth_state_repo.consume("st1") is None  # single-use
```

- [ ] **Step 2:** Run → FAIL. **Step 3: Implement**

```python
# apps/backend/core/repositories/oauth_state_repo.py
import time
from core.dynamodb import get_table

_TTL_SECONDS = 600

def _table():
    return get_table("oauth-state")   # short name; DYNAMODB_TABLE_PREFIX auto-applied

def put(state: str, *, owner_id: str, provider: str, verifier: str) -> None:
    _table().put_item(Item={"state": state, "owner_id": owner_id, "provider": provider,
                            "verifier": verifier, "ttl": int(time.time()) + _TTL_SECONDS})

def consume(state: str) -> dict | None:
    rec = _table().get_item(Key={"state": state}).get("Item")
    if not rec:
        return None
    _table().delete_item(Key={"state": state})            # single-use
    if int(rec["ttl"]) < int(time.time()):                # belt-and-suspenders; DDB TTL deletion can lag up to 48h
        return None
    return rec
```

- [ ] **Step 4:** Run → PASS. **Step 5: Commit** (`feat(credentials): oauth-state repo (single-use PKCE+state)`).

### Task 5.2: OAuth dance service (authorize URL + code exchange)

**Files:**
- Create: `apps/backend/core/services/credentials/oauth_dance.py`
- Test: `apps/backend/tests/credentials/test_oauth_dance.py` (extend)

- [ ] **Step 1: Failing test**

```python
def test_build_authorize_url_has_pkce_and_state(monkeypatch):
    from core.services.credentials import oauth_dance
    saved = {}
    monkeypatch.setattr(oauth_dance.oauth_state_repo, "put",
                        lambda state, **kw: saved.update(state=state, **kw))
    url = oauth_dance.start("org_1", "google", redirect_uri="https://x/cb")
    assert "code_challenge=" in url and "code_challenge_method=S256" in url
    assert f"state={saved['state']}" in url
    assert "access_type=offline" in url   # google extra param
```

- [ ] **Step 2:** Run → FAIL. **Step 3: Implement**

```python
# apps/backend/core/services/credentials/oauth_dance.py
"""Generic 3-legged OAuth (authorization_code + PKCE). One BYO OAuth app per
provider; metadata comes from the registry. Mirrors the ChatGPT precedent but,
unlike it, we generate PKCE/state ourselves (the provider does not)."""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode

import httpx

from core.repositories import oauth_state_repo
from core.services.credentials import registry


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def start(owner_id: str, provider_key: str, *, redirect_uri: str) -> str:
    p = registry.get_provider(provider_key)
    assert p.oauth, f"{provider_key} has no oauth spec"
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(32)
    oauth_state_repo.put(state, owner_id=owner_id, provider=provider_key, verifier=verifier)
    params = {
        "client_id": _client_id(provider_key),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": p.oauth.scope_separator.join(p.oauth.scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        **p.oauth.extra_authorize_params,
    }
    return f"{p.oauth.authorization_url}?{urlencode(params)}"


def exchange(state: str, code: str, *, redirect_uri: str) -> tuple[str, str, dict]:
    rec = oauth_state_repo.consume(state)
    if not rec:
        raise ValueError("invalid or expired OAuth state")
    p = registry.get_provider(rec["provider"])
    resp = httpx.post(p.oauth.token_url, data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": _client_id(rec["provider"]),
        "client_secret": _client_secret(rec["provider"]),
        "code_verifier": rec["verifier"],
    }, timeout=15)
    resp.raise_for_status()
    tok = resp.json()
    credential = {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token", ""),
        "token_expiry": _expiry_iso(tok.get("expires_in")),
        "client_id": _client_id(rec["provider"]),
        "client_secret": _client_secret(rec["provider"]),
    }
    return rec["owner_id"], rec["provider"], credential


# BYO OAuth app creds per provider, from Secrets Manager (one set per provider per env).
def _client_id(provider: str) -> str: ...      # read isol8/<env>/oauth-clients/<provider>/client_id
def _client_secret(provider: str) -> str: ...  # read .../client_secret
def _expiry_iso(expires_in): ...               # now + expires_in -> ISO8601, or "" if absent
```

> Implement `_client_id`/`_client_secret` via the existing KMS/Secrets-Manager helper (`core/crypto/kms_secrets.py`); `_expiry_iso` is `datetime.now(tz=utc)+timedelta(seconds=expires_in)`. These are mechanical — show full bodies when implementing; no placeholders ship.

- [ ] **Step 4:** Run → PASS. **Step 5: Commit** (`feat(credentials): generic OAuth dance (authorize + PKCE exchange)`).

### Task 5.3: Authorize + callback routes

**Files:**
- Modify: `apps/backend/routers/connections.py`
- Test: `apps/backend/tests/credentials/test_connections_routes.py` (extend)

- [ ] **Step 1: Failing test**

```python
def test_authorize_returns_redirect_url(client, auth_org_1, monkeypatch):
    monkeypatch.setattr("routers.connections.oauth_dance.start",
                        lambda owner, prov, redirect_uri: "https://accounts.google.com/o/oauth2/v2/auth?x=1")
    r = client.get("/api/v1/connections/google/authorize", headers=auth_org_1)
    assert r.status_code == 200 and r.json()["authorize_url"].startswith("https://accounts.google.com")


def test_callback_exchanges_and_applies(client, monkeypatch):
    monkeypatch.setattr("routers.connections.oauth_dance.exchange",
                        lambda state, code, redirect_uri: ("org_1", "google",
                            {"access_token": "a", "refresh_token": "r", "client_id": "c", "client_secret": "s"}))
    applied = {}
    monkeypatch.setattr("routers.connections.apply_connection.apply_connection",
                        lambda owner, prov, cred: applied.update(owner=owner, prov=prov))
    monkeypatch.setattr("routers.connections.connection_repo.put", lambda *a, **k: None)
    r = client.get("/api/v1/oauth/google/callback?code=abc&state=st1", follow_redirects=False)
    assert r.status_code in (302, 307)            # redirect back to the panel
    assert applied == {"owner": "org_1", "prov": "google"}
```

- [ ] **Step 2:** Run → FAIL. **Step 3: Implement** (add to `connections.py`)

```python
from fastapi import Request
from fastapi.responses import RedirectResponse
from core.config import settings
from core.services.credentials import oauth_dance

def _redirect_uri(provider: str) -> str:
    return f"{settings.PUBLIC_API_BASE}/api/v1/oauth/{provider}/callback"

@router.get("/{provider}/authorize")
def authorize(provider: str, auth: AuthContext = Depends(get_current_user)):
    p = registry.get_provider(provider)
    if p.acquisition != "direct_oauth":
        raise HTTPException(400, f"{provider} is not an OAuth integration")
    return {"authorize_url": oauth_dance.start(auth.owner_id, provider, redirect_uri=_redirect_uri(provider))}

# NOTE: mounted at /api/v1/oauth/... (not under /connections) — register a second
# APIRouter(prefix="/oauth") in this module and include it in main.py.
oauth_router = APIRouter(prefix="/oauth", tags=["connections"])

@oauth_router.get("/{provider}/callback")
def callback(provider: str, code: str, state: str):
    owner_id, prov, credential = oauth_dance.exchange(state, code, redirect_uri=_redirect_uri(provider))
    connection_repo.put(owner_id, prov, credential, status="connected")
    apply_connection.apply_connection(owner_id, prov, credential)
    return RedirectResponse(f"{settings.FRONTEND_BASE}/settings/connections?connected={prov}")
```

- [ ] **Step 4:** Register `oauth_router` in `main.py` under `/api/v1`. Add the public ALB host route / confirm `PUBLIC_API_BASE` resolves to the externally-reachable API origin (the callback must be reachable by the customer's browser). Run → PASS.
- [ ] **Step 5: Commit** (`feat(credentials): OAuth authorize + callback routes`).

---

## M6 — Google via `gog` (FINAL design 2026-05-30)

**Decision: KEEP `gog`** (not ADC / a Printing-Press CLI). gog is steipete's (OpenClaw's author) and OpenClaw hardcodes it by binary name — the gateway auto-spawns `gog gmail watch serve`, ships a bundled gog skill, and gog gives 280 Workspace commands + a stable agent exit-code contract. A generic ADC/`google-auth` tool can't match the OpenClaw-native integration. (Evidence: gog-assessment workflow synthesis 2026-05-29.)

**Acquisition = Isol8-brokered OAuth (the M5 dance); seed = run gog on the BACKEND against the owner's EFS; retain nothing** (decided 2026-05-30 — "Option 2", chosen over in-container exec because gog's native `--remote` flow forces a paste-the-URL UX + a full ECS-Exec/SSM channel). The backend already does the OAuth code exchange (`oauth_dance.exchange`), already mounts per-user EFS, and already runs as root there. So the `gog_cli` materializer runs the gog binary **on the backend** with `XDG_CONFIG_HOME` pointed at `EFS_MOUNT_PATH/{owner}/.config` — gog writes its real, password-derived keyring exactly where the container reads it (`/home/node/.openclaw/.config/gogcli`; the per-user EFS access point mounts at `/home/node/.openclaw`, `container-stack.ts:462`). **No ECS-Exec, no SSM, no in-container exec.** The keyring is a portable (machine-independent, password-derived) JWE, so a backend-written keyring decrypts in the container under the same shared `GOG_KEYRING_PASSWORD`. The container's gog remains the runtime actor + self-refresher (`refresh_owner: actor`); the backend touches the token only during the exchange and **keeps nothing** (Task 6.5).

**Verified gog contract (probed against gog v0.11.0):**
- `gog auth credentials set - --no-input` ← stdin `{"web":{"client_id":…,"client_secret":…,"auth_uri":"https://accounts.google.com/o/oauth2/auth","token_uri":"https://oauth2.googleapis.com/token"}}` (a **flat** client is rejected — the `web`/`installed` wrapper is required) → writes `$XDG_CONFIG_HOME/gogcli/credentials.json`.
- `gog auth tokens import - --no-input` ← stdin `{"email": <addr>, "refresh_token": <rt>}` (the field is **`email`, NOT `account`**; other fields are ignored) → writes the keyring.
- `GOG_KEYRING_BACKEND=file` + `GOG_KEYRING_PASSWORD` required at seed **and** every runtime read. Linux honors `XDG_CONFIG_HOME` (macOS ignores it — irrelevant; backend + container are Linux).

**Supersedes** the M3 `gog_keyring` file-write materializer (can't fabricate the JWE keyring) and the earlier "ECS-exec in container" M6 draft.

### Task 6.1: Register the Google BYO OAuth app (one-time, manual platform step)
- [ ] Register an Isol8 OAuth **Web** app in Google Cloud Console (scopes from the registry; redirect `https://api-<env>.isol8.co/api/v1/oauth/google/callback`); store `{client_id, client_secret}` in Secrets Manager `isol8/<env>/oauth-clients/google` (the M5 dance already reads this). **One-time; all users share it (BYO client). No per-user step.**

### Task 6.2: Shared `GOG_KEYRING_PASSWORD` secret + container/runtime env (IaC)
- [ ] `auth-stack.ts`: create one shared secret `isol8/<env>/gog-keyring-password` (random, KMS-encrypted) beside `encryptionKey`. Export its name like the others.
- [ ] `container-stack.ts` per-user openclaw task-def: add env `XDG_CONFIG_HOME=/home/node/.openclaw/.config` + `GOG_KEYRING_BACKEND=file` (alongside `HOME`); add secret `GOG_KEYRING_PASSWORD` `valueFrom` the shared secret. The `ecs_manager` cloner preserves base env/secrets, so no `ecs_manager.py` change.
- [ ] `service-stack.ts` backend service: add secret `GOG_KEYRING_PASSWORD` `valueFrom` the same shared secret (the backend's gog run needs it).
- [ ] `config.py`: add `GOG_KEYRING_PASSWORD: str = os.getenv("GOG_KEYRING_PASSWORD", "")`.
- [ ] Tests: CDK assertions that the per-user task def carries the 2 env keys + the `GOG_KEYRING_PASSWORD` secret, and the backend service carries the secret.

### Task 6.3: gog binary in the backend image
- [ ] `apps/backend/Dockerfile`: add a `golang`-builder stage that `go install`s the **pinned** gogcli at the **same version** as `apps/infra/openclaw/Dockerfile` (pin BOTH — keyring-format compat); `COPY --from=builder` the binary to `/usr/local/bin/gog` in the runtime stage. Add a comment that the two pins move in lockstep. (No Go in the final image.)

### Task 6.4: `gog_cli` materializer — run gog on the backend
- [ ] Rewrite `materializers/gog_keyring.py` → `materializers/gog_cli.py`:
  - Fetch the account email: `GET https://gmail.googleapis.com/gmail/v1/users/me/profile` with `Authorization: Bearer <access_token>` → `emailAddress` (the `gmail.modify` scope already grants it — no scope change).
  - Build the client JSON (`web` wrapper) and token JSON (`{"email": …, "refresh_token": …}`).
  - `env = {**os.environ, "XDG_CONFIG_HOME": str(owner_efs_root(owner_id) / ".config"), "GOG_KEYRING_BACKEND": "file", "GOG_KEYRING_PASSWORD": settings.GOG_KEYRING_PASSWORD}`.
  - `subprocess.run(["gog","auth","credentials","set","-","--no-input"], input=client_json, env=env, check=True, capture_output=True, text=True)` then the same for `["gog","auth","tokens","import","-","--no-input"]` with `token_json`. Fixed argv only — never caller-supplied args.
  - `chown` the `…/.config/gogcli` tree to `1000:1000` when `os.geteuid()==0` (mirror `base.py`).
  - `remove(owner_id, …)`: `rm -rf` the `owner_efs_root(owner_id)/.config/gogcli` dir.
  - `materializers/__init__.py`: register `MATERIALIZERS["gog_cli"]` + `MATERIALIZER_REMOVERS["gog_cli"]`; drop `gog_keyring`.
  - `providers.yaml`: `google.wiring.materializer: gog_cli`.
- [ ] Tests (mock `subprocess.run` + `httpx`): assert the two argv lists, the stdin JSON shapes (web wrapper; `email` not `account`), the env (`XDG_CONFIG_HOME` = owner EFS `.config`, keyring backend/password), the getProfile call, and the chown; `remove()` deletes the gogcli dir.

### Task 6.5: Non-retention for `refresh_owner: actor` providers
- [ ] After `apply_connection` succeeds for a provider whose `refresh_owner == "actor"`, scrub the secret value from the connections vault row (keep the connection **record/metadata** so the UI still lists it + disconnect works). The durable credential then lives only in the container's EFS keyring; the backend kept nothing. Implement in the connect + oauth-callback paths in `connections.py` (or a `connection_repo` helper), gated on the registry `refresh_owner`.
- [ ] Tests: after a google connect, the vault row carries no `refresh_token`/`access_token`; the connection still lists as connected; disconnect still removes the EFS keyring + the row.

### Task 6.6: End-to-end smoke (dev, manual)
- [ ] One-click Connect Google in the panel (backend exchanges + seeds) → keyring on EFS (`ls /home/node/.openclaw/.config/gogcli/`) → `gog gmail messages list --json --no-input` in the container returns mail → force-expire → gog self-refreshes **in the container** → agent uses Google in chat, **no restart** → confirm the vault row retains **no** refresh token (non-retention).

**Exit:** one-click Google connect; the agent reads Gmail / writes Calendar; gog self-refreshes in the container; the backend retained nothing.

### Task 6.7 (separate sub-milestone): inbound Gmail → agent — central push, thin backend
**Deferred — pursue when "agent reacts to new mail" is a product requirement.** Verified design (gog-assessment + gmail-push workflows 2026-05-29): **central Pub/Sub PUSH, thin backend, container owns the Gmail work.** NOT per-tenant Pub/Sub, NOT per-tenant Tailscale (those are OpenClaw's single-operator shape — Tailscale exists only to expose the in-container watch-serve publicly and defaults to off; Isol8 already has a public edge).

- **Backend owns (thin):** ONE shared GCP Pub/Sub topic + ONE push subscription → a new public `/api/v1/webhooks/gmail` route. (The API edge is the public **API Gateway HTTP API**; Clerk is a per-route dependency, not an edge gate — `webhooks.py` and the OAuth callback are already public, unauth'd-by-Clerk routes. So this is the same pattern as the Stripe webhook, auth'd by Google's **OIDC JWT**, not Clerk.) On push: verify OIDC, decode `emailAddress` → `owner_id`, **forward the raw push** to that owner's container's `gog gmail watch serve` port (in-VPC, like the `:18789` hook port — confirm reachability at build time).
- **Container owns (native gog, no new Gmail code):** set `hooks.gmail.{account, topic=<shared topic>, tailscale.mode=off}` and add `"presets": ["gmail"]` to the hooks block in `config.py:687-721` (**the one-line gap** — today `/hooks/gmail` 404s, `hooks-request-handler.ts:456`). The gateway auto-starts `gog gmail watch start` (register + 12h renew → shared topic) and `gog gmail watch serve` (local), which does `history.list` + message fetch + the `/hooks/gmail` POST → agent run. We reimplement none of gog's Gmail logic.
- **Push, not pull (long-term):** you already run a public webhook surface, so pull's "no public endpoint" advantage is moot; push is lower-ops (Google owns delivery/retries/fan-out) and matches the existing webhook pattern. Verify the Pub/Sub OIDC JWT, ACK fast + process async, idempotent on message id.

**Exit (when pursued):** a new email to a connected user's inbox wakes their agent — no per-tenant infra.

---

## M7 — Connections panel UI

### Task 7.1: `useConnections` hook + panel page

**Files:**
- Create: `apps/frontend/src/hooks/useConnections.ts`
- Create: `apps/frontend/src/app/settings/connections/page.tsx`
- Create: `apps/frontend/src/components/settings/ConnectionsList.tsx`
- Create: `apps/frontend/src/components/settings/ConnectButton.tsx`

- [ ] **Step 1:** `useConnections.ts` — SWR over `GET /connections` via `useApi()`; `connectPaste(provider, token)` → `POST /connections/{provider}`; `startOAuth(provider)` → `GET /connections/{provider}/authorize` then `window.location.href = authorize_url`; `disconnect(provider)` → `DELETE`. Mirror `useChatGPTOAuth.ts` for the redirect/return handling (`?connected=` query param refresh).

```typescript
// apps/frontend/src/hooks/useConnections.ts (shape)
export function useConnections() {
  const api = useApi();
  const { data, mutate } = useSWR("/connections", (p) => api.get(p));
  const connectPaste = async (provider: string, token: string) => {
    await api.post(`/connections/${provider}`, { token }); await mutate();
  };
  const startOAuth = async (provider: string) => {
    const { authorize_url } = await api.get(`/connections/${provider}/authorize`);
    window.location.href = authorize_url;
  };
  const disconnect = async (provider: string) => { await api.del(`/connections/${provider}`); await mutate(); };
  return { connections: data?.connections ?? [], connectPaste, startOAuth, disconnect, mutate };
}
```

- [ ] **Step 2:** `ConnectionsList.tsx` renders each provider with a status dot + Connect/Disconnect. `ConnectButton.tsx` shows a paste field for `acquisition === "paste_token"`, else a "Connect" button calling `startOAuth`. Reuse the visual language of `MyChannelsSection.tsx`.
- [ ] **Step 3:** `page.tsx` is a Clerk-protected `/settings/connections` route composing the list. Add a nav entry in the settings layout.
- [ ] **Step 4:** Lint + typecheck: `cd apps/frontend && pnpm run lint`. **Step 5: Commit** (`feat(frontend): integrations connections panel`).

---

## M8 — Marketplace `requires.credentials` seam

### Task 8.1: Manifest schema + pre-deploy validator

**Files:**
- Modify: `apps/backend/schemas/marketplace.py` (add `CredentialRequirement`)
- Modify: `apps/backend/core/services/catalog_service.py` (`deploy_from_artifact` pre-check)
- Test: `apps/backend/tests/credentials/test_marketplace_requires_credentials.py`

- [ ] **Step 1: Failing test**

```python
def test_deploy_blocked_when_required_provider_not_connected(monkeypatch):
    from core.services import catalog_service
    monkeypatch.setattr(catalog_service.connection_repo, "list_for_owner",
                        lambda owner: [])  # nothing connected
    manifest = {"requires": {"credentials": [{"provider": "greenhouse", "optional": False}]}}
    with pytest.raises(catalog_service.MissingCredentialsError) as e:
        catalog_service.assert_credentials_satisfied("org_1", manifest)
    assert "greenhouse" in e.value.missing


def test_deploy_allowed_when_connected(monkeypatch):
    from core.services import catalog_service
    monkeypatch.setattr(catalog_service.connection_repo, "list_for_owner",
                        lambda owner: [{"provider": "greenhouse", "status": "connected"}])
    manifest = {"requires": {"credentials": [{"provider": "greenhouse", "optional": False}]}}
    catalog_service.assert_credentials_satisfied("org_1", manifest)  # no raise
```

- [ ] **Step 2:** Run → FAIL. **Step 3: Implement** the validator + raise `MissingCredentialsError(missing=[...])`; call it at the top of `deploy_from_artifact`. The router (`marketplace_listings.py`) catches it and returns `409` with the missing-provider list so the UI can surface "Connect these first".
- [ ] **Step 4:** Run → PASS. **Step 5: Commit** (`feat(marketplace): block deploy until required providers connected`).

### Task 8.2: Fill empty credential slots on deploy

- [ ] **Step 1:** After `deploy_from_artifact` merges the seller slice (which has blanked `apiKey`/`env`/`headers` via `catalog_slice._scrub_secret_fields`), iterate the manifest's `requires.credentials`; for each already-connected provider call `apply_connection.apply_connection(owner_id, provider, decrypted_credential)` to fill the buyer's own token into the freshly-deployed config. Test that a connected provider's slot is populated post-deploy.
- [ ] **Step 2: Commit** (`feat(marketplace): fill buyer credential slots on deploy`).

### Task 8.3: Safety-scanner credential rules

- [ ] **Step 1:** Extend `core/services/marketplace_safety.py` with rules rejecting bundles that ship a hardcoded token in `env`/`headers`/`apiKey` (non-empty after scrub) or that include direct OAuth client-secret literals. Add tests. **Step 2: Commit** (`feat(marketplace): credential-leak safety rules`).

---

## Final verification (run once at the end, per feedback_run_tests_at_end)

- [ ] Backend suite: `cd apps/backend && uv run pytest tests/credentials/ -v`
- [ ] Full backend: `cd apps/backend && uv run pytest tests/ -q`
- [ ] Frontend: `cd apps/frontend && pnpm test && pnpm run lint`
- [ ] CDK synth: `cd apps/infra && pnpm cdk synth -q`
- [ ] Manual dev smoke: M6 Google end-to-end (connect → chat → self-refresh, no restart) per Task 6.3.

---

## Notes for the implementer

- **Reuse, don't reinvent.** The materializer is `workspace.py:700-786` generalized; the vault is `oauth_service` Fernet; the wiring is `config_patcher`; the paste/connect UX is `MyChannelsSection.tsx`/`useChatGPTOAuth.ts`. If you find yourself writing new EFS-write or Fernet code, stop and reuse.
- **gog keyring format is the one unknown.** Tasks 3.3 + 6.3 carry an explicit "verify against real `gog` output" step — do not assume the `tokens.json` shape; confirm it in the dev smoke and adjust the materializer.
- **OAuth metadata is transcribed from Nango's open catalog** (`packages/providers/providers.yaml`) and must be verified against each vendor's own OAuth docs. Keep registry keys aligned with Nango's provider keys.
- **Do not deploy the parked `credentials-stack.ts`** — it is the deferred Nango path and has a vault-fatal encryption-key bug. This plan touches no Nango infra.
- **Key rotation:** `core/encryption.get_fernet()` is single-key — rotating `ENCRYPTION_KEY` makes every stored connection undecryptable. If rotatability matters for the vault, have `connection_repo` decrypt via `cryptography.fernet.MultiFernet([new, old])` (encrypt with the primary, decrypt against the list) instead of a bare `Fernet`. Not required for v1, but cheap to design in now and avoids a future re-connect-everything event.
- **PR discipline:** cut a branch, never push to main directly (memory `feedback_always_pr_never_direct_main`); run targeted tests per task, full suite at the end.
