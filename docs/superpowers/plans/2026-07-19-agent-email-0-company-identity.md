# Company Identity — Implementation Plan (prerequisite for agent email)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every provisioned owner gets a real, auto-generated, editable company name plus a platform-unique slug — the identifier the agent's email address will later be built from.

**Architecture:** A new prefix-convention DynamoDB table `company-slugs` (PK = `slug`) makes reservation a *conditional put* — the only atomic "claim this name" primitive DynamoDB offers — with a `by-owner` GSI for reverse lookup. `core/services/company_identity.py` generates a sensible initial name (Clerk org name for orgs, prettified email local part for personal owners) and reserves a slug derived from it, appending a numeric suffix on collision. `provision_org` uses that name instead of today's `name=owner_email`.

**Tech Stack:** Python 3 / FastAPI backend (uv, pytest, pytest-asyncio, moto), AWS CDK v2 (TypeScript, jest), DynamoDB.

**Design spec:** `docs/superpowers/specs/2026-07-19-provisioned-agent-email-design.md` §6.3a

## Global Constraints

- **The slug is derived from the name ONCE, at provisioning, then held independently.** Renaming the company does **not** move the slug — and therefore never moves an email address derived from it. A slug is an identifier other people have been given, not a label. (Changing the *address* is a separate, deliberate action, out of scope here.)
- Slug reservation MUST be atomic — a conditional put, never read-then-write. Two owners racing on "Acme" must not both win.
- Re-reserving a slug you already hold is a **success**, not a collision (provisioning is idempotent and may re-run).
- Name generation and slug reservation are **best-effort**: a failure must never fail `provision_org`. Falling back to today's behaviour (name = owner email, no slug) is acceptable; losing the org is not.
- Slugs are lowercase, `[a-z0-9-]` only, max 32 chars, and may not be one of the reserved words (they will become email local parts).
- Table uses the **prefix convention** (`core.dynamodb.get_table("company-slugs")`), matching `channel-links` — no table-name env var.
- A new DynamoDB table requires the full CDK chain: `database-stack.ts` → `service-stack.ts` (props + grant) → `isol8-stage.ts` **and** `local-stage.ts` → `observability-stack.ts`. Grants are explicit per-table; there is no wildcard.
- Backend tests: `cd apps/backend && uv run pytest`. Infra tests: `cd apps/infra && pnpm test`.
- Commit after each task.

---

### Task 1: `company-slugs` table + reservation repository

**Files:**
- Create: `apps/backend/core/repositories/company_slug_repo.py`
- Test: `apps/backend/tests/unit/repositories/test_company_slug_repo.py`
- Modify: `apps/infra/lib/stacks/database-stack.ts` (public property near line 30; table near the `channelLinksTable` block at line 154)
- Modify: `apps/infra/lib/stacks/service-stack.ts` (props interface near line 105; grant near line 556)
- Modify: `apps/infra/lib/isol8-stage.ts` (service wiring near line 95; observability wiring near line 190)
- Modify: `apps/infra/lib/local-stage.ts` (same two places, near lines 97 and 189)
- Modify: `apps/infra/lib/stacks/observability-stack.ts` (props near line 59; arrays near lines 389 and 1548)
- Test: `apps/infra/test/database-stack.test.ts`

**Interfaces:**
- Consumes: `core.dynamodb.get_table`, `run_in_thread`, `utc_now_iso` (existing).
- Produces:
  - `normalize(value: str) -> str`
  - `async reserve(*, slug: str, owner_id: str) -> bool` — True when claimed (or already yours), False when held by someone else
  - `async get_by_slug(slug: str) -> dict | None`
  - `async get_by_owner(owner_id: str) -> dict | None`
  - Item shape: `{slug, owner_id, created_at}`
  - Physical table `isol8-${env}-company-slugs`, PK `slug` (S), GSI `by-owner` PK `owner_id` (S)

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/repositories/test_company_slug_repo.py`:

```python
"""company_slug_repo: atomic slug reservation via conditional put."""

import os
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


@pytest.fixture
def slug_table():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        table = resource.create_table(
            TableName="test-company-slugs",
            KeySchema=[{"AttributeName": "slug", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "slug", "AttributeType": "S"},
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
        table.meta.client.get_waiter("table_exists").wait(TableName="test-company-slugs")
        with (
            patch("core.dynamodb._table_prefix", "test-"),
            patch("core.dynamodb._dynamodb_resource", resource),
        ):
            yield table


@pytest.mark.asyncio
async def test_reserve_claims_a_free_slug(slug_table):
    from core.repositories import company_slug_repo

    assert await company_slug_repo.reserve(slug="Acme", owner_id="user_1") is True

    row = await company_slug_repo.get_by_slug("acme")
    assert row is not None
    assert row["owner_id"] == "user_1"


@pytest.mark.asyncio
async def test_reserve_rejects_a_slug_held_by_another_owner(slug_table):
    from core.repositories import company_slug_repo

    assert await company_slug_repo.reserve(slug="acme", owner_id="user_1") is True
    assert await company_slug_repo.reserve(slug="acme", owner_id="user_2") is False

    row = await company_slug_repo.get_by_slug("acme")
    assert row["owner_id"] == "user_1"  # original holder untouched


@pytest.mark.asyncio
async def test_reserve_is_idempotent_for_the_same_owner(slug_table):
    """Provisioning can re-run; re-claiming your own slug is a success."""
    from core.repositories import company_slug_repo

    assert await company_slug_repo.reserve(slug="acme", owner_id="user_1") is True
    assert await company_slug_repo.reserve(slug="acme", owner_id="user_1") is True


@pytest.mark.asyncio
async def test_get_by_slug_missing_returns_none(slug_table):
    from core.repositories import company_slug_repo

    assert await company_slug_repo.get_by_slug("nobody") is None


@pytest.mark.asyncio
async def test_get_by_owner_returns_the_owners_slug(slug_table):
    from core.repositories import company_slug_repo

    await company_slug_repo.reserve(slug="acme", owner_id="user_1")
    await company_slug_repo.reserve(slug="other", owner_id="user_2")

    row = await company_slug_repo.get_by_owner("user_1")
    assert row is not None
    assert row["slug"] == "acme"
    assert await company_slug_repo.get_by_owner("user_missing") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/repositories/test_company_slug_repo.py -v`
Expected: FAIL — `ImportError: cannot import name 'company_slug_repo'`

- [ ] **Step 3: Write minimal implementation**

Create `apps/backend/core/repositories/company_slug_repo.py`:

```python
"""Platform-unique company slugs.

One row per reserved slug, with the slug as the partition key — which makes
reservation a *conditional put*, the only atomic "claim this name" primitive
DynamoDB offers. Read-then-write would let two owners race onto the same
slug. A ``by-owner`` GSI answers the reverse question.

Prefix-convention table (``core.dynamodb.get_table``), matching
``channel_link_repo``.
"""

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from core.dynamodb import get_table, run_in_thread, utc_now_iso


def _get_table():
    return get_table("company-slugs")


def normalize(value: str) -> str:
    return value.strip().lower()


async def reserve(*, slug: str, owner_id: str) -> bool:
    """Claim ``slug`` for ``owner_id``.

    Returns True when the slug is now held by this owner — including the
    case where they already held it, since provisioning is idempotent and
    may re-run. Returns False when another owner holds it.
    """
    table = _get_table()
    item = {
        "slug": normalize(slug),
        "owner_id": owner_id,
        "created_at": utc_now_iso(),
    }
    try:
        await run_in_thread(
            table.put_item,
            Item=item,
            ConditionExpression="attribute_not_exists(slug) OR owner_id = :owner",
            ExpressionAttributeValues={":owner": owner_id},
        )
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


async def get_by_slug(slug: str) -> dict | None:
    table = _get_table()
    response = await run_in_thread(table.get_item, Key={"slug": normalize(slug)})
    return response.get("Item")


async def get_by_owner(owner_id: str) -> dict | None:
    table = _get_table()
    response = await run_in_thread(
        table.query,
        IndexName="by-owner",
        KeyConditionExpression=Key("owner_id").eq(owner_id),
        Limit=1,
    )
    items = response.get("Items", [])
    return items[0] if items else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/repositories/test_company_slug_repo.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Add the CDK table test**

Add to `apps/infra/test/database-stack.test.ts`, inside the existing top-level `describe` block (reuse the `template` that file already builds; add `Match` to the `aws-cdk-lib/assertions` import if absent):

```typescript
  test("creates the company-slugs table with a by-owner GSI", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: Match.stringLikeRegexp("company-slugs$"),
      KeySchema: [{ AttributeName: "slug", KeyType: "HASH" }],
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
Expected: FAIL — no table matching `company-slugs`

- [ ] **Step 6: Create the table and wire the full CDK chain**

In `apps/infra/lib/stacks/database-stack.ts`, add the public property beside the other tables (near line 30):

```typescript
  public readonly companySlugsTable: dynamodb.Table;
```

and the table immediately after the `channelLinksTable` block (line ~162):

```typescript
    this.companySlugsTable = createTable("CompanySlugsTable", "company-slugs", {
      partitionKey: { name: "slug", type: dynamodb.AttributeType.STRING },
    });
    this.companySlugsTable.addGlobalSecondaryIndex({
      indexName: "by-owner",
      partitionKey: { name: "owner_id", type: dynamodb.AttributeType.STRING },
    });
```

In `apps/infra/lib/stacks/service-stack.ts`, add to the `database` props interface (beside `channelLinksTable: dynamodb.Table;`, line ~105):

```typescript
    companySlugsTable: dynamodb.Table;
```

and grant beside the `channelLinksTable` grant (line ~556):

```typescript
    props.database.companySlugsTable.grantReadWriteData(this.taskRole);
```

In `apps/infra/lib/isol8-stage.ts` (line ~95) and `apps/infra/lib/local-stage.ts` (line ~97), beside `channelLinksTable: database.channelLinksTable,`:

```typescript
        companySlugsTable: database.companySlugsTable,
```

In `apps/infra/lib/stacks/observability-stack.ts`, add to the `databaseTables` props interface (line ~59):

```typescript
    companySlugsTable: dynamodb.ITable;
```

and to both table arrays (lines ~389 and ~1548):

```typescript
      ["companySlugs", props.databaseTables.companySlugsTable],
```

```typescript
      ["company-slugs", props.databaseTables.companySlugsTable],
```

Then pass it where the observability stack is constructed in both stage files (`isol8-stage.ts` line ~190, `local-stage.ts` line ~189):

```typescript
          companySlugsTable: database.companySlugsTable,
```

- [ ] **Step 7: Run the infra suite and synth**

Run: `cd apps/infra && pnpm test`
Expected: PASS (all suites — stage wiring must satisfy every stack's props)

Run: `cd apps/infra && npx cdk synth 2>&1 | tail -5`
Expected: synth completes with no TypeScript or missing-prop error

- [ ] **Step 8: Commit**

```bash
git add apps/backend/core/repositories/company_slug_repo.py apps/backend/tests/unit/repositories/test_company_slug_repo.py apps/infra/lib apps/infra/test/database-stack.test.ts
git commit -m "feat(company-identity): company-slugs table + atomic reservation repo"
```

---

### Task 2: `company_identity` service — name generation + slug allocation

**Files:**
- Create: `apps/backend/core/services/company_identity.py`
- Test: `apps/backend/tests/unit/services/test_company_identity.py`

**Interfaces:**
- Consumes: `company_slug_repo.reserve`, `company_slug_repo.get_by_owner` (Task 1); `core.services.clerk_admin.get_organization(org_id) -> dict | None` (existing — returns the raw Clerk org JSON including `name`, and returns None on missing key / 404 / network error).
- Produces:
  - `slugify(value: str) -> str`
  - `prettify_email_local_part(email: str) -> str`
  - `async generate_company_name(*, org_id: str | None, owner_email: str) -> str`
  - `async allocate_company_slug(*, owner_id: str, name: str) -> str | None`

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/services/test_company_identity.py`:

```python
"""company_identity: initial name generation + atomic slug allocation."""

import os

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import company_identity  # noqa: E402


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Acme Robotics", "acme-robotics"),
        ("  Acme   Robotics!!  ", "acme-robotics"),
        ("Ünicode Ço", "nicode-o"),
        ("---", ""),
        ("A" * 60, "a" * 32),
    ],
)
def test_slugify(raw, expected):
    assert company_identity.slugify(raw) == expected


@pytest.mark.parametrize(
    "email,expected",
    [
        ("jane.doe@gmail.com", "Jane Doe"),
        ("jane.doe+tag@gmail.com", "Jane Doe"),
        ("jane_doe-smith@x.io", "Jane Doe Smith"),
        ("j@x.io", "J"),
    ],
)
def test_prettify_email_local_part(email, expected):
    assert company_identity.prettify_email_local_part(email) == expected


@pytest.mark.asyncio
async def test_generate_name_uses_clerk_org_name_for_orgs(monkeypatch):
    async def fake_get_org(org_id):
        return {"id": org_id, "name": "Acme Robotics", "slug": "acme-robotics"}

    monkeypatch.setattr(company_identity.clerk_admin, "get_organization", fake_get_org)

    name = await company_identity.generate_company_name(
        org_id="org_123", owner_email="owner@example.com"
    )
    assert name == "Acme Robotics"


@pytest.mark.asyncio
async def test_generate_name_falls_back_to_email_when_clerk_returns_nothing(monkeypatch):
    async def fake_get_org(org_id):
        return None

    monkeypatch.setattr(company_identity.clerk_admin, "get_organization", fake_get_org)

    name = await company_identity.generate_company_name(
        org_id="org_123", owner_email="jane.doe@example.com"
    )
    assert name == "Jane Doe"


@pytest.mark.asyncio
async def test_generate_name_for_personal_owner_uses_email(monkeypatch):
    """A personal owner has no org, so Clerk is never consulted."""
    called = []

    async def fake_get_org(org_id):
        called.append(org_id)
        return {"name": "should not be used"}

    monkeypatch.setattr(company_identity.clerk_admin, "get_organization", fake_get_org)

    name = await company_identity.generate_company_name(
        org_id=None, owner_email="jane.doe@example.com"
    )
    assert name == "Jane Doe"
    assert called == []


@pytest.mark.asyncio
async def test_allocate_slug_claims_the_plain_slug_when_free(monkeypatch):
    attempts = []

    async def fake_reserve(*, slug, owner_id):
        attempts.append(slug)
        return True

    monkeypatch.setattr(company_identity.company_slug_repo, "reserve", fake_reserve)

    slug = await company_identity.allocate_company_slug(
        owner_id="user_1", name="Acme Robotics"
    )
    assert slug == "acme-robotics"
    assert attempts == ["acme-robotics"]


@pytest.mark.asyncio
async def test_allocate_slug_suffixes_on_collision(monkeypatch):
    async def fake_reserve(*, slug, owner_id):
        return slug == "acme-3"

    monkeypatch.setattr(company_identity.company_slug_repo, "reserve", fake_reserve)

    slug = await company_identity.allocate_company_slug(owner_id="user_1", name="Acme")
    assert slug == "acme-3"


@pytest.mark.asyncio
async def test_allocate_slug_avoids_reserved_words(monkeypatch):
    """Reserved local parts (support@, admin@…) must never be handed out."""
    attempts = []

    async def fake_reserve(*, slug, owner_id):
        attempts.append(slug)
        return True

    monkeypatch.setattr(company_identity.company_slug_repo, "reserve", fake_reserve)

    slug = await company_identity.allocate_company_slug(owner_id="user_1", name="Support")
    assert slug != "support"
    assert attempts[0] != "support"


@pytest.mark.asyncio
async def test_allocate_slug_falls_back_when_name_slugifies_to_empty(monkeypatch):
    async def fake_reserve(*, slug, owner_id):
        return True

    monkeypatch.setattr(company_identity.company_slug_repo, "reserve", fake_reserve)

    slug = await company_identity.allocate_company_slug(owner_id="user_1", name="---")
    assert slug
    assert company_identity.slugify(slug) == slug


@pytest.mark.asyncio
async def test_allocate_slug_returns_none_when_repo_fails(monkeypatch):
    """Slug allocation is best-effort — provisioning must survive it."""

    async def boom(**kwargs):
        raise RuntimeError("dynamo down")

    monkeypatch.setattr(company_identity.company_slug_repo, "reserve", boom)

    assert await company_identity.allocate_company_slug(owner_id="user_1", name="Acme") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_company_identity.py -v`
Expected: FAIL — `ImportError: cannot import name 'company_identity' from 'core.services'`

- [ ] **Step 3: Write minimal implementation**

Create `apps/backend/core/services/company_identity.py`:

```python
"""Company display name + platform-unique slug.

The name is auto-generated at provisioning and editable afterwards (via
``PATCH /teams/settings``). The slug is derived from that name ONCE, at
provisioning, and then held independently: renaming the company does NOT
move the slug, because the slug becomes an identifier other people have
been given (it is the local part of the agent's email address), not a
label. Changing the address is a separate, deliberate action.

Both operations are best-effort — ``provision_org`` must survive either
failing.
"""

import logging
import re
import unicodedata

from core.repositories import company_slug_repo
from core.services import clerk_admin

logger = logging.getLogger(__name__)

_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")
MAX_SLUG_LEN = 32
_MAX_SUFFIX_ATTEMPTS = 50
_FALLBACK_SLUG = "company"

# These become email local parts, so they must never be handed to a tenant.
RESERVED_SLUGS = frozenset(
    {
        "abuse", "admin", "administrator", "billing", "contact", "help",
        "hostmaster", "info", "isol8", "mail", "no-reply", "noreply",
        "postmaster", "root", "security", "support", "team", "webmaster",
        "www",
    }
)


def slugify(value: str) -> str:
    """Lowercase ``[a-z0-9-]`` form, capped at ``MAX_SLUG_LEN``.

    Non-ASCII is decomposed and stripped rather than transliterated — the
    result has to be a legal email local part, not a faithful rendering.
    """
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_STRIP_RE.sub("-", ascii_only.lower()).strip("-")
    return slug[:MAX_SLUG_LEN].strip("-")


def prettify_email_local_part(email: str) -> str:
    """``jane.doe+tag@gmail.com`` -> ``Jane Doe``."""
    local = email.split("@", 1)[0]
    local = local.split("+", 1)[0]
    words = [w for w in re.split(r"[._\-]+", local) if w]
    return " ".join(w.capitalize() for w in words) or local


async def generate_company_name(*, org_id: str | None, owner_email: str) -> str:
    """The initial company name.

    Org owners get their Clerk organization's real name; personal owners
    get a prettified form of their email local part. Never raises — a
    Clerk hiccup falls back to the email.
    """
    if org_id:
        try:
            org = await clerk_admin.get_organization(org_id)
        except Exception as e:  # noqa: BLE001 — naming must not block provisioning
            logger.warning("company_identity: Clerk org lookup failed for %s: %s", org_id, e)
            org = None
        if org:
            name = (org.get("name") or "").strip()
            if name:
                return name
    return prettify_email_local_part(owner_email)


async def allocate_company_slug(*, owner_id: str, name: str) -> str | None:
    """Reserve a unique slug derived from ``name``.

    Returns the reserved slug, or None if allocation failed. Collisions and
    reserved words take a numeric suffix. Never raises.
    """
    base = slugify(name) or _FALLBACK_SLUG
    try:
        for attempt in range(1, _MAX_SUFFIX_ATTEMPTS + 1):
            candidate = base if attempt == 1 else f"{base}-{attempt}"
            # A reserved word is skipped, not failed: "support" becomes
            # "support-2", which is a fine tenant slug.
            if candidate in RESERVED_SLUGS:
                continue
            if await company_slug_repo.reserve(slug=candidate, owner_id=owner_id):
                return candidate
        logger.warning(
            "company_identity: no free slug for owner %s after %d attempts (base=%s)",
            owner_id, _MAX_SUFFIX_ATTEMPTS, base,
        )
        return None
    except Exception as e:  # noqa: BLE001 — allocation must not block provisioning
        logger.warning("company_identity: slug allocation failed for %s: %s", owner_id, e)
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_company_identity.py -v`
Expected: PASS (all cases)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/company_identity.py apps/backend/tests/unit/services/test_company_identity.py
git commit -m "feat(company-identity): name generation + atomic slug allocation"
```

---

### Task 3: Use the generated name and slug in `provision_org`

**Files:**
- Modify: `apps/backend/core/services/paperclip_provisioning.py` (import at module top; name generation before the `create_company` block at ~line 322; slug allocation after `company_id` is known at ~line 345)
- Test: `apps/backend/tests/unit/services/test_paperclip_provisioning.py`

**Interfaces:**
- Consumes: `generate_company_name(*, org_id, owner_email) -> str`, `allocate_company_slug(*, owner_id, name) -> str | None` (Task 2).
- Produces: no new symbols — `provision_org`'s signature and return type are unchanged. The observable change is that the Paperclip company's `name` is a real name instead of the owner's email, and the owner holds a reserved slug.

**Note:** `owner_id` is the Clerk org id for org owners and the user id for personal owners. `is_org_owner_id` already exists in the codebase for this distinction — locate it (it is used by `core/services/owner_facts.py`) and use it rather than string-prefix matching inline.

- [ ] **Step 1: Write the failing test**

Append to `apps/backend/tests/unit/services/test_paperclip_provisioning.py`, reusing that module's `_build_provision_org_doubles` helper (line 22) and its already-imported `AsyncMock`:

```python
@pytest.mark.asyncio
async def test_provision_org_names_company_from_generated_name(monkeypatch):
    """The company gets a real name, not the owner's email address."""
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

    monkeypatch.setattr(mod, "generate_company_name", fake_generate)
    monkeypatch.setattr(mod, "allocate_company_slug", fake_allocate)

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    await svc.provision_org(
        owner_id="org_xyz",
        owner_user_id="user_owner",
        owner_email="owner@example.com",
    )

    assert admin.create_company.await_args.kwargs["name"] == "Acme Robotics"


@pytest.mark.asyncio
async def test_provision_org_allocates_slug_for_owner(monkeypatch):
    import core.services.paperclip_provisioning as mod
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)
    admin.create_agent = AsyncMock(
        side_effect=[{"id": "agent-friday"}, {"id": "agent-atlas"}]
    )

    allocated = []

    async def fake_generate(*, org_id, owner_email):
        return "Acme Robotics"

    async def fake_allocate(*, owner_id, name):
        allocated.append({"owner_id": owner_id, "name": name})
        return "acme-robotics"

    monkeypatch.setattr(mod, "generate_company_name", fake_generate)
    monkeypatch.setattr(mod, "allocate_company_slug", fake_allocate)

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    await svc.provision_org(
        owner_id="org_xyz",
        owner_user_id="user_owner",
        owner_email="owner@example.com",
    )

    # Slug is keyed by owner_id (the container key), not owner_user_id.
    assert allocated == [{"owner_id": "org_xyz", "name": "Acme Robotics"}]


@pytest.mark.asyncio
async def test_provision_org_survives_name_generation_failure(monkeypatch):
    """A naming failure must not fail the provision; fall back to the email."""
    import core.services.paperclip_provisioning as mod
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)
    admin.create_agent = AsyncMock(
        side_effect=[{"id": "agent-friday"}, {"id": "agent-atlas"}]
    )

    async def boom(**kwargs):
        raise RuntimeError("clerk down")

    monkeypatch.setattr(mod, "generate_company_name", boom)
    monkeypatch.setattr(mod, "allocate_company_slug", boom)

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    row = await svc.provision_org(
        owner_id="org_xyz",
        owner_user_id="user_owner",
        owner_email="owner@example.com",
    )

    assert row.status == "active"
    # Fell back to the previous behaviour rather than aborting.
    assert admin.create_company.await_args.kwargs["name"] == "owner@example.com"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_paperclip_provisioning.py -k "generated_name or allocates_slug or name_generation_failure" -v`
Expected: FAIL — `AttributeError: module 'core.services.paperclip_provisioning' has no attribute 'generate_company_name'`

- [ ] **Step 3: Write minimal implementation**

Add the import near the other `core.services` imports at the top of `apps/backend/core/services/paperclip_provisioning.py`:

```python
from core.services.company_identity import allocate_company_slug, generate_company_name
```

Immediately **before** the `# 2. ADMIN creates the company.` block (~line 322), derive the name:

```python
            # The company name is user-visible and editable afterwards, so
            # generate a real one. Falling back to the owner's email keeps
            # the previous behaviour if naming fails — never abort here.
            try:
                company_name = await generate_company_name(
                    org_id=owner_id if is_org_owner_id(owner_id) else None,
                    owner_email=owner_email,
                )
            except Exception as e:  # noqa: BLE001 — naming must not fail provisioning
                logger.warning(
                    "paperclip_provisioning.provision_org: name generation failed "
                    "for owner_id %s: %s",
                    owner_id,
                    e,
                )
                company_name = owner_email
```

Then replace `name=owner_email` with `name=company_name` in **both** `create_company` calls (the initial call and the 401-retry).

Immediately **after** `company_id = company["id"]` (~line 345), allocate the slug:

```python
            # Reserve the platform-unique slug the agent's email address will
            # later be built from. Best-effort: an owner without a slug simply
            # has no address minted yet.
            try:
                await allocate_company_slug(owner_id=owner_id, name=company_name)
            except Exception as e:  # noqa: BLE001 — allocation must not fail provisioning
                logger.warning(
                    "paperclip_provisioning.provision_org: slug allocation failed "
                    "for owner_id %s: %s",
                    owner_id,
                    e,
                )
```

`is_org_owner_id` lives at `apps/backend/core/auth.py:107`. Import it at the top of the module:

```python
from core.auth import is_org_owner_id
```

(`core/services/owner_facts.py:51` imports it lazily inside a function; a module-level import is fine here — `paperclip_provisioning` already imports from `core.services` at module scope and there is no cycle, since `core.auth` imports neither.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_paperclip_provisioning.py -v`
Expected: PASS (new cases plus all pre-existing cases in the module)

- [ ] **Step 5: Run the full backend unit suite**

Run: `cd apps/backend && uv run pytest tests/unit -q`
Expected: PASS except one **pre-existing** failure,
`tests/unit/routers/teams/test_agents.py::test_create_agent_returns_upstream_status_on_paperclip_error`
(a `RuntimeError` from a missing `PAPERCLIP_SERVICE_TOKEN_KEY`; it fails identically on `main` and is unrelated to this work — do not attempt to fix it here).

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/paperclip_provisioning.py apps/backend/tests/unit/services/test_paperclip_provisioning.py
git commit -m "feat(company-identity): generate company name + slug at provisioning"
```

---

## Done — What Ships

New owners get a real, editable company name (Clerk org name, or a prettified email local part) instead of `name=owner@example.com`, plus an atomically-reserved platform-unique slug. Renaming via `PATCH /teams/settings` now actually works (fixed separately) and deliberately does **not** move the slug.

**Next:** the Email Foundation plan (`2026-07-19-agent-email-1-foundation.md`) builds the agent's address from this slug. Its Task 2 must be revised to take the slug rather than compute a hash digest, and its Task 4 reads the owner's slug via `company_slug_repo.get_by_owner`.
