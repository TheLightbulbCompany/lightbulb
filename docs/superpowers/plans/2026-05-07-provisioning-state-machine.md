# Provisioning State-Machine Consolidation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Isol8's scattered provisioning triggers with a single deterministic state-machine. Today, Paperclip and container provisioning fire from three different surfaces (Clerk webhooks, `/teams` autoprovision, `/chat`-driven container provision) with implicit ordering assumptions that don't hold for all user types. After this plan, provisioning fires from exactly one trigger per user — the moment that user's authorization prerequisites are complete — and runs all of Paperclip + container + openclaw.json in dependency order.

**Architecture:** A single canonical helper, `ensure_user_ready(owner_id, clerk_user_id)`, that idempotently provisions everything in the correct order: Paperclip company → provider pre-staging → ECS container → openclaw.json with the live Paperclip agent list. It's called from exactly the points where the user's last authorization signal lands: the Stripe `customer.subscription.created` webhook (for Bedrock — no separate setup), the `/oauth/poll` success endpoint (for ChatGPT OAuth), or the `/settings/llm` POST success endpoint (for BYO key). Org members ride the owner's subscription/provider/container — their `organizationMembership.created` webhook adds them to the existing Paperclip company; the container is already provisioned by the owner. The lazy `POST /container/provision` from `/chat`, the `/teams` autoprovision path, and the Clerk `organization.created` webhook driving Paperclip directly all go away.

**Tech Stack:** Python 3.13 (FastAPI, uv), httpx (Paperclip admin client), websockets (OpenClaw gateway RPC), DynamoDB (existing repos), pytest + pytest-asyncio.

**Premise:** The PR `feat: paperclip ↔ openclaw agent pairing` (currently #563) lands first or alongside this work — its UUID consistency between Paperclip and OpenClaw is what makes "openclaw.json reflects Paperclip's agent list" coherent. This plan builds on that foundation.

---

## Problem statement

Today's provisioning has three independent triggers per user type:

| User type | Paperclip provisioned by | Container provisioned by | Race? |
|---|---|---|---|
| Org owner | `organization.created` webhook | `/chat` open → `POST /container/provision` | No (webhook eager, /chat lazy — natural order) |
| Org member | `organizationMembership.created` webhook | (rides org owner's container) | No |
| Personal user | `/teams` autoprovision (lazy) | `/chat` open → `POST /container/provision` (lazy) | **YES** — if `/chat` opens before `/teams`, container provisions without Paperclip data; later Paperclip seeding leaves the container's `openclaw.json` permanently stale until reprovision |

Symptoms surfaced by Codex on PR #563:

- **P2:** "Pair seeded Friday when container already exists" — the personal-user race above.
- **P1 re-flag (`/teams`):** static code in `/teams` create_agent assumes the stored Paperclip token is correct, which it only is post-fix; rotation isn't enforced by code shape.
- **P1 re-flag (migration script):** doesn't rotate stored tokens; relies on operator wiping.

These are symptoms of the deeper problem: provisioning isn't gated on a single canonical signal. We work around it with idempotency + retries + best-effort fallbacks.

## Architecture

### The state machine

A user moves through these states, in dependency order:

```
                       ┌─────────────────────────┐
                       │   1. Clerk identity     │  ← user.created
                       └────────────┬────────────┘
                                    ↓
                       ┌─────────────────────────┐
                       │   2. Stripe subscription│  ← /billing/trial-checkout +
                       │      (trialing|active)  │     customer.subscription.created
                       └────────────┬────────────┘
                                    ↓
                       ┌─────────────────────────┐
                       │   3. Provider configured│  ← /onboarding completes:
                       │   (Bedrock|OAuth|BYO)   │     • Bedrock → no extra step
                       └────────────┬────────────┘     • OAuth   → /oauth/poll success
                                    ↓                  • BYO key → /settings/llm POST
                       ╔═════════════════════════╗
                       ║  ★ READINESS GATE ★     ║  ← Single trigger fires here:
                       ║  All three above true.  ║     ensure_user_ready()
                       ║  Now provision everything│
                       ║  in dependency order.   ║
                       ╚════════════╤════════════╝
                                    ↓
                       ┌─────────────────────────┐
                       │   4. Paperclip company  │  ← provision_org / provision_member
                       │      (Friday seeded)    │
                       └────────────┬────────────┘
                                    ↓
                       ┌─────────────────────────┐
                       │   5. Container          │  ← write_user_configs:
                       │      (ECS, EFS,         │     • pre-stage provider state
                       │       openclaw.json)    │     • fetch Paperclip agents
                       └─────────────────────────┘     • write openclaw.json
                                                       • create_user_service
                                                       • start_user_service
```

Org members hit a different ready point — they don't have their own subscription or provider. Their trigger is `organizationMembership.created`: add them to the existing Paperclip company, return.

### `ensure_user_ready` is the only provisioning entry point

```python
async def ensure_user_ready(*, owner_id: str, clerk_user_id: str) -> None:
    """Single canonical provisioning entry point. Idempotent.

    Runs after the readiness state-machine completes (see above):
      - subscription is trialing or active
      - provider is configured

    Provisions in dependency order:
      1. Paperclip company (if not already)
      2. Provider pre-staging
      3. Container (ECS + EFS + openclaw.json reflecting Paperclip agents)

    Idempotent at every step — short-circuits on existing state.

    For org members: see ensure_org_member_added_to_paperclip — they don't
    need their own container, just Paperclip company membership.
    """
```

### What goes away

- `POST /container/provision` invoked from `/chat`. (The endpoint can stay as an admin tool for re-provision.)
- `paperclip_autoprovision.ensure_paperclip_workspace` called from `routers/teams/agents.py` (the `/teams` autoprovision UX shortcut).
- `provision_org` invocation from `organization.created` webhook. (The webhook becomes informational — we record the org existed in Clerk, but don't provision anything until the readiness gate passes.)
- Frontend's `useContainerStatus` polling-and-provisioning logic moves to a passive "is the container ready?" check.
- The "lazy on /chat" UX (loading state on first `/chat`) — replaced by "container is already up" because provisioning happens during onboarding.

### What stays

- `organizationMembership.created` webhook — still adds members to existing Paperclip company on accept.
- Stripe `customer.subscription.deleted` webhook — still tears down container + disables Paperclip row.
- Provision gate (`provision_gate.py`) — still blocks `/chat` until readiness is true. After this refactor, the gate is a STATE QUERY only, not a trigger; provisioning happens earlier, asynchronously.
- All idempotency + retry + status-transition code in `ecs_manager.py` — unchanged.
- All the per-provider pre-staging (`_pre_stage_oauth_for_user`, `_build_byo_secrets_for_user`) — unchanged.

---

## File Structure

**New files:**
- `apps/backend/core/services/user_provision.py` — the unified `ensure_user_ready` helper and `ensure_org_member_added_to_paperclip` helper.
- `apps/backend/tests/unit/services/test_user_provision.py` — unit tests for both helpers.
- `apps/backend/tests/integration/test_provisioning_state_machine.py` — end-to-end tests of all four trigger points.

**Modified files (additions / wiring):**
- `apps/backend/routers/billing.py` — `customer.subscription.created` webhook fires `ensure_user_ready` when provider is `bedrock_claude`.
- `apps/backend/routers/oauth.py` — `/oauth/poll` success path fires `ensure_user_ready`.
- `apps/backend/routers/settings_keys.py` — BYO key save success fires `ensure_user_ready`.
- `apps/backend/routers/webhooks.py` — `organizationMembership.created` calls the new `ensure_org_member_added_to_paperclip` helper. `organization.created` is downgraded to record-keeping only.
- `apps/backend/core/services/provision_gate.py` — minor: ensure error states surface "still provisioning, please wait" rather than triggering a provision retry.

**Modified files (removals):**
- `apps/backend/routers/teams/agents.py` — remove the `ensure_paperclip_workspace` autoprovision call from `_ctx`. (Or downgrade to a defensive no-op-if-missing that surfaces an error rather than silently provisioning.)
- `apps/backend/routers/container.py` — `POST /container/provision` becomes admin-only (auth gate) or removed entirely. Frontend stops calling it.
- `apps/backend/core/services/paperclip_autoprovision.py` — kept as the implementation detail of `ensure_user_ready`'s Paperclip step, but the standalone `ensure_paperclip_workspace` callers go away.
- `apps/backend/routers/webhooks.py` — `_handle_organization_created` no longer calls `provision_org`.

**Frontend changes:**
- `apps/frontend/src/hooks/useContainerStatus.ts` — passive read-only status polling; no longer triggers `POST /container/provision`.
- `apps/frontend/src/app/onboarding/` — show "Setting up your workspace…" state with a polling check on container status, after the user finishes provider setup but before they're allowed to navigate to `/chat`.
- `apps/frontend/src/components/chat/ProvisioningStepper.tsx` — repurpose for the onboarding flow rather than the post-/chat-open waiting state.

---

## Pre-flight: confirm scope and rollout strategy

- [ ] **Step 0.1: Confirm rollout decision**

The existing system has live users. Two rollout strategies:

1. **Wipe-everything cutover:** Operator wipes all users, redeploys, users re-onboard. The new state machine handles them from scratch. Simplest, no migration code.
2. **Incremental migration:** Deploy the new code alongside the old; existing users' provisioning stays as-is until their next reprovision; new sign-ups use the new flow. Requires keeping both code paths working temporarily.

For Isol8 today, **(1) wipe** is the documented plan (admin@isol8 + prasiddha can re-onboard). This plan assumes (1).

If (2) is chosen later, additional migration tasks are required (out of scope here).

- [ ] **Step 0.2: Verify the agent-pairing PR (#563) is merged or stacked**

This plan builds on the UUID consistency between Paperclip and OpenClaw. Confirm `feat: paperclip ↔ openclaw agent pairing` is either merged to main or stacked as a parent branch. All file paths assume that branch's state.

---

## Task 1: Create `user_provision.py` skeleton with `ensure_user_ready` (idempotent state-machine)

**Files:**
- Create: `apps/backend/core/services/user_provision.py`
- Test: `apps/backend/tests/unit/services/test_user_provision.py`

- [ ] **Step 1.1: Write the failing test for the no-op-when-already-provisioned case**

```python
"""Unit tests for the unified user_provision helper."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.services.user_provision import ensure_user_ready


@pytest.mark.asyncio
async def test_ensure_user_ready_short_circuits_when_container_already_running():
    """Idempotent: if the container is already running and Paperclip is
    already provisioned, ensure_user_ready does no work."""
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

    await ensure_user_ready(
        owner_id="user_abc",
        clerk_user_id="user_abc",
        paperclip_repo=fake_paperclip_repo,
        container_repo=fake_container_repo,
        ecs=fake_ecs,
    )

    fake_ecs.provision_user_container.assert_not_awaited()
```

- [ ] **Step 1.2: Run test, verify it fails**

`cd apps/backend && uv run pytest tests/unit/services/test_user_provision.py -v` — expects `ModuleNotFoundError`.

- [ ] **Step 1.3: Implement skeleton**

```python
"""Unified user provisioning entry point.

After the readiness state-machine completes (subscription active +
provider configured), call ensure_user_ready to idempotently provision
Paperclip + container in dependency order. This is the SOLE provisioning
trigger; the lazy /chat-driven path and the /teams-autoprovision path
both call into here.

Org members hit a different path: ensure_org_member_added_to_paperclip
adds them to the existing Paperclip company. They don't need their own
container because they ride the org owner's.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def ensure_user_ready(
    *,
    owner_id: str,
    clerk_user_id: str,
    paperclip_repo,
    container_repo,
    ecs,
) -> None:
    """Idempotent: provisions Paperclip + container if not already done.

    Args:
        owner_id: Container owner — Clerk org_id for orgs, user_id for personal.
        clerk_user_id: The Clerk user who triggered the readiness signal.
        paperclip_repo, container_repo, ecs: Injected dependencies for testing.
    """
    # Phase 1: Paperclip
    paperclip_row = await paperclip_repo.get(owner_id)
    if paperclip_row is None or paperclip_row.status != "active":
        # Provision Paperclip company. To be implemented in Task 2.
        raise NotImplementedError("paperclip provisioning step")

    # Phase 2: Container
    container_row = await container_repo.get_by_owner_id(owner_id)
    if container_row is None or container_row.get("status") != "running":
        # Provision container. To be implemented in Task 3.
        raise NotImplementedError("container provisioning step")

    logger.info(
        "ensure_user_ready: owner=%s already fully provisioned (no-op)",
        owner_id,
    )
```

- [ ] **Step 1.4: Run test, verify it passes**

`cd apps/backend && uv run pytest tests/unit/services/test_user_provision.py::test_ensure_user_ready_short_circuits_when_container_already_running -v`

- [ ] **Step 1.5: Commit**

```bash
git add apps/backend/core/services/user_provision.py \
       apps/backend/tests/unit/services/test_user_provision.py
git commit -m "feat(provision): scaffold user_provision module skeleton

Unified provisioning entry point. ensure_user_ready idempotently
short-circuits when both Paperclip and container are fully provisioned.
Subsequent tasks fill in the actual provisioning steps."
```

---

## Task 2: Wire Paperclip provisioning step into `ensure_user_ready`

**Files:**
- Modify: `apps/backend/core/services/user_provision.py`
- Modify: `apps/backend/tests/unit/services/test_user_provision.py`

- [ ] **Step 2.1: Add test for "Paperclip not provisioned yet → provision_org runs"**

```python
@pytest.mark.asyncio
async def test_ensure_user_ready_provisions_paperclip_when_missing(monkeypatch):
    """If no PaperclipCompany row exists, provision_org runs to create
    the company + seed Friday."""
    fake_paperclip_repo = MagicMock()
    fake_paperclip_repo.get = AsyncMock(return_value=None)  # not provisioned
    fake_container_repo = MagicMock()
    fake_container_repo.get_by_owner_id = AsyncMock(
        return_value={"status": "running"}
    )
    fake_ecs = MagicMock()
    fake_provision_org = AsyncMock()
    monkeypatch.setattr(
        "core.services.user_provision._provision_paperclip_company",
        fake_provision_org,
    )

    await ensure_user_ready(
        owner_id="user_abc",
        clerk_user_id="user_abc",
        paperclip_repo=fake_paperclip_repo,
        container_repo=fake_container_repo,
        ecs=fake_ecs,
    )

    fake_provision_org.assert_awaited_once_with(
        owner_id="user_abc", clerk_user_id="user_abc",
    )
```

- [ ] **Step 2.2: Run test, verify it fails**

- [ ] **Step 2.3: Implement `_provision_paperclip_company`**

```python
async def _provision_paperclip_company(
    *, owner_id: str, clerk_user_id: str
) -> None:
    """Provision a fresh Paperclip company for owner_id.

    Routes to provision_org for new owners (org or personal — owner_id
    is the container key in both cases). Org members go through
    ensure_org_member_added_to_paperclip instead.
    """
    import httpx
    from core.config import settings
    from core.repositories.paperclip_repo import PaperclipRepo
    from core.services.paperclip_admin_client import PaperclipAdminClient
    from core.services.paperclip_owner_email import lookup_owner_email
    from core.services.paperclip_provisioning import PaperclipProvisioning

    owner_email = await lookup_owner_email(
        org_id=owner_id if owner_id.startswith("org_") else None,
        fallback_user_id=clerk_user_id,
    )
    if not owner_email:
        raise RuntimeError(
            f"Cannot provision Paperclip for {owner_id}: no email available"
        )

    http = httpx.AsyncClient(
        base_url=settings.PAPERCLIP_INTERNAL_URL,
        timeout=15.0,
    )
    try:
        admin = PaperclipAdminClient(http_client=http)
        repo = PaperclipRepo(table_name="paperclip-companies")
        provisioning = PaperclipProvisioning(
            admin_client=admin, repo=repo, env_name=settings.ENVIRONMENT or "",
        )
        await provisioning.provision_org(
            owner_id=owner_id,
            owner_user_id=clerk_user_id,
            owner_email=owner_email,
        )
    finally:
        await http.aclose()
```

Update `ensure_user_ready` to call `_provision_paperclip_company` in place of the `NotImplementedError`.

- [ ] **Step 2.4: Run tests; both should pass**

- [ ] **Step 2.5: Commit**

```bash
git add apps/backend/core/services/user_provision.py \
       apps/backend/tests/unit/services/test_user_provision.py
git commit -m "feat(provision): paperclip step in ensure_user_ready

Extracts provision_org orchestration into _provision_paperclip_company.
Idempotent: short-circuits when an active row already exists."
```

---

## Task 3: Wire container provisioning step into `ensure_user_ready`

**Files:**
- Modify: `apps/backend/core/services/user_provision.py`
- Modify: `apps/backend/tests/unit/services/test_user_provision.py`

- [ ] **Step 3.1: Add test for "container not running → ecs.provision_user_container runs"**

(Test similar to Step 2.1 but mocks the ECS path.)

- [ ] **Step 3.2: Run test, verify failure**

- [ ] **Step 3.3: Implement container step**

```python
async def _provision_container(
    *, owner_id: str, clerk_user_id: str, ecs
) -> None:
    """Provision the container for owner_id.

    Reads provider_choice + byo_provider from billing_repo (set during
    Stripe checkout). All pre-staging (OAuth tokens, BYO secrets) and
    the openclaw.json write happen inside provision_user_container.
    """
    from core.repositories import billing_repo

    account = await billing_repo.get_by_owner_id(owner_id)
    if account is None:
        raise RuntimeError(
            f"Cannot provision container for {owner_id}: no billing account"
        )

    await ecs.provision_user_container(
        owner_id,
        provider_choice=account["provider_choice"],
        byo_provider=account.get("byo_provider"),
    )
```

Update `ensure_user_ready` to call `_provision_container` in place of the `NotImplementedError`.

- [ ] **Step 3.4: Run tests**

- [ ] **Step 3.5: Commit**

---

## Task 4: Add `ensure_org_member_added_to_paperclip` helper

**Files:**
- Modify: `apps/backend/core/services/user_provision.py`
- Modify: `apps/backend/tests/unit/services/test_user_provision.py`

For org members joining existing orgs: they don't need their own container or a fresh Paperclip company — they just need a row in the existing company.

- [ ] **Step 4.1: Write tests covering "member not yet in Paperclip", "member already in Paperclip" (idempotent), and "no company exists yet" (raises so caller can wait/retry).**

- [ ] **Step 4.2: Implement**

```python
async def ensure_org_member_added_to_paperclip(
    *, org_id: str, member_user_id: str, member_email: str, owner_email: str
) -> None:
    """Add an org member to the existing Paperclip company.

    Called from organizationMembership.created webhook. Idempotent
    (provision_member short-circuits on active row).

    Does NOT provision a container — members ride the org owner's
    container.
    """
    # Implementation mirrors _provision_paperclip_company but routes
    # to provisioning.provision_member.
```

- [ ] **Step 4.3: Run tests**

- [ ] **Step 4.4: Commit**

---

## Task 5: Wire `ensure_user_ready` into Stripe `customer.subscription.created` webhook

**Files:**
- Modify: `apps/backend/routers/billing.py`
- Modify: `apps/backend/tests/unit/routers/test_billing_webhooks.py` (or wherever the tests live)

- [ ] **Step 5.1: Test that, when provider_choice = bedrock_claude and the webhook fires, ensure_user_ready is called**

- [ ] **Step 5.2: Test that, when provider_choice = chatgpt_oauth or byo_key, ensure_user_ready is NOT called (those have their own triggers in tasks 6 and 7)**

- [ ] **Step 5.3: Implement: in the `customer.subscription.created` handler, after persisting subscription state, dispatch a background task to call ensure_user_ready if provider_choice == "bedrock_claude"**

The dispatch should be `asyncio.create_task(...)` to keep the webhook response fast — the long ECS provision happens in the background.

- [ ] **Step 5.4: Run tests**

- [ ] **Step 5.5: Commit**

---

## Task 6: Wire `ensure_user_ready` into `/oauth/poll` success

**Files:**
- Modify: `apps/backend/routers/oauth.py`
- Modify: `apps/backend/tests/unit/routers/test_oauth.py`

- [ ] **Step 6.1: Test that, when /oauth/poll returns active tokens AND provider_choice = chatgpt_oauth AND subscription is trialing/active, ensure_user_ready is called**

- [ ] **Step 6.2: Test that the call is async (background task) so /oauth/poll returns quickly**

- [ ] **Step 6.3: Implement**

- [ ] **Step 6.4: Commit**

---

## Task 7: Wire `ensure_user_ready` into BYO key save

**Files:**
- Modify: `apps/backend/routers/settings_keys.py`
- Modify: relevant tests

- [ ] **Step 7.1: Test that BYO key save fires ensure_user_ready when subscription + provider choice match**

- [ ] **Step 7.2: Implement**

- [ ] **Step 7.3: Commit**

---

## Task 8: Switch `organizationMembership.created` webhook to use `ensure_org_member_added_to_paperclip`

**Files:**
- Modify: `apps/backend/routers/webhooks.py`
- Modify: `apps/backend/tests/unit/routers/test_webhooks.py`

- [ ] **Step 8.1: Test that membership.created calls the new helper and does NOT trigger container provision**

- [ ] **Step 8.2: Implement: replace direct `provisioning.provision_member` call with `ensure_org_member_added_to_paperclip`**

- [ ] **Step 8.3: Commit**

---

## Task 9: Downgrade `organization.created` webhook to record-keeping only

**Files:**
- Modify: `apps/backend/routers/webhooks.py`
- Modify: tests

- [ ] **Step 9.1: Test that organization.created no longer triggers Paperclip provisioning**

- [ ] **Step 9.2: Implement: remove the `provisioning.provision_org` call. Optionally log/store an `orgs` row (if we have such a table) for audit purposes.**

- [ ] **Step 9.3: Commit**

The actual Paperclip company creation now waits for the readiness gate to pass (subscription + provider). For org owners, this typically lands within minutes of org creation since the next thing they do is finish onboarding.

---

## Task 10: Remove `/teams` autoprovision side-effect

**Files:**
- Modify: `apps/backend/routers/teams/agents.py` and `routers/teams/deps.py`
- Modify: `apps/backend/core/services/paperclip_autoprovision.py` — keep as an internal helper but not called from `/teams`
- Modify: tests

- [ ] **Step 10.1: Test that /teams endpoints no longer trigger Paperclip autoprovision when called by a user without a row**

- [ ] **Step 10.2: Test that /teams returns a clear "not yet provisioned" error rather than silently provisioning**

The frontend should handle this case by showing the same "Setting up your workspace…" state from onboarding (Task 13).

- [ ] **Step 10.3: Implement: remove `await ensure_paperclip_workspace(...)` calls from `_ctx`. The function `ensure_paperclip_workspace` itself stays (used by `_provision_paperclip_company`).**

- [ ] **Step 10.4: Commit**

---

## Task 11: Remove `POST /container/provision` from `/chat` flow (frontend + backend)

**Files:**
- Modify: `apps/backend/routers/container.py`
- Modify: `apps/frontend/src/hooks/useContainerStatus.ts`
- Modify: tests

- [ ] **Step 11.1: Test that the backend endpoint, when called by a non-admin, returns 403 (or removed entirely — see decision in Step 11.3)**

- [ ] **Step 11.2: Test that the frontend's container-status hook stops calling /container/provision**

- [ ] **Step 11.3: Decision point: keep `POST /container/provision` as an admin-only endpoint (for manual reprovision) OR remove entirely.**

Recommendation: keep but restrict to admin. The endpoint is useful for ops (force a reprovision after a config change). Add an `is_admin` check.

- [ ] **Step 11.4: Implement (backend + frontend)**

- [ ] **Step 11.5: Commit**

---

## Task 12: Update onboarding UX to show "Setting up your workspace…"

**Files:**
- Modify: `apps/frontend/src/app/onboarding/page.tsx` (or equivalent)
- Modify: `apps/frontend/src/components/chat/ProvisioningStepper.tsx`
- Modify: `apps/frontend/src/hooks/useContainerStatus.ts`

After provider setup completes (the readiness gate passes), the user sees a "Setting up your workspace…" state with a polling check on `/container/status`. When it returns `running`, redirect to `/chat`.

- [ ] **Step 12.1: Wireframe the new onboarding flow** (no test yet)

- [ ] **Step 12.2: Add Playwright E2E test that exercises the full flow**

- [ ] **Step 12.3: Implement frontend changes**

- [ ] **Step 12.4: Manual smoke test on dev**

- [ ] **Step 12.5: Commit**

---

## Task 13: Integration tests covering all four trigger points

**Files:**
- Create: `apps/backend/tests/integration/test_provisioning_state_machine.py`

- [ ] **Step 13.1: Test the Bedrock path: user signs up → trial-checkout (bedrock) → customer.subscription.created → ensure_user_ready fires → container + Paperclip provisioned**

- [ ] **Step 13.2: Test the OAuth path: subscription created (oauth) → /oauth/poll completes → ensure_user_ready fires**

- [ ] **Step 13.3: Test the BYO path: subscription created (byo_key) → /settings/llm POST → ensure_user_ready fires**

- [ ] **Step 13.4: Test the org-member path: organizationMembership.created → ensure_org_member_added_to_paperclip fires; no container provisioning**

- [ ] **Step 13.5: Test idempotency: re-firing each trigger is a no-op**

- [ ] **Step 13.6: Test failure recovery: if container provisioning fails partway, the next trigger fires successfully**

- [ ] **Step 13.7: Commit**

---

## Task 14: Documentation update

**Files:**
- Modify: `CLAUDE.md`
- Create: `docs/architecture/2026-05-07-provisioning-state-machine.md`

- [ ] **Step 14.1: Add a "Provisioning state machine" section to CLAUDE.md replacing the old "lazy on /chat" description**

- [ ] **Step 14.2: Write the architecture doc explaining the state machine, the four trigger points, the org-member side path, and the rollout strategy used for this deploy (wipe + re-onboard)**

- [ ] **Step 14.3: Commit**

---

## Task 15: Manual smoke test on dev

**Files:** None — operational verification

- [ ] **Step 15.1: Wipe dev (per CLAUDE.md "Clean-Slate Dev Reset")**

- [ ] **Step 15.2: Sign up as personal user, choose Bedrock, verify container is up before /chat is opened**

- [ ] **Step 15.3: Sign up as personal user, choose ChatGPT OAuth, complete the device-code flow, verify container is up**

- [ ] **Step 15.4: Sign up as personal user, choose BYO key (Anthropic), submit key, verify container is up**

- [ ] **Step 15.5: Sign up as org owner, repeat 15.2-15.4 in org context**

- [ ] **Step 15.6: Invite an org member, accept, verify member can use /chat without their own container**

- [ ] **Step 15.7: Cancel subscription, verify container teardown still works**

- [ ] **Step 15.8: Sign up, sign in, but never finish provider setup — verify provision gate blocks /chat with the right message**

- [ ] **Step 15.9: Force-trigger each readiness signal twice — verify ensure_user_ready is idempotent**

---

## Out-of-scope follow-ups

These are tracked here but not part of this plan:

1. **Token rotation in the migration script** (Codex P1 re-flag from PR #563). After this consolidation lands, the migration script becomes near-redundant — fresh provisioning produces correct tokens. The script can either be deleted or kept as a defensive tool for any future "we changed the token shape" event.
2. **Defensive `/teams` agent-create** (Codex P1 re-flag from PR #563). After consolidation, `/teams` create_agent's stored-token dependency is solved at the source — every stored token is correctly minted. The defensive "mint fresh per call" is still nice-to-have but not required.
3. **Sub-quarter-runtime cleanup of legacy `"main"` agent fallback in `_build_agents_list`.** Once every owner is guaranteed to have a Paperclip company (provision-time invariant after this plan), the empty-paperclip-agents fallback in `build_openclaw_config_dict` is dead code.
4. **Trial cancellation / failed-conversion handling.** Stripe handles trial → active conversion automatically; subscription.deleted handles teardown. But for trial-cancellation specifically (user cancels mid-trial), verify the container teardown happens cleanly.
5. **Org owner role detection.** The current model assumes the org-creator is the owner. If Clerk allows ownership transfer, we'd need to handle that. Out of scope.

---

## Self-review

- **Spec coverage:** All five state-machine nodes have explicit handling in tasks 1-9. Org-member side path is covered in tasks 4 and 8. UX is in task 12. Tests are in task 13.
- **No placeholders:** The plan uses concrete file paths and concrete code snippets where possible. Some tasks are necessarily exploratory (e.g., decision in Task 11.3) — those are explicit decision points the engineer makes during execution.
- **Type consistency:** `ensure_user_ready(*, owner_id, clerk_user_id, paperclip_repo, container_repo, ecs)` is the consistent signature throughout. `ensure_org_member_added_to_paperclip` has a different signature reflecting its different role.

---

## Execution decision

When you're ready to execute:

1. **Stack on top of PR #563 OR after merge?** Recommend stacking (a fresh worktree branch off `worktree-paperclip-openclaw-agent-pairing`) so this PR can include the Codex-flagged P2/P1 fixes via the consolidation rather than as separate band-aids.
2. **Subagent-driven or inline execution?** Subagent-driven is the default — fresh subagent per task with two-stage review. Same pattern as PR #563.

When the consolidation lands, the agent-pairing PR's Codex P2 race becomes structurally impossible (single trigger, dependency-ordered). The two re-flagged P1s become moot for new sign-ups.
