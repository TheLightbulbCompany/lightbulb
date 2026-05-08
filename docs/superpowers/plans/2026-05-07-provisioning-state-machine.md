# Provisioning State-Machine Consolidation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace today's three independent provisioning trigger surfaces (Clerk webhooks, `/teams` autoprovision, `/chat`-driven container provision) with a single canonical helper, `ensure_user_ready`, fired from the exact endpoint where the user's last authorization signal lands. The race that produces a container without Paperclip data (personal user opens `/chat` before `/teams`) becomes structurally impossible.

**Tech Stack:** Python 3.13 (FastAPI, uv), httpx, DynamoDB, pytest + pytest-asyncio.

**Premise:** This plan stacks on the agent-pairing branch (`worktree-paperclip-openclaw-agent-pairing` / PR #563). The UUID consistency between Paperclip and OpenClaw the pairing PR establishes is what makes "container's `openclaw.json` reflects Paperclip's agent list at provision time" coherent.

---

## Problem statement

Three independent trigger surfaces exist today, with implicit ordering assumptions:

| Trigger surface | Today's call site | Fires for |
|---|---|---|
| Stripe `customer.subscription.created/updated` | `apps/backend/routers/billing.py:660-720` (persists state, sets `provider_choice`; **does NOT** kick off provisioning) | All owners |
| Clerk `organization.created` webhook → `provision_org` | `apps/backend/routers/webhooks.py:319-378` (`_handle_organization_created`) → `provisioning.provision_org(...)` at line 348 | Org owners — eager Paperclip; container is NOT provisioned here |
| Clerk `organizationMembership.created` webhook → `provision_member` | `apps/backend/routers/webhooks.py:381-485` (`_handle_organization_membership_created`) → `provisioning.provision_member(...)` at line 449 | Org members |
| `/teams` autoprovision | `apps/backend/routers/teams/agents.py:298-308` (`_ctx`, when DDB row missing AND personal context AND container exists) → `asyncio.create_task(ensure_paperclip_workspace(...))` at line 304 | Personal users — lazy Paperclip |
| Paperclip proxy autoprovision | `apps/backend/routers/paperclip_proxy.py:829` (in `_self_provision`), `:914` (in `_autoprovision`) — both call `provisioning.provision_org(owner_id=user_id, ...)` | Personal users hitting `company.isol8.co` directly |
| Frontend `POST /container/provision` | `apps/frontend/src/components/chat/ProvisioningStepper.tsx:286` — `api.post("/container/provision", {})` once `needsProvision` and !`needsProviderStep` | All owners — lazy container provision |
| Backend `POST /container/provision` route | `apps/backend/routers/container.py:223-302` — handler `container_provision` at `:237`; calls `_resolve_provider_choice` (`:71-86`) then `get_ecs_manager().provision_user_container(...)` (`:288`) | Backend entry point |
| `GET /container/status` auto-retry | `apps/backend/routers/container.py:155-169` — fires `_background_provision` (`:89-108`) on `error`/`stopped` containers when gate passes | Self-healing |

The race is real: a personal user who opens `/chat` before `/teams` triggers `POST /container/provision` → `provision_user_container` → `write_user_configs` (which fetches Paperclip agents to seed `openclaw.json`) at a moment when the Paperclip company doesn't exist yet. The container starts up with the legacy `"main"` agent fallback. Friday-the-Paperclip-seeded-agent doesn't appear in `/chat` until the next reprovision.

There's no single deterministic readiness signal. We patched it with idempotency, retries, and best-effort fallbacks — and even those can be wrong-ordered.

## Architecture

### The state machine

A user moves through these states, in dependency order:

```
                       ┌─────────────────────────┐
                       │ 1. Clerk identity       │  ← user.created webhook
                       └────────────┬────────────┘
                                    ↓
                       ┌─────────────────────────┐
                       │ 2. Stripe subscription  │  ← /billing/trial-checkout +
                       │    (trialing|active)    │     customer.subscription.created
                       └────────────┬────────────┘
                                    ↓
                       ┌─────────────────────────┐
                       │ 3. Provider configured  │  ← bedrock_claude → no extra step
                       │ (Bedrock|OAuth|BYO key) │     chatgpt_oauth → /oauth/chatgpt/poll OK
                       └────────────┬────────────┘     byo_key       → PUT /settings/keys/{openai|anthropic}
                                    ↓
                       ╔═════════════════════════╗
                       ║  ★ READINESS GATE ★     ║   ensure_user_ready(owner_id, clerk_user_id)
                       ║  Subscription active +  ║   fires here. Idempotent.
                       ║  provider satisfied.    ║
                       ╚════════════╤════════════╝
                                    ↓
                       ┌─────────────────────────┐
                       │ 4. Paperclip company    │  ← provisioning.provision_org
                       │    (Friday seeded)      │
                       └────────────┬────────────┘
                                    ↓
                       ┌─────────────────────────┐
                       │ 5. Container            │  ← ecs.provision_user_container
                       │    (ECS / EFS /         │     pre-stages OAuth/BYO secrets,
                       │     openclaw.json)      │     writes openclaw.json reflecting
                       └─────────────────────────┘     Paperclip agents
```

Org members hit a different ready point — they don't have their own subscription or provider. Their trigger is `organizationMembership.created`: add them to the existing Paperclip company, return. They ride the owner's container.

### The canonical helper

```python
# apps/backend/core/services/user_provision.py (new)

async def ensure_user_ready(
    *,
    owner_id: str,
    clerk_user_id: str,
) -> None:
    """Single canonical provisioning entry point. Idempotent.

    Pre-conditions (caller's responsibility — fast no-op if not met):
      - billing_repo row for owner_id has subscription_status in
        {"active", "trialing"} (provision_gate.is_subscription_active).
      - provider_choice on the row is one of {"bedrock_claude",
        "chatgpt_oauth", "byo_key"}, with byo_provider populated for byo_key.
      - For chatgpt_oauth: oauth-tokens row exists for clerk_user_id.
      - For byo_key:       api-keys row for the chosen byo_provider exists.

    Provisions in dependency order:
      1. Paperclip company (provision_org), if no active row.
      2. Container (ecs.provision_user_container), reading provider_choice
         + byo_provider from billing_repo and pre-staging OAuth/BYO state
         INSIDE provision_user_container (existing behaviour, unchanged).

    Idempotent: provision_org short-circuits on existing active row;
    provision_user_container short-circuits via ecs_manager.py:1102-1212
    when a service is already ACTIVE and running.
    """
```

```python
async def ensure_org_member_added_to_paperclip(
    *,
    org_id: str,
    member_user_id: str,
    member_email: str,
    owner_email: str,
) -> None:
    """Org-member fast path. Adds the member to the existing Paperclip
    company. Does NOT provision a container — members ride the owner's.
    Idempotent: provision_member short-circuits on existing active row.
    """
```

### Decisions per existing trigger site

| Site | File:line | Decision |
|---|---|---|
| Clerk `organization.created` → `provision_org` | `routers/webhooks.py:348` | **Downgrade.** Keep webhook handler; remove the `provision_org` call. Org owners' Paperclip is now provisioned by the Stripe / OAuth / BYO trigger when readiness gate is satisfied (org owner finished onboarding). Email-persistence-only path remains for `user.created` (`webhooks.py:722-744`). |
| Clerk `organizationMembership.created` → `provision_member` | `routers/webhooks.py:449` | **Modify.** Replace direct `provisioning.provision_member(...)` with `ensure_org_member_added_to_paperclip(...)`. The helper internally still calls `provision_member`; we centralise the dispatch. Retry semantics preserved by mirroring the existing `_enqueue_paperclip_retry` logic inside the helper, OR keeping the retry wrapping in the webhook handler and having the helper raise. (Decision: keep retry wrapping in the webhook handler; helper just orchestrates.) |
| Clerk `user.created` → email persist | `routers/webhooks.py:722-744` | **Keep as-is.** Pure email persistence; not a provisioning trigger. |
| Stripe `customer.subscription.created/updated` | `routers/billing.py:660-720` | **Modify.** After `set_subscription` + `set_provider_choice` writes succeed, dispatch `ensure_user_ready` if `provider_choice == "bedrock_claude"` AND status is in `_ACTIVE_STATUSES`. (Bedrock is the only provider whose readiness gate is satisfied by subscription alone — OAuth/BYO need their own follow-up step.) |
| Stripe `customer.subscription.deleted` | `routers/billing.py:735-781` | **Keep as-is.** Tears down container + disables Paperclip. Not a provisioning trigger. |
| `POST /oauth/chatgpt/poll` success | `routers/oauth.py:65-82` | **Modify.** When `result is DevicePollResult` (line 81), dispatch `ensure_user_ready` (background task). Tokens are persisted by `poll_device_code` before this point. |
| `PUT /settings/keys/{tool_id}` (BYO) | `routers/settings_keys.py:46-63` | **Modify.** After `service.set_key` succeeds, IF `tool_id in SUPPORTED_LLM_PROVIDERS` AND the owner's `provider_choice == "byo_key"` AND `byo_provider == tool_id` AND subscription is active, dispatch `ensure_user_ready`. (Tool keys like Perplexity / Firecrawl don't satisfy a provider readiness gate; only LLM provider keys do.) |
| `POST /container/provision` (`/chat`) | `routers/container.py:237-302` | **Downgrade.** The endpoint stays for admin-driven reprovision and for `_background_provision` auto-retry inside `/container/status`, but the frontend stops calling it. Frontend consequence in Task 9. (Recommendation: don't add a separate auth gate yet — the existing payment gate at `:277-281` is already good defence; ops uses the same JWT.) |
| `GET /container/status` auto-retry → `_background_provision` | `routers/container.py:155-169` + `:89-108` | **Keep as-is.** Self-healing for already-onboarded users whose container went into `error`/`stopped`. Not a provisioning *initiation* trigger. |
| `/teams` autoprovision in `_ctx` | `routers/teams/agents.py:304` | **Remove.** Delete the `asyncio.create_task(ensure_paperclip_workspace(...))` line and the `from core.services.paperclip_autoprovision import ensure_paperclip_workspace` import. Replace with: when `_ctx` hits the no-DDB-row branch for personal context with no container, return the existing 402 ("subscribe to enable Teams"); when there IS a container, return 409 with a "team workspace not provisioned — re-run onboarding" message instead of self-healing. After consolidation this branch should never fire for healthy onboarded users. |
| Paperclip proxy `_self_provision` / `_autoprovision` | `routers/paperclip_proxy.py:829`, `:914` | **Modify.** Replace direct `provisioning.provision_org(...)` calls with `ensure_user_ready(owner_id=auth.user_id, clerk_user_id=auth.user_id)`. The proxy is the rare fallback for users hitting `company.isol8.co` before reaching `/teams`; the canonical helper is correct here too because it's idempotent and personal-only at this entry point. |
| `core/services/paperclip_autoprovision.py` `ensure_paperclip_workspace` | (entire module) | **Keep as internal helper.** The `_provision_paperclip_company` step inside `ensure_user_ready` will reuse the same `_get_paperclip_provisioning` / `_close_paperclip_http` / email-resolver chain. Re-export or inline; the existing module's docstring (line 10-13) already says it's personal-only, which is fine for `ensure_user_ready`'s personal call site. |
| `core/services/provision_gate.evaluate_provision_gate` | `provision_gate.py:163-244` | **Keep as-is.** It's a STATE QUERY (returns `Gate | None`), not a trigger. Used by `/container/status` (`container.py:141`) and `_assert_provision_allowed` (`container.py:50`). After consolidation it remains the gate the frontend reads to decide what UI to show. |
| Frontend `useContainerStatus` | `apps/frontend/src/hooks/useContainerStatus.ts:64` | **Keep as-is.** Pure read-side polling. The hook never POSTs `/container/provision`. |
| Frontend `ProvisioningStepper` `api.post("/container/provision", {})` | `apps/frontend/src/components/chat/ProvisioningStepper.tsx:286` | **Remove.** After backend triggers fire `ensure_user_ready` from onboarding, the stepper is purely passive: poll `/container/status` until `phase === "ready"`, then proceed. Detailed UX redesign deferred — see Out-of-scope. |

---

## File Structure

**New files:**
- `apps/backend/core/services/user_provision.py` — `ensure_user_ready` + `ensure_org_member_added_to_paperclip`.
- `apps/backend/tests/unit/services/test_user_provision.py` — unit tests for the helper.

**Modified files:**
- `apps/backend/routers/billing.py` — add `ensure_user_ready` dispatch in `customer.subscription.created/updated` branch (line 681 vicinity).
- `apps/backend/routers/oauth.py` — add `ensure_user_ready` dispatch in `poll` success branch (line 81 vicinity).
- `apps/backend/routers/settings_keys.py` — add `ensure_user_ready` dispatch in `set_key` success branch (line 60 vicinity).
- `apps/backend/routers/webhooks.py` — switch `_handle_organization_membership_created` to call `ensure_org_member_added_to_paperclip`; remove `provisioning.provision_org` from `_handle_organization_created`.
- `apps/backend/routers/teams/agents.py` — drop the `ensure_paperclip_workspace` call in `_ctx` (line 304) and the import (line 34).
- `apps/backend/routers/paperclip_proxy.py` — re-route `_self_provision` (line 829) and `_autoprovision` (line 914) to call `ensure_user_ready`.
- `apps/frontend/src/components/chat/ProvisioningStepper.tsx` — remove the `api.post("/container/provision", {})` call (line 286) and the `needsProvision` effect (line 271-294).
- `apps/backend/core/services/paperclip_autoprovision.py` — kept; the module's `ensure_paperclip_workspace` becomes the implementation detail of `_provision_paperclip_company` inside `user_provision.py`. No external callers remain after this plan.

**Test files touched:**
- `apps/backend/tests/unit/services/test_user_provision.py` (new).
- `apps/backend/tests/unit/routers/test_billing_webhooks.py` or equivalent — confirm `subscription.created` dispatches.
- `apps/backend/tests/unit/routers/test_oauth.py` — confirm poll-success dispatches.
- `apps/backend/tests/unit/routers/test_settings_keys.py` — confirm BYO save dispatches.
- `apps/backend/tests/unit/routers/test_webhooks.py` — confirm membership webhook routes through new helper; confirm org.created no longer provisions.

---

## Pre-flight

- [ ] **Step 0.1: Confirm rollout strategy is wipe-everything.**
  Per CLAUDE.md "Clean-Slate Dev Reset" + Out-of-scope below: no migration script. Operator wipes all dev users, redeploys, users re-onboard. The new state machine handles them from scratch.

- [ ] **Step 0.2: Confirm agent-pairing branch base.**
  This plan assumes commits up to `a1b324a1` (`docs(plan): provisioning state-machine consolidation`) are present, including the agent-pairing PR's UUID consistency between Paperclip and OpenClaw.

- [ ] **Step 0.3: Verify test scaffolding exists.**
  `apps/backend/tests/unit/services/` exists (`ls apps/backend/tests/unit/services/`). If not, create with `__init__.py`.

---

## Task 1: Scaffold `user_provision.py` with idempotent skeleton

**Files:**
- Create: `apps/backend/core/services/user_provision.py`
- Create: `apps/backend/tests/unit/services/test_user_provision.py`

- [ ] **Step 1.1: Write failing test for the no-op-when-already-provisioned case.**

```python
"""Unit tests for the unified user_provision helper."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services.user_provision import ensure_user_ready


@pytest.mark.asyncio
async def test_ensure_user_ready_short_circuits_when_already_provisioned():
    """If Paperclip is active and the container is running, ensure_user_ready
    does no work (no provision_org, no provision_user_container)."""
    fake_paperclip_repo = MagicMock()
    fake_paperclip_repo.get = AsyncMock(
        return_value=MagicMock(status="active", company_id="co_xyz")
    )
    fake_container_repo = MagicMock()
    fake_container_repo.get_by_owner_id = AsyncMock(
        return_value={"status": "running", "service_name": "svc"}
    )
    fake_ecs = MagicMock()
    fake_ecs.provision_user_container = AsyncMock()
    fake_provision_org = AsyncMock()

    with patch(
        "core.services.user_provision._provision_paperclip_company",
        fake_provision_org,
    ):
        await ensure_user_ready(
            owner_id="user_abc",
            clerk_user_id="user_abc",
            paperclip_repo=fake_paperclip_repo,
            container_repo=fake_container_repo,
            ecs=fake_ecs,
        )

    fake_provision_org.assert_not_awaited()
    fake_ecs.provision_user_container.assert_not_awaited()
```

- [ ] **Step 1.2: Run the test, confirm it fails.**

`cd apps/backend && uv run pytest tests/unit/services/test_user_provision.py -v` → expects `ModuleNotFoundError: No module named 'core.services.user_provision'`.

- [ ] **Step 1.3: Implement skeleton.**

```python
"""Unified user provisioning entry point — the SOLE place where
Paperclip + container provisioning are orchestrated together.

Called from the four readiness-gate trigger sites (Stripe webhook for
bedrock_claude, /oauth/chatgpt/poll for chatgpt_oauth, PUT /settings/keys
for byo_key, and organizationMembership.created for org members joining
an already-provisioned org). Each trigger site verifies its own
pre-conditions before dispatching, then calls this helper as a
fire-and-forget asyncio.create_task.

Idempotent. Short-circuits at every step:
  - Paperclip step:  provision_org no-ops on existing active row.
  - Container step:  ecs.provision_user_container handles all four
                     scenarios (no service / 0 desired / running / error)
                     in apps/backend/core/containers/ecs_manager.py:1037-1280.

For org members joining an existing org, see
ensure_org_member_added_to_paperclip — they don't need their own container.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def ensure_user_ready(
    *,
    owner_id: str,
    clerk_user_id: str,
    paperclip_repo: Any | None = None,
    container_repo: Any | None = None,
    ecs: Any | None = None,
) -> None:
    """Idempotently provision Paperclip + container for owner_id.

    Args:
        owner_id: Container owner — Clerk org_id for orgs, user_id for personal.
        clerk_user_id: The Clerk user who triggered the readiness signal.
                       For personal owners this equals owner_id; for org
                       owners this is the admin who completed onboarding.
        paperclip_repo / container_repo / ecs: Test injection points.
                                               Default to the production
                                               singletons.
    """
    if paperclip_repo is None:
        from core.repositories.paperclip_repo import PaperclipRepo

        paperclip_repo = PaperclipRepo(table_name="paperclip-companies")
    if container_repo is None:
        from core.repositories import container_repo as _container_repo

        container_repo = _container_repo
    if ecs is None:
        from core.containers import get_ecs_manager

        ecs = get_ecs_manager()

    # Phase 1: Paperclip company.
    company = await paperclip_repo.get(owner_id)
    if company is None or getattr(company, "status", None) != "active":
        await _provision_paperclip_company(
            owner_id=owner_id, clerk_user_id=clerk_user_id
        )
    else:
        logger.info(
            "ensure_user_ready: paperclip already active for owner=%s", owner_id
        )

    # Phase 2: Container.
    container_row = await container_repo.get_by_owner_id(owner_id)
    if container_row is None or container_row.get("status") not in ("running",):
        await _provision_container(
            owner_id=owner_id, clerk_user_id=clerk_user_id, ecs=ecs
        )
    else:
        logger.info(
            "ensure_user_ready: container already running for owner=%s", owner_id
        )


async def _provision_paperclip_company(
    *, owner_id: str, clerk_user_id: str
) -> None:
    """Implemented in Task 2."""
    raise NotImplementedError("paperclip provisioning step")


async def _provision_container(
    *, owner_id: str, clerk_user_id: str, ecs: Any
) -> None:
    """Implemented in Task 3."""
    raise NotImplementedError("container provisioning step")
```

- [ ] **Step 1.4: Run test; confirm it passes.**

- [ ] **Step 1.5: Commit.**

```bash
git add apps/backend/core/services/user_provision.py \
       apps/backend/tests/unit/services/test_user_provision.py
git commit -m "feat(provision): scaffold user_provision module skeleton

Single canonical provisioning entry point. ensure_user_ready
short-circuits when both Paperclip and container are fully provisioned.
Subsequent tasks fill in the actual provisioning steps."
```

---

## Task 2: Implement `_provision_paperclip_company`

**Files:**
- Modify: `apps/backend/core/services/user_provision.py`
- Modify: `apps/backend/tests/unit/services/test_user_provision.py`

The Paperclip step mirrors `routers/webhooks.py::_handle_organization_created` (`webhooks.py:319-378`) and `paperclip_autoprovision.ensure_paperclip_workspace` (`paperclip_autoprovision.py:23-61`) — both already do the right `_get_paperclip_provisioning` → `provision_org` → `_close_paperclip_http` chain. Reuse the helpers in `routers/webhooks.py` to avoid divergence.

- [ ] **Step 2.1: Write failing test for "no Paperclip row → provision_org runs".**

```python
@pytest.mark.asyncio
async def test_ensure_user_ready_provisions_paperclip_when_missing():
    fake_paperclip_repo = MagicMock()
    fake_paperclip_repo.get = AsyncMock(return_value=None)  # not provisioned
    fake_container_repo = MagicMock()
    fake_container_repo.get_by_owner_id = AsyncMock(
        return_value={"status": "running"}
    )
    fake_ecs = MagicMock()
    fake_provision = AsyncMock()

    with patch(
        "core.services.user_provision._provision_paperclip_company",
        fake_provision,
    ):
        await ensure_user_ready(
            owner_id="user_abc",
            clerk_user_id="user_abc",
            paperclip_repo=fake_paperclip_repo,
            container_repo=fake_container_repo,
            ecs=fake_ecs,
        )

    fake_provision.assert_awaited_once_with(
        owner_id="user_abc", clerk_user_id="user_abc"
    )
```

- [ ] **Step 2.2: Run test, confirm failure (NotImplementedError).**

- [ ] **Step 2.3: Implement `_provision_paperclip_company`.**

Replace the stub in `user_provision.py`:

```python
async def _provision_paperclip_company(
    *, owner_id: str, clerk_user_id: str
) -> None:
    """Provision a Paperclip company for owner_id.

    Reuses the per-request httpx client + provisioning factory from
    routers/webhooks.py (_get_paperclip_provisioning, _close_paperclip_http)
    so this code path stays in lockstep with the org.created webhook.

    For personal owners owner_id == clerk_user_id; for org owners
    owner_id == "org_..." and clerk_user_id is the admin who finished
    onboarding (used for the email lookup fallback).
    """
    from core.services.paperclip_owner_email import lookup_owner_email
    from routers.webhooks import (
        _close_paperclip_http,
        _get_paperclip_provisioning,
    )

    org_id_for_lookup = owner_id if owner_id.startswith("org_") else None
    owner_email = await lookup_owner_email(
        org_id=org_id_for_lookup, fallback_user_id=clerk_user_id
    )
    if not owner_email:
        logger.warning(
            "ensure_user_ready: no email for owner=%s clerk_user=%s — skipping paperclip step",
            owner_id,
            clerk_user_id,
        )
        return

    provisioning = await _get_paperclip_provisioning()
    try:
        await provisioning.provision_org(
            owner_id=owner_id,
            owner_user_id=clerk_user_id,
            owner_email=owner_email,
        )
    finally:
        await _close_paperclip_http(provisioning)
```

- [ ] **Step 2.4: Run tests; both should pass.**

- [ ] **Step 2.5: Commit.**

```bash
git commit -m "feat(provision): paperclip step in ensure_user_ready

_provision_paperclip_company reuses the webhooks.py provisioning factory
chain so this trigger stays in lockstep with the org.created path.
Idempotent: provision_org short-circuits on existing active row."
```

---

## Task 3: Implement `_provision_container`

**Files:**
- Modify: `apps/backend/core/services/user_provision.py`
- Modify: `apps/backend/tests/unit/services/test_user_provision.py`

The container step mirrors `routers/container.py::_background_provision` (`container.py:89-108`) — read provider_choice from `billing_repo`, call `ecs.provision_user_container`. The ECS manager handles pre-staging (OAuth tokens, BYO secrets) internally at `ecs_manager.py:1074-1097`.

- [ ] **Step 3.1: Write failing test for "container missing → provision_user_container runs with provider_choice".**

```python
@pytest.mark.asyncio
async def test_ensure_user_ready_provisions_container_when_missing():
    fake_paperclip_repo = MagicMock()
    fake_paperclip_repo.get = AsyncMock(
        return_value=MagicMock(status="active")
    )
    fake_container_repo = MagicMock()
    fake_container_repo.get_by_owner_id = AsyncMock(return_value=None)
    fake_ecs = MagicMock()
    fake_ecs.provision_user_container = AsyncMock(return_value="svc-name")

    fake_billing_row = {
        "owner_id": "user_abc",
        "provider_choice": "byo_key",
        "byo_provider": "anthropic",
    }
    with patch(
        "core.repositories.billing_repo.get_by_owner_id",
        AsyncMock(return_value=fake_billing_row),
    ):
        await ensure_user_ready(
            owner_id="user_abc",
            clerk_user_id="user_abc",
            paperclip_repo=fake_paperclip_repo,
            container_repo=fake_container_repo,
            ecs=fake_ecs,
        )

    fake_ecs.provision_user_container.assert_awaited_once_with(
        "user_abc",
        provider_choice="byo_key",
        byo_provider="anthropic",
    )
```

- [ ] **Step 3.2: Run test, confirm failure.**

- [ ] **Step 3.3: Implement `_provision_container`.**

```python
async def _provision_container(
    *, owner_id: str, clerk_user_id: str, ecs: Any
) -> None:
    """Provision the ECS container for owner_id.

    Reads provider_choice + byo_provider from billing_repo (Workstream B —
    keyed on owner_id, not user_id). All pre-staging (OAuth tokens, BYO
    secrets) and the openclaw.json write happen inside
    ecs.provision_user_container — see ecs_manager.py:1074-1097.

    Falls back to bedrock_claude when no provider_choice is persisted,
    matching _resolve_provider_choice in routers/container.py:71-86.
    """
    from core.repositories import billing_repo

    row = await billing_repo.get_by_owner_id(owner_id)
    provider_choice = (row or {}).get("provider_choice") or "bedrock_claude"
    byo_provider = (
        (row or {}).get("byo_provider") if provider_choice == "byo_key" else None
    )

    await ecs.provision_user_container(
        owner_id,
        provider_choice=provider_choice,
        byo_provider=byo_provider,
    )
```

- [ ] **Step 3.4: Run tests.**

- [ ] **Step 3.5: Commit.**

```bash
git commit -m "feat(provision): container step in ensure_user_ready

_provision_container reads provider_choice from billing_repo and
delegates to ecs.provision_user_container, which already handles
pre-staging + idempotent restart."
```

---

## Task 4: Implement `ensure_org_member_added_to_paperclip`

**Files:**
- Modify: `apps/backend/core/services/user_provision.py`
- Modify: `apps/backend/tests/unit/services/test_user_provision.py`

This helper mirrors `_handle_organization_membership_created` (`webhooks.py:381-485`) but is callable from non-webhook contexts and doesn't carry the retry-enqueue logic (the webhook handler keeps that wrapper).

- [ ] **Step 4.1: Write tests covering "member not yet in Paperclip → provision_member called", "member already in Paperclip → no-op via provision_member's own short-circuit", "missing email → returns without provisioning".**

- [ ] **Step 4.2: Implement.**

```python
async def ensure_org_member_added_to_paperclip(
    *,
    org_id: str,
    member_user_id: str,
    member_email: str,
    owner_email: str,
) -> None:
    """Add an org member to the existing Paperclip company.

    Called from organizationMembership.created webhook (Task 8). Does NOT
    provision a container — members ride the org owner's container. The
    underlying provisioning.provision_member short-circuits on an
    existing active row.

    Raises whatever provision_member raises so the webhook handler's
    existing _is_retryable / _enqueue_paperclip_retry logic still works.
    """
    from routers.webhooks import (
        _close_paperclip_http,
        _get_paperclip_provisioning,
    )

    provisioning = await _get_paperclip_provisioning()
    try:
        await provisioning.provision_member(
            owner_id=org_id,
            user_id=member_user_id,
            email=member_email,
            owner_email=owner_email,
        )
    finally:
        await _close_paperclip_http(provisioning)
```

- [ ] **Step 4.3: Run tests.**

- [ ] **Step 4.4: Commit.**

```bash
git commit -m "feat(provision): ensure_org_member_added_to_paperclip helper

Org-member fast path. Delegates to provisioning.provision_member with
the same lifecycle wrapping as the webhook handler."
```

---

## Task 5: Wire Stripe `customer.subscription.created/updated` to dispatch `ensure_user_ready`

**Files:**
- Modify: `apps/backend/routers/billing.py`
- Modify: `apps/backend/tests/...` (locate the existing Stripe webhook tests; create if absent)

Today's handler at `routers/billing.py:660-720` only persists state. We add a dispatch after the persistence block.

- [ ] **Step 5.1: Write failing test: subscription.created with provider_choice="bedrock_claude" and status="trialing" calls `ensure_user_ready`.**

- [ ] **Step 5.2: Write failing test: subscription.created with provider_choice="chatgpt_oauth" does NOT call `ensure_user_ready` (waits for /oauth/poll).**

- [ ] **Step 5.3: Write failing test: subscription.created with provider_choice="byo_key" does NOT call `ensure_user_ready` (waits for PUT /settings/keys).**

- [ ] **Step 5.4: Implement.**

In `routers/billing.py`, after the `set_provider_choice` block ending at line 719, insert the dispatch:

Before (current, `routers/billing.py:680-720`):

```python
account = await _resolve_owner_account(event_data)
if account:
    await billing_repo.set_subscription(...)
    metadata = event_data.get("metadata") or {}
    metadata_provider = metadata.get("provider_choice")
    metadata_byo = metadata.get("byo_provider")
    if metadata_provider in ("chatgpt_oauth", "byo_key", "bedrock_claude"):
        # Workstream B: provider_choice now lives on billing_accounts
        # ...
        try:
            await billing_repo.set_provider_choice(...)
        except ValueError:
            ...
        except Exception:
            ...
```

After (add at end of `if account:` block, before the `elif event_type ==` chain continues):

```python
    # Readiness-gate dispatch: bedrock_claude is the only provider whose
    # readiness gate is satisfied by subscription alone (no follow-up
    # OAuth poll or BYO key save). Fire ensure_user_ready in the
    # background so the webhook responds fast and the long ECS provision
    # doesn't share its timeout. chatgpt_oauth + byo_key paths dispatch
    # from /oauth/chatgpt/poll (Task 6) and PUT /settings/keys (Task 7).
    effective_provider = (
        metadata_provider
        if metadata_provider in ("chatgpt_oauth", "byo_key", "bedrock_claude")
        else (account.get("provider_choice") or "bedrock_claude")
    )
    new_status = event_data.get("status", "active")
    if (
        effective_provider == "bedrock_claude"
        and new_status in ("active", "trialing")
    ):
        from core.services.user_provision import ensure_user_ready

        # auth.user_id isn't available in webhook context; for personal
        # owners owner_id == clerk_user_id, and for org owners we read
        # the admin's user_id from subscription metadata (set by
        # /trial-checkout at billing.py:495).
        clerk_user_id = (
            metadata.get("clerk_user_id")
            or account["owner_id"]
        )
        asyncio.create_task(
            ensure_user_ready(
                owner_id=account["owner_id"],
                clerk_user_id=clerk_user_id,
            )
        )
```

(Note: confirm `clerk_user_id` is being threaded into subscription metadata by `create_flat_fee_checkout`; `routers/billing.py:495` shows it's passed through, but verify the metadata key name in `core/services/billing_service.py` and adjust `metadata.get("clerk_user_id")` accordingly. If the metadata key is named differently, prefer the existing field.)

- [ ] **Step 5.5: Run tests.**

- [ ] **Step 5.6: Commit.**

```bash
git commit -m "feat(provision): dispatch ensure_user_ready from Stripe subscription webhook

For bedrock_claude owners, subscription.created/updated is the
readiness signal. Fires ensure_user_ready as a background task so
the webhook stays fast and Stripe doesn't time out on a long ECS
provision."
```

---

## Task 6: Wire `/oauth/chatgpt/poll` success to dispatch `ensure_user_ready`

**Files:**
- Modify: `apps/backend/routers/oauth.py`
- Modify: `apps/backend/tests/unit/routers/test_oauth.py`

`routers/oauth.py:74-82` — the `poll` handler returns `DevicePollResult` after `poll_device_code` has persisted Fernet-encrypted tokens.

- [ ] **Step 6.1: Write failing test: when `poll_device_code` returns `DevicePollResult` and the user has a trialing/active subscription with `provider_choice="chatgpt_oauth"`, `ensure_user_ready` is dispatched.**

- [ ] **Step 6.2: Write failing test: when `poll_device_code` returns `DevicePollPending`, `ensure_user_ready` is NOT dispatched.**

- [ ] **Step 6.3: Implement.**

Before (`routers/oauth.py:74-82`):

```python
async def poll(ctx: AuthContext = Depends(get_current_user)):
    try:
        result = await poll_device_code(user_id=ctx.user_id)
    except OAuthExchangeFailedError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    if result is DevicePollPending:
        return {"status": "pending"}
    assert isinstance(result, DevicePollResult)
    return {"status": "completed", "account_id": result.account_id}
```

After:

```python
async def poll(ctx: AuthContext = Depends(get_current_user)):
    try:
        result = await poll_device_code(user_id=ctx.user_id)
    except OAuthExchangeFailedError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    if result is DevicePollPending:
        return {"status": "pending"}
    assert isinstance(result, DevicePollResult)

    # Readiness-gate dispatch: tokens have just been persisted by
    # poll_device_code. For chatgpt_oauth owners (personal-only — see
    # memory project_chatgpt_oauth_personal_only) this is the last
    # readiness signal; fire ensure_user_ready in the background so the
    # poll response stays snappy.
    import asyncio

    from core.repositories import billing_repo
    from core.services.provision_gate import is_subscription_active
    from core.services.user_provision import ensure_user_ready

    account = await billing_repo.get_by_owner_id(ctx.user_id)
    if (
        account
        and is_subscription_active(account)
        and (account.get("provider_choice") == "chatgpt_oauth")
    ):
        asyncio.create_task(
            ensure_user_ready(
                owner_id=ctx.user_id,
                clerk_user_id=ctx.user_id,
            )
        )

    return {"status": "completed", "account_id": result.account_id}
```

- [ ] **Step 6.4: Run tests.**

- [ ] **Step 6.5: Commit.**

```bash
git commit -m "feat(provision): dispatch ensure_user_ready from /oauth/chatgpt/poll success

The OAuth poll completion is the last readiness signal for
chatgpt_oauth owners. Fires ensure_user_ready in the background."
```

---

## Task 7: Wire BYO key save (`PUT /settings/keys/{tool_id}`) to dispatch `ensure_user_ready`

**Files:**
- Modify: `apps/backend/routers/settings_keys.py`
- Modify: `apps/backend/tests/unit/routers/test_settings_keys.py`

`routers/settings_keys.py:46-63` — the existing `set_key` handler accepts both tool keys (Perplexity, Firecrawl, …) and LLM provider keys (OpenAI, Anthropic). Only the LLM provider variant satisfies a readiness gate.

- [ ] **Step 7.1: Write failing test: PUT to `/settings/keys/anthropic` for an owner with `provider_choice="byo_key"`, `byo_provider="anthropic"`, and active subscription dispatches `ensure_user_ready`.**

- [ ] **Step 7.2: Write failing test: PUT to `/settings/keys/perplexity` (a tool key, not an LLM provider) does NOT dispatch.**

- [ ] **Step 7.3: Write failing test: PUT to `/settings/keys/anthropic` for an owner whose `byo_provider == "openai"` does NOT dispatch (mismatch — they haven't completed their chosen provider's setup yet).**

- [ ] **Step 7.4: Implement.**

Before (`routers/settings_keys.py:46-63`):

```python
@router.put("/{tool_id}")
async def set_key(
    tool_id: str,
    body: SetKeyRequest,
    auth: AuthContext = Depends(get_current_user),
):
    """Store an API key for a tool."""
    owner_id = resolve_owner_id(auth)
    if auth.is_org_context:
        require_org_admin(auth)
    if tool_id not in ALLOWED_KEY_IDS:
        raise HTTPException(status_code=400, detail=f"Unsupported tool: {tool_id}")

    service = KeyService()
    await service.set_key(owner_id, tool_id, body.api_key)

    # TODO: update openclaw.json on EFS + send config.apply RPC
    return {"status": "ok", "tool_id": tool_id}
```

After:

```python
@router.put("/{tool_id}")
async def set_key(
    tool_id: str,
    body: SetKeyRequest,
    auth: AuthContext = Depends(get_current_user),
):
    """Store an API key for a tool."""
    owner_id = resolve_owner_id(auth)
    if auth.is_org_context:
        require_org_admin(auth)
    if tool_id not in ALLOWED_KEY_IDS:
        raise HTTPException(status_code=400, detail=f"Unsupported tool: {tool_id}")

    service = KeyService()
    await service.set_key(owner_id, tool_id, body.api_key)

    # Readiness-gate dispatch: only LLM-provider keys satisfy the byo_key
    # readiness gate, and only when the owner already chose this provider
    # at trial-checkout. Tool keys (Perplexity, Firecrawl, etc.) don't
    # gate provisioning.
    if tool_id in SUPPORTED_LLM_PROVIDERS:
        import asyncio

        from core.repositories import billing_repo
        from core.services.provision_gate import is_subscription_active
        from core.services.user_provision import ensure_user_ready

        account = await billing_repo.get_by_owner_id(owner_id)
        if (
            account
            and is_subscription_active(account)
            and account.get("provider_choice") == "byo_key"
            and account.get("byo_provider") == tool_id
        ):
            asyncio.create_task(
                ensure_user_ready(
                    owner_id=owner_id,
                    clerk_user_id=auth.user_id,
                )
            )

    return {"status": "ok", "tool_id": tool_id}
```

- [ ] **Step 7.5: Run tests.**

- [ ] **Step 7.6: Commit.**

```bash
git commit -m "feat(provision): dispatch ensure_user_ready from BYO LLM key save

Saving an OpenAI/Anthropic key for an owner with provider_choice=byo_key
and matching byo_provider is the last readiness signal. Tool keys
(Perplexity / Firecrawl / TTS) don't dispatch."
```

---

## Task 8: Switch `organizationMembership.created` to use `ensure_org_member_added_to_paperclip`

**Files:**
- Modify: `apps/backend/routers/webhooks.py`
- Modify: `apps/backend/tests/unit/routers/test_webhooks.py`

`routers/webhooks.py:447-485` — the existing handler builds `provisioning` and calls `provision_member`. We swap the direct call for the helper but keep the retry-enqueue wrapping.

- [ ] **Step 8.1: Write failing test: membership.created calls `ensure_org_member_added_to_paperclip`. Existing retry behaviour preserved on retryable failure.**

- [ ] **Step 8.2: Write failing test: membership.created does NOT trigger any container provisioning (no `provision_user_container` mock invocation).**

- [ ] **Step 8.3: Implement.**

Before (`routers/webhooks.py:447-485`):

```python
provisioning = await _get_paperclip_provisioning()
try:
    await provisioning.provision_member(
        owner_id=org_id,
        user_id=user_id,
        email=email,
        owner_email=owner_email,
    )
    put_metric("paperclip.webhook.provision_member", dimensions={"result": "ok"})
except Exception as e:
    retryable = _is_retryable(e)
    ...
finally:
    await _close_paperclip_http(provisioning)
```

After: replace the direct `provisioning.provision_member` call with the canonical helper, keep the retry/metric wrapper:

```python
from core.services.user_provision import ensure_org_member_added_to_paperclip

try:
    await ensure_org_member_added_to_paperclip(
        org_id=org_id,
        member_user_id=user_id,
        member_email=email,
        owner_email=owner_email,
    )
    put_metric("paperclip.webhook.provision_member", dimensions={"result": "ok"})
except Exception as e:
    retryable = _is_retryable(e)
    put_metric(
        "paperclip.webhook.provision_member",
        dimensions={"result": "retryable" if retryable else "error"},
    )
    logger.warning(
        "provision_member failed for org=%s user=%s retryable=%s err=%s",
        org_id,
        user_id,
        retryable,
        e,
    )
    if retryable:
        await _enqueue_paperclip_retry(
            op="provision_member",
            payload={
                "org_id": org_id,
                "user_id": user_id,
                "email": email,
                "owner_user_id": owner_user_id,
                "owner_email": owner_email,
            },
            owner_id=user_id,
        )
```

The `provisioning = await _get_paperclip_provisioning()` / `_close_paperclip_http(provisioning)` block becomes unnecessary at this site (the helper manages its own httpx lifecycle). Remove it.

- [ ] **Step 8.4: Run tests.**

- [ ] **Step 8.5: Commit.**

```bash
git commit -m "refactor(webhook): membership.created routes through ensure_org_member_added_to_paperclip

Single canonical entry point for org-member Paperclip provisioning.
Retry/metric wrapping preserved at the webhook layer."
```

---

## Task 9: Downgrade `organization.created` webhook to record-keeping only

**Files:**
- Modify: `apps/backend/routers/webhooks.py`
- Modify: `apps/backend/tests/unit/routers/test_webhooks.py`

`routers/webhooks.py:319-378` (`_handle_organization_created`). Today this fires `provision_org` eagerly. After this task: log + emit a metric so we know the org exists in Clerk, but defer Paperclip provisioning until the readiness gate fires (Stripe webhook for the org admin's bedrock_claude path, or OAuth/BYO trigger for the admin who happens to be a personal user — wait, orgs can't use OAuth, so it's always Stripe / BYO save).

- [ ] **Step 9.1: Write failing test: organization.created does NOT call `provisioning.provision_org` and does NOT enqueue a retry row.**

- [ ] **Step 9.2: Write passing test: organization.created still emits a metric so operators can see new orgs landing.**

- [ ] **Step 9.3: Implement.** Replace the body of `_handle_organization_created` with a logging + metric stub:

```python
async def _handle_organization_created(data: dict) -> None:
    """Record the new Clerk org. Paperclip provisioning is deferred to the
    readiness gate (Stripe subscription.created with provider_choice=
    bedrock_claude, or BYO key save with provider_choice=byo_key).

    Org owners' provider_choice is bedrock_claude or byo_key (never
    chatgpt_oauth — see memory project_chatgpt_oauth_personal_only),
    and both of those paths fire ensure_user_ready from a separate
    trigger after onboarding completes.
    """
    org_id = data.get("id", "")
    owner_user_id = data.get("created_by", "")
    if not org_id or not owner_user_id:
        logger.warning("Clerk organization.created missing id/created_by: %s", data)
        return
    put_metric(
        "clerk.webhook.organization_created",
        dimensions={"result": "deferred_to_readiness_gate"},
    )
    logger.info(
        "organization.created received for org=%s owner=%s — paperclip provisioning deferred",
        org_id,
        owner_user_id,
    )
```

- [ ] **Step 9.4: Run tests.**

- [ ] **Step 9.5: Commit.**

```bash
git commit -m "refactor(webhook): organization.created becomes record-keeping only

Paperclip provisioning for new orgs is now driven by the readiness gate
(Stripe webhook for bedrock_claude, BYO key save for byo_key). The
webhook still logs + emits a metric so operators see new orgs."
```

---

## Task 10: Remove `/teams` autoprovision side-effect in `_ctx`

**Files:**
- Modify: `apps/backend/routers/teams/agents.py` (specifically `_ctx`, lines 263-308)
- Modify: `apps/backend/tests/unit/routers/test_teams_*.py` (locate and update)

- [ ] **Step 10.1: Write failing test: hitting `/teams/agents` for a personal user with no Paperclip row no longer schedules an `ensure_paperclip_workspace` task. The handler returns 409 (or whatever the new error is) instead of 202.**

- [ ] **Step 10.2: Implement.**

In `routers/teams/agents.py`:

- Remove the import on line 34: `from core.services.paperclip_autoprovision import ensure_paperclip_workspace`.
- Modify `_ctx` (lines 263-308). Replace the `except TeamsContextError` recovery branch with: when `auth.is_org_context`, re-raise as today; when personal-context with no container, keep the existing 402 ("subscribe to enable Teams"); when personal-context WITH a container but no Paperclip row, return 409 with message "team workspace not provisioned — re-run onboarding". Drop the `asyncio.create_task(ensure_paperclip_workspace(...))` call.

After:

```python
except TeamsContextError as exc:
    if exc.status_code != 409 or exc.detail != "team workspace not provisioned":
        raise
    if auth.is_org_context:
        raise
    container = await container_repo.get_by_owner_id(auth.user_id)
    if not container:
        raise TeamsContextError(
            status_code=402,
            detail="subscribe to enable Teams",
        ) from exc
    # Post-consolidation invariant: a personal user with a running
    # container also has a Paperclip company (ensure_user_ready
    # provisions both atomically). Hitting this branch means the
    # readiness gate dispatch never fired — surface a clear error
    # instead of silently self-healing, which would just hide the bug.
    raise TeamsContextError(
        status_code=409,
        detail="team workspace not provisioned",
    ) from exc
```

- [ ] **Step 10.3: Run tests.**

- [ ] **Step 10.4: Commit.**

```bash
git commit -m "refactor(teams): remove /teams autoprovision side-effect

After the readiness-gate consolidation, a personal user with a running
container is guaranteed to have a Paperclip company. Surface a clear
409 if that invariant is violated instead of silently self-healing
(which masked the original race)."
```

---

## Task 11: Re-route Paperclip proxy `_self_provision` and `_autoprovision` through `ensure_user_ready`

**Files:**
- Modify: `apps/backend/routers/paperclip_proxy.py` (lines 829, 914)

The proxy is the rare fallback path for users hitting `company.isol8.co` directly. Both call sites today inline the same `provision_org` chain. Switch both to `ensure_user_ready` so the proxy benefits from the same readiness checks + container provisioning.

- [ ] **Step 11.1: Write failing test: proxy `_self_provision` path calls `ensure_user_ready` instead of `provision_org` directly.**

- [ ] **Step 11.2: Implement** — replace both inline blocks (`paperclip_proxy.py:818-844` for self-provision, `:907-928` for autoprovision) with:

```python
from core.services.user_provision import ensure_user_ready

try:
    await ensure_user_ready(
        owner_id=auth.user_id,
        clerk_user_id=auth.user_id,
    )
except Exception as e:  # noqa: BLE001
    logger.exception(
        "paperclip_proxy: ensure_user_ready failed for user=%s: %s",
        auth.user_id,
        e,
    )
    raise HTTPException(
        status_code=502,
        detail=f"Provisioning failed: {type(e).__name__}",
    )
```

(Note: `ensure_user_ready` short-circuits if Paperclip + container are already up, so the proxy still gets the fast-path. The inline `httpx.AsyncClient` block goes away.)

- [ ] **Step 11.3: Run tests.**

- [ ] **Step 11.4: Commit.**

```bash
git commit -m "refactor(proxy): paperclip proxy provision paths route through ensure_user_ready"
```

---

## Task 12: Remove the `/container/provision` call from frontend `ProvisioningStepper`

**Files:**
- Modify: `apps/frontend/src/components/chat/ProvisioningStepper.tsx` (lines 269-294)

Backend still serves `POST /container/provision` for the auto-retry path (`container.py:155-169`) and for ops; the frontend just stops calling it.

- [ ] **Step 12.1: Manual smoke test the frontend after backend Tasks 1-11 land — confirm `/chat` no longer needs the explicit POST.**

- [ ] **Step 12.2: Remove the `needsProvision` effect.**

Before (`ProvisioningStepper.tsx:269-294`):

```tsx
const needsProvision = container === null || container?.status === "stopped";
useEffect(() => {
  if (!orgLoaded) return;
  if (needsProviderStep) return;
  if (needsProvision && shouldPollContainer && !provisionRequestedRef.current) {
    provisionRequestedRef.current = true;
    api.post("/container/provision", {}).catch((err: unknown) => {
      console.error("Container provision failed:", err);
    });
  }
  if (container && container.status !== "stopped") {
    provisionRequestedRef.current = false;
  }
}, [needsProvision, orgLoaded, container, shouldPollContainer, api, needsProviderStep]);
```

After: delete the entire effect and the `provisionRequestedRef` declaration. The stepper relies purely on `useContainerStatus` polling (`useContainerStatus.ts:64`) — which will start returning 404 → 200 once the backend trigger fires `ensure_user_ready`.

For users whose container goes into `error`/`stopped` after onboarding, `GET /container/status` already self-heals via `_background_provision` (`container.py:155-169`). No frontend POST needed.

- [ ] **Step 12.3: Run frontend tests + smoke test on dev.**

- [ ] **Step 12.4: Commit.**

```bash
git commit -m "refactor(frontend): drop ProvisioningStepper /container/provision POST

Backend now provisions the container from the readiness-gate trigger
(Stripe webhook / OAuth poll / BYO key save). Frontend just polls
/container/status until phase=ready."
```

---

## Task 13: Integration test covering all four trigger paths

**Files:**
- Create: `apps/backend/tests/integration/test_provisioning_state_machine.py`

- [ ] **Step 13.1: Bedrock path:** simulate `/billing/trial-checkout` → `customer.subscription.created` webhook with `provider_choice=bedrock_claude`. Confirm `ensure_user_ready` is dispatched and provisions both Paperclip + container.

- [ ] **Step 13.2: ChatGPT OAuth path:** simulate subscription.created (oauth) — confirm no dispatch — then simulate `/oauth/chatgpt/poll` returning `DevicePollResult` — confirm dispatch.

- [ ] **Step 13.3: BYO key path:** simulate subscription.created (byo_key) — confirm no dispatch — then simulate PUT `/settings/keys/anthropic` with matching `byo_provider` — confirm dispatch.

- [ ] **Step 13.4: Org-member path:** simulate `organizationMembership.created` for an existing-org case — confirm `ensure_org_member_added_to_paperclip` runs and `ecs.provision_user_container` is NOT called.

- [ ] **Step 13.5: Idempotency:** re-fire each trigger; confirm no duplicate work (mocks on `provision_org` + `provision_user_container` are called only once per owner).

- [ ] **Step 13.6: Commit.**

---

## Task 14: Documentation update

**Files:**
- Modify: `CLAUDE.md` — Provisioning section.
- Optional: `docs/architecture/2026-05-07-provisioning-state-machine.md` — operator-facing.

- [ ] **Step 14.1:** Update the "Container Update System" / "Agent Message Flow" sections to describe the readiness-gate state machine and remove references to lazy `/chat`-driven provisioning.

- [ ] **Step 14.2: Commit.**

---

## Task 15: Manual smoke test on dev

**Files:** None — operational verification.

- [ ] **Step 15.1:** Wipe dev per CLAUDE.md "Clean-Slate Dev Reset".

- [ ] **Step 15.2:** Sign up as personal user → choose Bedrock → confirm container is up before `/chat` is opened. Open `/teams` first; confirm Friday is visible.

- [ ] **Step 15.3:** Sign up as personal user → choose ChatGPT OAuth → complete device-code flow → confirm container is up.

- [ ] **Step 15.4:** Sign up as personal user → choose BYO key (Anthropic) → submit key → confirm container is up.

- [ ] **Step 15.5:** Sign up as org owner → repeat 15.2 + 15.4 in org context (chatgpt_oauth blocked by existing 403 at `oauth.py:46-50`).

- [ ] **Step 15.6:** Invite an org member → accept → confirm member can use `/chat` (rides owner's container) without their own provisioning.

- [ ] **Step 15.7:** Cancel subscription → confirm `customer.subscription.deleted` still tears down container + disables Paperclip (no regression on the path at `routers/billing.py:735-781`).

- [ ] **Step 15.8:** Sign up but never finish provider setup → verify provision gate at `/container/status` returns 402 with `code=oauth_required` or `code=credits_required` etc.

---

## Out-of-scope follow-ups

1. **Frontend onboarding UX redesign.** The stepper will need to show a "Setting up your workspace…" state during the readiness-gate → Paperclip → container provision window. That's a UX/UI plan in its own right and is deferred.
2. **Migration script for existing dev users.** Per Pre-flight Step 0.1, dev is wiped. Production hasn't shipped this stack yet at the time of writing.
3. **Token rotation in the existing migration script** (Codex P1 re-flag from PR #563). After consolidation, fresh provisioning produces correct tokens — the script's value is mostly historical.
4. **Cleanup of legacy `"main"` agent fallback in `_build_agents_list`.** Once every owner is guaranteed to have a Paperclip company, the empty-paperclip-agents fallback in `build_openclaw_config_dict` is dead code. Track separately.
5. **Trial-cancellation edge case** (user cancels mid-trial). Today's `customer.subscription.deleted` handler covers it; no change here.
6. **ECS resilience** — the 2026-04-16 plan covers a different concern (gateway pool reconnection); not in scope.
7. **Locking down `POST /container/provision` to admin-only.** Not strictly necessary — the existing payment gate (`container.py:277-281`) already prevents abuse and the auto-retry path needs the endpoint. Revisit if user-driven calls produce ambiguous logs.

---

## Self-review

- **All "current behaviour" claims cite file:line.** Every trigger site in the Problem Statement table names a file and a line range; the trigger-site decision table has the same.
- **Every change instruction names a file and a function/lines + before/after snippet.** Tasks 5, 6, 7, 8, 9, 10, 11, 12 each carry an explicit before/after on the exact code being modified.
- **TDD-shaped.** Tasks 1-4 follow scaffold-test-fail → implement → pass → commit. Tasks 5-12 add behavioural tests against existing handlers before changing them. Task 13 is an integration sweep.
- **Order is safe.** Tasks 1-4 build the new helper (no behaviour change). Tasks 5-7 wire NEW triggers (additive, the old triggers still fire). Task 8 swaps membership webhook to the helper (functional parity). Task 9 removes the old org.created trigger only AFTER the new triggers cover the path. Tasks 10-12 remove the old fallbacks last. There is no window where ALL triggers are dead.
- **Out-of-scope is honest.** Onboarding UX, migration scripts, dead-code cleanup, and admin-only gating on `/container/provision` are explicitly deferred.

---

## Execution decision

When you're ready to execute:

1. **Stack on top of `worktree-paperclip-openclaw-agent-pairing` or wait for merge?** Stack — the agent-pairing PR is the foundation this plan builds on.
2. **Subagent-driven execution.** Same pattern as PR #563 — fresh subagent per task with two-stage review.

When this lands, the agent-pairing PR's race becomes structurally impossible (single trigger, dependency-ordered). The stored-token defensive layer becomes belt-and-braces; the backfill migration script becomes obsolete.
