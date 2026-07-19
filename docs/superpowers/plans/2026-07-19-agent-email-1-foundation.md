# Provisioned Agent Email — Plan 1: Foundation & Provisioning

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every newly provisioned owner gets a deterministic, durable agent-email address row (owner → Friday), and the agent learns its own address through owner-facts. No mail is sent or received yet.

**Architecture:** A new prefix-convention DynamoDB table `agent-email-addresses` (PK = `address`, GSI `by-owner`) maps a provisioned address to its `(owner_id, agent_id)` plus a verified-sender allowlist. A small `core/services/agent_email.py` builds a *deterministic* address from `(owner_id, agent_id, agent_name)` + the environment's mail domain, which makes minting naturally idempotent. `provision_org` mints best-effort right after Friday is seeded; `owner_facts` publishes the address into `isol8-facts.json` so the baked isol8 skill can tell the agent its own address.

**Tech Stack:** Python 3 / FastAPI backend (uv, pytest, pytest-asyncio, moto), AWS CDK v2 (TypeScript, jest), DynamoDB.

**Design spec:** `docs/superpowers/specs/2026-07-19-provisioned-agent-email-design.md`

## Global Constraints

- Mail domain comes from `settings.AGENT_EMAIL_DOMAIN`. **An empty value disables minting entirely.** Plan 1 deliberately does **not** set this in CDK — the machinery ships inert and Plan 2 flips it on once the Resend domain is verified.
- Minting is **idempotent** (deterministic address) and **best-effort**: a mail-side failure must never fail or undo `provision_org`. Friday surviving is more important than the address.
- The table uses the **prefix convention** (`core.dynamodb.get_table("agent-email-addresses")`), matching `channel_link_repo` — **no** table-name env var.
- All addresses and sender emails are stored and compared **lowercased**.
- A new DynamoDB table requires the full CDK chain: `database-stack.ts` → `service-stack.ts` (props + grant) → `isol8-stage.ts` **and** `local-stage.ts` → `observability-stack.ts`. Grants are explicit per-table; there is no wildcard.
- Run backend tests from `apps/backend` with `uv run pytest`; infra tests from `apps/infra` with `pnpm test`.
- Commit after each task.

**Deferred to Plan 2:** the backfill script for existing owners (pointless until `AGENT_EMAIL_DOMAIN` is live).

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
        address="Friday-AB12CD34EF@Agents.Isol8.co",
        owner_id="user_1",
        agent_id="agent_friday",
        verified_senders=["Owner@Example.com"],
    )

    row = await agent_email_repo.get_by_address("friday-ab12cd34ef@agents.isol8.co")
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
        address="friday-a@agents.isol8.co",
        owner_id="user_1",
        agent_id="agent_friday",
        verified_senders=[],
    )
    await agent_email_repo.put(
        address="atlas-b@agents.isol8.co",
        owner_id="user_1",
        agent_id="agent_atlas",
        verified_senders=[],
    )
    await agent_email_repo.put(
        address="friday-c@agents.isol8.co",
        owner_id="user_2",
        agent_id="agent_friday",
        verified_senders=[],
    )

    rows = await agent_email_repo.query_by_owner("user_1")
    assert {r["address"] for r in rows} == {
        "friday-a@agents.isol8.co",
        "atlas-b@agents.isol8.co",
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/repositories/test_agent_email_repo.py -v`
Expected: FAIL — `ModuleNotFoundError` / `ImportError: cannot import name 'agent_email_repo'`

- [ ] **Step 3: Write minimal implementation**

Create `apps/backend/core/repositories/agent_email_repo.py`:

```python
"""Address-book for provisioned agent email.

One row per provisioned agent email address. The address itself is the
partition key because inbound mail is routed by recipient; a ``by-owner`` GSI
lists an owner's addresses.

Prefix-convention table (``core.dynamodb.get_table``), matching
``channel_link_repo`` — the same shape of per-owner external-identity mapping.
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

Run: `cd apps/backend && uv run pytest tests/unit/repositories/test_agent_email_repo.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/repositories/agent_email_repo.py apps/backend/tests/unit/repositories/test_agent_email_repo.py
git commit -m "feat(agent-email): address-book repository"
```

---

### Task 2: `agent_email` service — deterministic address + idempotent mint

**Files:**
- Create: `apps/backend/core/services/agent_email.py`
- Modify: `apps/backend/core/config.py` (add `AGENT_EMAIL_DOMAIN` beside the other feature-config fields, e.g. near `MARKETPLACE_ARTIFACTS_BUCKET` around line 245)
- Test: `apps/backend/tests/unit/services/test_agent_email.py`

**Interfaces:**
- Consumes: `agent_email_repo.put`, `agent_email_repo.get_by_address` (Task 1); `settings.AGENT_EMAIL_DOMAIN`.
- Produces:
  - `build_agent_email_address(*, owner_id: str, agent_id: str, agent_name: str, domain: str) -> str`
  - `async mint_agent_email_address(*, owner_id: str, agent_id: str, agent_name: str, owner_email: str) -> str | None`

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/services/test_agent_email.py`:

```python
"""agent_email: deterministic address building + best-effort idempotent mint."""

import os

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import agent_email  # noqa: E402


def test_build_address_is_deterministic_and_lowercased():
    a = agent_email.build_agent_email_address(
        owner_id="user_1", agent_id="agent_friday", agent_name="Friday",
        domain="agents.isol8.co",
    )
    b = agent_email.build_agent_email_address(
        owner_id="user_1", agent_id="agent_friday", agent_name="Friday",
        domain="agents.isol8.co",
    )
    assert a == b
    assert a == a.lower()
    assert a.startswith("friday-")
    assert a.endswith("@agents.isol8.co")


def test_build_address_differs_per_owner_and_per_agent():
    base = dict(agent_name="Friday", domain="agents.isol8.co")
    a = agent_email.build_agent_email_address(owner_id="user_1", agent_id="agent_friday", **base)
    b = agent_email.build_agent_email_address(owner_id="user_2", agent_id="agent_friday", **base)
    c = agent_email.build_agent_email_address(owner_id="user_1", agent_id="agent_atlas", **base)
    assert len({a, b, c}) == 3


def test_build_address_slugifies_awkward_agent_names():
    addr = agent_email.build_agent_email_address(
        owner_id="user_1", agent_id="agent_x", agent_name="  Chief  Ops!! ",
        domain="agents.isol8.co",
    )
    assert addr.startswith("chief-ops-")


@pytest.mark.asyncio
async def test_mint_skips_when_domain_unset(monkeypatch):
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "")
    calls = []

    async def fake_put(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(agent_email.agent_email_repo, "put", fake_put)

    result = await agent_email.mint_agent_email_address(
        owner_id="user_1", agent_id="agent_friday",
        agent_name="Friday", owner_email="o@example.com",
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
        owner_id="user_1", agent_id="agent_friday",
        agent_name="Friday", owner_email="o@example.com",
    )

    assert address is not None and address.endswith("@agents.isol8.co")
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
        owner_id="user_1", agent_id="agent_friday",
        agent_name="Friday", owner_email="o@example.com",
    )
    assert address is not None
    assert puts == []


@pytest.mark.asyncio
async def test_mint_never_raises_on_repo_failure(monkeypatch):
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "agents.isol8.co")

    async def boom(*args, **kwargs):
        raise RuntimeError("dynamo down")

    monkeypatch.setattr(agent_email.agent_email_repo, "get_by_address", boom)

    result = await agent_email.mint_agent_email_address(
        owner_id="user_1", agent_id="agent_friday",
        agent_name="Friday", owner_email="o@example.com",
    )
    assert result is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_agent_email.py -v`
Expected: FAIL — `ImportError: cannot import name 'agent_email' from 'core.services'`

- [ ] **Step 3: Add the config field**

In `apps/backend/core/config.py`, alongside the other feature-config fields (immediately after `MARKETPLACE_ARTIFACTS_BUCKET`):

```python
    # Provisioned agent email (set by CDK ServiceStack). Empty disables address
    # minting entirely, so an env without a verified mail domain never mints.
    AGENT_EMAIL_DOMAIN: str = os.getenv("AGENT_EMAIL_DOMAIN", "")
```

- [ ] **Step 4: Write minimal implementation**

Create `apps/backend/core/services/agent_email.py`:

```python
"""Provisioned agent email addresses.

Builds the deterministic address for an (owner, agent) pair and mints its
address-book row. Deterministic means minting is naturally idempotent: the same
owner+agent always yields the same address, so a re-run re-affirms one row
instead of creating a second address.

Minting is best-effort by contract — ``provision_org`` calls it after seeding
Friday, and a mail-side failure must never undo a successful provision.
"""

import hashlib
import logging
import re

from core.config import settings
from core.repositories import agent_email_repo

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_TOKEN_LEN = 10


def _slug(agent_name: str) -> str:
    slug = _SLUG_RE.sub("-", agent_name.strip().lower()).strip("-")
    return slug or "agent"


def build_agent_email_address(
    *, owner_id: str, agent_id: str, agent_name: str, domain: str
) -> str:
    """``<agent-slug>-<token>@<domain>``.

    The token is a short digest of owner+agent, so the address is stable across
    re-provisions, unique per (owner, agent), and leaks neither identifier.
    """
    digest = hashlib.sha256(f"{owner_id}:{agent_id}".encode()).hexdigest()
    return f"{_slug(agent_name)}-{digest[:_TOKEN_LEN]}@{domain}".lower()


async def mint_agent_email_address(
    *, owner_id: str, agent_id: str, agent_name: str, owner_email: str
) -> str | None:
    """Mint (or re-affirm) an agent's address row.

    Returns the address, or None when the feature is unconfigured or minting
    failed. Never raises.
    """
    domain = settings.AGENT_EMAIL_DOMAIN.strip()
    if not domain:
        logger.debug("agent_email.mint: AGENT_EMAIL_DOMAIN unset; skipping %s", owner_id)
        return None

    try:
        address = build_agent_email_address(
            owner_id=owner_id, agent_id=agent_id, agent_name=agent_name, domain=domain
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

Run: `cd apps/backend && uv run pytest tests/unit/services/test_agent_email.py -v`
Expected: PASS (7 passed)

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/agent_email.py apps/backend/core/config.py apps/backend/tests/unit/services/test_agent_email.py
git commit -m "feat(agent-email): deterministic address builder + idempotent mint"
```

---

### Task 3: CDK — `agent-email-addresses` table + IAM + stage wiring

**Files:**
- Modify: `apps/infra/lib/stacks/database-stack.ts` (public property near line 30; table creation near the `channelLinksTable` block at line 154)
- Modify: `apps/infra/lib/stacks/service-stack.ts` (props interface near line 105; grant near line 556)
- Modify: `apps/infra/lib/isol8-stage.ts` (service-stack wiring near line 95; observability wiring near line 190)
- Modify: `apps/infra/lib/local-stage.ts` (same two places, near lines 97 and 189)
- Modify: `apps/infra/lib/stacks/observability-stack.ts` (props near line 59; arrays near lines 389 and 1548)
- Test: `apps/infra/test/database-stack.test.ts`

**Interfaces:**
- Consumes: nothing from earlier tasks (infra only).
- Produces: DynamoDB table `isol8-${env}-agent-email-addresses`, PK `address` (S), GSI `by-owner` PK `owner_id` (S); read/write granted to the backend task role. This is the physical table that Task 1's prefix-convention repo resolves to.

**Note:** `AGENT_EMAIL_DOMAIN` is deliberately **not** set here — the feature ships inert; Plan 2 sets it once the Resend domain is verified.

- [ ] **Step 1: Write the failing test**

Add to `apps/infra/test/database-stack.test.ts`, inside the existing top-level `describe` block (reuse the `template` that file already builds):

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

If `Match` is not already imported in that file, add it: `import { Match, Template } from "aws-cdk-lib/assertions";`

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/infra && pnpm test -- database-stack`
Expected: FAIL — no DynamoDB table matching `agent-email-addresses`

- [ ] **Step 3: Create the table in `database-stack.ts`**

Add the public property beside the other table declarations (near line 30):

```typescript
  public readonly agentEmailAddressesTable: dynamodb.Table;
```

Add the table immediately after the `channelLinksTable` block (line ~162):

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

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/infra && pnpm test -- database-stack`
Expected: PASS

- [ ] **Step 5: Wire the grant and stage plumbing**

In `apps/infra/lib/stacks/service-stack.ts`, add to the `database` props interface (beside `channelLinksTable: dynamodb.Table;`, line ~105):

```typescript
    agentEmailAddressesTable: dynamodb.Table;
```

And grant beside the `channelLinksTable` grant (line ~556):

```typescript
    props.database.agentEmailAddressesTable.grantReadWriteData(this.taskRole);
```

In `apps/infra/lib/isol8-stage.ts` (line ~95) and `apps/infra/lib/local-stage.ts` (line ~97), beside `channelLinksTable: database.channelLinksTable,`:

```typescript
        agentEmailAddressesTable: database.agentEmailAddressesTable,
```

In `apps/infra/lib/stacks/observability-stack.ts`, add to the `databaseTables` props interface (line ~59):

```typescript
    agentEmailAddressesTable: dynamodb.ITable;
```

and to both table arrays (lines ~389 and ~1548 respectively):

```typescript
      ["agentEmailAddresses", props.databaseTables.agentEmailAddressesTable],
```

```typescript
      ["agent-email-addresses", props.databaseTables.agentEmailAddressesTable],
```

Then pass it in both stage files where the observability stack is constructed (`isol8-stage.ts` line ~190, `local-stage.ts` line ~189):

```typescript
          agentEmailAddressesTable: database.agentEmailAddressesTable,
```

- [ ] **Step 6: Run the full infra suite and synth**

Run: `cd apps/infra && pnpm test`
Expected: PASS (all suites — the stage wiring must satisfy every stack's props)

Run: `cd apps/infra && npx cdk synth 2>&1 | tail -5`
Expected: synth completes without a TypeScript or missing-prop error

- [ ] **Step 7: Commit**

```bash
git add apps/infra/lib apps/infra/test/database-stack.test.ts
git commit -m "feat(agent-email): agent-email-addresses table + IAM + stage wiring"
```

---

### Task 4: Mint the address during `provision_org`

**Files:**
- Modify: `apps/backend/core/services/paperclip_provisioning.py` (import at module top; call just before the step-8 persist block, ~line 479)
- Test: `apps/backend/tests/unit/services/test_paperclip_provisioning.py`

**Interfaces:**
- Consumes: `mint_agent_email_address(*, owner_id, agent_id, agent_name, owner_email) -> str | None` (Task 2).
- Produces: no new symbols — `provision_org`'s existing contract is unchanged.

- [ ] **Step 1: Write the failing test**

Append these cases to `apps/backend/tests/unit/services/test_paperclip_provisioning.py`. They reuse that module's existing `_build_provision_org_doubles` helper (line 22) and its already-imported `AsyncMock` / `PaperclipApiError`:

```python
@pytest.mark.asyncio
async def test_provision_org_mints_agent_email_for_friday(monkeypatch):
    """A successful provision mints exactly one address, for the seeded Friday."""
    import core.services.paperclip_provisioning as mod
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)
    admin.create_agent = AsyncMock(
        side_effect=[{"id": "agent-friday"}, {"id": "agent-atlas"}]
    )

    minted = []

    async def fake_mint(*, owner_id, agent_id, agent_name, owner_email):
        minted.append(
            {
                "owner_id": owner_id,
                "agent_id": agent_id,
                "agent_name": agent_name,
                "owner_email": owner_email,
            }
        )
        return "friday-abc1234567@agents.isol8.co"

    monkeypatch.setattr(mod, "mint_agent_email_address", fake_mint)

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    await svc.provision_org(
        owner_id="org_xyz",
        owner_user_id="user_owner",
        owner_email="owner@example.com",
    )

    # The address is keyed by owner_id (the container key), NOT owner_user_id.
    assert len(minted) == 1
    assert minted[0] == {
        "owner_id": "org_xyz",
        "agent_id": "agent-friday",
        "agent_name": "Friday",
        "owner_email": "owner@example.com",
    }


@pytest.mark.asyncio
async def test_provision_org_survives_agent_email_mint_failure(monkeypatch):
    """Defense-in-depth: a mail-side failure must NOT fail the provision."""
    import core.services.paperclip_provisioning as mod
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)
    admin.create_agent = AsyncMock(
        side_effect=[{"id": "agent-friday"}, {"id": "agent-atlas"}]
    )

    async def boom(**kwargs):
        raise RuntimeError("mail down")

    monkeypatch.setattr(mod, "mint_agent_email_address", boom)

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    row = await svc.provision_org(
        owner_id="org_xyz",
        owner_user_id="user_owner",
        owner_email="owner@example.com",
    )

    assert row.status == "active"
    repo.put.assert_awaited()


@pytest.mark.asyncio
async def test_provision_org_skips_mint_when_friday_seed_failed(monkeypatch):
    """No Friday means no agent to own an address, so nothing is minted."""
    import core.services.paperclip_provisioning as mod
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)
    # Friday fails, Atlas succeeds — mirrors the existing unparented-Atlas case.
    admin.create_agent = AsyncMock(
        side_effect=[PaperclipApiError("boom"), {"id": "agent-atlas"}]
    )

    minted = []

    async def fake_mint(**kwargs):
        minted.append(kwargs)

    monkeypatch.setattr(mod, "mint_agent_email_address", fake_mint)

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    await svc.provision_org(
        owner_id="org_xyz",
        owner_user_id="user_owner",
        owner_email="owner@example.com",
    )

    assert minted == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_paperclip_provisioning.py -k "agent_email or mint" -v`
Expected: FAIL — `AttributeError: module 'core.services.paperclip_provisioning' has no attribute 'mint_agent_email_address'`

- [ ] **Step 3: Write minimal implementation**

Add the import near the other `core.services` imports at the top of `apps/backend/core/services/paperclip_provisioning.py`:

```python
from core.services.agent_email import mint_agent_email_address
```

Insert the mint immediately before the `# 8. Persist.` block (~line 479), after Friday and Atlas are seeded:

```python
            # Mint the agent's email address. Best-effort and guarded on Friday
            # existing: the address is a convenience, and a mail-side failure
            # must never undo a successful provision (same posture as Atlas).
            if friday_agent_id:
                try:
                    await mint_agent_email_address(
                        owner_id=owner_id,
                        agent_id=friday_agent_id,
                        agent_name="Friday",
                        owner_email=owner_email,
                    )
                except Exception as e:  # noqa: BLE001 — never fail the provision
                    logger.warning(
                        "paperclip_provisioning.provision_org: agent-email mint "
                        "failed for owner_id %s: %s",
                        owner_id,
                        e,
                    )
```

The inner `try/except` is deliberate belt-and-braces: `mint_agent_email_address` already swallows its own errors, but this whole block sits inside the provisioning `try` that calls `_mark_failed`, so a raise here would wrongly mark a healthy org failed.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_paperclip_provisioning.py -v`
Expected: PASS (new cases plus all pre-existing cases in the module)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/paperclip_provisioning.py apps/backend/tests/unit/services/test_paperclip_provisioning.py
git commit -m "feat(agent-email): mint address during provision_org"
```

---

### Task 5: Publish the address in owner-facts

**Files:**
- Modify: `apps/backend/core/services/owner_facts.py` (`build_owner_facts` ~line 82; `write_owner_facts` ~line 93)
- Test: `apps/backend/tests/unit/services/test_owner_facts.py`

**Interfaces:**
- Consumes: `agent_email_repo.query_by_owner(owner_id) -> list[dict]` (Task 1).
- Produces: `isol8-facts.json` gains an `email_address` key — `str` when the owner has an address, `None` otherwise. The baked isol8 skill reads this in Plan 2.

- [ ] **Step 1: Write the failing test**

Add to `apps/backend/tests/unit/services/test_owner_facts.py`:

```python
@pytest.mark.asyncio
async def test_write_owner_facts_includes_email_address(monkeypatch):
    import json

    from core.services import owner_facts

    written = {}

    class FakeWs:
        def write_file(self, user_id, path, content):
            written["content"] = content

    async def fake_billing(owner_id, *, consistent_read=False):
        return {"subscription_status": "active", "provider_choice": "bedrock_claude"}

    async def fake_query_by_owner(owner_id):
        return [{"address": "friday-abc123@agents.isol8.co", "status": "active"}]

    monkeypatch.setattr(owner_facts.billing_repo, "get_by_owner_id", fake_billing)
    monkeypatch.setattr(owner_facts, "get_workspace", lambda: FakeWs())
    monkeypatch.setattr(
        owner_facts.agent_email_repo, "query_by_owner", fake_query_by_owner
    )

    await owner_facts.write_owner_facts("user_abc")

    facts = json.loads(written["content"])
    assert facts["email_address"] == "friday-abc123@agents.isol8.co"


@pytest.mark.asyncio
async def test_write_owner_facts_email_address_none_when_unminted(monkeypatch):
    import json

    from core.services import owner_facts

    written = {}

    class FakeWs:
        def write_file(self, user_id, path, content):
            written["content"] = content

    async def fake_billing(owner_id, *, consistent_read=False):
        return {"subscription_status": "active", "provider_choice": "bedrock_claude"}

    async def fake_query_by_owner(owner_id):
        return []

    monkeypatch.setattr(owner_facts.billing_repo, "get_by_owner_id", fake_billing)
    monkeypatch.setattr(owner_facts, "get_workspace", lambda: FakeWs())
    monkeypatch.setattr(
        owner_facts.agent_email_repo, "query_by_owner", fake_query_by_owner
    )

    await owner_facts.write_owner_facts("user_abc")

    facts = json.loads(written["content"])
    assert facts["email_address"] is None


@pytest.mark.asyncio
async def test_write_owner_facts_survives_email_lookup_failure(monkeypatch):
    import json

    from core.services import owner_facts

    written = {}

    class FakeWs:
        def write_file(self, user_id, path, content):
            written["content"] = content

    async def fake_billing(owner_id, *, consistent_read=False):
        return {"subscription_status": "active", "provider_choice": "bedrock_claude"}

    async def boom(owner_id):
        raise RuntimeError("dynamo down")

    monkeypatch.setattr(owner_facts.billing_repo, "get_by_owner_id", fake_billing)
    monkeypatch.setattr(owner_facts, "get_workspace", lambda: FakeWs())
    monkeypatch.setattr(owner_facts.agent_email_repo, "query_by_owner", boom)

    await owner_facts.write_owner_facts("user_abc")

    facts = json.loads(written["content"])
    assert facts["email_address"] is None
    assert facts["plan"]  # the rest of the facts still wrote
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_owner_facts.py -k email -v`
Expected: FAIL — `AttributeError: module 'core.services.owner_facts' has no attribute 'agent_email_repo'`

- [ ] **Step 3: Write minimal implementation**

In `apps/backend/core/services/owner_facts.py`, add the import beside the existing repo import:

```python
from core.repositories import agent_email_repo
```

Add a lookup helper above `write_owner_facts`:

```python
async def _lookup_email_address(owner_id: str) -> str | None:
    """The owner's provisioned agent email, or None. Never raises — the address
    is a nicety and must not stop the rest of the facts from being written."""
    try:
        rows = await agent_email_repo.query_by_owner(owner_id)
    except Exception as e:  # noqa: BLE001 — facts must never block callers
        logger.warning("owner_facts: agent-email lookup failed for %s: %s", owner_id, e)
        return None
    for row in rows:
        if row.get("status") == "active":
            return row.get("address")
    return None
```

Give `build_owner_facts` an `email_address` parameter and emit it in the dict:

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

Run: `cd apps/backend && uv run pytest tests/unit/services/test_owner_facts.py -v`
Expected: PASS (new cases plus all pre-existing cases — `email_address` defaults to `None`, so existing `build_owner_facts` callers and assertions are unaffected)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/owner_facts.py apps/backend/tests/unit/services/test_owner_facts.py
git commit -m "feat(agent-email): publish address in owner facts"
```

---

### Task 6: Full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Run the backend unit suite**

Run: `cd apps/backend && uv run pytest tests/unit -q`
Expected: PASS, no regressions. If anything fails, it is almost certainly this change — these are dependent systems.

- [ ] **Step 2: Run the infra suite**

Run: `cd apps/infra && pnpm test`
Expected: PASS

- [ ] **Step 3: Confirm the feature is inert**

Verify `AGENT_EMAIL_DOMAIN` is set nowhere in `apps/infra/lib`:

Run: `grep -rn "AGENT_EMAIL_DOMAIN" apps/infra/lib || echo "not set — feature inert as intended"`
Expected: `not set — feature inert as intended`

- [ ] **Step 4: Commit any lint fixups**

```bash
git add -A
git commit -m "chore(agent-email): lint fixups" || echo "nothing to commit"
```

---

## Plan 1 Done — What Ships

A deployed but **inert** foundation: the table exists, the backend can mint and read addresses, `provision_org` mints for every new owner, and `owner_facts` publishes the address — all gated off because `AGENT_EMAIL_DOMAIN` is unset.

**Plan 2 (Inbound)** then: verify the Resend receiving domain, set `AGENT_EMAIL_DOMAIN` in CDK (which switches minting on), add the backfill script for existing owners, build the `email.received` webhook router with signature verification + sender allowlist + cap gating, deliver into the container via the native `/hooks/agent`, and teach the baked isol8 skill about the address.
