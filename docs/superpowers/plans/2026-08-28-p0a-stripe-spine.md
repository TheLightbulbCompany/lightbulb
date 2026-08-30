# P0a — Stripe Spine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every paid company one Standard-style Stripe account connected to the Connect platform we already run — created by us or connected by the owner — raised by Friday as an approval card, tracked on the billing row, kept current by Connect webhooks, and shown on Settings → Connections.

**Architecture:** A new `company_stripe` service wraps the Stripe calls (**Accounts v2**: `StripeClient.v2.core.accounts.create` with the `merchant` configuration, `dashboard: full`, Stripe-collected fees and losses; v2 account links; Standard OAuth stays v1 because Stripe lists OAuth as v1-only, and v2 endpoints manage those accounts too); `billing_repo` gains diff-written `company_stripe_*` fields plus a GSI to find an owner by account id; the existing `/webhooks/stripe-marketplace` receiver learns to route `account.updated` for company accounts and `account.application.deauthorized`; a `/api/v1/company/stripe/*` router exposes status/create/link/connect/disconnect behind the paid + org-admin gates and a hook-token route lets Friday file the approval; the frontend adds a section on `/settings/connections` and a "Set up Stripe" action on the `connect_stripe` approval row.

**Tech Stack:** FastAPI, `stripe` Python SDK (global `stripe.api_key`), DynamoDB via boto3 + moto for tests, pytest + `AsyncMock`, Next.js/React + SWR, AWS CDK (TypeScript), Paperclip admin client for approvals.

**Spec:** `docs/superpowers/specs/2026-08-28-owner-data-plane-design.md` §3.1, §3.6, §4.5

## Global Constraints

- Zero Connect fees: accounts are created on **Accounts v2** with `defaults.responsibilities = {fees_collector: "stripe", losses_collector: "stripe"}`, `dashboard: "full"`, `configuration.merchant.capabilities.card_payments.requested = true`; no `application_fee` anywhere. Verified 2026-08-28: Accounts v2 is GA for all Stripe users (dahlia changelog 2026-04-22), our dev platform already serves `GET /v2/core/accounts`, and `stripe` 14.4.1 exposes `StripeClient(...).v2.core.accounts` / `.account_links`. Pin `stripe>=14.4.1` in `apps/backend/pyproject.toml` (today `>=8.0.0`).
- Standard OAuth ("connect existing") and `OAuth.deauthorize` stay on v1 — Stripe's v2 limitations list names OAuth as v1-only; v2 endpoints still read/manage those accounts (a fresh v1 account may take up to 10 minutes before v2 endpoints see it).
- Status booleans are read through the v1 shape (`stripe.Account.retrieve(acct)` → `details_submitted` / `charges_enabled` / `payouts_enabled`) for both doors — v1 endpoints accept v2 ids and return the v1 structure — so the webhook, repo and pages have one status vocabulary. v1 `account.updated` still fires for v2 accounts (Connected-accounts scope), so the existing endpoint keeps working; a v2 thin-event destination is optional and not part of P0a.
- The accounts layer is paid-only: every mutating owner route calls `require_always_on_entitlement(owner_id)` (`routers/teams/_guards.py:20`), which raises 402 with `code: routines_require_subscription`.
- Money is org-admin-only: in an org context every mutating route calls `require_org_admin(auth)` (`core/auth.py:118`).
- Owner id is always `resolve_owner_id(auth)` (`core/auth.py:97`) — `AuthContext` has no `owner_id` field.
- Account Links are single-use and short-lived: mint on request, return once, never store, never email.
- Billing-row writes go through `_write_changed_fields` (diff-only, `billing_repo.py:265`); never `put_item` a whole row.
- The Stripe SDK is used as in `payout_service.py`: set `stripe.api_key = settings.STRIPE_SECRET_KEY` immediately before each call; blocking calls run via `asyncio.to_thread`.
- Wipe never deletes the owner's Stripe account; it only deauthorizes the platform link.
- Any change under `apps/infra/lib/` must run `cd apps/infra && npx cdk synth` and commit the regenerated `.github/workflows/deploy.yml` in the same PR.
- Branch: work on `docs/owner-data-plane-design` (worktree `.worktrees/owner-data-plane-design`) or a feature branch off it; never `main`. Commit after every task; push only when the owner says so.
- Tests: run targeted files with `cd apps/backend && uv run pytest <file> -v` and `ruff check` locally; CI runs the full suite.

---

### Task 1: Config, CDK env vars, and the account-id GSI

**Files:**
- Modify: `apps/backend/core/config.py:393-413`
- Modify: `apps/infra/lib/stacks/service-stack.ts:1549-1559`
- Modify: `apps/infra/lib/stacks/database-stack.ts:113-137` (billing-accounts table)
- Test: `apps/infra/test/database-stack.test.ts`
- Regenerate: `.github/workflows/deploy.yml`

**Interfaces:**
- Produces: `settings.STRIPE_COMPANY_REFRESH_URL: str`, `settings.STRIPE_COMPANY_RETURN_URL: str`, `settings.STRIPE_COMPANY_OAUTH_REDIRECT_URL: str`, `settings.STRIPE_CONNECT_CLIENT_ID: str`; DynamoDB GSI `company-stripe-account-index` (hash `company_stripe_account_id`) on `isol8-{env}-billing-accounts`.

- [ ] **Step 1: Add the settings**

In `apps/backend/core/config.py`, directly after `STRIPE_CONNECT_RETURN_URL: str = ""` (line 397) add:

```python
    # Company Stripe account (the owner's own business account on our Connect
    # platform). Hosted onboarding bounces back to these; the Standard-OAuth
    # "connect existing" flow returns to the backend callback, which must be
    # registered verbatim as a redirect URI in Stripe → Connect settings.
    STRIPE_COMPANY_REFRESH_URL: str = ""
    STRIPE_COMPANY_RETURN_URL: str = ""
    STRIPE_COMPANY_OAUTH_REDIRECT_URL: str = ""
    # Connect platform client id (ca_…). Public, not a secret.
    STRIPE_CONNECT_CLIENT_ID: str = ""
```

- [ ] **Step 1b: Pin the SDK**

In `apps/backend/pyproject.toml` change `"stripe>=8.0.0"` to `"stripe>=14.4.1"` (the installed version; first to ship `StripeClient.v2.core.accounts` + `account_links`), then run `cd apps/backend && uv lock && uv sync` and confirm with `uv run python -c "from stripe import StripeClient; print(StripeClient('sk_test_x').v2.core.accounts)"`.

- [ ] **Step 2: Add the CDK env vars**

In `apps/infra/lib/stacks/service-stack.ts`, directly after the `STRIPE_CONNECT_RETURN_URL` entry (line 1559) add:

```ts
      // Company Stripe account — owner's own business account on the same
      // Connect platform. Return/refresh land on Settings → Connections; the
      // OAuth redirect is the backend callback on the API host.
      STRIPE_COMPANY_REFRESH_URL:
        env === "prod"
          ? `https://${rootDomain}/settings/connections?stripe=refresh`
          : `https://dev.${rootDomain}/settings/connections?stripe=refresh`,
      STRIPE_COMPANY_RETURN_URL:
        env === "prod"
          ? `https://${rootDomain}/settings/connections?stripe=return`
          : `https://dev.${rootDomain}/settings/connections?stripe=return`,
      STRIPE_COMPANY_OAUTH_REDIRECT_URL:
        env === "prod"
          ? `https://api.${rootDomain}/api/v1/company/stripe/connect/callback`
          : `https://api-dev.${rootDomain}/api/v1/company/stripe/connect/callback`,
      STRIPE_CONNECT_CLIENT_ID: props.stripeConnectClientId ?? "",
```

Add `stripeConnectClientId?: string` to the service stack props interface (search `interface ServiceStackProps` in the same file) and thread it from `apps/infra/lib/isol8-stage.ts` with the dev/prod `ca_…` ids as plain string constants next to the existing Stripe price ids.

- [ ] **Step 3: Add the GSI**

In `apps/infra/lib/stacks/database-stack.ts`, on the billing-accounts table (the one that already declares `stripe-customer-index` and `apple-original-transaction-index`), add:

```ts
    billingAccountsTable.addGlobalSecondaryIndex({
      indexName: "company-stripe-account-index",
      partitionKey: { name: "company_stripe_account_id", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
```

- [ ] **Step 4: Write the failing infra test**

In `apps/infra/test/database-stack.test.ts`, inside the existing `describe` that builds the DatabaseStack `template`, add:

```ts
  test("billing-accounts has the company-stripe-account-index GSI", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", Match.objectLike({
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({ IndexName: "company-stripe-account-index" }),
      ]),
    }));
  });
```

- [ ] **Step 5: Run the infra test to verify it fails, then passes**

Run: `cd apps/infra && npx jest test/database-stack.test.ts -t "company-stripe-account-index"`
Expected: FAIL before Step 3 is applied; PASS after.

- [ ] **Step 6: Synth and commit the regenerated workflow**

Run: `cd apps/infra && npx cdk synth > /dev/null && git status --short .github/workflows/deploy.yml`
Expected: `deploy.yml` unchanged or modified only by the new env vars.

- [ ] **Step 7: Commit**

```bash
git add apps/backend/core/config.py apps/infra/lib/stacks/service-stack.ts apps/infra/lib/stacks/database-stack.ts apps/infra/lib/isol8-stage.ts apps/infra/test/database-stack.test.ts .github/workflows/deploy.yml
git commit -m "feat(company-stripe): settings, CDK env vars, and billing GSI for the company Stripe account"
```

---

### Task 2: billing_repo company-Stripe setters

**Files:**
- Modify: `apps/backend/core/repositories/billing_repo.py` (append after `set_apple_subscription`, ~line 470)
- Test: `apps/backend/tests/unit/repositories/test_billing_repo_company_stripe.py`

**Interfaces:**
- Produces:
  - `async def set_company_stripe_account(owner_id: str, *, account_id: str, source: str) -> dict | None` — binds once; `None` if no row or already bound.
  - `async def set_company_stripe_status(owner_id: str, *, account_id: str, details_submitted: bool, charges_enabled: bool, payouts_enabled: bool) -> dict | None` — `None` if the row is bound to a different account.
  - `async def clear_company_stripe_account(owner_id: str, *, account_id: str) -> dict | None`.
  - `async def get_by_company_stripe_account_id(account_id: str) -> dict | None` (GSI query).
- Row fields: `company_stripe_account_id`, `company_stripe_source` (`"created" | "connected"`), `company_stripe_details_submitted`, `company_stripe_charges_enabled`, `company_stripe_payouts_enabled`, `company_stripe_connected_at`, `company_stripe_updated_at`.

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/unit/repositories/test_billing_repo_company_stripe.py
import os
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


@pytest.fixture
def billing_table():
    with mock_aws():
        client = boto3.resource("dynamodb", region_name="us-east-1")
        table = client.create_table(
            TableName="test-billing-accounts",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "owner_id", "AttributeType": "S"},
                {"AttributeName": "stripe_customer_id", "AttributeType": "S"},
                {"AttributeName": "company_stripe_account_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "stripe-customer-index",
                    "KeySchema": [{"AttributeName": "stripe_customer_id", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "company-stripe-account-index",
                    "KeySchema": [{"AttributeName": "company_stripe_account_id", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        with patch("core.dynamodb._table_prefix", "test-"), patch("core.dynamodb._dynamodb_resource", client):
            table.put_item(Item={"owner_id": "org_a", "owner_type": "org", "stripe_customer_id": "cus_a", "plan": "flat"})
            yield table


@pytest.mark.asyncio
async def test_set_company_stripe_account_binds_once(billing_table):
    from core.repositories import billing_repo

    row = await billing_repo.set_company_stripe_account("org_a", account_id="acct_1", source="created")
    assert row["company_stripe_account_id"] == "acct_1"
    assert row["company_stripe_source"] == "created"
    assert row["plan"] == "flat"  # untouched field survives the diff write

    again = await billing_repo.set_company_stripe_account("org_a", account_id="acct_2", source="connected")
    assert again is None
    assert billing_table.get_item(Key={"owner_id": "org_a"})["Item"]["company_stripe_account_id"] == "acct_1"


@pytest.mark.asyncio
async def test_set_company_stripe_account_no_row_is_none(billing_table):
    from core.repositories import billing_repo

    assert await billing_repo.set_company_stripe_account("org_missing", account_id="acct_1", source="created") is None


@pytest.mark.asyncio
async def test_status_update_requires_matching_account(billing_table):
    from core.repositories import billing_repo

    await billing_repo.set_company_stripe_account("org_a", account_id="acct_1", source="created")
    row = await billing_repo.set_company_stripe_status(
        "org_a", account_id="acct_1", details_submitted=True, charges_enabled=True, payouts_enabled=False
    )
    assert row["company_stripe_charges_enabled"] is True
    assert row["company_stripe_payouts_enabled"] is False

    stale = await billing_repo.set_company_stripe_status(
        "org_a", account_id="acct_other", details_submitted=True, charges_enabled=True, payouts_enabled=True
    )
    assert stale is None


@pytest.mark.asyncio
async def test_get_by_company_stripe_account_id(billing_table):
    from core.repositories import billing_repo

    await billing_repo.set_company_stripe_account("org_a", account_id="acct_1", source="connected")
    found = await billing_repo.get_by_company_stripe_account_id("acct_1")
    assert found["owner_id"] == "org_a"
    assert await billing_repo.get_by_company_stripe_account_id("acct_nope") is None


@pytest.mark.asyncio
async def test_clear_company_stripe_account(billing_table):
    from core.repositories import billing_repo

    await billing_repo.set_company_stripe_account("org_a", account_id="acct_1", source="created")
    assert await billing_repo.clear_company_stripe_account("org_a", account_id="acct_zzz") is None
    row = await billing_repo.clear_company_stripe_account("org_a", account_id="acct_1")
    assert "company_stripe_account_id" not in row
    assert "company_stripe_source" not in row
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/repositories/test_billing_repo_company_stripe.py -v`
Expected: FAIL with `AttributeError: module 'core.repositories.billing_repo' has no attribute 'set_company_stripe_account'`.

- [ ] **Step 3: Implement**

Append to `apps/backend/core/repositories/billing_repo.py` (after `_try_set_apple_subscription`). `ClientError` is `from botocore.exceptions import ClientError` — add the import at the top if the module doesn't already have it.

```python
# ---------------------------------------------------------------------------
# Company Stripe account — the owner's own business account on our Connect
# platform. One binding per owner; status mirrors Connect `account.updated`.
# Every write is a diff (see _write_changed_fields) so billing/Apple/provider
# fields can never be rolled back by these setters.
# ---------------------------------------------------------------------------

_COMPANY_STRIPE_FIELDS = (
    "company_stripe_account_id",
    "company_stripe_source",
    "company_stripe_details_submitted",
    "company_stripe_charges_enabled",
    "company_stripe_payouts_enabled",
    "company_stripe_connected_at",
    "company_stripe_updated_at",
)


def _is_condition_failure(e: ClientError) -> bool:
    return e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


async def _write_company_stripe(owner_id: str, mutate, *, condition: str, values: dict) -> dict | None:
    existing = await get_by_owner_id(owner_id)
    if existing is None:
        return None
    before = dict(existing)
    after = dict(existing)
    mutate(after)
    after["company_stripe_updated_at"] = utc_now_iso()
    try:
        await _write_changed_fields(owner_id, before, after, condition=condition, values=values)
    except ClientError as e:
        if _is_condition_failure(e):
            return None
        raise
    return after


async def set_company_stripe_account(owner_id: str, *, account_id: str, source: str) -> dict | None:
    """Bind the company account exactly once. None = no row, or already bound."""
    if source not in ("created", "connected"):
        raise ValueError("source must be 'created' or 'connected'")
    if not account_id:
        raise ValueError("account_id must be non-empty (it keys a GSI)")

    def mutate(row: dict) -> None:
        row["company_stripe_account_id"] = account_id
        row["company_stripe_source"] = source
        row["company_stripe_details_submitted"] = False
        row["company_stripe_charges_enabled"] = False
        row["company_stripe_payouts_enabled"] = False
        row["company_stripe_connected_at"] = utc_now_iso()

    return await _write_company_stripe(
        owner_id,
        mutate,
        condition="attribute_exists(owner_id) AND attribute_not_exists(company_stripe_account_id)",
        values={},
    )


async def set_company_stripe_status(
    owner_id: str,
    *,
    account_id: str,
    details_submitted: bool,
    charges_enabled: bool,
    payouts_enabled: bool,
) -> dict | None:
    """Mirror Connect's account.updated. None = the row is bound to a different account."""

    def mutate(row: dict) -> None:
        row["company_stripe_details_submitted"] = bool(details_submitted)
        row["company_stripe_charges_enabled"] = bool(charges_enabled)
        row["company_stripe_payouts_enabled"] = bool(payouts_enabled)

    return await _write_company_stripe(
        owner_id,
        mutate,
        condition="company_stripe_account_id = :acct",
        values={":acct": account_id},
    )


async def clear_company_stripe_account(owner_id: str, *, account_id: str) -> dict | None:
    """Unbind after deauthorization. Conditioned on the id so a stale event can't clear a rebound row."""

    def mutate(row: dict) -> None:
        for key in _COMPANY_STRIPE_FIELDS:
            row.pop(key, None)

    return await _write_company_stripe(
        owner_id,
        mutate,
        condition="company_stripe_account_id = :acct",
        values={":acct": account_id},
    )


async def get_by_company_stripe_account_id(account_id: str) -> dict | None:
    if not account_id:
        return None
    resp = await run_in_thread(
        _get_table().query,
        IndexName="company-stripe-account-index",
        KeyConditionExpression="company_stripe_account_id = :acct",
        ExpressionAttributeValues={":acct": account_id},
        Limit=1,
    )
    items = resp.get("Items") or []
    return items[0] if items else None
```

Note: `_write_company_stripe` sets `company_stripe_updated_at` on the copy *before* `clear_…`'s pop runs? No — `mutate` runs first, then `updated_at` is set, so `clear_` leaves exactly one company field (`company_stripe_updated_at`). That is intended: it records when the unbind happened. The test asserts only the id and source are gone.

- [ ] **Step 4: Run to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/repositories/test_billing_repo_company_stripe.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/repositories/billing_repo.py apps/backend/tests/unit/repositories/test_billing_repo_company_stripe.py
git commit -m "feat(company-stripe): billing_repo setters + account-id lookup for the company Stripe account"
```

---

### Task 3: `company_stripe` service

**Files:**
- Create: `apps/backend/core/services/company_stripe.py`
- Test: `apps/backend/tests/unit/services/test_company_stripe.py`

**Interfaces:**
- Produces:
  - `async def create_company_account(*, owner_id: str, email: str, company_name: str) -> str` (returns `acct_…`)
  - `async def create_company_onboarding_link(*, account_id: str) -> str`
  - `def build_connect_authorize_url(*, state: str) -> str`
  - `async def exchange_connect_code(*, code: str) -> str` (returns `acct_…`)
  - `async def deauthorize(*, account_id: str) -> bool`
  - `async def retrieve_status(*, account_id: str) -> dict` (`{details_submitted, charges_enabled, payouts_enabled}`)

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/unit/services/test_company_stripe.py
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.config import settings  # noqa: E402
from core.services import company_stripe  # noqa: E402


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setattr(settings, "STRIPE_CONNECT_CLIENT_ID", "ca_test")
    monkeypatch.setattr(settings, "STRIPE_COMPANY_REFRESH_URL", "https://dev.example/settings/connections?stripe=refresh")
    monkeypatch.setattr(settings, "STRIPE_COMPANY_RETURN_URL", "https://dev.example/settings/connections?stripe=return")
    monkeypatch.setattr(settings, "STRIPE_COMPANY_OAUTH_REDIRECT_URL", "https://api-dev.example/api/v1/company/stripe/connect/callback")


def _client_with(accounts_create=None, links_create=None):
    """A stand-in for StripeClient exposing only v2.core.accounts / account_links."""
    accounts = SimpleNamespace(create=accounts_create or MagicMock())
    links = SimpleNamespace(create=links_create or MagicMock())
    return SimpleNamespace(v2=SimpleNamespace(core=SimpleNamespace(accounts=accounts, account_links=links)))


@pytest.mark.asyncio
async def test_create_company_account_uses_v2_merchant_config_and_stripe_fees():
    create = MagicMock(return_value=SimpleNamespace(id="acct_new"))
    with patch("core.services.company_stripe._client", return_value=_client_with(accounts_create=create)):
        account_id = await company_stripe.create_company_account(
            owner_id="org_a", email="founder@example.com", company_name="Northwind Ceramics"
        )
    assert account_id == "acct_new"
    params, options = create.call_args.args[0], create.call_args.kwargs["options"]
    assert params["dashboard"] == "full"
    assert params["contact_email"] == "founder@example.com"
    assert params["display_name"] == "Northwind Ceramics"
    assert params["identity"] == {"country": "us"}
    assert params["configuration"] == {"merchant": {"capabilities": {"card_payments": {"requested": True}}}}
    assert params["defaults"] == {
        "currency": "usd",
        "responsibilities": {"fees_collector": "stripe", "losses_collector": "stripe"},
    }
    assert params["metadata"] == {"owner_id": "org_a", "kind": "company"}
    assert options["idempotency_key"].startswith("company_account_create:")


@pytest.mark.asyncio
async def test_create_company_onboarding_link_v2():
    link = MagicMock(return_value=SimpleNamespace(url="https://accounts.stripe.com/r/acct_new#alu_x"))
    with patch("core.services.company_stripe._client", return_value=_client_with(links_create=link)):
        url = await company_stripe.create_company_onboarding_link(account_id="acct_new")
    assert url == "https://accounts.stripe.com/r/acct_new#alu_x"
    params = link.call_args.args[0]
    assert params["account"] == "acct_new"
    assert params["use_case"] == {
        "type": "account_onboarding",
        "account_onboarding": {
            "configurations": ["merchant"],
            "refresh_url": settings.STRIPE_COMPANY_REFRESH_URL,
            "return_url": settings.STRIPE_COMPANY_RETURN_URL,
        },
    }


def test_build_connect_authorize_url():
    url = company_stripe.build_connect_authorize_url(state="st_123")
    assert url.startswith("https://connect.stripe.com/oauth/authorize?")
    assert "client_id=ca_test" in url
    assert "scope=read_write" in url
    assert "state=st_123" in url
    assert "redirect_uri=https%3A%2F%2Fapi-dev.example%2Fapi%2Fv1%2Fcompany%2Fstripe%2Fconnect%2Fcallback" in url


@pytest.mark.asyncio
async def test_exchange_connect_code_returns_account_id():
    with patch("core.services.company_stripe.stripe.OAuth.token") as token:
        token.return_value = {"stripe_user_id": "acct_existing"}
        assert await company_stripe.exchange_connect_code(code="ac_1") == "acct_existing"
    assert token.call_args.kwargs == {"grant_type": "authorization_code", "code": "ac_1"}


@pytest.mark.asyncio
async def test_deauthorize_calls_oauth_with_client_id():
    with patch("core.services.company_stripe.stripe.OAuth.deauthorize") as deauth:
        deauth.return_value = {"stripe_user_id": "acct_existing"}
        assert await company_stripe.deauthorize(account_id="acct_existing") is True
    assert deauth.call_args.kwargs == {"client_id": "ca_test", "stripe_user_id": "acct_existing"}


@pytest.mark.asyncio
async def test_retrieve_status_shape():
    with patch("core.services.company_stripe.stripe.Account.retrieve") as retrieve:
        retrieve.return_value = {"details_submitted": True, "charges_enabled": True, "payouts_enabled": False}
        status = await company_stripe.retrieve_status(account_id="acct_x")
    assert status == {"details_submitted": True, "charges_enabled": True, "payouts_enabled": False}
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_company_stripe.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.services.company_stripe'`.

- [ ] **Step 3: Implement**

```python
# apps/backend/core/services/company_stripe.py
"""The company's own Stripe account on our Connect platform (Accounts v2).

A ``merchant``-configured v2 account with the full Dashboard; Stripe collects
the account's fees and carries its losses, so the platform pays nothing and
takes no application fee. The owner keeps the account and logs into
dashboard.stripe.com. Two doors — create via ``v2.core.accounts`` + hosted
onboarding, or connect an existing account via Standard OAuth (v1-only by
Stripe's own limitations list) — both land the same ``acct_…`` on the
billing row, and v2 endpoints manage either kind.

Status booleans are read through the v1 shape (``details_submitted`` /
``charges_enabled`` / ``payouts_enabled``) for both doors — v1 endpoints
accept v2 ids — so the webhook, repo and pages share one vocabulary.

Blocking SDK calls run on a worker thread, as in ``payout_service``.
"""

from __future__ import annotations

import asyncio
import uuid
from urllib.parse import urlencode

import stripe
from stripe import StripeClient

from core.config import settings

CONNECT_AUTHORIZE_URL = "https://connect.stripe.com/oauth/authorize"

# v2 defaults: Stripe collects fees from the account and carries its losses
# (the platform pays nothing); full Dashboard; merchant config = accept
# card payments and receive payouts to its own bank.
ACCOUNT_DEFAULTS = {
    "currency": "usd",
    "responsibilities": {"fees_collector": "stripe", "losses_collector": "stripe"},
}
MERCHANT_CONFIGURATION = {"merchant": {"capabilities": {"card_payments": {"requested": True}}}}


def _client() -> StripeClient:
    return StripeClient(settings.STRIPE_SECRET_KEY)


def _set_key() -> None:
    stripe.api_key = settings.STRIPE_SECRET_KEY


async def create_company_account(*, owner_id: str, email: str, company_name: str) -> str:
    """Create the account shell. ``identity`` carries only the country —
    entity type and everything else are collected by Stripe in hosted
    onboarding (``requirements_collector`` defaults to ``stripe``). Fresh
    uuid4 idempotency key on purpose — a stable key would return a deleted
    account from Stripe's 24h cache; the router de-dups on the billing row."""
    client = _client()
    account = await asyncio.to_thread(
        client.v2.core.accounts.create,
        {
            "contact_email": email,
            "display_name": company_name,
            "dashboard": "full",
            "identity": {"country": "us"},
            "configuration": MERCHANT_CONFIGURATION,
            "defaults": ACCOUNT_DEFAULTS,
            "metadata": {"owner_id": owner_id, "kind": "company"},
        },
        options={"idempotency_key": f"company_account_create:{uuid.uuid4()}"},
    )
    return account.id


async def create_company_onboarding_link(*, account_id: str) -> str:
    """Single-use, ~10-minute hosted onboarding link (v2). Mint per request."""
    client = _client()
    link = await asyncio.to_thread(
        client.v2.core.account_links.create,
        {
            "account": account_id,
            "use_case": {
                "type": "account_onboarding",
                "account_onboarding": {
                    "configurations": ["merchant"],
                    "refresh_url": settings.STRIPE_COMPANY_REFRESH_URL,
                    "return_url": settings.STRIPE_COMPANY_RETURN_URL,
                },
            },
        },
    )
    return link.url


def build_connect_authorize_url(*, state: str) -> str:
    """Standard OAuth for an owner who already runs Stripe."""
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.STRIPE_CONNECT_CLIENT_ID,
            "scope": "read_write",
            "state": state,
            "redirect_uri": settings.STRIPE_COMPANY_OAUTH_REDIRECT_URL,
        }
    )
    return f"{CONNECT_AUTHORIZE_URL}?{query}"


async def exchange_connect_code(*, code: str) -> str:
    _set_key()
    resp = await asyncio.to_thread(stripe.OAuth.token, grant_type="authorization_code", code=code)
    return resp["stripe_user_id"]


async def deauthorize(*, account_id: str) -> bool:
    """Revoke the platform's access. The account itself keeps working as a
    normal Stripe account — this is the owner's exit, never a delete."""
    _set_key()
    await asyncio.to_thread(
        stripe.OAuth.deauthorize,
        client_id=settings.STRIPE_CONNECT_CLIENT_ID,
        stripe_user_id=account_id,
    )
    return True


async def retrieve_status(*, account_id: str) -> dict:
    _set_key()
    account = await asyncio.to_thread(stripe.Account.retrieve, account_id)
    return {
        "details_submitted": bool(account.get("details_submitted")),
        "charges_enabled": bool(account.get("charges_enabled")),
        "payouts_enabled": bool(account.get("payouts_enabled")),
    }
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_company_stripe.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/company_stripe.py apps/backend/tests/unit/services/test_company_stripe.py
git commit -m "feat(company-stripe): service for create / onboarding link / Standard OAuth / deauthorize"
```

---

### Task 4: Webhook routing for company accounts

**Files:**
- Modify: `apps/backend/routers/marketplace_purchases.py:171-190` (dispatch) and `:395-419` (`_handle_account_updated`)
- Test: `apps/backend/tests/unit/routers/test_marketplace_purchases_company_account.py`

**Interfaces:**
- Consumes: `billing_repo.get_by_company_stripe_account_id`, `billing_repo.set_company_stripe_status`, `billing_repo.clear_company_stripe_account` (Task 2).
- Produces: `async def _handle_account_deauthorized(account_id: str | None) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/unit/routers/test_marketplace_purchases_company_account.py
import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from routers import marketplace_purchases as mod  # noqa: E402


@pytest.mark.asyncio
async def test_account_updated_for_company_account_writes_status_not_seller_flush():
    account = {
        "id": "acct_co",
        "details_submitted": True,
        "charges_enabled": True,
        "payouts_enabled": False,
        "metadata": {"owner_id": "org_a", "kind": "company"},
    }
    with (
        patch.object(mod.billing_repo, "get_by_company_stripe_account_id", AsyncMock(return_value={"owner_id": "org_a"})),
        patch.object(mod.billing_repo, "set_company_stripe_status", AsyncMock(return_value={})) as set_status,
        patch.object(mod, "_flush_seller_held_balance", AsyncMock()) as flush,
    ):
        await mod._handle_account_updated(account)
    set_status.assert_awaited_once_with(
        "org_a", account_id="acct_co", details_submitted=True, charges_enabled=True, payouts_enabled=False
    )
    flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_account_updated_for_seller_account_keeps_seller_path():
    account = {"id": "acct_seller", "payouts_enabled": True, "metadata": {"seller_id": "user_s"}}
    with (
        patch.object(mod.billing_repo, "get_by_company_stripe_account_id", AsyncMock(return_value=None)),
        patch.object(mod.marketplace_repo, "get_payout_account", AsyncMock(return_value={"stripe_connect_account_id": "acct_seller"})),
        patch.object(mod, "_flush_seller_held_balance", AsyncMock()) as flush,
    ):
        await mod._handle_account_updated(account)
    flush.assert_awaited_once_with(seller_id="user_s", connect_account_id="acct_seller")


@pytest.mark.asyncio
async def test_account_deauthorized_clears_binding():
    with (
        patch.object(mod.billing_repo, "get_by_company_stripe_account_id", AsyncMock(return_value={"owner_id": "org_a"})),
        patch.object(mod.billing_repo, "clear_company_stripe_account", AsyncMock(return_value={})) as clear,
    ):
        await mod._handle_account_deauthorized("acct_co")
    clear.assert_awaited_once_with("org_a", account_id="acct_co")


@pytest.mark.asyncio
async def test_account_deauthorized_unknown_account_is_noop():
    with (
        patch.object(mod.billing_repo, "get_by_company_stripe_account_id", AsyncMock(return_value=None)),
        patch.object(mod.billing_repo, "clear_company_stripe_account", AsyncMock()) as clear,
    ):
        await mod._handle_account_deauthorized("acct_nobody")
        await mod._handle_account_deauthorized(None)
    clear.assert_not_awaited()
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_marketplace_purchases_company_account.py -v`
Expected: first test FAILS on `set_status.assert_awaited_once_with` (the current handler returns early on `payouts_enabled=False`); the deauthorized tests FAIL with `AttributeError: module has no attribute '_handle_account_deauthorized'`.

- [ ] **Step 3: Implement**

In `apps/backend/routers/marketplace_purchases.py`:

Add `from core.repositories import billing_repo` next to the existing `marketplace_repo` import.

Replace the body of `_handle_account_updated` with (keep the existing docstring):

```python
    # Company accounts (the owner's own business account, created by us or
    # connected via Standard OAuth) mirror their state onto the billing row
    # and never touch the seller flush. Looked up by account id, not by
    # metadata: an OAuth-connected account carries no metadata of ours.
    account_id = account.get("id") or ""
    company_row = await billing_repo.get_by_company_stripe_account_id(account_id)
    if company_row is not None:
        await billing_repo.set_company_stripe_status(
            company_row["owner_id"],
            account_id=account_id,
            details_submitted=bool(account.get("details_submitted")),
            charges_enabled=bool(account.get("charges_enabled")),
            payouts_enabled=bool(account.get("payouts_enabled")),
        )
        return

    if not account.get("payouts_enabled"):
        return
    seller_id = (account.get("metadata") or {}).get("seller_id")
    if not seller_id:
        return
    pa = await marketplace_repo.get_payout_account(seller_id) or {}
    connect_account_id = pa.get("stripe_connect_account_id") or account.get("id")
    if not connect_account_id:
        return
    await _flush_seller_held_balance(seller_id=seller_id, connect_account_id=connect_account_id)
```

Add the new handler after it:

```python
async def _handle_account_deauthorized(account_id: str | None) -> None:
    """The owner revoked the platform from their Stripe Dashboard (or we did
    via /company/stripe/disconnect). Unbind; never delete anything."""
    if not account_id:
        return
    row = await billing_repo.get_by_company_stripe_account_id(account_id)
    if row is None:
        return
    await billing_repo.clear_company_stripe_account(row["owner_id"], account_id=account_id)
```

In the dispatcher, after the `account.updated` branch add:

```python
        elif event_type == "account.application.deauthorized":
            # The connected account id rides on the event envelope, not the
            # data object (which is the Application).
            await _handle_account_deauthorized(event.get("account"))
```

Register `account.application.deauthorized` on the Connect-scoped webhook endpoint in the Stripe Dashboard for dev and prod (owner action; note it in the PR description).

- [ ] **Step 4: Run to verify they pass, plus the existing split test**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_marketplace_purchases_company_account.py tests/unit/routers/test_marketplace_purchases_webhook_split.py -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/routers/marketplace_purchases.py apps/backend/tests/unit/routers/test_marketplace_purchases_company_account.py
git commit -m "feat(company-stripe): route account.updated / deauthorized for company accounts"
```

---

### Task 5: `/api/v1/company/stripe` router

**Files:**
- Create: `apps/backend/routers/company_stripe.py`
- Modify: `apps/backend/main.py` (after line 951, the `marketplace_payouts` include)
- Test: `apps/backend/tests/unit/routers/test_company_stripe.py`

**Interfaces:**
- Consumes: Task 2 repo functions, Task 3 service functions, `require_always_on_entitlement` (`routers/teams/_guards.py`), `require_org_admin` / `resolve_owner_id` (`core/auth.py`), `oauth_state_repo.put/consume`.
- Produces routes (all under `/api/v1/company/stripe`):
  - `GET /status` → `{"account_id": str|None, "source": str|None, "details_submitted": bool, "charges_enabled": bool, "payouts_enabled": bool, "dashboard_url": "https://dashboard.stripe.com/" | None}`
  - `POST /create` → `{"account_id", "onboarding_url"}` (creates if absent; 409 if bound via `connected`)
  - `POST /onboarding-link` → `{"onboarding_url"}` (409 if none/connected)
  - `GET /connect/start` → `{"authorize_url"}`
  - `GET /connect/callback?code&state` → 302 to `STRIPE_COMPANY_RETURN_URL` with `&stripe=connected` or `&stripe_error=…`
  - `POST /disconnect` → `{"disconnected": true}`

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/unit/routers/test_company_stripe.py
import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.auth import AuthContext, get_current_user  # noqa: E402
from main import app  # noqa: E402
from routers import company_stripe as mod  # noqa: E402


@pytest.fixture
def client():
    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="user_admin", org_id="org_a", org_role="org:admin", email="founder@example.com"
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def member_client():
    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="user_member", org_id="org_a", org_role="org:member", email="m@example.com"
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _paid(monkeypatch):
    monkeypatch.setattr(mod, "require_always_on_entitlement", AsyncMock())


def test_status_unbound(client):
    with patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})):
        resp = client.get("/api/v1/company/stripe/status")
    assert resp.status_code == 200
    assert resp.json() == {
        "account_id": None,
        "source": None,
        "details_submitted": False,
        "charges_enabled": False,
        "payouts_enabled": False,
        "dashboard_url": None,
    }


def test_create_binds_and_returns_link(client):
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})),
        patch.object(mod, "resolve_company_name", AsyncMock(return_value="Northwind Ceramics")),
        patch.object(mod.company_stripe, "create_company_account", AsyncMock(return_value="acct_new")) as create,
        patch.object(mod.billing_repo, "set_company_stripe_account", AsyncMock(return_value={"company_stripe_account_id": "acct_new"})) as bind,
        patch.object(mod.company_stripe, "create_company_onboarding_link", AsyncMock(return_value="https://connect.stripe.com/setup/x")),
    ):
        resp = client.post("/api/v1/company/stripe/create")
    assert resp.status_code == 200
    assert resp.json() == {"account_id": "acct_new", "onboarding_url": "https://connect.stripe.com/setup/x"}
    create.assert_awaited_once_with(owner_id="org_a", email="founder@example.com", company_name="Northwind Ceramics")
    bind.assert_awaited_once_with("org_a", account_id="acct_new", source="created")


def test_create_reuses_existing_created_account(client):
    row = {"owner_id": "org_a", "company_stripe_account_id": "acct_old", "company_stripe_source": "created"}
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value=row)),
        patch.object(mod.company_stripe, "create_company_account", AsyncMock()) as create,
        patch.object(mod.company_stripe, "create_company_onboarding_link", AsyncMock(return_value="https://x")),
    ):
        resp = client.post("/api/v1/company/stripe/create")
    assert resp.status_code == 200
    assert resp.json()["account_id"] == "acct_old"
    create.assert_not_awaited()


def test_create_refuses_when_connected_account_bound(client):
    row = {"owner_id": "org_a", "company_stripe_account_id": "acct_theirs", "company_stripe_source": "connected"}
    with patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value=row)):
        resp = client.post("/api/v1/company/stripe/create")
    assert resp.status_code == 409


def test_create_requires_org_admin(member_client):
    resp = member_client.post("/api/v1/company/stripe/create")
    assert resp.status_code == 403


def test_create_requires_paid(client, monkeypatch):
    monkeypatch.setattr(
        mod,
        "require_always_on_entitlement",
        AsyncMock(side_effect=HTTPException(status_code=402, detail={"blocked": True, "code": "routines_require_subscription"})),
    )
    resp = client.post("/api/v1/company/stripe/create")
    assert resp.status_code == 402


def test_connect_start_stores_state(client):
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})),
        patch.object(mod.oauth_state_repo, "put", AsyncMock()) as put,
        patch.object(mod.company_stripe, "build_connect_authorize_url", return_value="https://connect.stripe.com/oauth/authorize?x"),
    ):
        resp = client.get("/api/v1/company/stripe/connect/start")
    assert resp.status_code == 200
    assert resp.json() == {"authorize_url": "https://connect.stripe.com/oauth/authorize?x"}
    kwargs = put.call_args.kwargs
    assert kwargs["owner_id"] == "org_a"
    assert kwargs["provider"] == "stripe_company"


def test_connect_callback_binds_and_redirects(client, monkeypatch):
    monkeypatch.setattr(mod.settings, "STRIPE_COMPANY_RETURN_URL", "https://dev.example/settings/connections?stripe=return")
    with (
        patch.object(mod.oauth_state_repo, "consume", AsyncMock(return_value={"owner_id": "org_a", "provider": "stripe_company"})),
        patch.object(mod.company_stripe, "exchange_connect_code", AsyncMock(return_value="acct_theirs")),
        patch.object(mod.billing_repo, "set_company_stripe_account", AsyncMock(return_value={})) as bind,
    ):
        resp = client.get("/api/v1/company/stripe/connect/callback?code=ac_1&state=st_1", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://dev.example/settings/connections?stripe=return&stripe_connect=connected"
    bind.assert_awaited_once_with("org_a", account_id="acct_theirs", source="connected")


def test_connect_callback_bad_state_redirects_with_error(client, monkeypatch):
    monkeypatch.setattr(mod.settings, "STRIPE_COMPANY_RETURN_URL", "https://dev.example/settings/connections?stripe=return")
    with patch.object(mod.oauth_state_repo, "consume", AsyncMock(return_value=None)):
        resp = client.get("/api/v1/company/stripe/connect/callback?code=ac_1&state=bad", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].endswith("&stripe_error=state")


def test_disconnect_deauthorizes_and_clears(client):
    row = {"owner_id": "org_a", "company_stripe_account_id": "acct_x", "company_stripe_source": "created"}
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value=row)),
        patch.object(mod.company_stripe, "deauthorize", AsyncMock(return_value=True)) as deauth,
        patch.object(mod.billing_repo, "clear_company_stripe_account", AsyncMock(return_value={})) as clear,
    ):
        resp = client.post("/api/v1/company/stripe/disconnect")
    assert resp.status_code == 200
    deauth.assert_awaited_once_with(account_id="acct_x")
    clear.assert_awaited_once_with("org_a", account_id="acct_x")
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_company_stripe.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'routers.company_stripe'`.

- [ ] **Step 3: Implement the router**

```python
# apps/backend/routers/company_stripe.py
"""The company's Stripe account — /api/v1/company/stripe/*.

Owner-facing (Clerk) routes are paid-only and org-admin-only for anything
that mutates; the OAuth callback is public (Stripe redirects there) and is
bound to the owner via the single-use ``oauth-state`` row.
"""

from __future__ import annotations

import logging
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.config import settings
from core.repositories import billing_repo, oauth_state_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services import company_stripe
from core.services.paperclip_admin_client import shared_admin_client
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.paperclip_openclaw_pairing import resolve_company_id_for_owner
from routers.teams._guards import require_always_on_entitlement

logger = logging.getLogger(__name__)
router = APIRouter()

STATE_PROVIDER = "stripe_company"
DASHBOARD_URL = "https://dashboard.stripe.com/"


def _gate_admin(auth: AuthContext) -> None:
    if auth.is_org_context:
        require_org_admin(auth)


async def resolve_company_name(owner_id: str) -> str:
    """The Paperclip company name (the owner named it in Teams); falls back to
    the owner id so a Stripe account is never created with an empty name."""
    repo = PaperclipRepo()
    company_id = await resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
    if not company_id:
        return owner_id
    cookie = await get_admin_session_cookie()
    company = await shared_admin_client().get_company(session_cookie=cookie, company_id=company_id)
    return (company or {}).get("name") or owner_id


def _status_payload(row: dict | None) -> dict:
    row = row or {}
    account_id = row.get("company_stripe_account_id")
    return {
        "account_id": account_id,
        "source": row.get("company_stripe_source"),
        "details_submitted": bool(row.get("company_stripe_details_submitted")),
        "charges_enabled": bool(row.get("company_stripe_charges_enabled")),
        "payouts_enabled": bool(row.get("company_stripe_payouts_enabled")),
        "dashboard_url": DASHBOARD_URL if account_id else None,
    }


@router.get("/status")
async def status(auth: Annotated[AuthContext, Depends(get_current_user)]):
    owner_id = resolve_owner_id(auth)
    row = await billing_repo.get_by_owner_id(owner_id)
    return _status_payload(row)


@router.post("/create")
async def create(auth: Annotated[AuthContext, Depends(get_current_user)]):
    """Create the company account if absent and return a fresh onboarding link."""
    _gate_admin(auth)
    owner_id = resolve_owner_id(auth)
    await require_always_on_entitlement(owner_id)
    row = await billing_repo.get_by_owner_id(owner_id) or {}
    account_id = row.get("company_stripe_account_id")
    if account_id and row.get("company_stripe_source") == "connected":
        raise HTTPException(status_code=409, detail="An existing Stripe account is already connected")
    if not account_id:
        company_name = await resolve_company_name(owner_id)
        account_id = await company_stripe.create_company_account(
            owner_id=owner_id, email=auth.email or "", company_name=company_name
        )
        bound = await billing_repo.set_company_stripe_account(owner_id, account_id=account_id, source="created")
        if bound is None:
            # A concurrent create bound first; use the winner. The orphan shell
            # on Stripe is an empty account with no obligations.
            row = await billing_repo.get_by_owner_id(owner_id) or {}
            account_id = row.get("company_stripe_account_id") or account_id
    url = await company_stripe.create_company_onboarding_link(account_id=account_id)
    return {"account_id": account_id, "onboarding_url": url}


@router.post("/onboarding-link")
async def onboarding_link(auth: Annotated[AuthContext, Depends(get_current_user)]):
    _gate_admin(auth)
    owner_id = resolve_owner_id(auth)
    await require_always_on_entitlement(owner_id)
    row = await billing_repo.get_by_owner_id(owner_id) or {}
    account_id = row.get("company_stripe_account_id")
    if not account_id or row.get("company_stripe_source") != "created":
        raise HTTPException(status_code=409, detail="No created Stripe account to onboard")
    url = await company_stripe.create_company_onboarding_link(account_id=account_id)
    return {"onboarding_url": url}


@router.get("/connect/start")
async def connect_start(auth: Annotated[AuthContext, Depends(get_current_user)]):
    _gate_admin(auth)
    owner_id = resolve_owner_id(auth)
    await require_always_on_entitlement(owner_id)
    row = await billing_repo.get_by_owner_id(owner_id) or {}
    if row.get("company_stripe_account_id"):
        raise HTTPException(status_code=409, detail="A Stripe account is already bound")
    state = secrets.token_urlsafe(32)
    await oauth_state_repo.put(state, owner_id=owner_id, provider=STATE_PROVIDER, verifier="")
    return {"authorize_url": company_stripe.build_connect_authorize_url(state=state)}


def _return(query: str) -> RedirectResponse:
    base = settings.STRIPE_COMPANY_RETURN_URL
    sep = "&" if "?" in base else "?"
    return RedirectResponse(url=f"{base}{sep}{query}", status_code=302)


@router.get("/connect/callback")
async def connect_callback(code: str = Query(default=""), state: str = Query(default="")):
    """Public: Stripe redirects here. Owner is recovered from the state row."""
    rec = await oauth_state_repo.consume(state) if state else None
    if not rec or rec.get("provider") != STATE_PROVIDER or not code:
        return _return("stripe_error=state")
    owner_id = rec["owner_id"]
    try:
        account_id = await company_stripe.exchange_connect_code(code=code)
    except Exception:
        logger.exception("company_stripe: OAuth exchange failed owner_id=%s", owner_id)
        return _return("stripe_error=exchange")
    bound = await billing_repo.set_company_stripe_account(owner_id, account_id=account_id, source="connected")
    if bound is None:
        return _return("stripe_error=already_bound")
    # `stripe_connect`, not `stripe`: RETURN_URL already carries `?stripe=return`
    # for hosted onboarding, and a duplicate key would hide this outcome.
    return _return("stripe_connect=connected")


@router.post("/disconnect")
async def disconnect(auth: Annotated[AuthContext, Depends(get_current_user)]):
    """Revoke the platform link and unbind. The owner's account is untouched."""
    _gate_admin(auth)
    owner_id = resolve_owner_id(auth)
    row = await billing_repo.get_by_owner_id(owner_id) or {}
    account_id = row.get("company_stripe_account_id")
    if not account_id:
        raise HTTPException(status_code=409, detail="No Stripe account bound")
    await company_stripe.deauthorize(account_id=account_id)
    await billing_repo.clear_company_stripe_account(owner_id, account_id=account_id)
    return {"disconnected": True}
```

`shared_admin_client().get_company` — confirm the exact method name in `core/services/paperclip_admin_client.py` (search `def get_company`); if it is named differently (e.g. `get_company_by_id`), use that name in both the router and the test patch target `resolve_company_name` (the test patches the whole helper, so only the router body needs the real name).

- [ ] **Step 4: Mount it**

In `apps/backend/main.py`, after line 951 (`app.include_router(marketplace_payouts.router)`):

```python
from routers import company_stripe  # noqa: E402

app.include_router(company_stripe.router, prefix="/api/v1/company/stripe", tags=["company-stripe"])
```

- [ ] **Step 5: Run to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_company_stripe.py -v && uv run ruff check routers/company_stripe.py`
Expected: 10 passed; ruff clean.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/routers/company_stripe.py apps/backend/main.py apps/backend/tests/unit/routers/test_company_stripe.py
git commit -m "feat(company-stripe): /company/stripe router — status, create, onboarding link, connect existing, disconnect"
```

---

### Task 6: Friday-raised approval card + skill instruction

**Files:**
- Create: `apps/backend/core/services/company_stripe_approval.py`
- Modify: `apps/backend/routers/company_stripe.py` (add the hook-token route)
- Modify: `apps/infra/openclaw/skills/isol8/SKILL.md` (new section after "Your email")
- Test: `apps/backend/tests/unit/services/test_company_stripe_approval.py`

**Interfaces:**
- Produces: `async def request_company_stripe(*, owner_id: str, issue_id: str | None, reason: str) -> str | None` (returns approval id; `None` when already connected or a pending request exists); route `POST /api/v1/company/stripe/u/{owner_id}/request` (Bearer = container hook token) body `{"issue_id": str|null, "reason": str}` → `{"approval_id": str|None, "status": "filed"|"exists"|"connected"}`.
- Approval shape filed in Paperclip: `{"type": "connect_stripe", "payload": {"isol8_action": "company_stripe", "title": "Set up <company> on Stripe", "reason": <reason>}, "issueIds": [issue_id] if issue_id else []}`.

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/unit/services/test_company_stripe_approval.py
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import company_stripe_approval as mod  # noqa: E402


def _admin(existing: list[dict]):
    admin = MagicMock()
    admin.list_approvals = AsyncMock(return_value={"approvals": existing})
    admin.create_approval = AsyncMock(return_value={"id": "apr_new"})
    return admin


@pytest.mark.asyncio
async def test_files_connect_stripe_approval():
    admin = _admin(existing=[])
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})),
        patch.object(mod, "resolve_company_id_for_owner", AsyncMock(return_value="cmp_1")),
        patch.object(mod, "get_admin_session_cookie", AsyncMock(return_value="cookie")),
        patch.object(mod, "shared_admin_client", return_value=admin),
        patch.object(mod, "resolve_company_name", AsyncMock(return_value="Northwind Ceramics")),
    ):
        approval_id = await mod.request_company_stripe(owner_id="org_a", issue_id="iss_1", reason="Need to sell the first product")
    assert approval_id == "apr_new"
    body = admin.create_approval.call_args.kwargs["body"]
    assert body["type"] == "connect_stripe"
    assert body["issueIds"] == ["iss_1"]
    assert body["payload"]["isol8_action"] == "company_stripe"
    assert body["payload"]["title"] == "Set up Northwind Ceramics on Stripe"
    assert body["payload"]["reason"] == "Need to sell the first product"


@pytest.mark.asyncio
async def test_skips_when_already_connected():
    admin = _admin(existing=[])
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a", "company_stripe_account_id": "acct_x"})),
        patch.object(mod, "shared_admin_client", return_value=admin),
    ):
        assert await mod.request_company_stripe(owner_id="org_a", issue_id=None, reason="x") is None
    admin.create_approval.assert_not_awaited()


@pytest.mark.asyncio
async def test_reuses_pending_request():
    admin = _admin(existing=[{"id": "apr_old", "type": "connect_stripe", "status": "pending", "payload": {"isol8_action": "company_stripe"}}])
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a"})),
        patch.object(mod, "resolve_company_id_for_owner", AsyncMock(return_value="cmp_1")),
        patch.object(mod, "get_admin_session_cookie", AsyncMock(return_value="cookie")),
        patch.object(mod, "shared_admin_client", return_value=admin),
    ):
        assert await mod.request_company_stripe(owner_id="org_a", issue_id=None, reason="x") == "apr_old"
    admin.create_approval.assert_not_awaited()
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_company_stripe_approval.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement the service**

```python
# apps/backend/core/services/company_stripe_approval.py
"""Friday asks the owner to set up the company's Stripe account.

Files a Paperclip approval of type ``connect_stripe`` so it lands in the
Teams inbox (and on the issue that needed it). The card is informational —
the owner acts on it from the row's "Set up Stripe" button, which calls
``POST /company/stripe/create`` and then approves the request. Never a gate:
if the owner ignores it, chat keeps working.
"""

from __future__ import annotations

import logging

from core.repositories import billing_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services.paperclip_admin_client import shared_admin_client
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.paperclip_openclaw_pairing import resolve_company_id_for_owner
from routers.company_stripe import resolve_company_name

logger = logging.getLogger(__name__)

APPROVAL_TYPE = "connect_stripe"
ACTION_MARKER = "company_stripe"


def _is_pending_company_stripe(approval: dict) -> bool:
    return (
        approval.get("type") == APPROVAL_TYPE
        and approval.get("status") in ("pending", "revision_requested")
        and (approval.get("payload") or {}).get("isol8_action") == ACTION_MARKER
    )


async def request_company_stripe(*, owner_id: str, issue_id: str | None, reason: str) -> str | None:
    row = await billing_repo.get_by_owner_id(owner_id) or {}
    if row.get("company_stripe_account_id"):
        return None
    repo = PaperclipRepo()
    company_id = await resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
    if not company_id:
        logger.info("company_stripe_approval: no Paperclip company for owner_id=%s", owner_id)
        return None
    cookie = await get_admin_session_cookie()
    admin = shared_admin_client()
    existing = await admin.list_approvals(session_cookie=cookie, company_id=company_id)
    for approval in (existing or {}).get("approvals") or []:
        if _is_pending_company_stripe(approval):
            return approval.get("id")
    company_name = await resolve_company_name(owner_id)
    body = {
        "type": APPROVAL_TYPE,
        "payload": {
            "isol8_action": ACTION_MARKER,
            "title": f"Set up {company_name} on Stripe",
            "reason": (reason or "")[:500],
        },
        "issueIds": [issue_id] if issue_id else [],
    }
    created = await admin.create_approval(session_cookie=cookie, company_id=company_id, body=body)
    return created.get("id")
```

If `list_approvals` returns a bare list rather than `{"approvals": [...]}` (check the method body in `paperclip_admin_client.py:1146`), normalise with `approvals = existing if isinstance(existing, list) else (existing or {}).get("approvals") or []` and update the test fixture accordingly.

- [ ] **Step 4: Add the hook-token route**

Append to `apps/backend/routers/company_stripe.py`:

```python
from fastapi import Request  # add to the existing fastapi import line
from pydantic import BaseModel
from routers.agent_builder import _require_hook_token
from core.services import company_stripe_approval


class RequestStripeBody(BaseModel):
    issue_id: str | None = None
    reason: str = ""


@router.post("/u/{owner_id}/request")
async def request_from_container(owner_id: str, body: RequestStripeBody, request: Request):
    """Container-authed (hook token): Friday asks the owner to set up Stripe."""
    await _require_hook_token(owner_id, request)
    row = await billing_repo.get_by_owner_id(owner_id) or {}
    if row.get("company_stripe_account_id"):
        return {"approval_id": None, "status": "connected"}
    approval_id = await company_stripe_approval.request_company_stripe(
        owner_id=owner_id, issue_id=body.issue_id, reason=body.reason
    )
    return {"approval_id": approval_id, "status": "filed" if approval_id else "exists"}
```

Because `company_stripe_approval` imports `resolve_company_name` from the router and the router imports the service, place the `from core.services import company_stripe_approval` import **inside** `request_from_container` to avoid the import cycle.

- [ ] **Step 5: Teach Friday**

In `apps/infra/openclaw/skills/isol8/SKILL.md`, after the "Your email" section, add:

````markdown
## Your company's Stripe account

Money for the business (selling a product, taking payments, paying for a
provider) needs the company's own Stripe account. Check `~/.openclaw/isol8-facts.json`
for `company_stripe: "connected"`; if it is absent or `"none"`, do not
improvise a Stripe signup — ask the owner through the platform:

```bash
curl -s -X POST -H "Authorization: Bearer $OPENCLAW_HOOK_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"issue_id":"'"$PAPERCLIP_TASK_ID"'","reason":"<one sentence on why>"}' \
  "$ISOL8_API_URL/api/v1/company/stripe/u/$ISOL8_OWNER_ID/request"
```

This files a "Set up <company> on Stripe" card in the owner's Inbox. It is
never a gate: keep working on everything that does not need money, and say
in your reply that you asked. The owner keeps the account and 100% of the
revenue; Lightbulb AI takes no fee. Never create the Stripe account in the
browser yourself.
````

Add `company_stripe` to the facts written by `core/services/owner_facts.py`: the value is `"connected"` when the billing row has `company_stripe_account_id` and `company_stripe_charges_enabled` is true, `"pending"` when the id is set but charges are not enabled, else `"none"`. Find the dict the module writes (search `"plan":`) and add the key beside `provider_choice`.

- [ ] **Step 6: Run to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_company_stripe_approval.py tests/unit/routers/test_company_stripe.py -v && uv run pytest tests/unit -k owner_facts -q`
Expected: all passed.

- [ ] **Step 7: Commit**

```bash
git add apps/backend/core/services/company_stripe_approval.py apps/backend/routers/company_stripe.py apps/backend/core/services/owner_facts.py apps/infra/openclaw/skills/isol8/SKILL.md apps/backend/tests/unit/services/test_company_stripe_approval.py
git commit -m "feat(company-stripe): Friday-raised connect_stripe approval card + skill instruction"
```

---

### Task 7: Wipe deauthorizes, never deletes

**Files:**
- Modify: `apps/backend/core/services/account_wipe.py:786-797` (after Step 8)
- Test: `apps/backend/tests/unit/services/test_account_wipe_company_stripe.py`

**Interfaces:**
- Consumes: `company_stripe.deauthorize`, `billing_repo.clear_company_stripe_account`.
- Produces: a `WipeStep(system="stripe", action="deauthorize_company_account", …)` in the report.

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/unit/services/test_account_wipe_company_stripe.py
import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import account_wipe as mod  # noqa: E402


@pytest.mark.asyncio
async def test_wipe_deauthorizes_company_stripe_account_and_never_deletes_it():
    steps = []
    with (
        patch.object(mod.company_stripe, "deauthorize", AsyncMock(return_value=True)) as deauth,
        patch.object(mod.billing_repo, "clear_company_stripe_account", AsyncMock(return_value={})) as clear,
    ):
        await mod._wipe_company_stripe(steps, owner_id="org_a", billing_row={"company_stripe_account_id": "acct_x"})
    deauth.assert_awaited_once_with(account_id="acct_x")
    clear.assert_awaited_once_with("org_a", account_id="acct_x")
    assert steps[-1].system == "stripe"
    assert steps[-1].action == "deauthorize_company_account"
    assert steps[-1].status == "ok"


@pytest.mark.asyncio
async def test_wipe_skips_when_no_company_account():
    steps = []
    await mod._wipe_company_stripe(steps, owner_id="org_a", billing_row={})
    assert steps[-1].status == "skipped"
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_account_wipe_company_stripe.py -v`
Expected: FAIL with `AttributeError: … has no attribute '_wipe_company_stripe'`.

- [ ] **Step 3: Implement**

In `apps/backend/core/services/account_wipe.py` add imports near the existing `delete_stripe_customer` import (line 106):

```python
from core.services import company_stripe
```

(`billing_repo` is already imported in this module — verify; if not, add `from core.repositories import billing_repo`.)

Add the helper next to `_step` / `_skip`:

```python
async def _wipe_company_stripe(steps: list[WipeStep], *, owner_id: str, billing_row: dict | None) -> None:
    """Step 8b: revoke the platform's link to the company's Stripe account.

    NEVER deletes the account — it is the owner's business account and keeps
    working as a normal Stripe account after we let go (spec §3.6).
    """
    account_id = (billing_row or {}).get("company_stripe_account_id")
    if not account_id:
        _skip(steps, "stripe", "deauthorize_company_account", "no company_stripe_account_id on billing row")
        return

    async def _s8b(account_id: str = account_id) -> str:
        await company_stripe.deauthorize(account_id=account_id)
        await billing_repo.clear_company_stripe_account(owner_id, account_id=account_id)
        return f"company_stripe_account_id={account_id} deauthorized (account retained by owner)"

    await _step(steps, system="stripe", action="deauthorize_company_account", fn=_s8b)
```

In `wipe_account`, immediately after the Step 8 block (after the `_skip(steps, "stripe", "delete_stripe_customer", …)` line), add:

```python
    # --- Step 8b: company Stripe account — deauthorize only ------------------
    await _wipe_company_stripe(steps, owner_id=owner_id, billing_row=ids.billing)
```

Mirror it in `preview_wipe` with a `_preview_step` entry `system="stripe", action="deauthorize_company_account"` conditioned on the same field, matching the file's existing preview pattern for Step 8.

- [ ] **Step 4: Run to verify they pass, plus the existing wipe tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_account_wipe_company_stripe.py tests/unit/services -k account_wipe -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/account_wipe.py apps/backend/tests/unit/services/test_account_wipe_company_stripe.py
git commit -m "feat(company-stripe): wipe deauthorizes the company account and never deletes it"
```

---

### Task 8: Frontend — Connections section and the approval row action

**Files:**
- Create: `apps/frontend/src/components/settings/CompanyStripeSection.tsx`
- Modify: `apps/frontend/src/app/settings/connections/page.tsx` (render the section above `<ConnectionsList>`; extend `oauthReturnBanner` for `stripe=connected` / `stripe_error`)
- Modify: `apps/frontend/src/components/teams/inbox/lib/approvalLabel.ts:14-19`
- Modify: `apps/frontend/src/components/teams/inbox/ApprovalRow.tsx`
- Test: `apps/frontend/src/components/settings/__tests__/CompanyStripeSection.test.tsx`

**Interfaces:**
- Consumes: `useApi()` (`get/post`), `useIsWorkspaceAdmin()` (already used on the page), `useApprovalMutations()` (`approve(id)`), backend routes from Task 5.
- Produces: `CompanyStripeSection({ canManage }: { canManage: boolean })`; `ApprovalRow` renders "Set up Stripe" + "Connect existing" for `approval.type === "connect_stripe"`.

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/src/components/settings/__tests__/CompanyStripeSection.test.tsx
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

const get = vi.fn();
const post = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ get, post }) }));

import { CompanyStripeSection } from "@/components/settings/CompanyStripeSection";

describe("CompanyStripeSection", () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
    vi.spyOn(window, "open").mockImplementation(() => null);
  });

  it("offers both doors when nothing is bound", async () => {
    get.mockResolvedValue({ account_id: null, source: null, details_submitted: false, charges_enabled: false, payouts_enabled: false, dashboard_url: null });
    render(<CompanyStripeSection canManage />);
    expect(await screen.findByText("Set up Stripe")).toBeInTheDocument();
    expect(screen.getByText("Connect existing Stripe")).toBeInTheDocument();
  });

  it("creates and opens the onboarding link", async () => {
    get.mockResolvedValue({ account_id: null, source: null, details_submitted: false, charges_enabled: false, payouts_enabled: false, dashboard_url: null });
    post.mockResolvedValue({ account_id: "acct_new", onboarding_url: "https://connect.stripe.com/setup/x" });
    render(<CompanyStripeSection canManage />);
    fireEvent.click(await screen.findByText("Set up Stripe"));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/company/stripe/create", {}));
    expect(window.open).toHaveBeenCalledWith("https://connect.stripe.com/setup/x", "_blank", "noopener");
  });

  it("shows status and disconnect when bound", async () => {
    get.mockResolvedValue({ account_id: "acct_x", source: "created", details_submitted: true, charges_enabled: true, payouts_enabled: false, dashboard_url: "https://dashboard.stripe.com/" });
    render(<CompanyStripeSection canManage />);
    expect(await screen.findByText(/Charges enabled/)).toBeInTheDocument();
    expect(screen.getByText("Disconnect")).toBeInTheDocument();
  });

  it("hides actions for non-admins", async () => {
    get.mockResolvedValue({ account_id: null, source: null, details_submitted: false, charges_enabled: false, payouts_enabled: false, dashboard_url: null });
    render(<CompanyStripeSection canManage={false} />);
    await screen.findByText(/Stripe/);
    expect(screen.queryByText("Set up Stripe")).toBeNull();
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/frontend && pnpm vitest run src/components/settings/__tests__/CompanyStripeSection.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the section**

```tsx
// apps/frontend/src/components/settings/CompanyStripeSection.tsx
"use client";

import { useState } from "react";
import useSWR from "swr";
import { useApi } from "@/lib/api";
import { notifyApiError } from "@/lib/notify";

type CompanyStripeStatus = {
  account_id: string | null;
  source: "created" | "connected" | null;
  details_submitted: boolean;
  charges_enabled: boolean;
  payouts_enabled: boolean;
  dashboard_url: string | null;
};

const STATUS_PATH = "/company/stripe/status";

export function CompanyStripeSection({ canManage }: { canManage: boolean }) {
  const api = useApi();
  const { data, mutate, isLoading } = useSWR<CompanyStripeStatus>(STATUS_PATH, (p: string) => api.get(p));
  const [busy, setBusy] = useState(false);

  async function run(fn: () => Promise<void>) {
    setBusy(true);
    try {
      await fn();
      await mutate();
    } catch (e) {
      notifyApiError(e, "Stripe action failed. Try again.");
    } finally {
      setBusy(false);
    }
  }

  const bound = Boolean(data?.account_id);

  // The shared client returns `unknown`; narrow at the call site.
  const createLink = async () => (await api.post("/company/stripe/create", {})) as { onboarding_url: string };
  const onboardingLink = async () =>
    (await api.post("/company/stripe/onboarding-link", {})) as { onboarding_url: string };
  const connectStart = async () => (await api.get("/company/stripe/connect/start")) as { authorize_url: string };

  return (
    <section className="settings-card" data-testid="company-stripe">
      <div className="settings-card-header">
        <h2 className="settings-card-title">Stripe</h2>
        <p className="settings-card-desc">
          Your company&apos;s own Stripe account. You keep the account, the Dashboard and 100% of your
          revenue — Lightbulb AI takes no fee.
        </p>
      </div>

      {isLoading && <p className="text-sm text-[var(--mute)]">Loading…</p>}

      {!isLoading && !bound && (
        <div className="flex flex-wrap gap-2">
          {canManage ? (
            <>
              <button
                type="button"
                className="settings-btn-save"
                disabled={busy}
                onClick={() =>
                  run(async () => {
                    const r = await createLink();
                    window.open(r.onboarding_url, "_blank", "noopener");
                  })
                }
              >
                Set up Stripe
              </button>
              <button
                type="button"
                className="settings-btn-outline"
                disabled={busy}
                onClick={() =>
                  run(async () => {
                    const r = await connectStart();
                    window.open(r.authorize_url, "_blank", "noopener");
                  })
                }
              >
                Connect existing Stripe
              </button>
            </>
          ) : (
            <p className="text-sm text-[var(--mute)]">A workspace admin sets up Stripe.</p>
          )}
        </div>
      )}

      {!isLoading && bound && data && (
        <div className="flex flex-col gap-2">
          <dl className="text-sm text-[var(--mute)]">
            <div className="flex gap-2"><dt>Account</dt><dd className="font-mono text-[#1c1917]">{data.account_id}</dd></div>
            <div className="flex gap-2"><dt>Source</dt><dd>{data.source === "connected" ? "Connected existing account" : "Created by Lightbulb AI"}</dd></div>
            <div className="flex gap-2">
              <dt>Status</dt>
              <dd>
                {data.charges_enabled ? "Charges enabled" : data.details_submitted ? "Under review" : "Onboarding incomplete"}
                {data.payouts_enabled ? " · Payouts enabled" : ""}
              </dd>
            </div>
          </dl>
          <div className="flex flex-wrap gap-2">
            {data.dashboard_url && (
              <a className="settings-btn-outline" href={data.dashboard_url} target="_blank" rel="noopener noreferrer">
                Open Stripe Dashboard
              </a>
            )}
            {canManage && data.source === "created" && !data.charges_enabled && (
              <button
                type="button"
                className="settings-btn-save"
                disabled={busy}
                onClick={() =>
                  run(async () => {
                    const r = await onboardingLink();
                    window.open(r.onboarding_url, "_blank", "noopener");
                  })
                }
              >
                Finish onboarding
              </button>
            )}
            {canManage && (
              <button
                type="button"
                className="settings-btn-outline"
                disabled={busy}
                onClick={() => run(async () => { await api.post("/company/stripe/disconnect", {}); })}
              >
                Disconnect
              </button>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
```

Class names are the ones `apps/frontend/src/app/settings/settings.css` already defines (`settings-card`, `settings-card-header`, `settings-card-title`, `settings-card-desc`, `settings-btn-save`, `settings-btn-outline`); add no new CSS.

- [ ] **Step 4: Mount it and handle the return banner**

In `apps/frontend/src/app/settings/connections/page.tsx`:

- Import `CompanyStripeSection` and render `<CompanyStripeSection canManage={canManage} />` directly above `<ConnectionsList canManage={canManage} />`, using the same `canManage` the page already derives from `useIsWorkspaceAdmin()`.
- Extend `oauthReturnBanner(connected, error)` to also take `stripe: string | null`, `stripeConnect: string | null` and `stripeError: string | null` (read from `searchParams.get("stripe")` / `searchParams.get("stripe_connect")` / `searchParams.get("stripe_error")`): `stripeConnect === "connected"` → success "Connected your Stripe account."; `stripe === "return"` → success "Stripe onboarding finished — status updates in a moment."; `stripe === "refresh"` → no banner (the link expired; the section's "Finish onboarding" button mints a new one); `stripeError` → error "Couldn't connect Stripe. Please try again." Strip those params the same way the page strips `connected`/`error`.

- [ ] **Step 5: Approval row**

In `approvalLabel.ts` add `connect_stripe: "Connect Stripe",` to `TYPE_LABEL`.

In `ApprovalRow.tsx`, inside the `isActionable && !rejecting` block, replace the single Approve button with:

```tsx
            {approval.type === "connect_stripe" ? (
              <>
                <EditorialButton
                  size="sm"
                  disabled={busy}
                  onClick={() =>
                    run(async () => {
                      const r = (await api.post("/company/stripe/create", {})) as { onboarding_url: string };
                      window.open(r.onboarding_url, "_blank", "noopener");
                      await approve(approval.id);
                    })
                  }
                >
                  Set up Stripe
                </EditorialButton>
                <EditorialButton
                  size="sm"
                  disabled={busy}
                  onClick={() =>
                    run(async () => {
                      const r = (await api.get("/company/stripe/connect/start")) as { authorize_url: string };
                      window.open(r.authorize_url, "_blank", "noopener");
                      await approve(approval.id);
                    })
                  }
                >
                  Connect existing
                </EditorialButton>
              </>
            ) : (
              <EditorialButton size="sm" disabled={busy} onClick={() => run(() => approve(approval.id))}>
                Approve
              </EditorialButton>
            )}
```

and add `const api = useApi();` with `import { useApi } from "@/lib/api";` at the top. Both buttons use `EditorialButton`'s default variant (`"primary"`, see `teams/shell/EditorialButton.tsx:16`).

- [ ] **Step 6: Run tests, typecheck, lint**

Run: `cd apps/frontend && pnpm vitest run src/components/settings/__tests__/CompanyStripeSection.test.tsx && pnpm tsc --noEmit && pnpm lint`
Expected: 4 passed; tsc and lint clean.

- [ ] **Step 7: Commit**

```bash
git add apps/frontend/src/components/settings/CompanyStripeSection.tsx apps/frontend/src/app/settings/connections/page.tsx apps/frontend/src/components/teams/inbox/lib/approvalLabel.ts apps/frontend/src/components/teams/inbox/ApprovalRow.tsx apps/frontend/src/components/settings/__tests__/CompanyStripeSection.test.tsx
git commit -m "feat(company-stripe): Settings → Connections section and connect_stripe approval action"
```

---

### Task 9: Dev verification, PR

**Files:** none new.

- [ ] **Step 1: Owner actions before the dev run**

Owner (not the agent): in the Stripe Dashboard for the **dev** platform account — (a) Connect settings → add the redirect URI `https://api-dev.flickmeon.ai/api/v1/company/stripe/connect/callback`; (b) brand the Connect Onboarding form (name/colour/icon — required by Connect Onboarding); (c) on the Connect-scoped webhook endpoint add `account.application.deauthorized`; (d) confirm the `ca_…` client id used in Task 1 — it is a prerequisite for connect-existing, disconnect AND wipe step 8b (all call `OAuth.deauthorize`).

- [ ] **Step 2: Open the PR and watch CI**

```bash
git push -u origin HEAD
gh pr create --title "feat(company-stripe): the company's Stripe account on our Connect platform (P0a)" --body "$(cat <<'EOF'
Implements P0a of docs/superpowers/specs/2026-08-28-owner-data-plane-design.md (§3.1, §3.6, §4.5).

- Accounts v2 connected account per company (merchant configuration, full dashboard, Stripe collects fees and losses — zero Connect fees): create + hosted onboarding, or connect existing (Standard OAuth); Friday-raised `connect_stripe` approval card; status mirrored from Connect webhooks; deauthorize on disconnect and on wipe (never delete). `stripe` pinned `>=14.4.1` for `StripeClient.v2.core`.
- New: `core/services/company_stripe.py`, `company_stripe_approval.py`, `routers/company_stripe.py`; billing-accounts GSI `company-stripe-account-index`; Settings → Connections section; approval-row action; skill section.

Owner actions: register the OAuth redirect URI and `account.application.deauthorized` on the dev/prod Connect webhook; brand Connect Onboarding.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr checks --watch
```

- [ ] **Step 3: Verify on dev with Claude-in-Chrome (paid dev account)**

1. Open `https://dev.flickmeon.ai/settings/connections` → "Stripe" section shows both doors.
2. Click "Set up Stripe" → Connect Onboarding opens in a new tab → complete with Stripe test data → returns to `/settings/connections?stripe=return`.
3. `aws dynamodb get-item --table-name isol8-dev-billing-accounts --key '{"owner_id":{"S":"<owner>"}}' --profile isol8-admin` → `company_stripe_account_id` set, `company_stripe_source = created`; after the `account.updated` delivery, `company_stripe_details_submitted = true`.
4. In the owner's container chat, ask Friday to "sell our first product" → she files the card → Inbox shows "Connect Stripe: Set up <company> on Stripe" (on a second dev owner with no account).
5. Click "Disconnect" → row cleared; the Stripe test account still opens at dashboard.stripe.com (proof we never deleted it).
6. Prove from the backend log group that `/webhooks/stripe-marketplace` received `account.updated` for the company account and did not run the seller flush.
7. `OAuth.deauthorize` is accepted on a platform-CREATED v2 account (not just an OAuth-connected one) — disconnect on the account from step 2 and confirm Stripe does not error.
8. `account.updated` from a v2 account arrives at the v1 Connect webhook endpoint (the same delivery step 6 looks for, but confirmed for the v2-created account specifically).

- [ ] **Step 4: Report**

Post the six verification results on the PR as a comment; do not merge until the owner says so.
