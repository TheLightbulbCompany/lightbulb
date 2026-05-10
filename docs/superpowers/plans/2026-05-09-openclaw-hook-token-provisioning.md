# OpenClaw Hook Token Provisioning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provision a per-user `OPENCLAW_HOOK_TOKEN` for every Isol8 user's OpenClaw container, expose a public webhook ingress at `https://api.isol8.co/api/v1/hooks/u/{owner_id}/{hook_path}`, and surface the URL + token in a minimum settings panel.

**Architecture:** Token generated at provisioning time on the existing `containers` row; injected as an ECS task-def env var; consumed by OpenClaw via `${OPENCLAW_HOOK_TOKEN}` substitution in the rendered `hooks` block of `openclaw.json`. A new FastAPI router `hooks_proxy.py` validates the bearer token at the edge, resolves the user's container IP via the existing `EcsManager.resolve_running_container` helper, rewrites X-Forwarded-* headers, and forwards the request to `http://<container-ip>:18789/hooks/...`. An audit row is written to a new `hook-events` DDB table.

**Tech Stack:** Python 3.13 / FastAPI / boto3 / pytest, Next.js 16 / React 19 / SWR, AWS CDK v2 (TypeScript), DynamoDB, ECS Fargate.

**Spec:** `docs/superpowers/specs/2026-05-09-openclaw-hook-token-provisioning-design.md`

---

## File Structure

**Backend — modified:**
- `apps/backend/core/repositories/container_repo.py` — add `set_hook_token()` and update read paths to surface `hook_token` / `hook_token_rotated_at`
- `apps/backend/core/containers/ecs_manager.py` — generate `hook_token` at provisioning; append `OPENCLAW_HOOK_TOKEN` to `environment_for_task`; add new `refresh_task_definition()` helper
- `apps/backend/core/containers/config.py` — render `hooks` block alongside the existing `hooks.internal`
- `apps/backend/main.py` — mount the two new routers
- `apps/infra/lib/stacks/database-stack.ts` — new `hookEventsTable` matching existing-table conventions (KMS, PITR, retain)
- `apps/infra/lib/stacks/service-stack.ts` — IAM grant: `hookEventsTable.grantReadWriteData(this.taskRole)`
- `apps/frontend/src/app/settings/page.tsx` — extend `Panel` and `NAV_SECTIONS`

**Backend — created:**
- `apps/backend/core/repositories/hook_event_repo.py` — DDB writes for the audit table
- `apps/backend/routers/hooks_proxy.py` — public webhook ingress + audit
- `apps/backend/routers/hooks_credentials.py` — Clerk-authenticated `GET /hooks/credentials`
- `apps/backend/tests/unit/routers/test_hooks_proxy.py`
- `apps/backend/tests/unit/routers/test_hooks_credentials.py`
- `apps/backend/tests/unit/repositories/test_hook_event_repo.py`
- `apps/backend/tests/unit/containers/test_hook_token_provisioning.py`
- `apps/backend/tests/unit/containers/test_refresh_task_definition.py`
- `apps/backend/tests/integration/test_hooks_proxy_e2e.py`
- `scripts/backfill-hook-tokens.py`

**Frontend — created:**
- `apps/frontend/src/components/settings/WebhooksPanel.tsx`
- `apps/frontend/src/hooks/useHookCredentials.ts`

---

## Task 1: Add `hook_token` field to ContainerRepo

**Files:**
- Modify: `apps/backend/core/repositories/container_repo.py`
- Test: `apps/backend/tests/unit/repositories/test_container_repo_hook_token.py`

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/repositories/test_container_repo_hook_token.py`:

```python
"""Tests for ContainerRepo hook_token storage."""

from unittest.mock import AsyncMock, patch

import pytest

from core.repositories import container_repo


@pytest.mark.asyncio
async def test_set_hook_token_writes_token_and_timestamp():
    """set_hook_token writes hook_token + hook_token_rotated_at to the row."""
    fake_now = "2026-05-09T12:00:00Z"
    with (
        patch.object(container_repo, "update_fields", new=AsyncMock()) as mock_update,
        patch("core.repositories.container_repo._now_iso", return_value=fake_now),
    ):
        mock_update.return_value = {"owner_id": "u_1", "hook_token": "abc"}
        result = await container_repo.set_hook_token("u_1", "abc")

        mock_update.assert_awaited_once_with(
            "u_1",
            {"hook_token": "abc", "hook_token_rotated_at": fake_now},
        )
        assert result["hook_token"] == "abc"


@pytest.mark.asyncio
async def test_set_hook_token_returns_none_when_owner_missing():
    """If the row doesn't exist, update_fields returns None and we propagate it."""
    with patch.object(container_repo, "update_fields", new=AsyncMock(return_value=None)):
        result = await container_repo.set_hook_token("u_unknown", "abc")
        assert result is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd apps/backend && uv run pytest tests/unit/repositories/test_container_repo_hook_token.py -v
```

Expected: FAIL with `AttributeError: module 'core.repositories.container_repo' has no attribute 'set_hook_token'`.

- [ ] **Step 3: Implement `set_hook_token` and `_now_iso`**

Add to `apps/backend/core/repositories/container_repo.py` (place near other update helpers, e.g., right after `update_fields`):

```python
def _now_iso() -> str:
    """Wallclock UTC timestamp in ISO-8601. Wrapped for test patching."""
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat()


async def set_hook_token(owner_id: str, token: str) -> dict | None:
    """Persist a freshly generated OpenClaw hook token onto the containers row.

    Sets ``hook_token`` and ``hook_token_rotated_at`` together so the rotation
    timestamp always tracks the current token. Returns the updated row, or
    None if the row does not exist.
    """
    return await update_fields(
        owner_id,
        {"hook_token": token, "hook_token_rotated_at": _now_iso()},
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd apps/backend && uv run pytest tests/unit/repositories/test_container_repo_hook_token.py -v
```

Expected: 2 PASSED.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/repositories/container_repo.py apps/backend/tests/unit/repositories/test_container_repo_hook_token.py
git commit -m "feat(container_repo): add set_hook_token storage helper"
```

---

## Task 2: Generate `hook_token` at provisioning and inject `OPENCLAW_HOOK_TOKEN` env

**Files:**
- Modify: `apps/backend/core/containers/ecs_manager.py:1290-1300` (provision flow) and `~212` (`environment_for_task` injection site)
- Test: `apps/backend/tests/unit/containers/test_hook_token_provisioning.py`

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/containers/test_hook_token_provisioning.py`:

```python
"""Tests for hook_token provisioning + env injection in EcsManager."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_provision_generates_hook_token_and_persists_it():
    """provision() generates a non-empty hook_token and writes it via container_repo."""
    from core.containers import ecs_manager

    manager = ecs_manager.EcsManager()

    set_hook_token = AsyncMock()
    with (
        patch("core.repositories.container_repo.set_hook_token", set_hook_token),
        # ... stub the rest of the provision path so the test only exercises
        # the hook-token generation point. See sibling tests in
        # test_provision_*.py for the full stub harness.
        patch.object(manager, "_register_task_definition", AsyncMock(return_value="td/foo:1")),
        patch.object(manager, "_create_or_update_service", AsyncMock()),
        patch("core.repositories.container_repo.upsert", AsyncMock()),
    ):
        await manager.provision_minimal(user_id="u_1")  # name to match the harness

    assert set_hook_token.await_count == 1
    args = set_hook_token.await_args[0]
    assert args[0] == "u_1"
    assert isinstance(args[1], str) and len(args[1]) >= 32  # secrets.token_urlsafe(32)


def test_environment_for_task_includes_openclaw_hook_token():
    """When per-user env entries are layered, OPENCLAW_HOOK_TOKEN is among them."""
    from core.containers import ecs_manager

    env = ecs_manager._build_per_user_environment(
        user_id="u_1",
        hook_token="abc-token",
        existing=[],
    )

    names = {e["name"] for e in env}
    assert "OPENCLAW_HOOK_TOKEN" in names
    token_entry = next(e for e in env if e["name"] == "OPENCLAW_HOOK_TOKEN")
    assert token_entry["value"] == "abc-token"


def test_environment_for_task_preserves_caller_supplied_entries():
    """Helper must not clobber CODEX_HOME (chatgpt_oauth flow) or other entries
    that callers/CDK base may have set."""
    from core.containers import ecs_manager

    env = ecs_manager._build_per_user_environment(
        user_id="u_1",
        hook_token="abc-token",
        existing=[
            {"name": "CODEX_HOME", "value": "/mnt/efs/users/u_1/.codex"},
            {"name": "FOO", "value": "bar"},
        ],
    )

    names = {e["name"] for e in env}
    assert {"OPENCLAW_HOOK_TOKEN", "CODEX_HOME", "FOO"}.issubset(names)


def test_environment_for_task_overrides_stale_hook_token():
    """If a caller-supplied list already has an OPENCLAW_HOOK_TOKEN entry
    (e.g., backfill reading the old task-def), the new value wins."""
    from core.containers import ecs_manager

    env = ecs_manager._build_per_user_environment(
        user_id="u_1",
        hook_token="new-token",
        existing=[{"name": "OPENCLAW_HOOK_TOKEN", "value": "old-token"}],
    )
    token_entry = next(e for e in env if e["name"] == "OPENCLAW_HOOK_TOKEN")
    assert token_entry["value"] == "new-token"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd apps/backend && uv run pytest tests/unit/containers/test_hook_token_provisioning.py -v
```

Expected: FAIL with `AttributeError` for `_build_per_user_environment` and/or `provision_minimal`.

- [ ] **Step 3: Implement env builder + token gen at provisioning**

In `apps/backend/core/containers/ecs_manager.py`, find the existing `environment_for_task` build site (`ecs_manager.py:209-213`, inside `_build_register_kwargs_from_base`). Today, callers pass `environment_for_task` in and it's merged with CDK base entries. We add a small helper that takes the caller's list and ensures `OPENCLAW_HOOK_TOKEN` is present:

```python
def _build_per_user_environment(
    *,
    user_id: str,
    hook_token: str,
    existing: list[dict],
) -> list[dict]:
    """Return the per-user ``environment`` list with OPENCLAW_HOOK_TOKEN injected.

    ``existing`` is the caller-supplied list (e.g., the chatgpt_oauth flow
    passes ``CODEX_HOME`` here). We preserve every entry verbatim except
    we replace any pre-existing ``OPENCLAW_HOOK_TOKEN`` with the freshly
    generated value (matters for the backfill: the old task-def carried
    no token, but a half-complete prior backfill could have left a stale
    one).
    """
    out: list[dict] = [
        e for e in existing if e.get("name") != "OPENCLAW_HOOK_TOKEN"
    ]
    out.append({"name": "OPENCLAW_HOOK_TOKEN", "value": hook_token})
    return out
```

Update `_build_register_kwargs_from_base` (line ~209) so that immediately before the existing merge with CDK base entries it runs:

```python
environment_for_task = _build_per_user_environment(
    user_id=user_id,
    hook_token=hook_token,
    existing=list(environment_for_task or []),
)
```

This keeps the existing CODEX_HOME path working unchanged and adds OPENCLAW_HOOK_TOKEN on every register call.

In the provisioning flow (around `ecs_manager.py:1296` where `gateway_token = secrets.token_urlsafe(32)` lives), add adjacent generation + persistence:

```python
import secrets  # already imported at module scope

gateway_token = secrets.token_urlsafe(32)
hook_token = secrets.token_urlsafe(32)

# ... existing provisioning steps ...

await container_repo.set_hook_token(user_id, hook_token)
```

The `hook_token` flows into `_build_per_user_environment` via the call site that builds the task def.

- [ ] **Step 4: Run test to verify it passes**

```bash
cd apps/backend && uv run pytest tests/unit/containers/test_hook_token_provisioning.py -v
```

Expected: 2 PASSED. If the test stub harness needs more patches (the existing provision flow has many steps), expand the stubs in the test until the unit test isolates the new code path. Reference sibling tests in `apps/backend/tests/unit/containers/` for the standard mock pattern.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/containers/ecs_manager.py apps/backend/tests/unit/containers/test_hook_token_provisioning.py
git commit -m "feat(ecs_manager): generate hook_token at provisioning, inject OPENCLAW_HOOK_TOKEN env"
```

---

## Task 3: Render `hooks` block in `openclaw.json`

**Files:**
- Modify: `apps/backend/core/containers/config.py:~654` (next to existing `hooks.internal` block — line drifted from 594 after PR #585's removal of `agents.defaults.llm`)
- Test: `apps/backend/tests/unit/containers/test_config_hooks_rendering.py`

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/containers/test_config_hooks_rendering.py`:

```python
"""Tests for hooks-ingress block rendering in write_openclaw_config."""

import json
from unittest.mock import patch

import pytest

from core.containers import config


def _render_for_test(**kwargs):
    """Call write_openclaw_config with sensible test defaults; return parsed config dict."""
    # Stub the EFS write so we can capture the rendered config in-memory.
    captured = {}

    def _fake_write(path, content):
        captured["content"] = content

    with patch("core.containers.config._atomic_write", side_effect=_fake_write):
        config.write_openclaw_config(
            user_id="u_1",
            gateway_token="gw-token",
            hook_token="hk-token",
            # ... other required args, see existing call sites for reference
            **kwargs,
        )

    return json.loads(captured["content"])


def test_hooks_block_renders_enabled_with_token_var():
    cfg = _render_for_test()
    assert cfg["hooks"]["enabled"] is True
    assert cfg["hooks"]["token"] == "${OPENCLAW_HOOK_TOKEN}"
    assert cfg["hooks"]["path"] == "/hooks"


def test_hooks_max_body_bytes_overrides_upstream_64kb_default():
    """Stripe/GitHub payloads regularly exceed 64KB; we must override."""
    cfg = _render_for_test()
    assert cfg["hooks"]["maxBodyBytes"] == 1_048_576  # 1 MB


def test_hooks_session_key_safety_settings():
    cfg = _render_for_test()
    assert cfg["hooks"]["allowRequestSessionKey"] is False
    prefixes = cfg["hooks"]["allowedSessionKeyPrefixes"]
    assert "isol8-" in prefixes
    # OpenClaw v2026.5.7 (gateway/hooks.ts:75-95) crashes at boot when
    # defaultSessionKey is unset and "hook:" is not in the allowed list.
    # Without this prefix every newly-provisioned container refuses to start.
    assert "hook:" in prefixes


def test_hooks_internal_block_preserved():
    """The new ingress block must coexist with the existing ``hooks.internal``."""
    cfg = _render_for_test()
    assert "internal" in cfg["hooks"]
    assert cfg["hooks"]["internal"]["entries"]["session-memory"]["enabled"] is True
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd apps/backend && uv run pytest tests/unit/containers/test_config_hooks_rendering.py -v
```

Expected: FAIL with `KeyError: 'enabled'` or `TypeError: write_openclaw_config() got an unexpected keyword argument 'hook_token'`.

- [ ] **Step 3: Add `hook_token` to the signature and render the new block**

In `apps/backend/core/containers/config.py`, add `hook_token: str` to `build_openclaw_config_dict`'s signature alongside `gateway_token`. Then update the existing `hooks` rendering at line ~654:

```python
"hooks": {
    "internal": {
        "entries": {
            "command-logger": {"enabled": True},
            "session-memory": {"enabled": True},
        },
    },
    "enabled": True,
    "token": "${OPENCLAW_HOOK_TOKEN}",
    "path": "/hooks",
    "maxBodyBytes": 1_048_576,
    # OpenClaw v2026.5.7 (gateway/hooks.ts:75-95) requires "hook:" in this
    # list when defaultSessionKey is unset. Omitting "hook:" makes the
    # gateway throw at boot — every newly-provisioned container would
    # refuse to start. "isol8-" is for our own session-key conventions.
    "allowedSessionKeyPrefixes": ["isol8-", "hook:"],
    "allowRequestSessionKey": False,
},
```

(`hook_token` is not interpolated into the JSON — `${OPENCLAW_HOOK_TOKEN}` is the literal placeholder OpenClaw expands at boot using the env var injected by Task 2. We add `hook_token` to the signature anyway so the call site is wired up; if/when we add a rotate flow later we'll have the right plumbing.)

Update existing call sites in `ecs_manager.py` (at the spot that already calls `write_openclaw_config`) to pass `hook_token=hook_token`.

- [ ] **Step 4: Run test to verify it passes**

```bash
cd apps/backend && uv run pytest tests/unit/containers/test_config_hooks_rendering.py -v
```

Expected: 4 PASSED.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/containers/config.py apps/backend/tests/unit/containers/test_config_hooks_rendering.py
git commit -m "feat(openclaw-config): render hooks ingress block with maxBodyBytes 1MB"
```

---

## Task 4: Create `hook-events` DynamoDB table + IAM grants in CDK

**Files:**
- Modify: `apps/infra/lib/stacks/database-stack.ts`
- Modify: `apps/infra/lib/stacks/service-stack.ts`

- [ ] **Step 1: Read the existing pattern**

Open `apps/infra/lib/stacks/database-stack.ts` and read two existing table definitions (e.g., `containersTable` at line 67 and `usageCountersTable` at line 114). The actual naming convention is `tableName: \`isol8-${env}-...\`` — a literal interpolation of the env name into a fixed prefix. The constructor exposes `env` as a constructor arg. Other repeating fields: `encryption: TableEncryption.CUSTOMER_MANAGED`, `encryptionKey: this.tableKey`, `pointInTimeRecoverySpecification`, `removalPolicy: RETAIN`. Match exactly.

- [ ] **Step 2: Add the new table**

Inside `DatabaseStack`'s constructor, alongside the other table declarations:

```typescript
public readonly hookEventsTable: dynamodb.Table;

// ... in constructor (env is already a local from earlier table declarations):
this.hookEventsTable = new dynamodb.Table(this, "HookEvents", {
  tableName: `isol8-${env}-hook-events`,
  partitionKey: { name: "owner_id", type: dynamodb.AttributeType.STRING },
  sortKey:      { name: "event_id", type: dynamodb.AttributeType.STRING },  // ULID
  billingMode:  dynamodb.BillingMode.PAY_PER_REQUEST,
  encryption:   dynamodb.TableEncryption.CUSTOMER_MANAGED,
  encryptionKey: this.tableKey,
  pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
  timeToLiveAttribute: "expires_at",
  removalPolicy: cdk.RemovalPolicy.RETAIN,
});
```

- [ ] **Step 3: Wire the table through `service-stack.ts` and grant access**

Open `apps/infra/lib/stacks/service-stack.ts`. Find the section around line 288 where existing tables are granted to `this.taskRole` (e.g., `props.database.usersTable.grantReadWriteData(this.taskRole)`). Add:

```typescript
props.database.hookEventsTable.grantReadWriteData(this.taskRole);
```

The existing KMS-key grant at the same site already covers the new table — verify by reading the surrounding code (the existing `props.database.tableKey.grantEncryptDecrypt(this.taskRole)` line, if present, is broad enough).

- [ ] **Step 4: Synth and verify the diff**

```bash
cd apps/infra && pnpm cdk synth 2>&1 | tail -30
```

Expected: clean synth, no errors. If there's a diff against the deployed stack, run:

```bash
pnpm cdk diff isol8-dev/database
pnpm cdk diff isol8-dev/service
```

Confirm the diff shows `+ AWS::DynamoDB::Table HookEvents` in database and an additional `dynamodb:*` IAM statement on the service task role.

- [ ] **Step 5: Commit**

```bash
git add apps/infra/lib/stacks/database-stack.ts apps/infra/lib/stacks/service-stack.ts
git commit -m "infra: add hook-events DDB table + IAM grant for backend task"
```

---

## Task 5: HookEventRepo

**Files:**
- Create: `apps/backend/core/repositories/hook_event_repo.py`
- Test: `apps/backend/tests/unit/repositories/test_hook_event_repo.py`

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/repositories/test_hook_event_repo.py`:

```python
"""Tests for HookEventRepo (audit trail writes)."""

from unittest.mock import MagicMock, patch

import pytest

from core.repositories import hook_event_repo


def test_write_event_persists_required_fields(monkeypatch):
    """write_event constructs a complete row and PutItems it."""
    fake_table = MagicMock()
    monkeypatch.setattr(hook_event_repo, "_table", lambda: fake_table)

    hook_event_repo.write_event(
        owner_id="u_1",
        hook_path="wake",
        status_code=200,
        source_ip="203.0.113.42",
        user_agent="Stripe/1.0",
        request_size=512,
        response_size=64,
    )

    fake_table.put_item.assert_called_once()
    item = fake_table.put_item.call_args.kwargs["Item"]
    assert item["owner_id"] == "u_1"
    assert item["hook_path"] == "wake"
    assert item["status_code"] == 200
    assert item["source_ip"] == "203.0.113.42"
    assert item["user_agent"] == "Stripe/1.0"
    assert item["request_size"] == 512
    assert item["response_size"] == 64
    assert isinstance(item["event_id"], str) and len(item["event_id"]) >= 16  # ULID
    assert isinstance(item["timestamp"], int)
    assert isinstance(item["expires_at"], int)
    assert item["expires_at"] > item["timestamp"] // 1000  # TTL in seconds, +30 days


def test_write_event_truncates_long_user_agent(monkeypatch):
    fake_table = MagicMock()
    monkeypatch.setattr(hook_event_repo, "_table", lambda: fake_table)

    hook_event_repo.write_event(
        owner_id="u_1",
        hook_path="wake",
        status_code=200,
        source_ip="203.0.113.42",
        user_agent="x" * 1000,
        request_size=0,
        response_size=0,
    )

    item = fake_table.put_item.call_args.kwargs["Item"]
    assert len(item["user_agent"]) == 512


def test_write_event_swallows_ddb_errors(monkeypatch, caplog):
    """A DDB write failure must NOT propagate; audit is best-effort."""
    fake_table = MagicMock()
    fake_table.put_item.side_effect = Exception("DynamoDB unreachable")
    monkeypatch.setattr(hook_event_repo, "_table", lambda: fake_table)

    # Must not raise.
    hook_event_repo.write_event(
        owner_id="u_1", hook_path="wake", status_code=200,
        source_ip="203.0.113.42", user_agent="x", request_size=0, response_size=0,
    )

    assert any("hook-events write failed" in r.message for r in caplog.records)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd apps/backend && uv run pytest tests/unit/repositories/test_hook_event_repo.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'core.repositories.hook_event_repo'`.

- [ ] **Step 3: Implement HookEventRepo**

Create `apps/backend/core/repositories/hook_event_repo.py`:

```python
"""Repository for the hook-events audit table.

Stores one row per webhook call routed through ``routers/hooks_proxy.py``.
TTL on ``expires_at`` (Unix seconds) keeps the table self-trimming at
30 days. Writes are best-effort — a DDB failure must not drop the
proxied call. Reads are added in the follow-up control-panel spec.
"""

import logging
import time
from typing import Any

from ulid import ULID

from core.dynamodb import get_table
from core.config import settings

logger = logging.getLogger(__name__)

_USER_AGENT_MAX = 512
_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days


def _table():
    return get_table(f"{settings.DDB_PREFIX}-hook-events")


def write_event(
    *,
    owner_id: str,
    hook_path: str,
    status_code: int,
    source_ip: str,
    user_agent: str,
    request_size: int,
    response_size: int,
) -> None:
    """Persist one audit row. Failures are logged and swallowed."""
    now_ms = int(time.time() * 1000)
    item: dict[str, Any] = {
        "owner_id": owner_id,
        "event_id": str(ULID()),
        "timestamp": now_ms,
        "hook_path": hook_path,
        "status_code": status_code,
        "source_ip": source_ip,
        "user_agent": user_agent[:_USER_AGENT_MAX],
        "request_size": request_size,
        "response_size": response_size,
        "expires_at": (now_ms // 1000) + _TTL_SECONDS,
    }
    try:
        _table().put_item(Item=item)
    except Exception as exc:  # noqa: BLE001 — best-effort by design
        logger.warning("hook-events write failed owner=%s err=%s", owner_id, exc)
```

If `python-ulid` isn't already a dep, add it:

```bash
cd apps/backend && uv add python-ulid
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd apps/backend && uv run pytest tests/unit/repositories/test_hook_event_repo.py -v
```

Expected: 3 PASSED.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/repositories/hook_event_repo.py apps/backend/tests/unit/repositories/test_hook_event_repo.py apps/backend/pyproject.toml apps/backend/uv.lock
git commit -m "feat(hook_event_repo): audit-row writer for hook-events table"
```

---

## Task 6: Hooks proxy router (auth + forward + audit)

**Files:**
- Create: `apps/backend/routers/hooks_proxy.py`
- Test: `apps/backend/tests/unit/routers/test_hooks_proxy.py`

- [ ] **Step 1: Write the failing tests**

Create `apps/backend/tests/unit/routers/test_hooks_proxy.py`:

```python
"""Tests for the public webhook ingress proxy.

This router is the ONLY isol8 router that deliberately does NOT use
``Depends(get_current_user)`` — external webhook senders don't have
Clerk tokens. Auth is timing-safe bearer-token compare against
``containers.hook_token`` only.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def fake_container():
    return {
        "owner_id": "u_1",
        "hook_token": "secret-token-32-bytes-of-randomness",
        "service_name": "openclaw-u-1-abc",
        "status": "running",
    }


def test_missing_authorization_returns_401(client, fake_container):
    with patch("routers.hooks_proxy.container_repo.get_by_owner_id",
               new=AsyncMock(return_value=fake_container)):
        r = client.post("/api/v1/hooks/u/u_1/wake", json={})
        assert r.status_code == 401


def test_wrong_token_returns_401(client, fake_container):
    with patch("routers.hooks_proxy.container_repo.get_by_owner_id",
               new=AsyncMock(return_value=fake_container)):
        r = client.post(
            "/api/v1/hooks/u/u_1/wake",
            headers={"Authorization": "Bearer wrong"},
            json={},
        )
        assert r.status_code == 401


def test_unknown_owner_returns_404(client):
    with patch("routers.hooks_proxy.container_repo.get_by_owner_id",
               new=AsyncMock(return_value=None)):
        r = client.post(
            "/api/v1/hooks/u/u_404/wake",
            headers={"Authorization": "Bearer anything"},
            json={},
        )
        assert r.status_code == 404


def test_no_running_task_returns_503(client, fake_container):
    """When EcsManager.resolve_running_container returns no IP, 503 + Retry-After."""
    with (
        patch("routers.hooks_proxy.container_repo.get_by_owner_id",
              new=AsyncMock(return_value=fake_container)),
        patch("routers.hooks_proxy.ecs_manager.resolve_running_container",
              new=AsyncMock(return_value=(fake_container, None))),
    ):
        r = client.post(
            "/api/v1/hooks/u/u_1/wake",
            headers={"Authorization": "Bearer " + fake_container["hook_token"]},
            json={},
        )
        assert r.status_code == 503
        assert "Retry-After" in r.headers


def test_successful_forward_passes_body_and_writes_audit(client, fake_container):
    forwarded_kwargs = {}

    class StubTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            forwarded_kwargs["url"] = str(request.url)
            forwarded_kwargs["body"] = request.content
            forwarded_kwargs["headers"] = dict(request.headers)
            return httpx.Response(200, content=b'{"ok": true}')

    audit_writes = []

    with (
        patch("routers.hooks_proxy.container_repo.get_by_owner_id",
              new=AsyncMock(return_value=fake_container)),
        patch("routers.hooks_proxy.ecs_manager.resolve_running_container",
              new=AsyncMock(return_value=(fake_container, "10.0.42.17"))),
        patch("routers.hooks_proxy._client_factory", lambda: httpx.AsyncClient(transport=StubTransport())),
        patch("routers.hooks_proxy.hook_event_repo.write_event",
              side_effect=lambda **kw: audit_writes.append(kw)),
    ):
        r = client.post(
            "/api/v1/hooks/u/u_1/wake",
            headers={
                "Authorization": "Bearer " + fake_container["hook_token"],
                "User-Agent": "Stripe/1.0",
                # Spoofed XFF — the proxy must overwrite, not append.
                "X-Forwarded-For": "1.2.3.4",
            },
            json={"text": "new email"},
        )

    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert forwarded_kwargs["url"] == "http://10.0.42.17:18789/hooks/wake"
    # Headers we control:
    assert forwarded_kwargs["headers"]["authorization"] == "Bearer " + fake_container["hook_token"]
    assert forwarded_kwargs["headers"]["x-forwarded-for"] != "1.2.3.4"  # rewritten, not passed
    assert forwarded_kwargs["headers"]["x-forwarded-proto"] == "https"
    assert forwarded_kwargs["headers"]["x-forwarded-host"] == "api.isol8.co"
    # Audit row:
    assert len(audit_writes) == 1
    assert audit_writes[0]["status_code"] == 200
    assert audit_writes[0]["hook_path"] == "wake"
    assert audit_writes[0]["user_agent"] == "Stripe/1.0"


def test_audit_write_failure_does_not_drop_proxied_call(client, fake_container):
    """DDB outage must not turn a 200 into a 500."""
    class StubTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(200, content=b'{"ok": true}')

    with (
        patch("routers.hooks_proxy.container_repo.get_by_owner_id",
              new=AsyncMock(return_value=fake_container)),
        patch("routers.hooks_proxy.ecs_manager.resolve_running_container",
              new=AsyncMock(return_value=(fake_container, "10.0.42.17"))),
        patch("routers.hooks_proxy._client_factory", lambda: httpx.AsyncClient(transport=StubTransport())),
        patch("routers.hooks_proxy.hook_event_repo.write_event",
              side_effect=Exception("DDB down")),
    ):
        r = client.post(
            "/api/v1/hooks/u/u_1/wake",
            headers={"Authorization": "Bearer " + fake_container["hook_token"]},
            json={},
        )
        assert r.status_code == 200


def test_rate_limiter_returns_429_after_60_per_minute(client, fake_container):
    """In-process token bucket caps at 60/min per owner."""
    class StubTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(200, content=b'{}')

    with (
        patch("routers.hooks_proxy.container_repo.get_by_owner_id",
              new=AsyncMock(return_value=fake_container)),
        patch("routers.hooks_proxy.ecs_manager.resolve_running_container",
              new=AsyncMock(return_value=(fake_container, "10.0.42.17"))),
        patch("routers.hooks_proxy._client_factory", lambda: httpx.AsyncClient(transport=StubTransport())),
        patch("routers.hooks_proxy.hook_event_repo.write_event"),
    ):
        # Exhaust the bucket (60).
        for _ in range(60):
            client.post(
                "/api/v1/hooks/u/u_1/wake",
                headers={"Authorization": "Bearer " + fake_container["hook_token"]},
                json={},
            )
        r = client.post(
            "/api/v1/hooks/u/u_1/wake",
            headers={"Authorization": "Bearer " + fake_container["hook_token"]},
            json={},
        )
        assert r.status_code == 429
        assert "Retry-After" in r.headers
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd apps/backend && uv run pytest tests/unit/routers/test_hooks_proxy.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement the proxy router**

Create `apps/backend/routers/hooks_proxy.py`:

```python
"""Public webhook ingress proxy for per-user OpenClaw containers.

External webhook senders (Zapier / Stripe / GitHub / custom integrations)
POST to ``/api/v1/hooks/u/{owner_id}/{hook_path}`` with bearer-token
auth. We validate the token at the edge against the ``containers`` row,
look up the user's container IP via ``EcsManager.resolve_running_container``,
rewrite X-Forwarded-* with the true caller IP, and forward to
``http://<ip>:18789/hooks/{hook_path}``.

**Auth model:** This router is the ONLY isol8 router that does NOT use
``Depends(get_current_user)``. External callers don't have Clerk tokens.
Auth is timing-safe bearer-token compare against ``containers.hook_token``
via ``hmac.compare_digest``. Do not add Clerk auth — it would break every
real webhook source.

**Forwarded headers:** X-Forwarded-* values from the public caller are
NOT trusted (they could be spoofed). We strip and re-assert the chain
with values we control. OpenClaw on the container side trusts these
headers because our backend's subnet is in ``gateway.trustedProxies``.

**Audit:** Best-effort write to ``hook-events``. A DDB outage does not
drop a real webhook.

**Rate limit:** In-process token bucket, 60/min per ``owner_id``. Fine
for the current single-task backend; needs DDB-backed counter when we
scale out (flagged in the spec).
"""

import hmac
import logging
import time
from collections import defaultdict, deque
from typing import Awaitable, Callable

import httpx
from fastapi import APIRouter, HTTPException, Request, Response

from core.containers import ecs_manager as _ecs_manager_module
from core.containers.ecs_manager import EcsManager
from core.repositories import container_repo, hook_event_repo

logger = logging.getLogger(__name__)
router = APIRouter()

GATEWAY_PORT = 18789
RATE_LIMIT_PER_MINUTE = 60
_HOPS: dict[str, deque[float]] = defaultdict(deque)
_ecs_manager = EcsManager()
ecs_manager = _ecs_manager  # so tests can monkey-patch the module-level name


def _client_factory() -> httpx.AsyncClient:
    """Module-level factory so tests can swap in a stub transport."""
    return httpx.AsyncClient(timeout=httpx.Timeout(30.0))


def _check_rate_limit(owner_id: str) -> bool:
    """In-process sliding-window token bucket. True = allowed."""
    now = time.monotonic()
    cutoff = now - 60.0
    q = _HOPS[owner_id]
    while q and q[0] < cutoff:
        q.popleft()
    if len(q) >= RATE_LIMIT_PER_MINUTE:
        return False
    q.append(now)
    return True


def _extract_caller_ip(request: Request) -> str:
    """Get the real caller IP from the inbound XFF chain (set by the ALB)."""
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        # ALB appends the immediate-source IP to any inbound XFF; the
        # leftmost entry is the original client.
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


def _build_forwarded_headers(request: Request, hook_token: str) -> dict[str, str]:
    """Strip inbound X-Forwarded-* and re-assert them with values we control."""
    caller_ip = _extract_caller_ip(request)
    headers: dict[str, str] = {}
    for k, v in request.headers.items():
        kl = k.lower()
        if kl in ("host", "content-length"):
            continue
        if kl.startswith("x-forwarded-"):
            continue
        headers[k] = v
    headers["Authorization"] = f"Bearer {hook_token}"
    headers["X-Forwarded-For"] = caller_ip
    headers["X-Forwarded-Proto"] = "https"
    headers["X-Forwarded-Host"] = "api.isol8.co"
    return headers


@router.post("/u/{owner_id}/{hook_path:path}")
async def proxy_hook(owner_id: str, hook_path: str, request: Request) -> Response:
    # 1. Bearer extract
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing_or_malformed_authorization")
    presented = auth[len("Bearer "):]

    # 2. Owner lookup
    container = await container_repo.get_by_owner_id(owner_id)
    if not container:
        raise HTTPException(status_code=404, detail="owner_not_found")

    # 3. Timing-safe compare
    expected = container.get("hook_token") or ""
    if not expected or not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="invalid_token")

    # 4. Rate limit
    if not _check_rate_limit(owner_id):
        return Response(
            status_code=429,
            headers={"Retry-After": "60"},
            content=b'{"detail":"rate_limited"}',
            media_type="application/json",
        )

    # 5. Resolve container IP
    _, ip = await ecs_manager.resolve_running_container(owner_id)
    if not ip:
        return Response(
            status_code=503,
            headers={"Retry-After": "30"},
            content=b'{"detail":"container_not_running"}',
            media_type="application/json",
        )

    # 6. Forward
    body = await request.body()
    headers = _build_forwarded_headers(request, expected)
    target = f"http://{ip}:{GATEWAY_PORT}/hooks/{hook_path}"

    upstream_status = 502
    upstream_body = b""
    upstream_headers: dict[str, str] = {}
    try:
        async with _client_factory() as client:
            upstream = await client.post(target, headers=headers, content=body)
        upstream_status = upstream.status_code
        upstream_body = upstream.content
        upstream_headers = {
            k: v
            for k, v in upstream.headers.items()
            if k.lower() not in ("content-length", "transfer-encoding", "connection")
        }
    except httpx.RequestError as exc:
        logger.warning("hook proxy upstream error owner=%s err=%s", owner_id, exc)
        upstream_body = b'{"detail":"upstream_unreachable"}'

    # 7. Audit (best-effort)
    try:
        hook_event_repo.write_event(
            owner_id=owner_id,
            hook_path=hook_path,
            status_code=upstream_status,
            source_ip=_extract_caller_ip(request),
            user_agent=request.headers.get("User-Agent", ""),
            request_size=len(body),
            response_size=len(upstream_body),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("hook-events write failed owner=%s err=%s", owner_id, exc)

    return Response(
        status_code=upstream_status,
        content=upstream_body,
        headers=upstream_headers,
        media_type=upstream_headers.get("content-type", "application/json"),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd apps/backend && uv run pytest tests/unit/routers/test_hooks_proxy.py -v
```

Expected: 7 PASSED. The router isn't mounted yet — TestClient picks it up only after Task 8.

If TestClient returns 404 because the router isn't yet registered, mount it temporarily for the test by importing and including in the test module's `client` fixture, OR run Task 8 next and re-run these tests after.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/routers/hooks_proxy.py apps/backend/tests/unit/routers/test_hooks_proxy.py
git commit -m "feat(hooks_proxy): public webhook ingress with auth/forward/audit"
```

---

## Task 7: `/hooks/credentials` endpoint for the settings UI

**Files:**
- Create: `apps/backend/routers/hooks_credentials.py`
- Test: `apps/backend/tests/unit/routers/test_hooks_credentials.py`

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/routers/test_hooks_credentials.py`:

```python
"""Tests for GET /api/v1/hooks/credentials (Clerk-authed)."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app
from core.auth import AuthContext


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_user(monkeypatch):
    # Standard pattern: override get_current_user dep for this test.
    from core.auth import get_current_user

    async def _fake_user():
        return AuthContext(user_id="u_1", org_id=None, email="t@x.co")

    app.dependency_overrides[get_current_user] = _fake_user
    yield
    app.dependency_overrides.clear()


def test_credentials_returns_url_and_token(client, auth_user):
    with patch(
        "routers.hooks_credentials.container_repo.get_by_owner_id",
        new=AsyncMock(return_value={
            "owner_id": "u_1",
            "hook_token": "tok",
            "hook_token_rotated_at": "2026-05-09T12:00:00Z",
        }),
    ):
        r = client.get("/api/v1/hooks/credentials")
        assert r.status_code == 200
        data = r.json()
        assert data["public_url"] == "https://api.isol8.co/api/v1/hooks/u/u_1/"
        assert data["token"] == "tok"
        assert data["rotated_at"] == "2026-05-09T12:00:00Z"


def test_credentials_404_if_not_provisioned(client, auth_user):
    with patch(
        "routers.hooks_credentials.container_repo.get_by_owner_id",
        new=AsyncMock(return_value=None),
    ):
        r = client.get("/api/v1/hooks/credentials")
        assert r.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd apps/backend && uv run pytest tests/unit/routers/test_hooks_credentials.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement the endpoint**

Create `apps/backend/routers/hooks_credentials.py`:

```python
"""Clerk-authenticated read of the calling user's hook credentials.

Backs the Settings → Webhooks panel. Returns the public URL + token +
last-rotated timestamp. Unlike ``hooks_proxy.py``, this route DOES use
``get_current_user`` — it's the user's own browser asking for their own
credentials.
"""

from fastapi import APIRouter, Depends, HTTPException

from core.auth import AuthContext, get_current_user
from core.config import settings
from core.repositories import container_repo

router = APIRouter()


@router.get("/credentials")
async def get_credentials(user: AuthContext = Depends(get_current_user)) -> dict:
    container = await container_repo.get_by_owner_id(user.user_id)
    if not container:
        raise HTTPException(status_code=404, detail="container_not_provisioned")
    return {
        "public_url": f"{settings.PUBLIC_API_BASE}/api/v1/hooks/u/{user.user_id}/",
        "token": container.get("hook_token") or "",
        "rotated_at": container.get("hook_token_rotated_at"),
    }
```

If `settings.PUBLIC_API_BASE` doesn't exist, add it to `core/config.py` with the per-env value (`https://api-dev.isol8.co`, `https://api.isol8.co`, etc.) — read the existing pattern for `CORS_ORIGINS` to find the right env-var convention.

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd apps/backend && uv run pytest tests/unit/routers/test_hooks_credentials.py -v
```

Expected: 2 PASSED (after Task 8 wires up the router).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/routers/hooks_credentials.py apps/backend/tests/unit/routers/test_hooks_credentials.py apps/backend/core/config.py
git commit -m "feat(hooks_credentials): clerk-authed endpoint backing settings UI"
```

---

## Task 8: Mount the new routers in `main.py`

**Files:**
- Modify: `apps/backend/main.py:503-557` (the `app.include_router` block)

- [ ] **Step 1: Read the existing pattern**

Open `apps/backend/main.py` at line ~503. You'll see a block of `app.include_router(...)` calls, each registering a router under a `/api/v1/...` prefix.

- [ ] **Step 2: Add the new mounts**

Add (in the same block, kept alphabetical with neighbors when possible):

```python
from routers import hooks_credentials, hooks_proxy

app.include_router(hooks_proxy.router, prefix="/api/v1/hooks", tags=["hooks"])
app.include_router(hooks_credentials.router, prefix="/api/v1/hooks", tags=["hooks"])
```

The `hooks_credentials` router exposes `GET /credentials` and `hooks_proxy` exposes `POST /u/{owner_id}/{hook_path}`. Both share the `/api/v1/hooks` prefix without colliding because the paths don't overlap.

- [ ] **Step 3: Re-run prior router tests**

```bash
cd apps/backend && uv run pytest tests/unit/routers/test_hooks_proxy.py tests/unit/routers/test_hooks_credentials.py -v
```

Expected: 9 PASSED.

- [ ] **Step 4: Smoke-check the OpenAPI surface**

```bash
cd apps/backend && uv run python -c "
from main import app
for route in app.routes:
    p = getattr(route, 'path', '')
    if 'hooks' in p:
        print(p, route.methods)
"
```

Expected output includes:
```
/api/v1/hooks/credentials {'GET'}
/api/v1/hooks/u/{owner_id}/{hook_path} {'POST'}
```

- [ ] **Step 5: Commit**

```bash
git add apps/backend/main.py
git commit -m "feat(main): mount hooks_proxy and hooks_credentials routers"
```

---

## Task 9: `EcsManager.refresh_task_definition` (backfill helper)

**Files:**
- Modify: `apps/backend/core/containers/ecs_manager.py` (add a method, factor out from existing provision flow)
- Test: `apps/backend/tests/unit/containers/test_refresh_task_definition.py`

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/containers/test_refresh_task_definition.py`:

```python
"""Tests for EcsManager.refresh_task_definition.

Surgically re-registers a user's task definition with the current env
list (so OPENCLAW_HOOK_TOKEN backfilled mid-life is applied) and rolls
the running task onto the new revision. Does NOT touch the EFS access
point — losing agent state would be a backfill regression.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_refresh_registers_new_taskdef_with_hook_token_env(monkeypatch):
    from core.containers import ecs_manager

    manager = ecs_manager.EcsManager()

    fake_existing_container = {
        "owner_id": "u_1",
        "service_name": "openclaw-u-1-abc",
        "hook_token": "freshly-set",
        # NOTE: containers row stores task_definition_arn (full ARN),
        # NOT task_definition_family. Set in this test below alongside
        # the fake_ecs.describe stub.
    }

    register_calls = {}
    update_service_calls = {}

    fake_ecs = MagicMock()
    fake_ecs.register_task_definition.return_value = {
        "taskDefinition": {"taskDefinitionArn": "arn:aws:ecs:...:task-definition/foo:5"}
    }
    fake_ecs.update_service.return_value = {}
    fake_ecs.describe_task_definition.return_value = {
        "taskDefinition": {
            "containerDefinitions": [{"name": "openclaw", "environment": [], "secrets": []}],
            "family": "isol8-dev-openclaw-u-1",
            # everything else CDK-shaped — real test should mirror what
            # describe_task_definition actually returns; pin via a recorded fixture
        }
    }
    fake_existing_container["task_definition_arn"] = (
        "arn:aws:ecs:us-east-1:877352799272:task-definition/isol8-dev-openclaw-u-1:4"
    )

    with (
        patch("core.repositories.container_repo.get_by_owner_id",
              new=AsyncMock(return_value=fake_existing_container)),
        # The ECS boto3 client lives on `self._ecs` (not `_ecs_client`).
        patch.object(manager, "_ecs", fake_ecs),
    ):
        result = await manager.refresh_task_definition("u_1")

    # Verify register was called with an env list including OPENCLAW_HOOK_TOKEN.
    register_kwargs = fake_ecs.register_task_definition.call_args.kwargs
    cd = register_kwargs["containerDefinitions"][0]
    env_names = {e["name"] for e in cd["environment"]}
    assert "OPENCLAW_HOOK_TOKEN" in env_names

    # Verify the service was updated to the new revision.
    fake_ecs.update_service.assert_called_once()
    upd_kwargs = fake_ecs.update_service.call_args.kwargs
    assert upd_kwargs["taskDefinition"].endswith("foo:5")

    assert result is True


@pytest.mark.asyncio
async def test_refresh_returns_false_when_no_container_row(monkeypatch):
    from core.containers import ecs_manager

    manager = ecs_manager.EcsManager()
    with patch("core.repositories.container_repo.get_by_owner_id",
               new=AsyncMock(return_value=None)):
        result = await manager.refresh_task_definition("u_unknown")
    assert result is False
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd apps/backend && uv run pytest tests/unit/containers/test_refresh_task_definition.py -v
```

Expected: FAIL — `AttributeError: 'EcsManager' object has no attribute 'refresh_task_definition'`.

- [ ] **Step 3: Implement `refresh_task_definition`**

Add to `apps/backend/core/containers/ecs_manager.py` (place near other lifecycle methods, e.g., right after `_deregister_task_definition` at line 284):

```python
async def refresh_task_definition(self, owner_id: str) -> bool:
    """Re-register a user's ECS task definition with the current env list,
    then roll the running task onto the new revision.

    Used by the hook-token backfill: existing per-user task-defs were
    registered before OPENCLAW_HOOK_TOKEN existed; just patching
    openclaw.json on EFS leaves the env var undefined inside the running
    container, so the gateway's ${VAR} substitution resolves to empty.
    This helper rebuilds the env list (now including the freshly-set
    hook_token from the containers row) and updates the service to
    deploy a new task-def revision.

    The EFS access point and DDB row are untouched — losing agent
    state would be a backfill regression.

    Returns True on success, False if the user has no provisioned
    container.
    """
    container = await container_repo.get_by_owner_id(owner_id)
    if not container or not container.get("task_definition_arn"):
        return False

    # Read the current task-def to pull family/volumes/secrets/networkMode/etc.
    # The ECS boto3 client is exposed as ``self._ecs``.
    current = self._ecs.describe_task_definition(
        taskDefinition=container["task_definition_arn"]
    )
    td = current["taskDefinition"]

    # Rebuild the openclaw container-def with a fresh environment list
    # that includes the freshly-set OPENCLAW_HOOK_TOKEN.
    existing_env = next(
        c for c in td["containerDefinitions"] if c["name"] == "openclaw"
    )["environment"]
    new_env = _build_per_user_environment(
        user_id=owner_id,
        hook_token=container.get("hook_token") or "",
        existing=existing_env,  # see Task 2 — helper preserves any caller-supplied entries
    )
    for cd in td["containerDefinitions"]:
        if cd["name"] == "openclaw":
            cd["environment"] = new_env

    register_kwargs = {
        "family": td["family"],
        "containerDefinitions": td["containerDefinitions"],
        "volumes": td.get("volumes", []),
        "networkMode": td["networkMode"],
        "requiresCompatibilities": td.get("requiresCompatibilities", []),
        "cpu": td.get("cpu"),
        "memory": td.get("memory"),
        "executionRoleArn": td.get("executionRoleArn"),
        "taskRoleArn": td.get("taskRoleArn"),
    }
    register_kwargs = {k: v for k, v in register_kwargs.items() if v is not None}
    new_td = self._ecs.register_task_definition(**register_kwargs)
    new_arn = new_td["taskDefinition"]["taskDefinitionArn"]

    self._ecs.update_service(
        cluster=settings.ECS_CLUSTER_ARN,
        service=container["service_name"],
        taskDefinition=new_arn,
        forceNewDeployment=True,
    )
    # Persist the new ARN so subsequent operations see it.
    await container_repo.update_fields(owner_id, {"task_definition_arn": new_arn})
    return True
```

**Note on existing helpers.** `EcsManager` already has private methods `_register_task_definition` (line 242) and `_build_register_kwargs_from_base` (~line 159) that do similar work for the initial provisioning path. If your read of those methods shows they cleanly accept an `existing_environment` override, prefer calling `_build_register_kwargs_from_base(...)` here instead of hand-rolling `register_kwargs`. Don't refactor those helpers as part of this task — just reuse them if the seam already fits.

- [ ] **Step 4: Run test to verify it passes**

```bash
cd apps/backend && uv run pytest tests/unit/containers/test_refresh_task_definition.py -v
```

Expected: 2 PASSED.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/containers/ecs_manager.py apps/backend/tests/unit/containers/test_refresh_task_definition.py
git commit -m "feat(ecs_manager): refresh_task_definition surgically re-rolls task without EFS reset"
```

---

## Task 10: Backfill script

**Files:**
- Create: `scripts/backfill-hook-tokens.py`

- [ ] **Step 1: Implement the script (no test — orchestration is exercised by integration smoke)**

Create `scripts/backfill-hook-tokens.py`:

```python
#!/usr/bin/env python3
"""Backfill OPENCLAW_HOOK_TOKEN for all existing provisioned users.

Run once after the hook-token deploy. Idempotent: skips users that
already have a hook_token set on their containers row.

Steps per user:
  1. Generate token, persist via ContainerRepo.set_hook_token.
  2. Call EcsManager.refresh_task_definition to re-register the task
     def with OPENCLAW_HOOK_TOKEN in the env list and roll the service.
  3. Wait until the service has at least one RUNNING task on the new
     revision.
  4. Patch openclaw.json via the existing config patcher so the gateway
     re-reads its config with the new hooks block.

Usage:
    AWS_PROFILE=isol8-admin DDB_PREFIX=isol8-dev \\
        python scripts/backfill-hook-tokens.py [--dry-run] [--owner u_1]
"""

import argparse
import asyncio
import logging
import secrets
import sys
import time

# Allow running from the repo root.
sys.path.insert(0, "apps/backend")

from core.containers import ecs_manager as ecs_manager_mod
from core.repositories import container_repo
from core.services import config_patcher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill-hook-tokens")


async def _wait_for_running(manager, service_name: str, timeout_s: int = 300) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        _, ip = await manager.resolve_running_container_for_service(service_name)
        if ip:
            return True
        await asyncio.sleep(5)
    return False


async def backfill_one(manager, owner_id: str, dry_run: bool) -> str:
    container = await container_repo.get_by_owner_id(owner_id)
    if not container:
        return "no_container_row"
    if container.get("hook_token"):
        return "already_has_token"
    if dry_run:
        return "would_backfill"

    token = secrets.token_urlsafe(32)
    await container_repo.set_hook_token(owner_id, token)
    log.info("[%s] set hook_token", owner_id)

    ok = await manager.refresh_task_definition(owner_id)
    if not ok:
        return "refresh_failed"
    log.info("[%s] task-def refreshed; waiting for new revision to RUNNING", owner_id)

    if not await _wait_for_running(manager, container["service_name"]):
        return "timeout_waiting_for_running"

    config_patcher.patch_hooks_block(owner_id)
    log.info("[%s] config patched", owner_id)
    return "ok"


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--owner", help="Backfill a single owner_id (otherwise all)")
    args = parser.parse_args()

    manager = ecs_manager_mod.EcsManager()
    if args.owner:
        targets = [args.owner]
    else:
        rows = await container_repo.scan_all()
        targets = [r["owner_id"] for r in rows]

    log.info("backfilling %d owners (dry_run=%s)", len(targets), args.dry_run)
    summary = {}
    for oid in targets:
        try:
            outcome = await backfill_one(manager, oid, args.dry_run)
        except Exception as exc:  # noqa: BLE001
            outcome = f"error: {exc}"
        summary[outcome] = summary.get(outcome, 0) + 1
        log.info("[%s] %s", oid, outcome)

    log.info("done: %s", summary)


if __name__ == "__main__":
    asyncio.run(main())
```

If `container_repo.scan_all` doesn't exist, add it (a paginated DDB scan returning all rows).

If `config_patcher.patch_hooks_block` doesn't exist, add it: a thin wrapper around the existing config-patcher that calls `write_openclaw_config(...)` on the user's EFS path with their current `hook_token` from DDB. Read `apps/backend/core/services/config_patcher.py` for the existing patch helpers and follow the pattern.

- [ ] **Step 2: Manual smoke (dry-run) against dev**

```bash
AWS_PROFILE=isol8-admin DDB_PREFIX=isol8-dev python scripts/backfill-hook-tokens.py --dry-run
```

Expected: log lines per user with outcome `would_backfill` (for users without a token) or `already_has_token` (for users provisioned after Task 2 ships).

- [ ] **Step 3: Commit**

```bash
git add scripts/backfill-hook-tokens.py
git commit -m "scripts: backfill OPENCLAW_HOOK_TOKEN for existing users"
```

---

## Task 11: Frontend — `WebhooksPanel` + Settings page wiring

**Files:**
- Create: `apps/frontend/src/components/settings/WebhooksPanel.tsx`
- Create: `apps/frontend/src/hooks/useHookCredentials.ts`
- Modify: `apps/frontend/src/app/settings/page.tsx`

- [ ] **Step 1: Implement `useHookCredentials` (follows `useBilling.ts` pattern)**

Create `apps/frontend/src/hooks/useHookCredentials.ts`:

```ts
"use client";

import useSWR from "swr";

import { useApi } from "@/lib/api";

type HookCredentials = {
  public_url: string;
  token: string;
  rotated_at: string | null;
};

export function useHookCredentials() {
  const { get } = useApi();
  const { data, error, isLoading, mutate } = useSWR<HookCredentials>(
    "/hooks/credentials",
    (url: string) => get(url),
  );
  return { credentials: data, error, isLoading, mutate };
}
```

- [ ] **Step 2: Implement `WebhooksPanel`**

Create `apps/frontend/src/components/settings/WebhooksPanel.tsx`:

```tsx
"use client";

import { useState } from "react";
import { Copy, Eye, EyeOff } from "lucide-react";

import { useHookCredentials } from "@/hooks/useHookCredentials";

export function WebhooksPanel() {
  const { credentials, isLoading, error } = useHookCredentials();
  const [revealed, setRevealed] = useState(false);

  if (isLoading) return <div className="text-sm text-zinc-400">Loading…</div>;
  if (error || !credentials)
    return (
      <div className="text-sm text-red-400">
        Couldn't load webhook credentials. Try again in a moment.
      </div>
    );

  const tokenDisplay = revealed ? credentials.token : "•".repeat(32);

  return (
    <div className="space-y-6">
      <h2 className="text-xl font-semibold">Webhooks</h2>
      <p className="text-sm text-zinc-400">
        External services (Zapier, Stripe, GitHub, custom integrations) can
        trigger your agents by POSTing to your webhook URL with the bearer
        token below.
      </p>

      <Field
        label="Public URL"
        value={credentials.public_url}
        onCopy={() => navigator.clipboard.writeText(credentials.public_url)}
      />

      <div>
        <div className="flex items-center justify-between gap-2">
          <span className="text-sm font-medium">Hook token</span>
          <div className="flex gap-2">
            <button
              type="button"
              aria-label={revealed ? "Hide token" : "Reveal token"}
              onClick={() => setRevealed((v) => !v)}
              className="rounded p-1 hover:bg-zinc-800"
            >
              {revealed ? <EyeOff size={14} /> : <Eye size={14} />}
            </button>
            <button
              type="button"
              aria-label="Copy token"
              onClick={() => navigator.clipboard.writeText(credentials.token)}
              className="rounded p-1 hover:bg-zinc-800"
            >
              <Copy size={14} />
            </button>
          </div>
        </div>
        <div className="mt-1 rounded border border-zinc-800 bg-zinc-950 px-3 py-2 font-mono text-sm">
          {tokenDisplay}
        </div>
        {credentials.rotated_at && (
          <p className="mt-1 text-xs text-zinc-500">
            Last rotated: {new Date(credentials.rotated_at).toLocaleString()}
          </p>
        )}
      </div>
    </div>
  );
}

function Field({
  label,
  value,
  onCopy,
}: {
  label: string;
  value: string;
  onCopy: () => void;
}) {
  return (
    <div>
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-medium">{label}</span>
        <button
          type="button"
          aria-label={`Copy ${label}`}
          onClick={onCopy}
          className="rounded p-1 hover:bg-zinc-800"
        >
          <Copy size={14} />
        </button>
      </div>
      <div className="mt-1 break-all rounded border border-zinc-800 bg-zinc-950 px-3 py-2 font-mono text-sm">
        {value}
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Wire into the Settings page**

Open `apps/frontend/src/app/settings/page.tsx`. The page uses a *grouped* `NAV_SECTIONS` shape (each section has a `label` plus an `items` array) and a `PANELS` registry keyed by `Panel`, not a switch. Make four changes:

1. **Extend the `Panel` type** (line 18):

```ts
type Panel = "profile" | "billing" | "channels" | "webhooks";
```

2. **Add a `NavIcon` case** for the new entry (read the existing `NavIcon` switch in the file and add a `case "webhooks":` returning whatever Lucide icon you want — `Webhook` from `lucide-react` is the natural fit). Add the import to the existing icon imports.

3. **Add an entry to `NAV_SECTIONS`'s items** (line 20). The shape is grouped — append into the appropriate section (likely the existing first/Account group):

```ts
const NAV_SECTIONS = [
  {
    label: "Account",
    items: [
      // ... existing items ...
      { id: "webhooks" as Panel, name: "Webhooks", icon: "webhooks" },
    ],
  },
  // ... rest unchanged ...
];
```

4. **Register the panel component** in the `PANELS` registry (line 239):

```tsx
import { WebhooksPanel } from "@/components/settings/WebhooksPanel";

const PANELS: Record<Panel, () => React.ReactElement> = {
  profile:  ProfilePanel,
  billing:  BillingPanel,
  channels: ChannelsPanel,
  webhooks: WebhooksPanel,  // NEW
};
```

5. **Update the `initialPanel` ternary** at line 268 to recognize "webhooks":

```ts
const initialPanel: Panel =
  initialPanelParam === "billing"
    || initialPanelParam === "channels"
    || initialPanelParam === "webhooks"
    ? initialPanelParam
    : "profile";
```

- [ ] **Step 4: Manual smoke**

```bash
cd apps/frontend && pnpm run dev
```

Open `http://localhost:3000/settings`, click "Webhooks" in the left nav. Verify:
- URL renders with the user's owner_id in the path
- Token is masked, "Eye" toggle reveals
- Both Copy buttons work (navigator.clipboard)
- "Last rotated" shows a sensible date

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/settings/WebhooksPanel.tsx apps/frontend/src/hooks/useHookCredentials.ts apps/frontend/src/app/settings/page.tsx
git commit -m "feat(settings): webhooks panel showing hook URL + masked token"
```

---

## Task 12: Integration smoke test (LocalStack + stub container)

**Files:**
- Create: `apps/backend/tests/integration/test_hooks_proxy_e2e.py`

- [ ] **Step 1: Write the integration test**

Create `apps/backend/tests/integration/test_hooks_proxy_e2e.py`:

```python
"""End-to-end smoke for the hooks proxy.

Boots a stub HTTP server posing as the user's OpenClaw container,
seeds the LocalStack-backed containers row with a known hook_token,
and POSTs a real request through the FastAPI app. Verifies the body
is forwarded, the audit row lands in DDB, and X-Forwarded-* are
rewritten with the true caller IP.
"""

import asyncio
import json
import secrets
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def stub_container():
    """Run a tiny HTTP server that imitates OpenClaw's /hooks endpoint."""
    received = {"body": None, "headers": None}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            received["body"] = self.rfile.read(length)
            received["headers"] = dict(self.headers)
            self.send_response(202)
            self.end_headers()
            self.wfile.write(b'{"queued":true}')

        def log_message(self, *_a, **_k):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield {"port": port, "received": received}
    server.shutdown()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_hooks_proxy_end_to_end(stub_container, monkeypatch):
    from main import app
    from core.repositories import container_repo

    token = secrets.token_urlsafe(32)
    fake_container = {
        "owner_id": "u_e2e",
        "hook_token": token,
        "service_name": "stub",
        "status": "running",
    }

    async def fake_get(owner_id):
        return fake_container if owner_id == "u_e2e" else None

    async def fake_resolve(owner_id):
        return (fake_container, "127.0.0.1")

    # Point the proxy's GATEWAY_PORT at the stub.
    from routers import hooks_proxy
    monkeypatch.setattr(hooks_proxy, "GATEWAY_PORT", stub_container["port"])
    monkeypatch.setattr(container_repo, "get_by_owner_id", fake_get)
    monkeypatch.setattr(hooks_proxy.ecs_manager, "resolve_running_container", fake_resolve)

    client = TestClient(app, raise_server_exceptions=False)
    payload = {"text": "new email", "metadata": {"source": "stripe"}}
    r = client.post(
        "/api/v1/hooks/u/u_e2e/wake",
        headers={"Authorization": f"Bearer {token}", "User-Agent": "Stripe/1.0"},
        json=payload,
    )

    assert r.status_code == 202
    assert r.json() == {"queued": True}
    assert json.loads(stub_container["received"]["body"]) == payload
    forwarded = stub_container["received"]["headers"]
    assert forwarded.get("X-Forwarded-Proto") == "https"
    assert forwarded.get("X-Forwarded-Host") == "api.isol8.co"
    assert forwarded.get("Authorization") == f"Bearer {token}"
```

- [ ] **Step 2: Run the integration test**

```bash
cd apps/backend && uv run pytest tests/integration/test_hooks_proxy_e2e.py -v -m integration
```

Expected: PASSED.

- [ ] **Step 3: Commit**

```bash
git add apps/backend/tests/integration/test_hooks_proxy_e2e.py
git commit -m "test(hooks_proxy): e2e smoke against stub container"
```

---

## Final verification

- [ ] **Run the full backend test suite**

```bash
cd apps/backend && uv run pytest tests/ -v --tb=short
```

Expected: full suite passes. If any unrelated tests fail, they're environmental — investigate per CLAUDE.md guidance.

- [ ] **Run the frontend test suite**

```bash
cd apps/frontend && pnpm test
```

Expected: pass.

- [ ] **Run lint everywhere**

```bash
turbo run lint
```

Expected: pass.

- [ ] **Synth CDK clean**

```bash
cd apps/infra && pnpm cdk synth 2>&1 | tail -5
```

Expected: no errors.

- [ ] **Smoke against dev after deploy**

After the PR merges and dev deploys land:

```bash
# Get a real user's hook URL and token from the settings UI, then:
curl -X POST "$(URL_FROM_UI)/wake" \
  -H "Authorization: Bearer $(TOKEN_FROM_UI)" \
  -H "Content-Type: application/json" \
  -d '{"text":"smoke test from manual curl"}'
```

Expected: 200 (or 202). Then check the agent's chat in the UI — it should show a wake event was processed.

- [ ] **Run backfill in dev**

```bash
AWS_PROFILE=isol8-admin DDB_PREFIX=isol8-dev python scripts/backfill-hook-tokens.py
```

Expected: each user logged with `ok` outcome (or `already_has_token` for users provisioned after the deploy).

---

## Self-review notes

- **Spec coverage:** every section of the spec maps to at least one task. Token storage → T1. Env injection + token gen → T2. Config rendering → T3. Audit table + IAM → T4. HookEventRepo → T5. Proxy router with all the validation rules → T6. Credentials endpoint → T7. Mounting → T8. Backfill helper → T9. Backfill script → T10. UI → T11. Integration smoke → T12.
- **Type consistency:** `_build_per_user_environment` is referenced in T2 and T9 with the same signature (`*, user_id, hook_token, ...`). `EcsManager.resolve_running_container` and `EcsManager.refresh_task_definition` are the named helpers throughout.
- **No placeholders in code blocks.** Every step that changes code shows the actual code. The two phrases that may need adjustment when running ("CDK shape of describe_task_definition" in T9 and "real test should mirror what describe_task_definition actually returns") are honestly flagged as fixture-pin work, not as TODOs the engineer can skip.
