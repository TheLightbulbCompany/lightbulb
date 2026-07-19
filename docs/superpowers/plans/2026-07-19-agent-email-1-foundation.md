# Provisioned Agent Email — Plan 1: Foundation & Provisioning

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every newly provisioned owner gets a durable agent-email address row derived from their company slug, and the agent learns its own address through owner-facts. No mail is sent or received yet.

**Architecture:** A new prefix-convention DynamoDB table `agent-email-addresses` (PK = `address`, GSI `by-owner`) maps a provisioned address to its `(owner_id, agent_id)` plus a verified-sender allowlist. `core/services/agent_email.py` builds the address from the owner's **company slug** (already allocated by the Company Identity work) and mints the row. `provision_org` mints best-effort right after Friday is seeded, passing the slug it just allocated. `owner_facts` publishes the address into `isol8-facts.json` so the baked isol8 skill can tell the agent its own address.

**Tech Stack:** Python 3 / FastAPI backend (uv, pytest, pytest-asyncio, moto), AWS CDK v2 (TypeScript, jest), DynamoDB.

**Design spec:** `docs/superpowers/specs/2026-07-19-provisioned-agent-email-design.md`

## Global Constraints

- Mail domain comes from `settings.AGENT_EMAIL_DOMAIN`. **An empty value disables minting entirely.** This plan deliberately does **not** set it in CDK — the machinery ships inert; a later plan flips it on once the Resend domain is verified.
- **Address shape:** the primary agent (Friday) gets `{slug}@{domain}` — the company's main address is its CEO's. Additional agents (later, gated) get `{slug}-{agent}@{domain}`.
- **The slug is an input, never looked up during provisioning.** `provision_org` passes the slug it just allocated from `allocate_company_slug`. It must NOT call `company_slug_repo.get_by_owner`, because a rare re-entry can leave an owner holding two slug rows and `get_by_owner` uses `Limit=1` with no ordering — the value it returns is not deterministic.
- Minting is **idempotent** (deterministic given the same slug) and **best-effort**: a mail-side failure must never fail or undo `provision_org`. Friday surviving matters more than the address.
- `status` is `"active"` (the current primary) or `"alias"` (a superseded address that must keep routing). Readers select `active`, and where more than one could match they MUST break the tie deterministically (earliest `created_at`) rather than taking an arbitrary row.
- The table uses the **prefix convention** (`core.dynamodb.get_table("agent-email-addresses")`), matching `channel-links` — no table-name env var.
- A new DynamoDB table requires the full CDK chain: `database-stack.ts` → `service-stack.ts` (props + grant) → `isol8-stage.ts` **and** `local-stage.ts` → `observability-stack.ts`. Grants are explicit per-table; there is no wildcard. **Note:** `service-stack`'s inline IAM policy is near CloudFormation's ~10KB ceiling and CDK packs statements in add-order — place the new grant beside the other DynamoDB grants and re-run `pnpm test`; if `BedrockGateUrlParamRead` gets pushed into the overflow managed policy, move the grant later in the sequence (see the existing comment at the `companySlugsTable` grant).
- All addresses and sender emails are stored and compared **lowercased**.
- Backend tests: `cd apps/backend && uv run pytest`. Infra tests: `cd apps/infra && pnpm test`. Run tests in the FOREGROUND.
- One **pre-existing** backend failure is unrelated to this work and must not be fixed here: `tests/unit/routers/teams/test_agents.py::test_create_agent_returns_upstream_status_on_paperclip_error` (missing `PAPERCLIP_SERVICE_TOKEN_KEY`; it only surfaces in isolated runs).
- Commit after each task.

**Deferred:** the backfill script for existing owners (pointless until `AGENT_EMAIL_DOMAIN` is live).

---

### Task 1: `agent_email_repo` — address-book data access

**Files:**
- Create: `apps/backend/core/repositories/agent_email_repo.py`
- Test: `apps/backend/tests/unit/repositories/test_agent_email_repo.py`

**Interfaces:**
- Consumes: `core.dynamodb.get_table`, `run_in_thread`, `utc_now_iso` (existing).
- Produces:
  - `normalize(value: str) -> str`
  - `async put(*, address: str, owner_id: str, agent_id: str, verified_senders: list[str], public: bool = False) -> dict`
  - `async get_by_address(address: str) -> dict | None`
  - `async query_by_owner(owner_id: str) -> list[dict]`
  - Item shape: `{address, owner_id, agent_id, verified_senders, public, status, created_at}`

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/repositories/test_agent_email_repo.py`:

```python
"""agent_email_repo: address-keyed rows with a by-owner GSI."""

import os
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


@pytest.fixture
def agent_email_table():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        table = resource.create_table(
            TableName="test-agent-email-addresses",
            KeySchema=[{"AttributeName": "address", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "address", "AttributeType": "S"},
                {"AttributeName": "owner_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "by-owner",
                    "KeySchema": [{"AttributeName": "owner_id", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(
            TableName="test-agent-email-addresses"
        )
        with (
            patch("core.dynamodb._table_prefix", "test-"),
            patch("core.dynamodb._dynamodb_resource", resource),
        ):
            yield table


@pytest.mark.asyncio
async def test_put_normalizes_and_get_by_address(agent_email_table):
    from core.repositories import agent_email_repo

    await agent_email_repo.put(
        address="Acme@Agents.Isol8.co",
        owner_id="user_1",
        agent_id="agent_friday",
        verified_senders=["Owner@Example.com"],
    )

    row = await agent_email_repo.get_by_address("acme@agents.isol8.co")
    assert row is not None
    assert row["owner_id"] == "user_1"
    assert row["agent_id"] == "agent_friday"
    assert row["verified_senders"] == ["owner@example.com"]
    assert row["public"] is False
    assert row["status"] == "active"
    assert row["created_at"]


@pytest.mark.asyncio
async def test_get_by_address_missing_returns_none(agent_email_table):
    from core.repositories import agent_email_repo

    assert await agent_email_repo.get_by_address("nope@agents.isol8.co") is None


@pytest.mark.asyncio
async def test_query_by_owner_returns_only_that_owner(agent_email_table):
    from core.repositories import agent_email_repo

    await agent_email_repo.put(
        address="acme@agents.isol8.co",
        owner_id="user_1",
        agent_id="agent_friday",
        verified_senders=[],
    )
    await agent_email_repo.put(
        address="acme-atlas@agents.isol8.co",
        owner_id="user_1",
        agent_id="agent_atlas",
        verified_senders=[],
    )
    await agent_email_repo.put(
        address="other@agents.isol8.co",
        owner_id="user_2",
        agent_id="agent_friday",
        verified_senders=[],
    )

    rows = await agent_email_repo.query_by_owner("user_1")
    assert {r["address"] for r in rows} == {
        "acme@agents.isol8.co",
        "acme-atlas@agents.isol8.co",
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/repositories/test_agent_email_repo.py -v --no-cov -p no:cacheprovider`
Expected: FAIL — `ImportError: cannot import name 'agent_email_repo'`

- [ ] **Step 3: Write minimal implementation**

Create `apps/backend/core/repositories/agent_email_repo.py`:

```python
"""Address-book for provisioned agent email.

One row per provisioned agent email address. The address is the partition
key because inbound mail is routed by recipient; a ``by-owner`` GSI lists an
owner's addresses.

``status`` is ``"active"`` for the current primary address and ``"alias"``
for a superseded one that must keep routing — so an owner can accumulate
several rows over time and readers must select deliberately.

Prefix-convention table (``core.dynamodb.get_table``), matching
``channel_link_repo``.
"""

from boto3.dynamodb.conditions import Key

from core.dynamodb import get_table, run_in_thread, utc_now_iso


def _get_table():
    return get_table("agent-email-addresses")


def normalize(value: str) -> str:
    """Addresses and sender emails are matched case-insensitively."""
    return value.strip().lower()


async def put(
    *,
    address: str,
    owner_id: str,
    agent_id: str,
    verified_senders: list[str],
    public: bool = False,
) -> dict:
    item = {
        "address": normalize(address),
        "owner_id": owner_id,
        "agent_id": agent_id,
        "verified_senders": [normalize(s) for s in verified_senders],
        "public": public,
        "status": "active",
        "created_at": utc_now_iso(),
    }
    table = _get_table()
    await run_in_thread(table.put_item, Item=item)
    return item


async def get_by_address(address: str) -> dict | None:
    table = _get_table()
    response = await run_in_thread(table.get_item, Key={"address": normalize(address)})
    return response.get("Item")


async def query_by_owner(owner_id: str) -> list[dict]:
    table = _get_table()
    response = await run_in_thread(
        table.query,
        IndexName="by-owner",
        KeyConditionExpression=Key("owner_id").eq(owner_id),
    )
    return response.get("Items", [])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/repositories/test_agent_email_repo.py -v --no-cov -p no:cacheprovider`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/repositories/agent_email_repo.py apps/backend/tests/unit/repositories/test_agent_email_repo.py
git commit -m "feat(agent-email): address-book repository"
```

---

### Task 2: `agent_email` service — slug-derived address + idempotent mint

**Files:**
- Create: `apps/backend/core/services/agent_email.py`
- Modify: `apps/backend/core/config.py` (add `AGENT_EMAIL_DOMAIN` immediately after `MARKETPLACE_ARTIFACTS_BUCKET`, around line 245)
- Test: `apps/backend/tests/unit/services/test_agent_email.py`

**Interfaces:**
- Consumes: `agent_email_repo.put`, `agent_email_repo.get_by_address` (Task 1); `settings.AGENT_EMAIL_DOMAIN`.
- Produces:
  - `build_agent_email_address(*, slug: str, agent_name: str, domain: str, is_primary: bool) -> str`
  - `async mint_agent_email_address(*, owner_id: str, agent_id: str, agent_name: str, owner_email: str, slug: str, is_primary: bool = True) -> str | None`

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/services/test_agent_email.py`:

```python
"""agent_email: slug-derived address building + best-effort idempotent mint."""

import os

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import agent_email  # noqa: E402


def test_primary_address_is_just_the_slug():
    """The company's main address is its CEO's — {slug}@domain."""
    addr = agent_email.build_agent_email_address(
        slug="acme", agent_name="Friday", domain="agents.isol8.co", is_primary=True
    )
    assert addr == "acme@agents.isol8.co"


def test_secondary_address_appends_the_agent():
    addr = agent_email.build_agent_email_address(
        slug="acme", agent_name="Atlas", domain="agents.isol8.co", is_primary=False
    )
    assert addr == "acme-atlas@agents.isol8.co"


def test_address_is_lowercased_and_agent_name_slugified():
    addr = agent_email.build_agent_email_address(
        slug="Acme", agent_name="  Chief  Ops!! ", domain="Agents.Isol8.co",
        is_primary=False,
    )
    assert addr == "acme-chief-ops@agents.isol8.co"


def test_address_is_deterministic():
    kwargs = dict(slug="acme", agent_name="Friday", domain="agents.isol8.co", is_primary=True)
    assert agent_email.build_agent_email_address(**kwargs) == (
        agent_email.build_agent_email_address(**kwargs)
    )


@pytest.mark.asyncio
async def test_mint_skips_when_domain_unset(monkeypatch):
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "")
    calls = []

    async def fake_put(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(agent_email.agent_email_repo, "put", fake_put)

    result = await agent_email.mint_agent_email_address(
        owner_id="user_1", agent_id="agent_friday", agent_name="Friday",
        owner_email="o@example.com", slug="acme",
    )
    assert result is None
    assert calls == []


@pytest.mark.asyncio
async def test_mint_skips_when_slug_missing(monkeypatch):
    """No slug means no address — an owner whose slug allocation failed."""
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "agents.isol8.co")
    calls = []

    async def fake_put(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(agent_email.agent_email_repo, "put", fake_put)

    result = await agent_email.mint_agent_email_address(
        owner_id="user_1", agent_id="agent_friday", agent_name="Friday",
        owner_email="o@example.com", slug="",
    )
    assert result is None
    assert calls == []


@pytest.mark.asyncio
async def test_mint_creates_row_with_owner_as_verified_sender(monkeypatch):
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "agents.isol8.co")
    puts = []

    async def fake_get(address):
        return None

    async def fake_put(**kwargs):
        puts.append(kwargs)
        return kwargs

    monkeypatch.setattr(agent_email.agent_email_repo, "get_by_address", fake_get)
    monkeypatch.setattr(agent_email.agent_email_repo, "put", fake_put)

    address = await agent_email.mint_agent_email_address(
        owner_id="user_1", agent_id="agent_friday", agent_name="Friday",
        owner_email="o@example.com", slug="acme",
    )

    assert address == "acme@agents.isol8.co"
    assert len(puts) == 1
    assert puts[0]["owner_id"] == "user_1"
    assert puts[0]["agent_id"] == "agent_friday"
    assert puts[0]["verified_senders"] == ["o@example.com"]


@pytest.mark.asyncio
async def test_mint_is_idempotent_when_row_exists(monkeypatch):
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "agents.isol8.co")
    puts = []

    async def fake_get(address):
        return {"address": address}

    async def fake_put(**kwargs):
        puts.append(kwargs)

    monkeypatch.setattr(agent_email.agent_email_repo, "get_by_address", fake_get)
    monkeypatch.setattr(agent_email.agent_email_repo, "put", fake_put)

    address = await agent_email.mint_agent_email_address(
        owner_id="user_1", agent_id="agent_friday", agent_name="Friday",
        owner_email="o@example.com", slug="acme",
    )
    assert address == "acme@agents.isol8.co"
    assert puts == []


@pytest.mark.asyncio
async def test_mint_never_raises_on_repo_failure(monkeypatch):
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "agents.isol8.co")

    async def boom(*args, **kwargs):
        raise RuntimeError("dynamo down")

    monkeypatch.setattr(agent_email.agent_email_repo, "get_by_address", boom)

    result = await agent_email.mint_agent_email_address(
        owner_id="user_1", agent_id="agent_friday", agent_name="Friday",
        owner_email="o@example.com", slug="acme",
    )
    assert result is None


@pytest.mark.asyncio
async def test_mint_never_raises_on_bad_input(monkeypatch):
    """Best-effort contract holds even for malformed arguments."""
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "agents.isol8.co")

    result = await agent_email.mint_agent_email_address(
        owner_id="user_1", agent_id="agent_friday", agent_name=None,
        owner_email="o@example.com", slug="acme",
    )
    assert result is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_agent_email.py -v --no-cov -p no:cacheprovider`
Expected: FAIL — `ImportError: cannot import name 'agent_email' from 'core.services'`

- [ ] **Step 3: Add the config field**

In `apps/backend/core/config.py`, immediately after `MARKETPLACE_ARTIFACTS_BUCKET`:

```python
    # Provisioned agent email (set by CDK ServiceStack). Empty disables address
    # minting entirely, so an env without a verified mail domain never mints.
    AGENT_EMAIL_DOMAIN: str = os.getenv("AGENT_EMAIL_DOMAIN", "")
```

- [ ] **Step 4: Write minimal implementation**

Create `apps/backend/core/services/agent_email.py`:

```python
"""Provisioned agent email addresses.

The address is built from the owner's company slug, which the Company
Identity work already reserved: the primary agent (Friday) gets
``{slug}@{domain}`` — a company's main address is its CEO's — and any
additional agent gets ``{slug}-{agent}@{domain}``.

The slug is always passed IN, never looked up here. A rare provisioning
re-entry can leave an owner holding two slug rows, and
``company_slug_repo.get_by_owner`` uses ``Limit=1`` with no ordering, so
reading it back would be nondeterministic. Callers pass the slug they just
allocated.

Minting is best-effort by contract — ``provision_org`` calls it after
seeding Friday, and a mail-side failure must never undo a good provision.
"""

import logging
import re

from core.config import settings
from core.repositories import agent_email_repo

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _agent_slug(agent_name: str) -> str:
    slug = _SLUG_RE.sub("-", agent_name.strip().lower()).strip("-")
    return slug or "agent"


def build_agent_email_address(
    *, slug: str, agent_name: str, domain: str, is_primary: bool
) -> str:
    """``{slug}@{domain}`` for the primary agent, ``{slug}-{agent}@{domain}``
    otherwise. Deterministic and lowercased."""
    local = slug.strip().lower()
    if not is_primary:
        local = f"{local}-{_agent_slug(agent_name)}"
    return f"{local}@{domain.strip().lower()}"


async def mint_agent_email_address(
    *,
    owner_id: str,
    agent_id: str,
    agent_name: str,
    owner_email: str,
    slug: str,
    is_primary: bool = True,
) -> str | None:
    """Mint (or re-affirm) an agent's address row.

    Returns the address, or None when the feature is unconfigured, the owner
    has no slug, or minting failed. Never raises.
    """
    domain = settings.AGENT_EMAIL_DOMAIN.strip()
    if not domain:
        logger.debug("agent_email.mint: AGENT_EMAIL_DOMAIN unset; skipping %s", owner_id)
        return None

    try:
        if not slug or not slug.strip():
            logger.debug("agent_email.mint: no slug for owner %s; skipping", owner_id)
            return None

        address = build_agent_email_address(
            slug=slug, agent_name=agent_name, domain=domain, is_primary=is_primary
        )
        if await agent_email_repo.get_by_address(address) is not None:
            return address
        await agent_email_repo.put(
            address=address,
            owner_id=owner_id,
            agent_id=agent_id,
            verified_senders=[owner_email] if owner_email else [],
        )
        logger.info("agent_email.mint: minted %s for owner %s", address, owner_id)
        return address
    except Exception as e:  # noqa: BLE001 — minting must never break provisioning
        logger.warning("agent_email.mint: failed for owner %s: %s", owner_id, e)
        return None
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_agent_email.py -v --no-cov -p no:cacheprovider`
Expected: PASS (10 passed)

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/agent_email.py apps/backend/core/config.py apps/backend/tests/unit/services/test_agent_email.py
git commit -m "feat(agent-email): slug-derived address builder + idempotent mint"
```

---

### Task 3: CDK — `agent-email-addresses` table + IAM + stage wiring

**Files:**
- Modify: `apps/infra/lib/stacks/database-stack.ts` (public property near line 30; table beside the `companySlugsTable` block)
- Modify: `apps/infra/lib/stacks/service-stack.ts` (props interface near line 105; grant beside the `companySlugsTable` grant)
- Modify: `apps/infra/lib/isol8-stage.ts` and `apps/infra/lib/local-stage.ts` (both the service and observability wiring sites, beside `companySlugsTable`)
- Modify: `apps/infra/lib/stacks/observability-stack.ts` (props + BOTH table arrays, beside `companySlugs`)
- Test: `apps/infra/test/database-stack.test.ts`

**Interfaces:**
- Produces: DynamoDB table `isol8-${env}-agent-email-addresses`, PK `address` (S), GSI `by-owner` PK `owner_id` (S); read/write granted to the backend task role. This is the physical table Task 1's prefix-convention repo resolves to.

**Note:** `AGENT_EMAIL_DOMAIN` is deliberately **not** set here — the feature ships inert.

- [ ] **Step 1: Write the failing test**

Add to `apps/infra/test/database-stack.test.ts`. Place it beside the existing `company-slugs` table test so related assertions live together (that test was appended into an unrelated `describe` block — do not repeat that mistake; put yours immediately after it):

```typescript
  test("creates the agent-email-addresses table with a by-owner GSI", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: Match.stringLikeRegexp("agent-email-addresses$"),
      KeySchema: [{ AttributeName: "address", KeyType: "HASH" }],
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: "by-owner",
          KeySchema: [{ AttributeName: "owner_id", KeyType: "HASH" }],
        }),
      ]),
    });
  });
```

Run: `cd apps/infra && pnpm test -- database-stack`
Expected: FAIL — no table matching `agent-email-addresses`

- [ ] **Step 2: Create the table and wire the full CDK chain**

Mirror exactly how `companySlugsTable` is defined, granted, and wired across all five files — it is the same shape (PK + single-attribute by-owner GSI) and was added by the immediately preceding work, so it is the correct template to copy.

In `database-stack.ts`:

```typescript
  public readonly agentEmailAddressesTable: dynamodb.Table;
```

```typescript
    this.agentEmailAddressesTable = createTable(
      "AgentEmailAddressesTable",
      "agent-email-addresses",
      {
        partitionKey: { name: "address", type: dynamodb.AttributeType.STRING },
      },
    );
    this.agentEmailAddressesTable.addGlobalSecondaryIndex({
      indexName: "by-owner",
      partitionKey: { name: "owner_id", type: dynamodb.AttributeType.STRING },
    });
```

In `service-stack.ts` props interface:

```typescript
    agentEmailAddressesTable: dynamodb.Table;
```

and the grant (place it beside the `companySlugsTable` grant, and heed the IAM-size note in Global Constraints):

```typescript
    props.database.agentEmailAddressesTable.grantReadWriteData(this.taskRole);
```

In `isol8-stage.ts` and `local-stage.ts`, at BOTH the service-stack and observability-stack construction sites:

```typescript
        agentEmailAddressesTable: database.agentEmailAddressesTable,
```

In `observability-stack.ts` props:

```typescript
    agentEmailAddressesTable: dynamodb.ITable;
```

and both arrays:

```typescript
      ["agentEmailAddresses", props.databaseTables.agentEmailAddressesTable],
```

```typescript
      ["agent-email-addresses", props.databaseTables.agentEmailAddressesTable],
```

- [ ] **Step 3: Run the infra suite and synth**

Run: `cd apps/infra && pnpm test`
Expected: PASS except the known pre-existing `credential-export-decouple.test.ts` failure (oauth-state ARN export), which is unrelated — do not fix it.

Run: `cd apps/infra && npx cdk synth 2>&1 | tail -5`
Expected: completes with no TypeScript or missing-prop error

- [ ] **Step 4: Commit**

```bash
git add apps/infra/lib apps/infra/test/database-stack.test.ts
git commit -m "feat(agent-email): agent-email-addresses table + IAM + stage wiring"
```

---

### Task 4: Mint the address during `provision_org`

**Files:**
- Modify: `apps/backend/core/services/paperclip_provisioning.py` (import at module top; capture the slug at the existing `allocate_company_slug` call; mint just before the step-8 persist block)
- Test: `apps/backend/tests/unit/services/test_paperclip_provisioning.py`

**Interfaces:**
- Consumes: `mint_agent_email_address(*, owner_id, agent_id, agent_name, owner_email, slug, is_primary=True) -> str | None` (Task 2).
- Produces: no new symbols — `provision_org`'s signature and return type are unchanged.

**Key detail:** `provision_org` already calls `allocate_company_slug(...)` and currently **discards** its return value. Capture it into a local and pass it to the mint. Do NOT read the slug back via `company_slug_repo.get_by_owner` — see Global Constraints.

- [ ] **Step 1: Write the failing test**

Append to `apps/backend/tests/unit/services/test_paperclip_provisioning.py`, reusing that module's `_build_provision_org_doubles` helper and its already-imported `AsyncMock`:

```python
@pytest.mark.asyncio
async def test_provision_org_mints_agent_email_from_allocated_slug(monkeypatch):
    """The address is built from the slug we just allocated, not a lookup."""
    import core.services.paperclip_provisioning as mod
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)
    admin.create_agent = AsyncMock(
        side_effect=[{"id": "agent-friday"}, {"id": "agent-atlas"}]
    )

    async def fake_generate(*, org_id, owner_email):
        return "Acme Robotics"

    async def fake_allocate(*, owner_id, name):
        return "acme-robotics"

    minted = []

    async def fake_mint(*, owner_id, agent_id, agent_name, owner_email, slug, is_primary=True):
        minted.append(
            {
                "owner_id": owner_id,
                "agent_id": agent_id,
                "agent_name": agent_name,
                "owner_email": owner_email,
                "slug": slug,
                "is_primary": is_primary,
            }
        )
        return f"{slug}@agents.isol8.co"

    monkeypatch.setattr(mod, "generate_company_name", fake_generate)
    monkeypatch.setattr(mod, "allocate_company_slug", fake_allocate)
    monkeypatch.setattr(mod, "mint_agent_email_address", fake_mint)

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    await svc.provision_org(
        owner_id="org_xyz",
        owner_user_id="user_owner",
        owner_email="owner@example.com",
    )

    assert len(minted) == 1
    assert minted[0] == {
        "owner_id": "org_xyz",
        "agent_id": "agent-friday",
        "agent_name": "Friday",
        "owner_email": "owner@example.com",
        "slug": "acme-robotics",
        "is_primary": True,
    }


@pytest.mark.asyncio
async def test_provision_org_skips_mint_when_slug_allocation_failed(monkeypatch):
    """No slug means no address — but the provision still succeeds."""
    import core.services.paperclip_provisioning as mod
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)
    admin.create_agent = AsyncMock(
        side_effect=[{"id": "agent-friday"}, {"id": "agent-atlas"}]
    )

    async def fake_generate(*, org_id, owner_email):
        return "Acme Robotics"

    async def fake_allocate(*, owner_id, name):
        return None  # allocation failed

    minted = []

    async def fake_mint(**kwargs):
        minted.append(kwargs)
        return None

    monkeypatch.setattr(mod, "generate_company_name", fake_generate)
    monkeypatch.setattr(mod, "allocate_company_slug", fake_allocate)
    monkeypatch.setattr(mod, "mint_agent_email_address", fake_mint)

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    row = await svc.provision_org(
        owner_id="org_xyz",
        owner_user_id="user_owner",
        owner_email="owner@example.com",
    )

    assert row.status == "active"
    assert minted == []


@pytest.mark.asyncio
async def test_provision_org_survives_agent_email_mint_failure(monkeypatch):
    """A mail-side failure must NOT fail the provision."""
    import core.services.paperclip_provisioning as mod
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)
    admin.create_agent = AsyncMock(
        side_effect=[{"id": "agent-friday"}, {"id": "agent-atlas"}]
    )

    async def fake_generate(*, org_id, owner_email):
        return "Acme Robotics"

    async def fake_allocate(*, owner_id, name):
        return "acme-robotics"

    async def boom(**kwargs):
        raise RuntimeError("mail down")

    monkeypatch.setattr(mod, "generate_company_name", fake_generate)
    monkeypatch.setattr(mod, "allocate_company_slug", fake_allocate)
    monkeypatch.setattr(mod, "mint_agent_email_address", boom)

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    row = await svc.provision_org(
        owner_id="org_xyz",
        owner_user_id="user_owner",
        owner_email="owner@example.com",
    )

    assert row.status == "active"
    repo.put.assert_awaited()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_paperclip_provisioning.py -k "mints_agent_email or skips_mint or agent_email_mint_failure" -v --no-cov -p no:cacheprovider`
Expected: FAIL — `AttributeError: module 'core.services.paperclip_provisioning' has no attribute 'mint_agent_email_address'`

- [ ] **Step 3: Write minimal implementation**

Add to the module-top imports:

```python
from core.services.agent_email import mint_agent_email_address
```

Change the existing slug-allocation call to capture its result. It currently reads `await allocate_company_slug(owner_id=owner_id, name=company_name)` inside a `try`; make it assign, and initialise the local to `None` before the `try` so it is always bound:

```python
            company_slug: str | None = None
            try:
                company_slug = await allocate_company_slug(
                    owner_id=owner_id, name=company_name
                )
            except Exception as e:  # noqa: BLE001 — allocation must not fail provisioning
                logger.warning(
                    "paperclip_provisioning.provision_org: slug allocation failed "
                    "for owner_id %s: %s",
                    owner_id,
                    e,
                )
```

Then, immediately before the `# 8. Persist.` block, mint the address:

```python
            # Mint the agent's email address from the slug we just allocated.
            # Best-effort and guarded on both Friday and the slug existing —
            # the address is a convenience, and a mail-side failure must never
            # undo a successful provision (same posture as the Atlas seed).
            if friday_agent_id and company_slug:
                try:
                    await mint_agent_email_address(
                        owner_id=owner_id,
                        agent_id=friday_agent_id,
                        agent_name="Friday",
                        owner_email=owner_email,
                        slug=company_slug,
                    )
                except Exception as e:  # noqa: BLE001 — never fail the provision
                    logger.warning(
                        "paperclip_provisioning.provision_org: agent-email mint "
                        "failed for owner_id %s: %s",
                        owner_id,
                        e,
                    )
```

The inner `try/except` is deliberate belt-and-braces: `mint_agent_email_address` already swallows its own errors, but this block sits inside the provisioning `try` that calls `_mark_failed`, so a raise here would wrongly mark a healthy org failed.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_paperclip_provisioning.py -v --no-cov -p no:cacheprovider`
Expected: PASS (new cases plus all pre-existing cases in the module)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/paperclip_provisioning.py apps/backend/tests/unit/services/test_paperclip_provisioning.py
git commit -m "feat(agent-email): mint address from allocated slug during provision_org"
```

---

### Task 5: Publish the address in owner-facts

**Files:**
- Modify: `apps/backend/core/services/owner_facts.py` (`build_owner_facts` ~line 82; `write_owner_facts` ~line 93)
- Test: `apps/backend/tests/unit/services/test_owner_facts.py`

**Interfaces:**
- Consumes: `agent_email_repo.query_by_owner(owner_id) -> list[dict]` (Task 1).
- Produces: `isol8-facts.json` gains an `email_address` key — `str` when the owner has an active address, `None` otherwise.

- [ ] **Step 1: Write the failing test**

Add to `apps/backend/tests/unit/services/test_owner_facts.py`:

```python
def _facts_env(monkeypatch, owner_facts, rows, written):
    """Shared doubles: billing row, workspace capture, address rows."""

    class FakeWs:
        def write_file(self, user_id, path, content):
            written["content"] = content

    async def fake_billing(owner_id, *, consistent_read=False):
        return {"subscription_status": "active", "provider_choice": "bedrock_claude"}

    async def fake_query_by_owner(owner_id):
        if callable(rows):
            return rows(owner_id)
        return rows

    monkeypatch.setattr(owner_facts.billing_repo, "get_by_owner_id", fake_billing)
    monkeypatch.setattr(owner_facts, "get_workspace", lambda: FakeWs())
    monkeypatch.setattr(owner_facts.agent_email_repo, "query_by_owner", fake_query_by_owner)


@pytest.mark.asyncio
async def test_write_owner_facts_includes_email_address(monkeypatch):
    import json

    from core.services import owner_facts

    written = {}
    _facts_env(
        monkeypatch, owner_facts,
        [{"address": "acme@agents.isol8.co", "status": "active", "created_at": "2026-01-01T00:00:00Z"}],
        written,
    )

    await owner_facts.write_owner_facts("user_abc")

    assert json.loads(written["content"])["email_address"] == "acme@agents.isol8.co"


@pytest.mark.asyncio
async def test_write_owner_facts_ignores_alias_rows(monkeypatch):
    """An alias still routes, but it is not the address we advertise."""
    import json

    from core.services import owner_facts

    written = {}
    _facts_env(
        monkeypatch, owner_facts,
        [
            {"address": "old@agents.isol8.co", "status": "alias", "created_at": "2026-01-01T00:00:00Z"},
            {"address": "acme@agents.isol8.co", "status": "active", "created_at": "2026-02-01T00:00:00Z"},
        ],
        written,
    )

    await owner_facts.write_owner_facts("user_abc")

    assert json.loads(written["content"])["email_address"] == "acme@agents.isol8.co"


@pytest.mark.asyncio
async def test_write_owner_facts_picks_oldest_active_deterministically(monkeypatch):
    """Two active rows can exist after a rare re-provision; the choice must
    not depend on DynamoDB's arbitrary return order."""
    import json

    from core.services import owner_facts

    written = {}
    _facts_env(
        monkeypatch, owner_facts,
        [
            {"address": "second@agents.isol8.co", "status": "active", "created_at": "2026-03-01T00:00:00Z"},
            {"address": "first@agents.isol8.co", "status": "active", "created_at": "2026-01-01T00:00:00Z"},
        ],
        written,
    )

    await owner_facts.write_owner_facts("user_abc")

    assert json.loads(written["content"])["email_address"] == "first@agents.isol8.co"


@pytest.mark.asyncio
async def test_write_owner_facts_email_address_none_when_unminted(monkeypatch):
    import json

    from core.services import owner_facts

    written = {}
    _facts_env(monkeypatch, owner_facts, [], written)

    await owner_facts.write_owner_facts("user_abc")

    assert json.loads(written["content"])["email_address"] is None


@pytest.mark.asyncio
async def test_write_owner_facts_survives_email_lookup_failure(monkeypatch):
    import json

    from core.services import owner_facts

    written = {}

    def boom_rows(owner_id):
        raise RuntimeError("dynamo down")

    _facts_env(monkeypatch, owner_facts, boom_rows, written)

    await owner_facts.write_owner_facts("user_abc")

    facts = json.loads(written["content"])
    assert facts["email_address"] is None
    assert facts["plan"]  # the rest of the facts still wrote
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_owner_facts.py -k email -v --no-cov -p no:cacheprovider`
Expected: FAIL — `AttributeError: module 'core.services.owner_facts' has no attribute 'agent_email_repo'`

- [ ] **Step 3: Write minimal implementation**

In `apps/backend/core/services/owner_facts.py`, add the import beside the existing repo import:

```python
from core.repositories import agent_email_repo
```

Add a lookup helper above `write_owner_facts`:

```python
async def _lookup_email_address(owner_id: str) -> str | None:
    """The owner's advertised agent email, or None.

    Selects the ``active`` row (aliases still route but are not advertised)
    and breaks ties on the earliest ``created_at`` — a rare re-provision can
    leave two active rows, and DynamoDB's return order is arbitrary, so an
    unordered pick would make the agent's own address nondeterministic.
    Never raises: the address is a nicety and must not stop the rest of the
    facts from being written.
    """
    try:
        rows = await agent_email_repo.query_by_owner(owner_id)
    except Exception as e:  # noqa: BLE001 — facts must never block callers
        logger.warning("owner_facts: agent-email lookup failed for %s: %s", owner_id, e)
        return None
    active = [r for r in rows if r.get("status") == "active"]
    if not active:
        return None
    active.sort(key=lambda r: r.get("created_at") or "")
    return active[0].get("address")
```

Give `build_owner_facts` an `email_address` parameter and emit it:

```python
def build_owner_facts(owner_id, account, *, email_address: str | None = None) -> dict:
    # ... existing body unchanged (provider_choice / byo_provider resolution) ...
    return {
        "plan": plan,
        "subscription_status": status,
        "provider": format_provider_label(provider_choice, byo_provider),
        "provider_choice": provider_choice,
        "owner_kind": "org" if is_org_owner_id(owner_id) else "personal",
        "email_address": email_address,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
```

And thread it through the writer:

```python
        facts = build_owner_facts(
            owner_id, account, email_address=await _lookup_email_address(owner_id)
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_owner_facts.py -v --no-cov -p no:cacheprovider`
Expected: PASS (new cases plus all pre-existing — `email_address` defaults to `None`, so existing `build_owner_facts` callers are unaffected)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/owner_facts.py apps/backend/tests/unit/services/test_owner_facts.py
git commit -m "feat(agent-email): publish address in owner facts"
```

---

### Task 6: Full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Backend unit suite**

Run: `cd apps/backend && uv run pytest tests/unit -q --no-cov -p no:cacheprovider`
Expected: PASS, no regressions.

- [ ] **Step 2: Infra suite**

Run: `cd apps/infra && pnpm test`
Expected: PASS except the known pre-existing `credential-export-decouple.test.ts` failure.

- [ ] **Step 3: Confirm the feature is inert**

Run: `grep -rn "AGENT_EMAIL_DOMAIN" apps/infra/lib || echo "not set — feature inert as intended"`
Expected: `not set — feature inert as intended`

- [ ] **Step 4: Commit any lint fixups**

```bash
git add -A && git commit -m "chore(agent-email): lint fixups" || echo "nothing to commit"
```

---

## Done — What Ships

A deployed but **inert** foundation: the table exists, addresses are built from the company slug, `provision_org` mints for every new owner, and `owner_facts` publishes the address — all gated off because `AGENT_EMAIL_DOMAIN` is unset.

**Next:** verify the Resend receiving domain, set `AGENT_EMAIL_DOMAIN`, backfill existing owners, then build the inbound webhook + native `/hooks/agent` delivery and the outbound relay.
