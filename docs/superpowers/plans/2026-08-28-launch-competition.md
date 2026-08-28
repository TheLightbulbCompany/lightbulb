# Lightbulb Launch Competition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the waitlist → drop → comp → competition loop: a public `/launch` page with waitlist + referral links, an activation-aware "enter" flow that reads the entrant's own Stripe revenue through a pasted restricted key, an hourly leaderboard sweep, automatic referral free-months on the referred person's first paid invoice, and an `/admin/launch` page with the economics.

**Architecture:** Two new DynamoDB tables (`launch-waitlist` keyed by email, `launch-entries` keyed by owner_id) read by full table name from env (the `isol8-`→`lightbulb-` prefix flip is mid-flight, so the new tables must not depend on `DYNAMODB_TABLE_PREFIX`). One new router (`/api/v1/launch`), four small services (phase math, service, revenue, referral), one webhook change (`handle_invoice_paid`), one lifespan loop, one ops endpoint. Frontend: one public route, one admin route, self-contained components — nothing under `components/landing/` or the brand-owned files.

**Tech Stack:** FastAPI + boto3 DynamoDB (moto in tests) + stripe-python ≥ 8 + Secrets Manager; AWS CDK (TypeScript, jest); Next.js 16 App Router + Tailwind + vitest/testing-library.

**Spec:** `docs/superpowers/specs/2026-08-28-launch-competition-design.md` — read it first; the decisions table there is binding.

## Global Constraints

- Backend: Python 3.12, ruff line-length 120, `asyncio_mode = "auto"` (pytest-asyncio); every DynamoDB call goes through `core.dynamodb.run_in_thread`; never log a Stripe key.
- Every `/launch*` and `/ops/launch*` route returns **404** when `settings.LAUNCH_DROP_AT` is empty. Prod is inert until an owner sets it.
- Table access for the two new tables is by **full name** from `settings.LAUNCH_WAITLIST_TABLE` / `settings.LAUNCH_ENTRIES_TABLE` (same pattern as `MARKETPLACE_LISTINGS_TABLE`), never via `get_table(short_name)`.
- Stripe restricted key prefix rule: `^rk_live_` in prod; `^rk_(live|test)_` when `settings.ENVIRONMENT != "prod"`.
- Comp/referral rewards: personal owners only (`owner_id` starting with `org_` is skipped).
- Stripe calls that mutate carry a stable `idempotency_key` (repo convention — `feedback_stripe_idempotency_with_stable_keys`).
- Frontend: tests live in `__tests__/` beside the code; public `/launch` reads use plain `fetch(BACKEND_URL + …)` (the shared client throws without a Clerk token); authed reads use `useApi()`.
- Infra: after any `apps/infra/lib` change run `cd apps/infra && npx cdk synth` and commit `.github/workflows/deploy.yml` if it changed.
- Do not touch `apps/frontend/src/app/page.tsx`, `components/landing/*`, `globals.css`, `landing.css`, `Logo.tsx` — owned by the in-flight brand branch.
- Work on branch `feat/launch-competition` in worktree `.worktrees/launch-competition`; commit after every task; never push without the owner's go.

## File Structure

**Backend (`apps/backend/`)**
- `core/config.py` — add `LAUNCH_*`, `STRIPE_FREE_MONTH_COUPON_ID`, `LAUNCH_WAITLIST_TABLE`, `LAUNCH_ENTRIES_TABLE`.
- `core/services/launch_phase.py` — pure schedule/phase math from settings. No I/O.
- `core/repositories/launch_waitlist_repo.py` — waitlist rows: join (position counter), get, claim user_id, attribute referral, counters, reward CAS, banked months.
- `core/repositories/launch_entries_repo.py` — entry rows: upsert, get, list, write revenue.
- `core/services/launch_service.py` — `me()`, `can_enter()`, `enter()`; key validation + secret storage; pot math.
- `core/services/launch_revenue.py` — Stripe balance-transaction summation, `refresh_entry`, `refresh_all`, `sweep_loop`.
- `core/services/launch_referral.py` — `on_invoice_paid()`: reward the referrer, drain banked months.
- `core/services/ops/launch.py` — admin compute.
- `routers/launch.py`, `schemas/launch.py` — the HTTP surface.
- `routers/ops.py` — add `/launch`, `/launch/refresh`.
- `core/services/stripe_webhook_handlers.py` — `handle_invoice_paid` delegates to `launch_referral`.
- `main.py` — mount router, start sweep loop.
- Tests under `tests/unit/{services,repositories,routers}/test_launch_*.py`.

**Infra (`apps/infra/`)**
- `lib/stacks/database-stack.ts` — two tables. `lib/stacks/service-stack.ts` — props, grants, env. `test/database-stack.test.ts`, `test/service-stack.test.ts` — assertions.

**Frontend (`apps/frontend/src/`)**
- `lib/launch.ts` — types + public fetchers + `formatCents`.
- `app/launch/page.tsx` — server shell with metadata; renders `<LaunchPage />`.
- `components/launch/LaunchPage.tsx` (client, phase switch), `LaunchHero.tsx`, `WaitlistForm.tsx`, `Leaderboard.tsx`, `MyLaunchCard.tsx`, `EnterForm.tsx`, `useLaunchRef.ts`.
- `app/admin/launch/page.tsx`, `app/admin/launch/LaunchAdminClient.tsx`, `app/admin/_lib/api.ts` (+`getLaunchOps`, types), `app/admin/_actions/launch.ts`, `app/admin/layout.tsx` (nav link).
- `lib/analytics.ts` — three new `AnalyticsEvent` members.

---

### Task 1: Settings + phase math

**Files:**
- Modify: `apps/backend/core/config.py` (after the `STRIPE_ULTRA_PRICE_ID` block, ~line 319)
- Create: `apps/backend/core/services/launch_phase.py`
- Test: `apps/backend/tests/unit/services/test_launch_phase.py`

**Interfaces:**
- Produces: `launch_phase.Schedule` dataclass (`drop_at`, `competition_start_at`, `competition_end_at`, `comp_slots`, `activation_window_hours`, `competition_days`), `launch_phase.schedule() -> Schedule | None` (None when `LAUNCH_DROP_AT` empty), `launch_phase.phase(s: Schedule, now: datetime) -> Literal["waitlist","activation","live","ended"]`, `launch_phase.pot_cents(entrants: int) -> int`, `launch_phase.parse_iso(s: str) -> datetime`.

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/unit/services/test_launch_phase.py
"""Phase math for the Launch Competition — pure functions over settings."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from core.services import launch_phase

DROP = datetime(2026, 9, 18, 16, 0, tzinfo=timezone.utc)


@pytest.fixture
def configured():
    with patch.object(launch_phase.settings, "LAUNCH_DROP_AT", "2026-09-18T16:00:00Z"), \
         patch.object(launch_phase.settings, "LAUNCH_ACTIVATION_WINDOW_HOURS", 72), \
         patch.object(launch_phase.settings, "LAUNCH_COMPETITION_DAYS", 30), \
         patch.object(launch_phase.settings, "LAUNCH_COMP_SLOTS", 220):
        yield


def test_schedule_is_none_when_unset():
    with patch.object(launch_phase.settings, "LAUNCH_DROP_AT", ""):
        assert launch_phase.schedule() is None


def test_schedule_derives_start_and_end(configured):
    s = launch_phase.schedule()
    assert s.drop_at == DROP
    assert s.competition_start_at == DROP + timedelta(hours=72)
    assert s.competition_end_at == DROP + timedelta(hours=72, days=30)
    assert s.comp_slots == 220


@pytest.mark.parametrize(
    "now,expected",
    [
        (DROP - timedelta(days=21), "waitlist"),
        (DROP - timedelta(seconds=1), "waitlist"),
        (DROP, "activation"),
        (DROP + timedelta(hours=71, minutes=59), "activation"),
        (DROP + timedelta(hours=72), "live"),
        (DROP + timedelta(hours=72, days=29), "live"),
        (DROP + timedelta(hours=72, days=30), "ended"),
    ],
)
def test_phase_boundaries(configured, now, expected):
    assert launch_phase.phase(launch_phase.schedule(), now) == expected


def test_pot_is_front_plus_per_entrant():
    with patch.object(launch_phase.settings, "LAUNCH_PRIZE_FRONT_CENTS", 100_000), \
         patch.object(launch_phase.settings, "LAUNCH_POT_PER_ENTRANT_CENTS", 1_000):
        assert launch_phase.pot_cents(0) == 100_000
        assert launch_phase.pot_cents(500) == 600_000


def test_parse_iso_accepts_z_and_offset():
    assert launch_phase.parse_iso("2026-09-18T16:00:00Z") == DROP
    assert launch_phase.parse_iso("2026-09-18T12:00:00-04:00") == DROP
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_launch_phase.py -v`
Expected: FAIL — `ImportError: cannot import name 'launch_phase'`.

- [ ] **Step 3: Add the settings**

In `apps/backend/core/config.py`, directly after the `STRIPE_ULTRA_PRICE_ID: str = ""` line, add:

```python
    # --- Lightbulb Launch Competition (docs/superpowers/specs/2026-08-28-launch-competition-design.md)
    # ISO-8601 UTC instant of the product drop. EMPTY disables the whole
    # feature: every /launch* route 404s and the revenue sweep never starts.
    # Prod stays inert until an owner sets it via CDK.
    LAUNCH_DROP_AT: str = ""
    # Display-only: how many waitlist positions are told "you're in the first N".
    # Enforcement of the comp is the Stripe promotion code's max_redemptions.
    LAUNCH_COMP_SLOTS: int = 220
    # Competition starts this many hours after the drop (the activation window).
    LAUNCH_ACTIVATION_WINDOW_HOURS: int = 72
    LAUNCH_COMPETITION_DAYS: int = 30
    LAUNCH_PRIZE_FRONT_CENTS: int = 100_000
    LAUNCH_POT_PER_ENTRANT_CENTS: int = 1_000
    # 100%-off-once coupon created by the owner in the Stripe console. Only the
    # referral reward uses it (the waitlist comp is a promotion code redeemed at
    # checkout). Empty ⇒ rewards are banked on the row, never applied.
    STRIPE_FREE_MONTH_COUPON_ID: str = ""
    # Full table names (CDK-wired). Read by full name, NOT via
    # DYNAMODB_TABLE_PREFIX: the prefix flip is mid-flight and these tables
    # only ever exist on the new prefix.
    LAUNCH_WAITLIST_TABLE: str = ""
    LAUNCH_ENTRIES_TABLE: str = ""
```

- [ ] **Step 4: Write the phase module**

```python
# apps/backend/core/services/launch_phase.py
"""Launch Competition schedule — pure functions over settings, no I/O.

Phase ladder: waitlist (before the drop) → activation (drop .. drop+window)
→ live (competition) → ended. The competition starts exactly when the
activation window closes; the free month IS the competition month.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from core.config import settings

Phase = Literal["waitlist", "activation", "live", "ended"]


@dataclass(frozen=True)
class Schedule:
    drop_at: datetime
    competition_start_at: datetime
    competition_end_at: datetime
    comp_slots: int
    activation_window_hours: int
    competition_days: int


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 instant; `Z` suffix and offsets both normalise to UTC."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def schedule() -> Schedule | None:
    raw = (settings.LAUNCH_DROP_AT or "").strip()
    if not raw:
        return None
    drop = parse_iso(raw)
    start = drop + timedelta(hours=settings.LAUNCH_ACTIVATION_WINDOW_HOURS)
    end = start + timedelta(days=settings.LAUNCH_COMPETITION_DAYS)
    return Schedule(
        drop_at=drop,
        competition_start_at=start,
        competition_end_at=end,
        comp_slots=settings.LAUNCH_COMP_SLOTS,
        activation_window_hours=settings.LAUNCH_ACTIVATION_WINDOW_HOURS,
        competition_days=settings.LAUNCH_COMPETITION_DAYS,
    )


def phase(s: Schedule, now: datetime | None = None) -> Phase:
    now = now or datetime.now(timezone.utc)
    if now < s.drop_at:
        return "waitlist"
    if now < s.competition_start_at:
        return "activation"
    if now < s.competition_end_at:
        return "live"
    return "ended"


def pot_cents(entrants: int) -> int:
    return settings.LAUNCH_PRIZE_FRONT_CENTS + settings.LAUNCH_POT_PER_ENTRANT_CENTS * max(entrants, 0)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_launch_phase.py -v && uv run ruff check core/services/launch_phase.py core/config.py`
Expected: 12 passed; ruff clean.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/config.py apps/backend/core/services/launch_phase.py apps/backend/tests/unit/services/test_launch_phase.py
git commit -m "feat(launch): settings + phase math for the Launch Competition"
```

---

### Task 2: CDK — two tables, grants, env

**Files:**
- Modify: `apps/infra/lib/stacks/database-stack.ts` (public fields ~line 58; table creation after the marketplace tables ~line 405)
- Modify: `apps/infra/lib/stacks/service-stack.ts` (props `database` interface ~line 136; grants after line 799; env after `MARKETPLACE_*` block ~line 1527)
- Modify: `apps/infra/lib/isol8-stage.ts` (the `database: {…}` object passed to ServiceStack ~line 169)
- Test: `apps/infra/test/database-stack.test.ts`, `apps/infra/test/service-stack.test.ts`

**Interfaces:**
- Produces: tables `lightbulb-{env}-launch-waitlist` (PK `email` S; GSI `referral-code-index` on `referral_code` S) and `lightbulb-{env}-launch-entries` (PK `owner_id` S); backend env `LAUNCH_WAITLIST_TABLE`, `LAUNCH_ENTRIES_TABLE`, `LAUNCH_DROP_AT`, `LAUNCH_COMP_SLOTS`, `LAUNCH_ACTIVATION_WINDOW_HOURS`, `LAUNCH_COMPETITION_DAYS`, `LAUNCH_PRIZE_FRONT_CENTS`, `LAUNCH_POT_PER_ENTRANT_CENTS`, `STRIPE_FREE_MONTH_COUPON_ID`.

- [ ] **Step 1: Write the failing infra tests**

Append to `apps/infra/test/database-stack.test.ts`:

```ts
describe("DatabaseStack — launch competition tables", () => {
  let template: Template;
  beforeAll(() => {
    template = buildStack("dev");
  });

  test("creates launch-waitlist keyed by email with a referral-code GSI", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "lightbulb-dev-launch-waitlist",
      KeySchema: [{ AttributeName: "email", KeyType: "HASH" }],
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: "referral-code-index",
          KeySchema: [{ AttributeName: "referral_code", KeyType: "HASH" }],
        }),
      ]),
      BillingMode: "PAY_PER_REQUEST",
    });
  });

  test("creates launch-entries keyed by owner_id", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "lightbulb-dev-launch-entries",
      KeySchema: [{ AttributeName: "owner_id", KeyType: "HASH" }],
    });
  });
});
```

Append to `apps/infra/test/service-stack.test.ts` inside an existing `describe` that has a synthesized service-stack `template` for dev (reuse the file's existing builder — look for the `describe` block that asserts `MARKETPLACE_LISTINGS_TABLE` around line 724 and add a sibling test):

```ts
  test("backend task env carries the launch table names and an EMPTY drop date", () => {
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            Match.objectLike({ Name: "LAUNCH_WAITLIST_TABLE", Value: Match.anyValue() }),
            Match.objectLike({ Name: "LAUNCH_ENTRIES_TABLE", Value: Match.anyValue() }),
            Match.objectLike({ Name: "LAUNCH_DROP_AT", Value: "" }),
          ]),
        }),
      ]),
    });
  });
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/infra && npx jest test/database-stack.test.ts test/service-stack.test.ts`
Expected: the three new tests FAIL (no such table / env).

- [ ] **Step 3: Add the tables**

In `database-stack.ts` add to the public fields (after `marketplaceSellersTable`):

```ts
  public readonly launchWaitlistTable: dynamodb.Table;
  public readonly launchEntriesTable: dynamodb.Table;
```

After the last marketplace table block (before the CfnOutputs), add:

```ts
    // Launch Competition (docs/superpowers/specs/2026-08-28-launch-competition-design.md).
    // Waitlist rows are keyed by lowercased email — the join to an account is
    // users.email (written by the Clerk user.created webhook). The counter row
    // (email = "__counter__") hands out positions atomically.
    this.launchWaitlistTable = createTable("LaunchWaitlistTable", "launch-waitlist", {
      partitionKey: { name: "email", type: dynamodb.AttributeType.STRING },
    });
    this.launchWaitlistTable.addGlobalSecondaryIndex({
      indexName: "referral-code-index",
      partitionKey: { name: "referral_code", type: dynamodb.AttributeType.STRING },
    });
    this.launchEntriesTable = createTable("LaunchEntriesTable", "launch-entries", {
      partitionKey: { name: "owner_id", type: dynamodb.AttributeType.STRING },
    });
```

- [ ] **Step 4: Wire the service stack**

In `service-stack.ts` `database` props interface add:

```ts
    launchWaitlistTable: dynamodb.Table;
    launchEntriesTable: dynamodb.Table;
```

After `props.database.marketplaceSellersTable.grantReadWriteData(this.taskRole);` add:

```ts
    // Launch Competition tables — read by FULL NAME from env (below), so they
    // do not ride the DYNAMODB_TABLE_PREFIX flip.
    props.database.launchWaitlistTable.grantReadWriteData(this.taskRole);
    props.database.launchEntriesTable.grantReadWriteData(this.taskRole);
```

After the `MARKETPLACE_*` env entries add:

```ts
      // Launch Competition. LAUNCH_DROP_AT EMPTY keeps the feature inert
      // (every /launch route 404s). Set the ISO instant per env when the
      // waitlist opens; the other values are the sheet's assumptions.
      LAUNCH_WAITLIST_TABLE: props.database.launchWaitlistTable.tableName,
      LAUNCH_ENTRIES_TABLE: props.database.launchEntriesTable.tableName,
      LAUNCH_DROP_AT: "",
      LAUNCH_COMP_SLOTS: "220",
      LAUNCH_ACTIVATION_WINDOW_HOURS: "72",
      LAUNCH_COMPETITION_DAYS: "30",
      LAUNCH_PRIZE_FRONT_CENTS: "100000",
      LAUNCH_POT_PER_ENTRANT_CENTS: "1000",
      // 100%-off-once coupon id from the Stripe console (live in prod, test in dev).
      STRIPE_FREE_MONTH_COUPON_ID: "",
```

In `isol8-stage.ts`, inside the `database: { … }` object passed to `ServiceStack`, add:

```ts
        launchWaitlistTable: databaseStack.launchWaitlistTable,
        launchEntriesTable: databaseStack.launchEntriesTable,
```

(match the variable name the file already uses for the database stack instance).

- [ ] **Step 5: Run infra tests + synth**

Run: `cd apps/infra && npx jest && npx cdk synth > /dev/null && git status --short .github/workflows/deploy.yml`
Expected: all tests pass; `deploy.yml` unchanged (no pipeline-level env was added). If it shows as modified, commit it in the same commit.

- [ ] **Step 6: Commit**

```bash
git add apps/infra/lib apps/infra/test .github/workflows/deploy.yml
git commit -m "feat(launch): launch-waitlist + launch-entries tables, grants, env"
```

---

### Task 3: Waitlist repository

**Files:**
- Create: `apps/backend/core/repositories/launch_waitlist_repo.py`
- Test: `apps/backend/tests/unit/repositories/test_launch_waitlist_repo.py`

**Interfaces:**
- Produces:
  - `normalize_email(raw: str) -> str` (lowercase + strip; raises `ValueError` if no `@`)
  - `async join(email: str, *, referred_by: str | None, source: str) -> dict` — idempotent; returns the row (existing or new). New rows get `position` from the counter and a fresh `referral_code`. If `referred_by` resolves to another row, that row's `referrals_signed_up` is incremented (only on a NEW join).
  - `async get(email: str) -> dict | None`
  - `async get_by_referral_code(code: str) -> dict | None`
  - `async claim_user(email: str, user_id: str) -> None` — sets `user_id` if unset.
  - `async attribute(email: str, referral_code: str) -> bool` — sets `referred_by` iff unset and code ≠ own; increments the referrer's `referrals_signed_up`; returns whether it wrote.
  - `async mark_rewarded(email: str) -> bool` — CAS: sets `referral_rewarded_at` iff absent.
  - `async add_counter(email: str, field: Literal["referrals_signed_up","referrals_paid","free_months_banked"], delta: int) -> int` — returns the new value (floored at 0 for negative deltas via a condition).
  - `async list_all() -> list[dict]` — every row except the counter row.
  - `async count() -> int`

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/unit/repositories/test_launch_waitlist_repo.py
"""launch-waitlist repository — moto-backed, mirrors test_admin_actions_repo."""

import os
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

TABLE = "test-launch-waitlist"


@pytest.fixture
def table():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        t = ddb.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "email", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "email", "AttributeType": "S"},
                {"AttributeName": "referral_code", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "referral-code-index",
                    "KeySchema": [{"AttributeName": "referral_code", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        t.meta.client.get_waiter("table_exists").wait(TableName=TABLE)
        from core.repositories import launch_waitlist_repo as repo

        with patch.object(repo.settings, "LAUNCH_WAITLIST_TABLE", TABLE), patch.object(repo, "_resource", lambda: ddb):
            yield repo


async def test_join_assigns_sequential_positions_and_codes(table):
    a = await table.join("A@Example.com ", referred_by=None, source="waitlist")
    b = await table.join("b@example.com", referred_by=None, source="waitlist")
    assert a["email"] == "a@example.com"
    assert (a["position"], b["position"]) == (1, 2)
    assert len(a["referral_code"]) == 8 and a["referral_code"] != b["referral_code"]
    assert a["source"] == "waitlist" and a["referrals_signed_up"] == 0


async def test_join_is_idempotent_per_email(table):
    first = await table.join("a@example.com", referred_by=None, source="waitlist")
    again = await table.join("A@EXAMPLE.COM", referred_by=None, source="signup")
    assert again["position"] == first["position"]
    assert again["referral_code"] == first["referral_code"]
    assert again["source"] == "waitlist"  # first write wins
    assert await table.count() == 2  # two people, not three


async def test_join_attributes_referral_and_counts_it(table):
    ref = await table.join("ref@example.com", referred_by=None, source="waitlist")
    new = await table.join("new@example.com", referred_by=ref["referral_code"], source="waitlist")
    assert new["referred_by"] == ref["referral_code"]
    assert (await table.get("ref@example.com"))["referrals_signed_up"] == 1


async def test_join_ignores_unknown_and_self_referral(table):
    row = await table.join("me@example.com", referred_by="NOPE1234", source="waitlist")
    assert "referred_by" not in row
    assert not await table.attribute("me@example.com", row["referral_code"])


async def test_attribute_only_once(table):
    ref = await table.join("ref@example.com", referred_by=None, source="waitlist")
    other = await table.join("other@example.com", referred_by=None, source="waitlist")
    me = await table.join("me@example.com", referred_by=None, source="signup")
    assert await table.attribute(me["email"], ref["referral_code"]) is True
    assert await table.attribute(me["email"], other["referral_code"]) is False
    assert (await table.get("me@example.com"))["referred_by"] == ref["referral_code"]
    assert (await table.get("ref@example.com"))["referrals_signed_up"] == 1
    assert (await table.get("other@example.com"))["referrals_signed_up"] == 0


async def test_get_by_referral_code(table):
    row = await table.join("a@example.com", referred_by=None, source="waitlist")
    assert (await table.get_by_referral_code(row["referral_code"]))["email"] == "a@example.com"
    assert await table.get_by_referral_code("ZZZZZZZZ") is None


async def test_claim_user_sets_once(table):
    await table.join("a@example.com", referred_by=None, source="waitlist")
    await table.claim_user("a@example.com", "user_1")
    await table.claim_user("a@example.com", "user_2")
    assert (await table.get("a@example.com"))["user_id"] == "user_1"


async def test_mark_rewarded_is_cas(table):
    await table.join("a@example.com", referred_by=None, source="waitlist")
    assert await table.mark_rewarded("a@example.com") is True
    assert await table.mark_rewarded("a@example.com") is False


async def test_add_counter_floors_at_zero(table):
    await table.join("a@example.com", referred_by=None, source="waitlist")
    assert await table.add_counter("a@example.com", "free_months_banked", 2) == 2
    assert await table.add_counter("a@example.com", "free_months_banked", -1) == 1
    assert await table.add_counter("a@example.com", "free_months_banked", -5) == 1  # refused, unchanged


async def test_list_all_excludes_counter_row(table):
    await table.join("a@example.com", referred_by=None, source="waitlist")
    await table.join("b@example.com", referred_by=None, source="waitlist")
    emails = {r["email"] for r in await table.list_all()}
    assert emails == {"a@example.com", "b@example.com"}


def test_normalize_email_rejects_garbage(table):
    with pytest.raises(ValueError):
        table.normalize_email("not-an-email")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/repositories/test_launch_waitlist_repo.py -v`
Expected: FAIL — `ImportError` on `launch_waitlist_repo`.

- [ ] **Step 3: Write the repository**

```python
# apps/backend/core/repositories/launch_waitlist_repo.py
"""launch-waitlist table — one row per email, plus a counter row.

Keyed by lowercased email because the waitlist exists before any account
does; the join to a Clerk user is ``users.email`` (written by the
``user.created`` webhook) and is stamped onto the row as ``user_id`` on the
first authed read (``claim_user``).

Table name comes from ``settings.LAUNCH_WAITLIST_TABLE`` (full name), not
``DYNAMODB_TABLE_PREFIX`` — the prefix flip is mid-flight and this table
only exists on the new prefix.
"""

from __future__ import annotations

import secrets
from typing import Literal

import boto3
from botocore.exceptions import ClientError

from core.config import settings
from core.dynamodb import run_in_thread, utc_now_iso

COUNTER_KEY = "__counter__"
REFERRAL_CODE_INDEX = "referral-code-index"
# Unambiguous alphabet (no 0/O, 1/I/L) — codes are read off screens and typed.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LEN = 8

CounterField = Literal["referrals_signed_up", "referrals_paid", "free_months_banked"]


def _resource():
    return boto3.resource("dynamodb")


def _table():
    return _resource().Table(settings.LAUNCH_WAITLIST_TABLE)


def normalize_email(raw: str) -> str:
    email = (raw or "").strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@") or len(email) > 254:
        raise ValueError("invalid_email")
    return email


def _new_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LEN))


def _plain(item: dict | None) -> dict | None:
    """Decimal → int for the counters DynamoDB hands back."""
    if not item:
        return None
    out = dict(item)
    for k in ("position", "referrals_signed_up", "referrals_paid", "free_months_banked"):
        if k in out:
            out[k] = int(out[k])
    return out


async def get(email: str) -> dict | None:
    resp = await run_in_thread(_table().get_item, Key={"email": email})
    return _plain(resp.get("Item"))


async def get_by_referral_code(code: str) -> dict | None:
    from boto3.dynamodb.conditions import Key

    resp = await run_in_thread(
        _table().query,
        IndexName=REFERRAL_CODE_INDEX,
        KeyConditionExpression=Key("referral_code").eq(code),
        Limit=1,
    )
    items = resp.get("Items") or []
    return _plain(items[0]) if items else None


async def _next_position() -> int:
    resp = await run_in_thread(
        _table().update_item,
        Key={"email": COUNTER_KEY},
        UpdateExpression="ADD #p :one",
        ExpressionAttributeNames={"#p": "position"},
        ExpressionAttributeValues={":one": 1},
        ReturnValues="UPDATED_NEW",
    )
    return int(resp["Attributes"]["position"])


async def join(email: str, *, referred_by: str | None, source: str) -> dict:
    """Idempotent per email. A NEW row takes the next position and a fresh
    referral code; an existing row is returned untouched (first write wins,
    including ``source`` and ``referred_by``)."""
    email = normalize_email(email)
    existing = await get(email)
    if existing:
        return existing

    referrer = await get_by_referral_code(referred_by) if referred_by else None
    if referrer and referrer["email"] == email:
        referrer = None

    # ponytail: a same-email race increments the counter twice and leaves a
    # gap in positions. Harmless — positions are an ordering, not a count.
    position = await _next_position()
    item: dict = {
        "email": email,
        "position": position,
        "referral_code": _new_code(),
        "source": source,
        "created_at": utc_now_iso(),
        "referrals_signed_up": 0,
        "referrals_paid": 0,
        "free_months_banked": 0,
    }
    if referrer:
        item["referred_by"] = referrer["referral_code"]
    try:
        await run_in_thread(_table().put_item, Item=item, ConditionExpression="attribute_not_exists(email)")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return await get(email)  # lost the race — the other writer's row is the truth
    if referrer:
        await add_counter(referrer["email"], "referrals_signed_up", 1)
    return _plain(item)


async def claim_user(email: str, user_id: str) -> None:
    try:
        await run_in_thread(
            _table().update_item,
            Key={"email": email},
            UpdateExpression="SET user_id = :u, claimed_at = :t",
            ConditionExpression="attribute_exists(email) AND attribute_not_exists(user_id)",
            ExpressionAttributeValues={":u": user_id, ":t": utc_now_iso()},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


async def attribute(email: str, referral_code: str) -> bool:
    """Set ``referred_by`` once. Returns True iff this call wrote it."""
    referrer = await get_by_referral_code(referral_code)
    if not referrer or referrer["email"] == email:
        return False
    try:
        await run_in_thread(
            _table().update_item,
            Key={"email": email},
            UpdateExpression="SET referred_by = :c",
            ConditionExpression="attribute_exists(email) AND attribute_not_exists(referred_by)",
            ExpressionAttributeValues={":c": referral_code},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return False
    await add_counter(referrer["email"], "referrals_signed_up", 1)
    return True


async def mark_rewarded(email: str) -> bool:
    """CAS guard for the referral payout: True exactly once per referred row."""
    try:
        await run_in_thread(
            _table().update_item,
            Key={"email": email},
            UpdateExpression="SET referral_rewarded_at = :t",
            ConditionExpression="attribute_exists(email) AND attribute_not_exists(referral_rewarded_at)",
            ExpressionAttributeValues={":t": utc_now_iso()},
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return False


async def add_counter(email: str, field: CounterField, delta: int) -> int:
    """Atomic ADD. A negative delta that would go below zero is refused and the
    current value is returned unchanged."""
    kwargs: dict = {
        "Key": {"email": email},
        "UpdateExpression": "ADD #f :d",
        "ExpressionAttributeNames": {"#f": field},
        "ExpressionAttributeValues": {":d": delta},
        "ReturnValues": "UPDATED_NEW",
    }
    if delta < 0:
        kwargs["ConditionExpression"] = "#f >= :need"
        kwargs["ExpressionAttributeValues"][":need"] = -delta
    try:
        resp = await run_in_thread(_table().update_item, **kwargs)
        return int(resp["Attributes"][field])
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        row = await get(email)
        return int((row or {}).get(field, 0))


async def list_all() -> list[dict]:
    items: list[dict] = []
    kwargs: dict = {}
    while True:
        page = await run_in_thread(_table().scan, **kwargs)
        items.extend(_plain(i) for i in page.get("Items", []) if i.get("email") != COUNTER_KEY)
        last = page.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs["ExclusiveStartKey"] = last


async def count() -> int:
    row = await run_in_thread(_table().get_item, Key={"email": COUNTER_KEY})
    return int((row.get("Item") or {}).get("position", 0))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/repositories/test_launch_waitlist_repo.py -v && uv run ruff check core/repositories/launch_waitlist_repo.py`
Expected: 11 passed; ruff clean. (Note `count()` returns the counter, so `test_join_is_idempotent_per_email` expects 2 — one position per NEW email.)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/repositories/launch_waitlist_repo.py apps/backend/tests/unit/repositories/test_launch_waitlist_repo.py
git commit -m "feat(launch): launch-waitlist repository — positions, referral codes, CAS reward guard"
```

---

### Task 4: Entries repository

**Files:**
- Create: `apps/backend/core/repositories/launch_entries_repo.py`
- Test: `apps/backend/tests/unit/repositories/test_launch_entries_repo.py`

**Interfaces:**
- Produces:
  - `async upsert(owner_id: str, *, display_name: str, public: bool, waitlist_position: int | None, currency: str, baseline_cents: int) -> dict` — creates or replaces name/public/baseline; preserves `entered_at` and the revenue fields on re-entry.
  - `async get(owner_id: str) -> dict | None`
  - `async list_all() -> list[dict]`
  - `async count() -> int`
  - `async set_revenue(owner_id: str, *, window_cents: int, delta_cents: int, currency: str) -> None` — also clears `refresh_error`, stamps `refreshed_at`.
  - `async set_refresh_error(owner_id: str, error: str) -> None` — stamps `refreshed_at` too (so a broken key doesn't get retried every minute).
  - `async delete(owner_id: str) -> None`

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/unit/repositories/test_launch_entries_repo.py
import os
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

TABLE = "test-launch-entries"


@pytest.fixture
def table():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        t = ddb.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        t.meta.client.get_waiter("table_exists").wait(TableName=TABLE)
        from core.repositories import launch_entries_repo as repo

        with patch.object(repo.settings, "LAUNCH_ENTRIES_TABLE", TABLE), patch.object(repo, "_resource", lambda: ddb):
            yield repo


async def test_upsert_creates_then_preserves_entered_at_and_revenue(table):
    row = await table.upsert("user_1", display_name="Ada", public=True, waitlist_position=7, currency="usd", baseline_cents=1000)
    assert row["entered_at"] and row["delta_cents"] == 0 and row["window_cents"] == 0
    await table.set_revenue("user_1", window_cents=2500, delta_cents=1500, currency="usd")
    again = await table.upsert("user_1", display_name="Ada L.", public=False, waitlist_position=7, currency="usd", baseline_cents=1200)
    assert again["entered_at"] == row["entered_at"]
    assert again["display_name"] == "Ada L." and again["public"] is False
    assert again["baseline_cents"] == 1200 and again["window_cents"] == 2500


async def test_set_revenue_clears_error_and_stamps(table):
    await table.upsert("user_1", display_name="Ada", public=True, waitlist_position=None, currency="usd", baseline_cents=0)
    await table.set_refresh_error("user_1", "stripe_auth")
    row = await table.get("user_1")
    assert row["refresh_error"] == "stripe_auth" and row["refreshed_at"]
    await table.set_revenue("user_1", window_cents=10, delta_cents=10, currency="usd")
    row = await table.get("user_1")
    assert "refresh_error" not in row and row["delta_cents"] == 10


async def test_list_count_delete(table):
    await table.upsert("user_1", display_name="A", public=True, waitlist_position=None, currency="usd", baseline_cents=0)
    await table.upsert("org_2", display_name="B", public=False, waitlist_position=3, currency="eur", baseline_cents=5)
    assert await table.count() == 2
    assert {r["owner_id"] for r in await table.list_all()} == {"user_1", "org_2"}
    await table.delete("user_1")
    assert await table.get("user_1") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/repositories/test_launch_entries_repo.py -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Write the repository**

```python
# apps/backend/core/repositories/launch_entries_repo.py
"""launch-entries table — one row per competing owner.

The entrant's Stripe restricted key is NOT here; it lives in Secrets Manager
on the BYO-key rail (see core.services.launch_service). Revenue numbers are
written by the hourly sweep (core.services.launch_revenue).
"""

from __future__ import annotations

import boto3

from core.config import settings
from core.dynamodb import run_in_thread, utc_now_iso

_INT_FIELDS = ("baseline_cents", "window_cents", "delta_cents", "waitlist_position")


def _resource():
    return boto3.resource("dynamodb")


def _table():
    return _resource().Table(settings.LAUNCH_ENTRIES_TABLE)


def _plain(item: dict | None) -> dict | None:
    if not item:
        return None
    out = dict(item)
    for k in _INT_FIELDS:
        if out.get(k) is not None:
            out[k] = int(out[k])
    return out


async def get(owner_id: str) -> dict | None:
    resp = await run_in_thread(_table().get_item, Key={"owner_id": owner_id})
    return _plain(resp.get("Item"))


async def upsert(
    owner_id: str,
    *,
    display_name: str,
    public: bool,
    waitlist_position: int | None,
    currency: str,
    baseline_cents: int,
) -> dict:
    now = utc_now_iso()
    resp = await run_in_thread(
        _table().update_item,
        Key={"owner_id": owner_id},
        UpdateExpression=(
            "SET display_name = :n, #pub = :p, currency = :c, baseline_cents = :b, "
            "waitlist_position = :w, updated_at = :now, "
            "entered_at = if_not_exists(entered_at, :now), "
            "window_cents = if_not_exists(window_cents, :zero), "
            "delta_cents = if_not_exists(delta_cents, :zero)"
        ),
        ExpressionAttributeNames={"#pub": "public"},
        ExpressionAttributeValues={
            ":n": display_name,
            ":p": bool(public),
            ":c": currency,
            ":b": int(baseline_cents),
            ":w": waitlist_position,
            ":now": now,
            ":zero": 0,
        },
        ReturnValues="ALL_NEW",
    )
    return _plain(resp["Attributes"])


async def set_revenue(owner_id: str, *, window_cents: int, delta_cents: int, currency: str) -> None:
    await run_in_thread(
        _table().update_item,
        Key={"owner_id": owner_id},
        UpdateExpression="SET window_cents = :w, delta_cents = :d, currency = :c, refreshed_at = :t REMOVE refresh_error",
        ConditionExpression="attribute_exists(owner_id)",
        ExpressionAttributeValues={":w": int(window_cents), ":d": int(delta_cents), ":c": currency, ":t": utc_now_iso()},
    )


async def set_refresh_error(owner_id: str, error: str) -> None:
    await run_in_thread(
        _table().update_item,
        Key={"owner_id": owner_id},
        UpdateExpression="SET refresh_error = :e, refreshed_at = :t",
        ConditionExpression="attribute_exists(owner_id)",
        ExpressionAttributeValues={":e": error[:200], ":t": utc_now_iso()},
    )


async def list_all() -> list[dict]:
    items: list[dict] = []
    kwargs: dict = {}
    while True:
        page = await run_in_thread(_table().scan, **kwargs)
        items.extend(_plain(i) for i in page.get("Items", []))
        last = page.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs["ExclusiveStartKey"] = last


async def count() -> int:
    # ponytail: scan-count; fine for hundreds of rows. Move to a counter row if
    # the leaderboard ever needs this on every public read at scale.
    total = 0
    kwargs: dict = {"Select": "COUNT"}
    while True:
        page = await run_in_thread(_table().scan, **kwargs)
        total += int(page.get("Count", 0))
        last = page.get("LastEvaluatedKey")
        if not last:
            return total
        kwargs["ExclusiveStartKey"] = last


async def delete(owner_id: str) -> None:
    await run_in_thread(_table().delete_item, Key={"owner_id": owner_id})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/repositories/test_launch_entries_repo.py -v && uv run ruff check core/repositories/launch_entries_repo.py`
Expected: 3 passed; ruff clean.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/repositories/launch_entries_repo.py apps/backend/tests/unit/repositories/test_launch_entries_repo.py
git commit -m "feat(launch): launch-entries repository"
```

---

### Task 5: Public endpoints — `GET /launch`, `POST /launch/waitlist`

**Files:**
- Create: `apps/backend/schemas/launch.py`
- Create: `apps/backend/routers/launch.py`
- Modify: `apps/backend/main.py` (router mount, next to `ops` at ~line 885)
- Test: `apps/backend/tests/unit/routers/test_launch_public.py`

**Interfaces:**
- Consumes: `launch_phase.schedule/phase/pot_cents` (Task 1); `launch_waitlist_repo.join/count/normalize_email` (Task 3); `launch_entries_repo.count` (Task 4); `core.services.rate_bucket.RateBucket`; `routers.hooks_proxy._extract_caller_ip`.
- Produces: `routers.launch.router` mounted at `/api/v1/launch`; `routers.launch.require_launch() -> Schedule` (FastAPI dependency raising 404 when unset — reused by every later route); `schemas.launch.LaunchInfo`, `WaitlistJoinRequest`, `WaitlistJoinResponse`.

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/unit/routers/test_launch_public.py
"""Unauthenticated Launch routes: /launch and /launch/waitlist."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

DROP = datetime.now(timezone.utc) + timedelta(days=10)


@pytest.fixture
def configured():
    with patch("core.services.launch_phase.settings.LAUNCH_DROP_AT", DROP.isoformat()):
        yield


@pytest.fixture(autouse=True)
def _fresh_bucket():
    from routers import launch

    launch._RATE.clear()
    yield
    launch._RATE.clear()


async def test_launch_info_404_when_unset(unauthenticated_async_client):
    with patch("core.services.launch_phase.settings.LAUNCH_DROP_AT", ""):
        r = await unauthenticated_async_client.get("/api/v1/launch")
    assert r.status_code == 404


@patch("routers.launch.launch_entries_repo")
@patch("routers.launch.launch_waitlist_repo")
async def test_launch_info_shape(mock_wl, mock_entries, configured, unauthenticated_async_client):
    mock_wl.count = AsyncMock(return_value=42)
    mock_entries.count = AsyncMock(return_value=3)
    r = await unauthenticated_async_client.get("/api/v1/launch")
    assert r.status_code == 200
    body = r.json()
    assert body["phase"] == "waitlist"
    assert body["waitlist_count"] == 42 and body["entrants"] == 3
    assert body["pot_cents"] == 100_000 + 3 * 1_000
    assert body["comp_slots"] == 220
    assert body["competition_start_at"] > body["drop_at"]
    assert r.headers["cache-control"] == "public, max-age=30"


@patch("routers.launch.launch_waitlist_repo")
async def test_join_waitlist_returns_position_and_code(mock_wl, configured, unauthenticated_async_client):
    mock_wl.normalize_email.side_effect = lambda s: s.strip().lower()
    mock_wl.join = AsyncMock(return_value={"email": "a@x.com", "position": 12, "referral_code": "ABCD2345"})
    r = await unauthenticated_async_client.post("/api/v1/launch/waitlist", json={"email": " A@x.com", "ref": "K7QX2MPP"})
    assert r.status_code == 200
    assert r.json() == {"position": 12, "referral_code": "ABCD2345", "in_comp_window": True}
    mock_wl.join.assert_awaited_once_with("a@x.com", referred_by="K7QX2MPP", source="waitlist")


@patch("routers.launch.launch_waitlist_repo")
async def test_join_waitlist_rejects_bad_email(mock_wl, configured, unauthenticated_async_client):
    mock_wl.normalize_email.side_effect = ValueError("invalid_email")
    r = await unauthenticated_async_client.post("/api/v1/launch/waitlist", json={"email": "nope"})
    assert r.status_code == 400 and r.json()["detail"] == "invalid_email"


@patch("routers.launch.launch_waitlist_repo")
async def test_join_waitlist_rate_limited_per_ip(mock_wl, configured, unauthenticated_async_client):
    mock_wl.normalize_email.side_effect = lambda s: s
    mock_wl.join = AsyncMock(return_value={"position": 1, "referral_code": "X"})
    for _ in range(10):
        assert (await unauthenticated_async_client.post("/api/v1/launch/waitlist", json={"email": "a@x.com"})).status_code == 200
    r = await unauthenticated_async_client.post("/api/v1/launch/waitlist", json={"email": "a@x.com"})
    assert r.status_code == 429 and r.headers["retry-after"] == "60"


async def test_join_waitlist_closed_after_drop(configured, unauthenticated_async_client):
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with patch("core.services.launch_phase.settings.LAUNCH_DROP_AT", past):
        r = await unauthenticated_async_client.post("/api/v1/launch/waitlist", json={"email": "a@x.com"})
    assert r.status_code == 409 and r.json()["detail"] == "waitlist_closed"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_launch_public.py -v`
Expected: FAIL — 404s on every route / `ModuleNotFoundError: routers.launch`.

- [ ] **Step 3: Write the schemas**

```python
# apps/backend/schemas/launch.py
"""Pydantic models for the Launch Competition HTTP surface."""

from __future__ import annotations

from pydantic import BaseModel, Field


class LaunchInfo(BaseModel):
    phase: str
    drop_at: str
    competition_start_at: str
    competition_end_at: str
    pot_cents: int
    entrants: int
    waitlist_count: int
    comp_slots: int


class WaitlistJoinRequest(BaseModel):
    email: str = Field(max_length=254)
    ref: str | None = Field(default=None, max_length=16)


class WaitlistJoinResponse(BaseModel):
    position: int
    referral_code: str
    in_comp_window: bool


class ReferralAttributeRequest(BaseModel):
    ref: str = Field(min_length=4, max_length=16)


class EnterRequest(BaseModel):
    display_name: str = Field(min_length=2, max_length=40)
    stripe_key: str = Field(min_length=20, max_length=200)
    public: bool = True


class LeaderboardRow(BaseModel):
    rank: int
    display_name: str
    delta_cents: int
    pct: float | None
    currency: str


class LeaderboardResponse(BaseModel):
    entrants: int
    rows: list[LeaderboardRow]
```

- [ ] **Step 4: Write the router (public half)**

```python
# apps/backend/routers/launch.py
"""Lightbulb Launch Competition — waitlist, entry, leaderboard.

Every route 404s until ``settings.LAUNCH_DROP_AT`` is set (``require_launch``),
so a deploy with the env unset is inert. Public routes are rate-limited per
caller IP with the same in-process bucket the hook proxies use.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from core.auth import AuthContext, get_current_user
from core.repositories import launch_entries_repo, launch_waitlist_repo
from core.services import launch_phase
from core.services.launch_phase import Schedule
from core.services.rate_bucket import RateBucket
from routers.hooks_proxy import _extract_caller_ip
from schemas.launch import (
    EnterRequest,
    LaunchInfo,
    LeaderboardResponse,
    ReferralAttributeRequest,
    WaitlistJoinRequest,
    WaitlistJoinResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter()

PUBLIC_RATE_PER_MINUTE = 10
_RATE = RateBucket(PUBLIC_RATE_PER_MINUTE)


def require_launch() -> Schedule:
    s = launch_phase.schedule()
    if s is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return s


def _rate_limit(request: Request, surface: str) -> None:
    if not _RATE.allow(f"{surface}:{_extract_caller_ip(request)}"):
        raise HTTPException(status_code=429, detail="rate_limited", headers={"Retry-After": "60"})


@router.get("", response_model=LaunchInfo, operation_id="launch_info")
async def launch_info(response: Response, s: Schedule = Depends(require_launch)) -> dict:
    entrants = await launch_entries_repo.count()
    waitlist_count = await launch_waitlist_repo.count()
    response.headers["Cache-Control"] = "public, max-age=30"
    return {
        "phase": launch_phase.phase(s),
        "drop_at": s.drop_at.isoformat(),
        "competition_start_at": s.competition_start_at.isoformat(),
        "competition_end_at": s.competition_end_at.isoformat(),
        "pot_cents": launch_phase.pot_cents(entrants),
        "entrants": entrants,
        "waitlist_count": waitlist_count,
        "comp_slots": s.comp_slots,
    }


@router.post("/waitlist", response_model=WaitlistJoinResponse, operation_id="launch_join_waitlist")
async def join_waitlist(body: WaitlistJoinRequest, request: Request, s: Schedule = Depends(require_launch)) -> dict:
    _rate_limit(request, "waitlist")
    if launch_phase.phase(s) != "waitlist":
        raise HTTPException(status_code=409, detail="waitlist_closed")
    try:
        email = launch_waitlist_repo.normalize_email(body.email)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_email")
    ref = (body.ref or "").strip().upper() or None
    row = await launch_waitlist_repo.join(email, referred_by=ref, source="waitlist")
    return {
        "position": row["position"],
        "referral_code": row["referral_code"],
        "in_comp_window": row["position"] <= s.comp_slots,
    }
```

(`EnterRequest`, `LeaderboardResponse`, `ReferralAttributeRequest`, `AuthContext`, `get_current_user` are imported now so Tasks 6 and 8 only append routes.)

- [ ] **Step 5: Mount the router**

In `apps/backend/main.py`, next to the `ops` mount (`app.include_router(ops.router, prefix="/api/v1/ops", tags=["ops"])`), add the import to the routers import block and:

```python
app.include_router(launch.router, prefix="/api/v1/launch", tags=["launch"])
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_launch_public.py -v && uv run ruff check routers/launch.py schemas/launch.py main.py`
Expected: 6 passed; ruff clean (unused imports are used by Tasks 6/8 — if ruff flags them now, add `# noqa: F401` on that import line and remove the noqa in Task 8).

- [ ] **Step 7: Commit**

```bash
git add apps/backend/schemas/launch.py apps/backend/routers/launch.py apps/backend/main.py apps/backend/tests/unit/routers/test_launch_public.py
git commit -m "feat(launch): public /launch info + waitlist join"
```

---

### Task 6: Signed-in state — `GET /launch/me`, `POST /launch/referral`

**Files:**
- Create: `apps/backend/core/services/launch_service.py`
- Modify: `apps/backend/routers/launch.py` (append two routes)
- Test: `apps/backend/tests/unit/services/test_launch_service_me.py`, `apps/backend/tests/unit/routers/test_launch_me.py`

**Interfaces:**
- Consumes: `launch_waitlist_repo.join/get/claim_user/attribute`, `launch_entries_repo.get`, `core.repositories.user_repo.get`, `core.repositories.billing_repo.get_by_owner_id`, `core.services.readiness_state.resolve_onboarding_state(owner_id)`, `core.auth.resolve_owner_id`.
- Produces:
  - `launch_service.resolve_email(auth: AuthContext) -> str | None` — `auth.email` else `users.email`.
  - `launch_service.can_enter(*, s: Schedule, billing: dict | None, onboarding: dict, now=None) -> tuple[bool, str | None]` — reasons: `phase_waitlist`, `phase_ended`, `no_subscription`, `not_activated`.
  - `launch_service.me(auth: AuthContext, s: Schedule) -> dict` — the `/launch/me` body.
  - `launch_service.referral_link(code: str) -> str` — `f"{settings.FRONTEND_URL}/launch?ref={code}"`.
  - `LIVE_FOR_ENTRY = frozenset({"active", "trialing"})`

- [ ] **Step 1: Write the failing service tests**

```python
# apps/backend/tests/unit/services/test_launch_service_me.py
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from core.auth import AuthContext
from core.services import launch_phase, launch_service

NOW = datetime.now(timezone.utc)


def _sched(drop_delta_days: int) -> launch_phase.Schedule:
    drop = NOW + timedelta(days=drop_delta_days)
    return launch_phase.Schedule(drop, drop + timedelta(hours=72), drop + timedelta(hours=72, days=30), 220, 72, 30)


ACTIVATED = {"bootstrap_state": "delivered", "bootstrap_delivered_at": "2026-09-19T00:00:00+00:00"}
NOT_ACTIVATED = {"bootstrap_state": "sent", "bootstrap_delivered_at": None}


@pytest.mark.parametrize(
    "sched,billing,onboarding,expected",
    [
        (_sched(+5), {"subscription_status": "active"}, ACTIVATED, (False, "phase_waitlist")),
        (_sched(-40), {"subscription_status": "active"}, ACTIVATED, (False, "phase_ended")),
        (_sched(-1), None, ACTIVATED, (False, "no_subscription")),
        (_sched(-1), {"subscription_status": "trial_local"}, ACTIVATED, (False, "no_subscription")),
        (_sched(-1), {"subscription_status": "active"}, NOT_ACTIVATED, (False, "not_activated")),
        (_sched(-1), {"subscription_status": "active"}, ACTIVATED, (True, None)),
        (_sched(-10), {"subscription_status": "trialing"}, ACTIVATED, (True, None)),
    ],
)
def test_can_enter_matrix(sched, billing, onboarding, expected):
    assert launch_service.can_enter(s=sched, billing=billing, onboarding=onboarding, now=NOW) == expected


async def test_resolve_email_prefers_jwt_then_users_row():
    assert await launch_service.resolve_email(AuthContext(user_id="u1", email="J@X.com")) == "j@x.com"
    with patch("core.services.launch_service.user_repo.get", AsyncMock(return_value={"user_id": "u1", "email": "row@x.com"})):
        assert await launch_service.resolve_email(AuthContext(user_id="u1")) == "row@x.com"
    with patch("core.services.launch_service.user_repo.get", AsyncMock(return_value=None)):
        assert await launch_service.resolve_email(AuthContext(user_id="u1")) is None


async def test_me_lazily_creates_row_claims_user_and_composes():
    auth = AuthContext(user_id="user_1", email="me@x.com")
    row = {"email": "me@x.com", "position": 300, "referral_code": "ABCD2345", "referrals_signed_up": 2,
           "referrals_paid": 1, "free_months_banked": 1}
    with patch("core.services.launch_service.launch_waitlist_repo") as wl, \
         patch("core.services.launch_service.launch_entries_repo.get", AsyncMock(return_value=None)), \
         patch("core.services.launch_service.billing_repo.get_by_owner_id", AsyncMock(return_value={"subscription_status": "active"})), \
         patch("core.services.launch_service.readiness_state.resolve_onboarding_state", AsyncMock(return_value=ACTIVATED)), \
         patch("core.services.launch_service.settings.FRONTEND_URL", "https://dev.flickmeon.ai"):
        wl.join = AsyncMock(return_value=row)
        wl.claim_user = AsyncMock()
        body = await launch_service.me(auth, _sched(-1))
    wl.join.assert_awaited_once_with("me@x.com", referred_by=None, source="signup")
    wl.claim_user.assert_awaited_once_with("me@x.com", "user_1")
    assert body["waitlist"] == {"position": 300, "referral_code": "ABCD2345", "in_comp_window": False,
                                "referral_link": "https://dev.flickmeon.ai/launch?ref=ABCD2345",
                                "referrals_signed_up": 2, "referrals_paid": 1, "free_months_banked": 1}
    assert body["activated"] is True and body["activated_at"] == ACTIVATED["bootstrap_delivered_at"]
    assert body["entry"] is None and body["can_enter"] is True and body["reason"] is None


async def test_me_without_email_has_no_waitlist_block():
    auth = AuthContext(user_id="user_1")
    with patch("core.services.launch_service.user_repo.get", AsyncMock(return_value=None)), \
         patch("core.services.launch_service.launch_entries_repo.get", AsyncMock(return_value=None)), \
         patch("core.services.launch_service.billing_repo.get_by_owner_id", AsyncMock(return_value=None)), \
         patch("core.services.launch_service.readiness_state.resolve_onboarding_state", AsyncMock(return_value=NOT_ACTIVATED)):
        body = await launch_service.me(auth, _sched(-1))
    assert body["waitlist"] is None and body["can_enter"] is False and body["reason"] == "no_subscription"
```

- [ ] **Step 2: Write the failing router tests**

```python
# apps/backend/tests/unit/routers/test_launch_me.py
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

DROP = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()


@pytest.fixture
def configured():
    with patch("core.services.launch_phase.settings.LAUNCH_DROP_AT", DROP):
        yield


async def test_me_requires_auth(configured, unauthenticated_async_client):
    assert (await unauthenticated_async_client.get("/api/v1/launch/me")).status_code in (401, 403)


@patch("routers.launch.launch_service.me", AsyncMock(return_value={"waitlist": None, "activated": False}))
async def test_me_delegates(configured, async_client):
    r = await async_client.get("/api/v1/launch/me")
    assert r.status_code == 200 and r.json()["activated"] is False


@patch("routers.launch.launch_waitlist_repo")
@patch("routers.launch.billing_repo")
@patch("routers.launch.launch_service.resolve_email", AsyncMock(return_value="me@x.com"))
async def test_referral_attributes_when_no_subscription(mock_billing, mock_wl, configured, async_client):
    mock_billing.get_by_owner_id = AsyncMock(return_value={"subscription_status": "trial_local"})
    mock_wl.join = AsyncMock(return_value={"email": "me@x.com"})
    mock_wl.attribute = AsyncMock(return_value=True)
    r = await async_client.post("/api/v1/launch/referral", json={"ref": "k7qx2mpp"})
    assert r.status_code == 200 and r.json() == {"attributed": True}
    mock_wl.attribute.assert_awaited_once_with("me@x.com", "K7QX2MPP")


@patch("routers.launch.billing_repo")
@patch("routers.launch.launch_service.resolve_email", AsyncMock(return_value="me@x.com"))
async def test_referral_refused_once_subscribed(mock_billing, configured, async_client):
    mock_billing.get_by_owner_id = AsyncMock(return_value={"subscription_status": "active"})
    r = await async_client.post("/api/v1/launch/referral", json={"ref": "K7QX2MPP"})
    assert r.status_code == 200 and r.json() == {"attributed": False}
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_launch_service_me.py tests/unit/routers/test_launch_me.py -v`
Expected: FAIL — `ImportError: launch_service` / 404 on `/launch/me`.

- [ ] **Step 4: Write the service**

```python
# apps/backend/core/services/launch_service.py
"""Launch Competition — per-user composition and the entry flow.

`me()` is the one place waitlist + billing + onboarding facts are combined
for a caller. `enter()` (Task 8) validates the pasted Stripe restricted key,
stores it on the BYO-key rail, computes the baseline and writes the entry.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from core.auth import AuthContext, resolve_owner_id
from core.config import settings
from core.repositories import billing_repo, launch_entries_repo, launch_waitlist_repo, user_repo
from core.services import readiness_state
from core.services.launch_phase import Schedule, phase

logger = logging.getLogger(__name__)

# trial_local can't enter: the $10 buy-in rides a subscription (paid or comped
# via the promo code), and a free-tier container has neither.
LIVE_FOR_ENTRY = frozenset({"active", "trialing"})


def referral_link(code: str) -> str:
    return f"{settings.FRONTEND_URL.rstrip('/')}/launch?ref={code}"


async def resolve_email(auth: AuthContext) -> str | None:
    if auth.email:
        return auth.email.strip().lower()
    row = await user_repo.get(auth.user_id)
    email = (row or {}).get("email")
    return email.strip().lower() if email else None


def can_enter(*, s: Schedule, billing: dict | None, onboarding: dict, now: datetime | None = None) -> tuple[bool, str | None]:
    p = phase(s, now or datetime.now(timezone.utc))
    if p == "waitlist":
        return False, "phase_waitlist"
    if p == "ended":
        return False, "phase_ended"
    if (billing or {}).get("subscription_status") not in LIVE_FOR_ENTRY:
        return False, "no_subscription"
    if onboarding.get("bootstrap_state") != "delivered":
        return False, "not_activated"
    return True, None


def _public_entry(entry: dict | None) -> dict | None:
    if not entry:
        return None
    return {
        "display_name": entry["display_name"],
        "public": bool(entry.get("public", True)),
        "entered_at": entry.get("entered_at"),
        "currency": entry.get("currency"),
        "baseline_cents": entry.get("baseline_cents", 0),
        "window_cents": entry.get("window_cents", 0),
        "delta_cents": entry.get("delta_cents", 0),
        "refreshed_at": entry.get("refreshed_at"),
        "refresh_error": entry.get("refresh_error"),
    }


async def me(auth: AuthContext, s: Schedule) -> dict:
    owner_id = resolve_owner_id(auth)
    email = await resolve_email(auth)
    waitlist: dict | None = None
    if email:
        row = await launch_waitlist_repo.join(email, referred_by=None, source="signup")
        await launch_waitlist_repo.claim_user(email, auth.user_id)
        waitlist = {
            "position": row["position"],
            "referral_code": row["referral_code"],
            "in_comp_window": row["position"] <= s.comp_slots,
            "referral_link": referral_link(row["referral_code"]),
            "referrals_signed_up": int(row.get("referrals_signed_up", 0)),
            "referrals_paid": int(row.get("referrals_paid", 0)),
            "free_months_banked": int(row.get("free_months_banked", 0)),
        }
    billing = await billing_repo.get_by_owner_id(owner_id)
    onboarding = await readiness_state.resolve_onboarding_state(owner_id)
    entry = await launch_entries_repo.get(owner_id)
    ok, reason = can_enter(s=s, billing=billing, onboarding=onboarding)
    return {
        "phase": phase(s),
        "waitlist": waitlist,
        "activated": onboarding.get("bootstrap_state") == "delivered",
        "activated_at": onboarding.get("bootstrap_delivered_at"),
        "entry": _public_entry(entry),
        "can_enter": ok,
        "reason": reason,
    }
```

- [ ] **Step 5: Append the routes to `routers/launch.py`**

Add the imports `from core.repositories import billing_repo` and `from core.services import launch_service`, then append:

```python
@router.get("/me", operation_id="launch_me")
async def launch_me(auth: AuthContext = Depends(get_current_user), s: Schedule = Depends(require_launch)) -> dict:
    return await launch_service.me(auth, s)


@router.post("/referral", operation_id="launch_attribute_referral")
async def attribute_referral(
    body: ReferralAttributeRequest,
    auth: AuthContext = Depends(get_current_user),
    s: Schedule = Depends(require_launch),
) -> dict:
    """Attribute a post-drop sign-up who arrived via ``/launch?ref=``.
    Refused once the caller has a live subscription — attribution must
    precede the first payment, which is what pays the referrer."""
    from core.auth import resolve_owner_id

    email = await launch_service.resolve_email(auth)
    if not email:
        return {"attributed": False}
    billing = await billing_repo.get_by_owner_id(resolve_owner_id(auth))
    if (billing or {}).get("subscription_status") in launch_service.LIVE_FOR_ENTRY:
        return {"attributed": False}
    await launch_waitlist_repo.join(email, referred_by=None, source="signup")
    wrote = await launch_waitlist_repo.attribute(email, body.ref.strip().upper())
    return {"attributed": bool(wrote)}
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_launch_service_me.py tests/unit/routers/test_launch_me.py tests/unit/routers/test_launch_public.py -v && uv run ruff check core/services/launch_service.py routers/launch.py`
Expected: all pass; ruff clean.

- [ ] **Step 7: Commit**

```bash
git add apps/backend/core/services/launch_service.py apps/backend/routers/launch.py apps/backend/tests/unit/services/test_launch_service_me.py apps/backend/tests/unit/routers/test_launch_me.py
git commit -m "feat(launch): /launch/me composition + referral attribution"
```

---

### Task 7: Stripe revenue read + restricted-key validation

**Files:**
- Create: `apps/backend/core/services/launch_revenue.py`
- Test: `apps/backend/tests/unit/services/test_launch_revenue.py`

**Interfaces:**
- Produces:
  - `launch_revenue.KEY_PATTERN_PROD = re.compile(r"^rk_live_[A-Za-z0-9]+$")`, `KEY_PATTERN_NONPROD = re.compile(r"^rk_(live|test)_[A-Za-z0-9]+$")`, `key_pattern() -> re.Pattern` (by `settings.ENVIRONMENT`).
  - `class KeyRejected(ValueError)` with `.reason` ∈ `bad_prefix | stripe_auth | stripe_permission | stripe_error`.
  - `async validate_key(key: str) -> None` — regex + one `stripe.BalanceTransaction.list(limit=1, api_key=key)` probe (via `run_in_thread`).
  - `async sum_net_revenue(key: str, start: datetime, end: datetime) -> tuple[int, str]` — `(cents, currency)`; raises `KeyRejected` on Stripe auth errors, `MixedCurrency` (subclass of `ValueError`) when transactions span currencies.
  - `secret_name(owner_id: str) -> str` = `f"isol8/{settings.ENVIRONMENT or 'dev'}/user-keys/{owner_id}/stripe-launch"`; `async store_key(owner_id, key)`, `async load_key(owner_id) -> str | None`, `async delete_key(owner_id)` (boto3 `secretsmanager` via `run_in_thread`, `create_or_update_secret` from `core.secrets_manager`).
  - `REVENUE_TYPES = {"charge", "payment"}`, `REFUND_TYPES = {"refund", "payment_refund"}`.
- Later tasks (8, 10) consume `validate_key`, `sum_net_revenue`, `store_key`, `load_key`, `delete_key`, `KeyRejected`, `MixedCurrency`.

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/unit/services/test_launch_revenue.py
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
import stripe

from core.services import launch_revenue as rev

START = datetime(2026, 9, 21, 16, tzinfo=timezone.utc)
END = datetime(2026, 10, 21, 16, tzinfo=timezone.utc)


def _page(items, has_more=False):
    page = MagicMock()
    page.data = [MagicMock(**i) for i in items]
    page.has_more = has_more
    return page


@pytest.mark.parametrize("env,key,ok", [
    ("prod", "rk_live_abc123", True), ("prod", "rk_test_abc123", False), ("prod", "sk_live_abc123", False),
    ("dev", "rk_test_abc123", True), ("dev", "sk_test_abc123", False), ("dev", "rk_live_abc 123", False),
])
async def test_validate_key_prefix(env, key, ok):
    with patch.object(rev.settings, "ENVIRONMENT", env), patch.object(rev.stripe.BalanceTransaction, "list", return_value=_page([])) as probe:
        if ok:
            await rev.validate_key(key)
            probe.assert_called_once_with(limit=1, api_key=key)
        else:
            with pytest.raises(rev.KeyRejected) as e:
                await rev.validate_key(key)
            assert e.value.reason == "bad_prefix"
            probe.assert_not_called()


async def test_validate_key_maps_stripe_errors():
    with patch.object(rev.settings, "ENVIRONMENT", "dev"):
        with patch.object(rev.stripe.BalanceTransaction, "list", side_effect=stripe.error.AuthenticationError("nope")):
            with pytest.raises(rev.KeyRejected) as e:
                await rev.validate_key("rk_test_abc")
            assert e.value.reason == "stripe_auth"
        with patch.object(rev.stripe.BalanceTransaction, "list", side_effect=stripe.error.PermissionError("no scope")):
            with pytest.raises(rev.KeyRejected) as e:
                await rev.validate_key("rk_test_abc")
            assert e.value.reason == "stripe_permission"


async def test_sum_net_revenue_charges_minus_refunds_paginated():
    pages = [
        _page([{"type": "charge", "amount": 5000, "currency": "usd", "id": "txn_1"},
               {"type": "refund", "amount": -1000, "currency": "usd", "id": "txn_2"},
               {"type": "payout", "amount": -4000, "currency": "usd", "id": "txn_3"}], has_more=True),
        _page([{"type": "payment", "amount": 2500, "currency": "usd", "id": "txn_4"},
               {"type": "stripe_fee", "amount": -30, "currency": "usd", "id": "txn_5"}]),
    ]
    with patch.object(rev.stripe.BalanceTransaction, "list", side_effect=pages) as lst:
        cents, currency = await rev.sum_net_revenue("rk_test_x", START, END)
    assert (cents, currency) == (6500, "usd")
    first_kwargs = lst.call_args_list[0].kwargs
    assert first_kwargs["created"] == {"gte": int(START.timestamp()), "lt": int(END.timestamp())}
    assert first_kwargs["limit"] == 100 and first_kwargs["api_key"] == "rk_test_x"
    assert lst.call_args_list[1].kwargs["starting_after"] == "txn_3"


async def test_sum_net_revenue_empty_is_zero_usd():
    with patch.object(rev.stripe.BalanceTransaction, "list", return_value=_page([])):
        assert await rev.sum_net_revenue("rk_test_x", START, END) == (0, "usd")


async def test_sum_net_revenue_mixed_currency_raises():
    page = _page([{"type": "charge", "amount": 100, "currency": "usd", "id": "a"},
                  {"type": "charge", "amount": 100, "currency": "eur", "id": "b"}])
    with patch.object(rev.stripe.BalanceTransaction, "list", return_value=page):
        with pytest.raises(rev.MixedCurrency):
            await rev.sum_net_revenue("rk_test_x", START, END)


def test_secret_name_is_on_the_user_keys_rail():
    with patch.object(rev.settings, "ENVIRONMENT", "prod"):
        assert rev.secret_name("user_1") == "isol8/prod/user-keys/user_1/stripe-launch"


async def test_store_and_load_key_round_trip_via_secrets_manager():
    sm = MagicMock()
    sm.get_secret_value.return_value = {"SecretString": "rk_test_x"}
    with patch.object(rev, "_sm", return_value=sm), patch.object(rev, "create_or_update_secret") as put:
        await rev.store_key("user_1", "rk_test_x")
        put.assert_called_once_with(sm, rev.secret_name("user_1"), "rk_test_x")
        assert await rev.load_key("user_1") == "rk_test_x"
        sm.get_secret_value.side_effect = sm.exceptions.ResourceNotFoundException = type("RNF", (Exception,), {})
        assert await rev.load_key("user_1") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_launch_revenue.py -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Write the module**

```python
# apps/backend/core/services/launch_revenue.py
"""Read an entrant's OWN Stripe revenue through a pasted restricted key.

Why a restricted key and not Connect OAuth: Stripe grants `read_only` OAuth
scope to Extension-registered apps only, and ours is a Platform (marketplace
payouts). A restricted key with Balance:Read + Charges:Read is read-only by
construction. It is stored on the BYO-key Secrets Manager rail (only the
backend task role and the per-user task EXECUTION role can read that prefix;
a running agent cannot) and is never logged or echoed.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

import boto3
import stripe

from core.config import settings
from core.dynamodb import run_in_thread
from core.secrets_manager import create_or_update_secret

logger = logging.getLogger(__name__)

KEY_PATTERN_PROD = re.compile(r"^rk_live_[A-Za-z0-9]+$")
KEY_PATTERN_NONPROD = re.compile(r"^rk_(live|test)_[A-Za-z0-9]+$")
REVENUE_TYPES = frozenset({"charge", "payment"})
REFUND_TYPES = frozenset({"refund", "payment_refund"})
_PAGE = 100


class KeyRejected(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class MixedCurrency(ValueError):
    pass


def key_pattern() -> re.Pattern:
    return KEY_PATTERN_PROD if settings.ENVIRONMENT == "prod" else KEY_PATTERN_NONPROD


def _list_page(key: str, **kwargs):
    return stripe.BalanceTransaction.list(api_key=key, **kwargs)


def _map_stripe_error(e: Exception) -> KeyRejected:
    if isinstance(e, stripe.error.AuthenticationError):
        return KeyRejected("stripe_auth")
    if isinstance(e, stripe.error.PermissionError):
        return KeyRejected("stripe_permission")
    return KeyRejected("stripe_error")


async def validate_key(key: str) -> None:
    if not key_pattern().match(key or ""):
        raise KeyRejected("bad_prefix")
    try:
        await run_in_thread(_list_page, key, limit=1)
    except stripe.error.StripeError as e:
        raise _map_stripe_error(e) from None


async def sum_net_revenue(key: str, start: datetime, end: datetime) -> tuple[int, str]:
    """Gross charges minus refunds in [start, end), in the account's balance
    currency. Payouts, fees, transfers and everything else are not revenue."""
    cents = 0
    currency: str | None = None
    kwargs: dict = {"created": {"gte": int(start.timestamp()), "lt": int(end.timestamp())}, "limit": _PAGE}
    while True:
        try:
            page = await run_in_thread(_list_page, key, **kwargs)
        except stripe.error.StripeError as e:
            raise _map_stripe_error(e) from None
        for txn in page.data:
            if txn.type not in REVENUE_TYPES and txn.type not in REFUND_TYPES:
                continue
            if currency is None:
                currency = txn.currency
            elif txn.currency != currency:
                raise MixedCurrency(f"{currency} vs {txn.currency}")
            cents += int(txn.amount)
        if not page.has_more or not page.data:
            break
        kwargs["starting_after"] = page.data[-1].id
    return cents, currency or "usd"


# --- Secrets Manager (BYO-key rail) ---------------------------------------


def secret_name(owner_id: str) -> str:
    env = settings.ENVIRONMENT or "dev"
    return f"isol8/{env}/user-keys/{owner_id}/stripe-launch"


def _sm():
    return boto3.client("secretsmanager", region_name=getattr(settings, "AWS_REGION", "us-east-1"))


async def store_key(owner_id: str, key: str) -> None:
    sm = _sm()
    await run_in_thread(create_or_update_secret, sm, secret_name(owner_id), key)


async def load_key(owner_id: str) -> str | None:
    sm = _sm()
    try:
        resp = await run_in_thread(sm.get_secret_value, SecretId=secret_name(owner_id))
    except sm.exceptions.ResourceNotFoundException:
        return None
    return resp.get("SecretString")


async def delete_key(owner_id: str) -> None:
    sm = _sm()
    try:
        await run_in_thread(sm.delete_secret, SecretId=secret_name(owner_id), ForceDeleteWithoutRecovery=True)
    except sm.exceptions.ResourceNotFoundException:
        return
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_launch_revenue.py -v && uv run ruff check core/services/launch_revenue.py`
Expected: 12 passed; ruff clean. (If `stripe.error.PermissionError` does not exist in the installed stripe version, use `stripe.PermissionError` / `stripe.AuthenticationError` — check with `uv run python -c "import stripe; print(stripe.PermissionError)"` and use whichever resolves, in both module and test.)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/launch_revenue.py apps/backend/tests/unit/services/test_launch_revenue.py
git commit -m "feat(launch): restricted-key validation + Stripe net-revenue summation"
```

---

### Task 8: `POST /launch/enter` + `GET /launch/leaderboard`

**Files:**
- Modify: `apps/backend/core/services/launch_service.py` (add `enter`, `leaderboard`)
- Modify: `apps/backend/routers/launch.py` (append two routes)
- Test: `apps/backend/tests/unit/services/test_launch_service_enter.py`, `apps/backend/tests/unit/routers/test_launch_enter.py`

**Interfaces:**
- Consumes: Task 6 `can_enter/resolve_email`, Task 7 `validate_key/sum_net_revenue/store_key/KeyRejected/MixedCurrency`, `launch_entries_repo.upsert/list_all/count`, `launch_waitlist_repo.get`.
- Produces:
  - `class EnterRefused(Exception)` with `.reason` (any `can_enter` reason, or a `KeyRejected.reason`, or `mixed_currency`).
  - `async launch_service.enter(auth, s, *, display_name, stripe_key, public) -> dict` (the `_public_entry` shape).
  - `launch_service.rank(entries: list[dict]) -> list[dict]` — public rows sorted by `delta_cents` desc, then `entered_at` asc; `pct = delta/baseline` rounded 4dp, `None` when baseline ≤ 0.
  - `async launch_service.leaderboard(limit: int = 50) -> dict` — `{"entrants": n, "rows": [...]}`.

- [ ] **Step 1: Write the failing service tests**

```python
# apps/backend/tests/unit/services/test_launch_service_enter.py
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from core.auth import AuthContext
from core.services import launch_phase, launch_revenue, launch_service

NOW = datetime.now(timezone.utc)
DROP = NOW - timedelta(days=1)
S = launch_phase.Schedule(DROP, DROP + timedelta(hours=72), DROP + timedelta(hours=72, days=30), 220, 72, 30)
ACTIVE = {"subscription_status": "active"}
ACTIVATED = {"bootstrap_state": "delivered", "bootstrap_delivered_at": "x"}


@pytest.fixture
def deps():
    with patch("core.services.launch_service.billing_repo.get_by_owner_id", AsyncMock(return_value=ACTIVE)), \
         patch("core.services.launch_service.readiness_state.resolve_onboarding_state", AsyncMock(return_value=ACTIVATED)), \
         patch("core.services.launch_service.launch_waitlist_repo.get", AsyncMock(return_value={"position": 12})), \
         patch("core.services.launch_service.launch_revenue.validate_key", AsyncMock()) as validate, \
         patch("core.services.launch_service.launch_revenue.sum_net_revenue", AsyncMock(return_value=(80_00, "usd"))) as summer, \
         patch("core.services.launch_service.launch_revenue.store_key", AsyncMock()) as store, \
         patch("core.services.launch_service.launch_entries_repo.upsert", AsyncMock(side_effect=lambda o, **kw: {"owner_id": o, **kw, "entered_at": "t", "window_cents": 0, "delta_cents": 0})) as upsert:
        yield {"validate": validate, "sum": summer, "store": store, "upsert": upsert}


async def test_enter_happy_path_stores_key_and_baseline(deps):
    auth = AuthContext(user_id="user_1", email="me@x.com")
    body = await launch_service.enter(auth, S, display_name="Ada", stripe_key="rk_test_x", public=True)
    deps["validate"].assert_awaited_once_with("rk_test_x")
    deps["store"].assert_awaited_once_with("user_1", "rk_test_x")
    start, end = deps["sum"].await_args.args[1], deps["sum"].await_args.args[2]
    assert end == S.competition_start_at and start == S.competition_start_at - timedelta(days=30)
    deps["upsert"].assert_awaited_once_with("user_1", display_name="Ada", public=True, waitlist_position=12, currency="usd", baseline_cents=8000)
    assert body["display_name"] == "Ada" and body["baseline_cents"] == 8000


async def test_enter_refuses_when_gate_fails(deps):
    with patch("core.services.launch_service.billing_repo.get_by_owner_id", AsyncMock(return_value=None)):
        with pytest.raises(launch_service.EnterRefused) as e:
            await launch_service.enter(AuthContext(user_id="user_1"), S, display_name="Ada", stripe_key="rk_test_x", public=True)
    assert e.value.reason == "no_subscription"
    deps["store"].assert_not_awaited()


async def test_enter_maps_key_rejection_and_never_stores(deps):
    deps["validate"].side_effect = launch_revenue.KeyRejected("stripe_auth")
    with pytest.raises(launch_service.EnterRefused) as e:
        await launch_service.enter(AuthContext(user_id="user_1"), S, display_name="Ada", stripe_key="sk_live_x", public=True)
    assert e.value.reason == "stripe_auth"
    deps["store"].assert_not_awaited()


async def test_enter_mixed_currency_refused(deps):
    deps["sum"].side_effect = launch_revenue.MixedCurrency("usd vs eur")
    with pytest.raises(launch_service.EnterRefused) as e:
        await launch_service.enter(AuthContext(user_id="user_1"), S, display_name="Ada", stripe_key="rk_test_x", public=True)
    assert e.value.reason == "mixed_currency"


def test_rank_orders_and_computes_pct():
    rows = launch_service.rank([
        {"owner_id": "a", "display_name": "A", "public": True, "delta_cents": 500, "baseline_cents": 1000, "currency": "usd", "entered_at": "2"},
        {"owner_id": "b", "display_name": "B", "public": True, "delta_cents": 500, "baseline_cents": 0, "currency": "usd", "entered_at": "1"},
        {"owner_id": "c", "display_name": "C", "public": False, "delta_cents": 9999, "baseline_cents": 1, "currency": "usd", "entered_at": "0"},
        {"owner_id": "d", "display_name": "D", "public": True, "delta_cents": -20, "baseline_cents": 100, "currency": "eur", "entered_at": "3"},
    ])
    assert [r["display_name"] for r in rows] == ["B", "A", "D"]
    assert rows[0] == {"rank": 1, "display_name": "B", "delta_cents": 500, "pct": None, "currency": "usd"}
    assert rows[1]["pct"] == 0.5 and rows[2]["pct"] == -0.2
```

- [ ] **Step 2: Write the failing router tests**

```python
# apps/backend/tests/unit/routers/test_launch_enter.py
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from core.services import launch_service

DROP = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()


@pytest.fixture
def configured():
    with patch("core.services.launch_phase.settings.LAUNCH_DROP_AT", DROP):
        yield


@patch("routers.launch.launch_service.enter", AsyncMock(return_value={"display_name": "Ada"}))
async def test_enter_ok(configured, async_client):
    r = await async_client.post("/api/v1/launch/enter", json={"display_name": "Ada", "stripe_key": "rk_test_" + "x" * 20, "public": True})
    assert r.status_code == 200 and r.json()["display_name"] == "Ada"


@patch("routers.launch.launch_service.enter", AsyncMock(side_effect=launch_service.EnterRefused("stripe_auth")))
async def test_enter_refused_maps_to_400_reason_and_never_echoes_key(configured, async_client):
    key = "rk_test_" + "s3cr3t" * 4
    r = await async_client.post("/api/v1/launch/enter", json={"display_name": "Ada", "stripe_key": key})
    assert r.status_code == 400 and r.json()["detail"] == "stripe_auth"
    assert key not in r.text


async def test_enter_validates_body(configured, async_client):
    r = await async_client.post("/api/v1/launch/enter", json={"display_name": "A", "stripe_key": "short"})
    assert r.status_code == 422


@patch("routers.launch.launch_service.leaderboard", AsyncMock(return_value={"entrants": 1, "rows": [{"rank": 1, "display_name": "A", "delta_cents": 5, "pct": None, "currency": "usd"}]}))
async def test_leaderboard_public_cached(configured, unauthenticated_async_client):
    r = await unauthenticated_async_client.get("/api/v1/launch/leaderboard")
    assert r.status_code == 200 and r.json()["rows"][0]["rank"] == 1
    assert r.headers["cache-control"] == "public, max-age=60"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_launch_service_enter.py tests/unit/routers/test_launch_enter.py -v`
Expected: FAIL — `AttributeError: enter` / 404s.

- [ ] **Step 4: Extend the service**

Append to `core/services/launch_service.py` (add `from datetime import timedelta` and `from core.services import launch_revenue` to the imports):

```python
class EnterRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


BASELINE_DAYS = 30


async def enter(auth: AuthContext, s: Schedule, *, display_name: str, stripe_key: str, public: bool) -> dict:
    owner_id = resolve_owner_id(auth)
    billing = await billing_repo.get_by_owner_id(owner_id)
    onboarding = await readiness_state.resolve_onboarding_state(owner_id)
    ok, reason = can_enter(s=s, billing=billing, onboarding=onboarding)
    if not ok:
        raise EnterRefused(reason or "refused")
    try:
        await launch_revenue.validate_key(stripe_key)
    except launch_revenue.KeyRejected as e:
        raise EnterRefused(e.reason) from None
    baseline_end = s.competition_start_at
    baseline_start = baseline_end - timedelta(days=BASELINE_DAYS)
    try:
        baseline_cents, currency = await launch_revenue.sum_net_revenue(stripe_key, baseline_start, baseline_end)
    except launch_revenue.MixedCurrency:
        raise EnterRefused("mixed_currency") from None
    except launch_revenue.KeyRejected as e:
        raise EnterRefused(e.reason) from None
    await launch_revenue.store_key(owner_id, stripe_key)
    position = None
    email = await resolve_email(auth)
    if email:
        row = await launch_waitlist_repo.get(email)
        position = int(row["position"]) if row else None
    entry = await launch_entries_repo.upsert(
        owner_id,
        display_name=display_name.strip(),
        public=public,
        waitlist_position=position,
        currency=currency,
        baseline_cents=baseline_cents,
    )
    logger.info("launch: %s entered (position=%s baseline=%s %s)", owner_id, position, baseline_cents, currency)
    return _public_entry(entry)


def rank(entries: list[dict]) -> list[dict]:
    visible = [e for e in entries if e.get("public", True)]
    visible.sort(key=lambda e: (-int(e.get("delta_cents", 0)), e.get("entered_at") or ""))
    rows = []
    for i, e in enumerate(visible, start=1):
        baseline = int(e.get("baseline_cents", 0))
        delta = int(e.get("delta_cents", 0))
        rows.append({
            "rank": i,
            "display_name": e["display_name"],
            "delta_cents": delta,
            "pct": round(delta / baseline, 4) if baseline > 0 else None,
            "currency": e.get("currency") or "usd",
        })
    return rows


async def leaderboard(limit: int = 50) -> dict:
    entries = await launch_entries_repo.list_all()
    return {"entrants": len(entries), "rows": rank(entries)[:limit]}
```

- [ ] **Step 5: Append the routes**

```python
@router.post("/enter", operation_id="launch_enter")
async def enter(body: EnterRequest, auth: AuthContext = Depends(get_current_user), s: Schedule = Depends(require_launch)) -> dict:
    try:
        return await launch_service.enter(
            auth, s, display_name=body.display_name, stripe_key=body.stripe_key.strip(), public=body.public
        )
    except launch_service.EnterRefused as e:
        # The reason is a short enum; the key is never part of any response or log.
        raise HTTPException(status_code=400, detail=e.reason)


@router.get("/leaderboard", response_model=LeaderboardResponse, operation_id="launch_leaderboard")
async def leaderboard(response: Response, s: Schedule = Depends(require_launch)) -> dict:
    response.headers["Cache-Control"] = "public, max-age=60"
    return await launch_service.leaderboard()
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_launch_service_enter.py tests/unit/routers/test_launch_enter.py tests/unit/routers/test_launch_public.py tests/unit/routers/test_launch_me.py -v && uv run ruff check core/services/launch_service.py routers/launch.py`
Expected: all pass; ruff clean (remove any `# noqa: F401` added in Task 5).

- [ ] **Step 7: Commit**

```bash
git add apps/backend/core/services/launch_service.py apps/backend/routers/launch.py apps/backend/tests/unit/services/test_launch_service_enter.py apps/backend/tests/unit/routers/test_launch_enter.py
git commit -m "feat(launch): enter with a restricted key + public leaderboard"
```

---

### Task 9: Referral payout on `invoice.paid`

**Files:**
- Create: `apps/backend/core/services/launch_referral.py`
- Modify: `apps/backend/core/services/stripe_webhook_handlers.py` (`handle_invoice_paid`, ~line 1271)
- Test: `apps/backend/tests/unit/services/test_launch_referral.py`, extend `tests/unit/services/test_stripe_webhook_handlers.py`

**Interfaces:**
- Consumes: `launch_waitlist_repo.get/get_by_referral_code/mark_rewarded/add_counter`, `billing_repo.list_by_stripe_customer_id/get_by_owner_id`, `user_repo.get`, `stripe.Subscription.retrieve/modify`, `capture_product_event`, `settings.STRIPE_FREE_MONTH_COUPON_ID`.
- Produces:
  - `async launch_referral.on_invoice_paid(invoice: dict) -> None` — never raises.
  - `async launch_referral.grant_free_month(owner_id: str, *, idempotency_key: str) -> bool` — True if a coupon was applied to the owner's live subscription, False if it had to be banked (no sub / already discounted / no coupon configured).
  - `launch_referral.subscription_has_discount(sub: dict) -> bool`.

**Behaviour** (from the spec): on `invoice.paid` with `amount_paid > 0`, resolve the paying owner from `invoice.customer` (skip orgs). (1) If the payer's waitlist row has `referred_by` and `mark_rewarded` wins the CAS → resolve the referrer, `referrals_paid += 1`, `grant_free_month(referrer)`; if it returns False → `free_months_banked += 1` on the referrer. (2) If the payer's own row has `free_months_banked > 0` → decrement, `grant_free_month(payer)`; if that returns False → increment back.

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/unit/services/test_launch_referral.py
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services import launch_referral as ref

PAYER_ROW = {"email": "payer@x.com", "referral_code": "PAYER123", "referred_by": "REFER123", "free_months_banked": 0}
REFERRER_ROW = {"email": "ref@x.com", "referral_code": "REFER123", "user_id": "user_ref", "free_months_banked": 0}


def _invoice(**over):
    inv = {"id": "in_1", "customer": "cus_1", "amount_paid": 5000, "billing_reason": "subscription_create"}
    inv.update(over)
    return inv


@pytest.fixture
def deps():
    with patch.object(ref, "billing_repo") as billing, patch.object(ref, "user_repo") as users, \
         patch.object(ref, "launch_waitlist_repo") as wl, patch.object(ref, "stripe") as stripe_mod, \
         patch.object(ref, "capture_product_event") as capture, \
         patch.object(ref.settings, "STRIPE_FREE_MONTH_COUPON_ID", "coupon_free"):
        billing.list_by_stripe_customer_id = AsyncMock(return_value=[{"owner_id": "user_payer", "stripe_subscription_id": "sub_payer"}])
        billing.get_by_owner_id = AsyncMock(return_value={"owner_id": "user_ref", "stripe_subscription_id": "sub_ref", "subscription_status": "active"})
        users.get = AsyncMock(side_effect=lambda uid: {"user_id": uid, "email": {"user_payer": "payer@x.com", "user_ref": "ref@x.com"}[uid]})
        wl.get = AsyncMock(side_effect=lambda e: {"payer@x.com": dict(PAYER_ROW), "ref@x.com": dict(REFERRER_ROW)}.get(e))
        wl.get_by_referral_code = AsyncMock(return_value=dict(REFERRER_ROW))
        wl.mark_rewarded = AsyncMock(return_value=True)
        wl.add_counter = AsyncMock(return_value=1)
        stripe_mod.Subscription.retrieve = MagicMock(return_value={"id": "sub_ref", "discount": None, "discounts": []})
        stripe_mod.Subscription.modify = MagicMock()
        yield {"billing": billing, "wl": wl, "stripe": stripe_mod, "capture": capture}


async def test_rewards_referrer_once_on_first_paid_invoice(deps):
    await ref.on_invoice_paid(_invoice())
    deps["wl"].mark_rewarded.assert_awaited_once_with("payer@x.com")
    deps["wl"].add_counter.assert_any_await("ref@x.com", "referrals_paid", 1)
    deps["stripe"].Subscription.modify.assert_called_once_with(
        "sub_ref", discounts=[{"coupon": "coupon_free"}], idempotency_key="referral_reward:sub_ref:in_1"
    )
    deps["capture"].assert_called()


async def test_zero_amount_invoice_does_nothing(deps):
    await ref.on_invoice_paid(_invoice(amount_paid=0))
    deps["wl"].mark_rewarded.assert_not_awaited()
    deps["stripe"].Subscription.modify.assert_not_called()


async def test_cas_loss_means_no_double_reward(deps):
    deps["wl"].mark_rewarded = AsyncMock(return_value=False)
    await ref.on_invoice_paid(_invoice())
    deps["stripe"].Subscription.modify.assert_not_called()
    assert not any(c.args[1] == "referrals_paid" for c in deps["wl"].add_counter.await_args_list)


async def test_banks_when_referrer_has_no_subscription(deps):
    deps["billing"].get_by_owner_id = AsyncMock(return_value={"owner_id": "user_ref", "subscription_status": "trial_local"})
    await ref.on_invoice_paid(_invoice())
    deps["stripe"].Subscription.modify.assert_not_called()
    deps["wl"].add_counter.assert_any_await("ref@x.com", "free_months_banked", 1)


async def test_banks_when_referrer_already_discounted(deps):
    deps["stripe"].Subscription.retrieve = MagicMock(return_value={"id": "sub_ref", "discount": {"coupon": {"id": "x"}}})
    await ref.on_invoice_paid(_invoice())
    deps["stripe"].Subscription.modify.assert_not_called()
    deps["wl"].add_counter.assert_any_await("ref@x.com", "free_months_banked", 1)


async def test_banks_when_no_coupon_configured(deps):
    with patch.object(ref.settings, "STRIPE_FREE_MONTH_COUPON_ID", ""):
        await ref.on_invoice_paid(_invoice())
    deps["stripe"].Subscription.modify.assert_not_called()
    deps["wl"].add_counter.assert_any_await("ref@x.com", "free_months_banked", 1)


async def test_drains_one_banked_month_for_the_payer(deps):
    payer = dict(PAYER_ROW, referred_by=None, free_months_banked=2)
    deps["wl"].get = AsyncMock(side_effect=lambda e: {"payer@x.com": payer}.get(e))
    deps["billing"].get_by_owner_id = AsyncMock(return_value={"owner_id": "user_payer", "stripe_subscription_id": "sub_payer", "subscription_status": "active"})
    deps["stripe"].Subscription.retrieve = MagicMock(return_value={"id": "sub_payer", "discount": None})
    await ref.on_invoice_paid(_invoice())
    deps["wl"].add_counter.assert_any_await("payer@x.com", "free_months_banked", -1)
    deps["stripe"].Subscription.modify.assert_called_once_with(
        "sub_payer", discounts=[{"coupon": "coupon_free"}], idempotency_key="banked_month:sub_payer:in_1"
    )


async def test_org_owner_is_skipped(deps):
    deps["billing"].list_by_stripe_customer_id = AsyncMock(return_value=[{"owner_id": "org_1", "stripe_subscription_id": "sub_o"}])
    await ref.on_invoice_paid(_invoice())
    deps["wl"].get.assert_not_awaited()


async def test_never_raises(deps):
    deps["billing"].list_by_stripe_customer_id = AsyncMock(side_effect=RuntimeError("ddb down"))
    await ref.on_invoice_paid(_invoice())  # no exception


def test_subscription_has_discount_handles_both_shapes():
    assert ref.subscription_has_discount({"discount": {"id": "di_1"}}) is True
    assert ref.subscription_has_discount({"discounts": ["di_1"]}) is True
    assert ref.subscription_has_discount({"discount": None, "discounts": []}) is False
```

Add to `tests/unit/services/test_stripe_webhook_handlers.py`:

```python
class TestInvoicePaidDelegatesToLaunchReferral:
    async def test_invoice_paid_calls_launch_referral(self):
        event = {"id": "evt_i", "type": "invoice.paid", "data": {"object": {"id": "in_1", "customer": "cus_1", "amount_paid": 5000}}}
        with patch("core.services.launch_referral.on_invoice_paid", new_callable=AsyncMock) as hook:
            await handlers.handle_invoice_paid(event, MagicMock())
        hook.assert_awaited_once_with(event["data"]["object"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_launch_referral.py tests/unit/services/test_stripe_webhook_handlers.py -k "launch or InvoicePaid" -v`
Expected: FAIL — `ImportError` / hook not awaited.

- [ ] **Step 3: Write the module**

```python
# apps/backend/core/services/launch_referral.py
"""Referral free months, paid out from Stripe ``invoice.paid``.

Rule (spec): the referrer earns a free month when the referred person's FIRST
PAID invoice lands. The once-only guard is ``launch_waitlist_repo.mark_rewarded``
on the referred row (a conditional write). The grant is a 100%-off-once coupon
applied to the referrer's subscription; when it cannot be applied it is banked
on the referrer's row and drained one per ``invoice.paid`` of their own.

Personal owners only. Never raises — this runs inside the webhook handler.
"""

from __future__ import annotations

import logging

import stripe

from core.config import settings
from core.dynamodb import run_in_thread
from core.observability.product_analytics import capture_product_event
from core.repositories import billing_repo, launch_waitlist_repo, user_repo

logger = logging.getLogger(__name__)


def subscription_has_discount(sub: dict) -> bool:
    return bool(sub.get("discount")) or bool(sub.get("discounts"))


async def grant_free_month(owner_id: str, *, idempotency_key: str) -> bool:
    """Apply the free-month coupon to the owner's live subscription.
    False ⇒ caller must bank it (no sub, already discounted, or no coupon)."""
    coupon = settings.STRIPE_FREE_MONTH_COUPON_ID
    if not coupon:
        return False
    billing = await billing_repo.get_by_owner_id(owner_id)
    sub_id = (billing or {}).get("stripe_subscription_id")
    if not sub_id or (billing or {}).get("subscription_status") not in ("active", "trialing"):
        return False
    sub = await run_in_thread(stripe.Subscription.retrieve, sub_id)
    if subscription_has_discount(sub):
        return False
    try:
        await run_in_thread(stripe.Subscription.modify, sub_id, discounts=[{"coupon": coupon}], idempotency_key=idempotency_key)
    except stripe.error.InvalidRequestError as e:
        # Older API versions reject `discounts` on subscription update; `coupon` is the legacy spelling.
        if "discounts" not in str(e):
            raise
        await run_in_thread(stripe.Subscription.modify, sub_id, coupon=coupon, idempotency_key=f"{idempotency_key}:legacy")
    capture_product_event(distinct_id=owner_id, event="launch_free_month_applied", properties={"subscription_id": sub_id})
    return True


async def _email_for_owner(owner_id: str) -> str | None:
    row = await user_repo.get(owner_id)
    email = (row or {}).get("email")
    return email.strip().lower() if email else None


async def _reward_referrer(payer_email: str, payer_row: dict, invoice_id: str) -> None:
    code = payer_row.get("referred_by")
    if not code:
        return
    if not await launch_waitlist_repo.mark_rewarded(payer_email):
        return  # already paid out for this referred person
    referrer = await launch_waitlist_repo.get_by_referral_code(code)
    if not referrer:
        return
    await launch_waitlist_repo.add_counter(referrer["email"], "referrals_paid", 1)
    referrer_owner = referrer.get("user_id")
    sub_id = ((await billing_repo.get_by_owner_id(referrer_owner)) or {}).get("stripe_subscription_id") if referrer_owner else None
    applied = False
    if referrer_owner:
        applied = await grant_free_month(referrer_owner, idempotency_key=f"referral_reward:{sub_id}:{invoice_id}")
    if not applied:
        await launch_waitlist_repo.add_counter(referrer["email"], "free_months_banked", 1)
    capture_product_event(
        distinct_id=referrer_owner or referrer["email"],
        event="launch_referral_rewarded",
        properties={"applied": applied, "referred_owner_email_hash": hash(payer_email) & 0xFFFFFFFF},
    )


async def _drain_banked(payer_owner: str, payer_email: str, payer_row: dict, invoice_id: str) -> None:
    if int(payer_row.get("free_months_banked", 0)) <= 0:
        return
    remaining = await launch_waitlist_repo.add_counter(payer_email, "free_months_banked", -1)
    sub_id = ((await billing_repo.get_by_owner_id(payer_owner)) or {}).get("stripe_subscription_id")
    if await grant_free_month(payer_owner, idempotency_key=f"banked_month:{sub_id}:{invoice_id}"):
        return
    await launch_waitlist_repo.add_counter(payer_email, "free_months_banked", 1)  # put it back
    logger.info("launch: banked month not applicable yet for %s (remaining=%s)", payer_owner, remaining + 1)


async def on_invoice_paid(invoice: dict) -> None:
    try:
        if int(invoice.get("amount_paid") or 0) <= 0:
            return
        customer = invoice.get("customer")
        if not customer:
            return
        rows = await billing_repo.list_by_stripe_customer_id(customer)
        if not rows:
            return
        sub_id = invoice.get("subscription")
        matches = [r for r in rows if r.get("stripe_subscription_id") == sub_id] if sub_id else rows
        owner_id = (matches or rows)[0]["owner_id"]
        if owner_id.startswith("org_"):
            return
        payer_email = await _email_for_owner(owner_id)
        if not payer_email:
            return
        payer_row = await launch_waitlist_repo.get(payer_email)
        if not payer_row:
            return
        invoice_id = invoice.get("id") or "unknown"
        await _reward_referrer(payer_email, payer_row, invoice_id)
        await _drain_banked(owner_id, payer_email, payer_row, invoice_id)
    except Exception:  # noqa: BLE001 — a launch bug must never fail the billing webhook
        logger.warning("launch_referral.on_invoice_paid failed", exc_info=True)
```

- [ ] **Step 4: Wire the webhook**

Replace the body of `handle_invoice_paid` in `stripe_webhook_handlers.py`:

```python
async def handle_invoice_paid(event: dict[str, Any], billing_service: BillingService) -> Response | None:
    event_data = event["data"]["object"]
    logger.info("Payment succeeded for customer %s", event_data.get("customer"))
    # Launch Competition referral payout — best-effort, never raises.
    from core.services import launch_referral

    await launch_referral.on_invoice_paid(event_data)
    return None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_launch_referral.py tests/unit/services/test_stripe_webhook_handlers.py -v && uv run ruff check core/services/launch_referral.py core/services/stripe_webhook_handlers.py`
Expected: all pass; ruff clean.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/launch_referral.py apps/backend/core/services/stripe_webhook_handlers.py apps/backend/tests/unit/services/test_launch_referral.py apps/backend/tests/unit/services/test_stripe_webhook_handlers.py
git commit -m "feat(launch): referral free month on the referred person's first paid invoice"
```

---

### Task 10: Hourly revenue sweep + lifespan wiring

**Files:**
- Modify: `apps/backend/core/services/launch_revenue.py` (append `refresh_entry`, `refresh_all`, `sweep_loop`)
- Modify: `apps/backend/main.py` (start the loop in `lifespan`, register in `system_health.BACKGROUND_TASKS`)
- Test: `apps/backend/tests/unit/services/test_launch_revenue_sweep.py`, extend `tests/unit/test_main_lifespan.py`

**Interfaces:**
- Consumes: Task 7 `load_key/sum_net_revenue/KeyRejected/MixedCurrency`, `launch_entries_repo.list_all/set_revenue/set_refresh_error`, `launch_phase.schedule/phase`.
- Produces: `async refresh_entry(entry: dict, s: Schedule, now: datetime | None = None) -> None`; `async refresh_all(*, force: bool = False, now=None) -> int` (entries refreshed); `async sweep_loop(interval_s: int = 3600) -> None`; `STALE_AFTER_S = 55 * 60`; `SWEEP_GRACE_AFTER_END = timedelta(hours=24)`.

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/unit/services/test_launch_revenue_sweep.py
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from core.services import launch_phase, launch_revenue as rev

NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
DROP = datetime(2026, 9, 18, 16, tzinfo=timezone.utc)
S = launch_phase.Schedule(DROP, DROP + timedelta(hours=72), DROP + timedelta(hours=72, days=30), 220, 72, 30)


@pytest.fixture
def repo():
    with patch.object(rev, "launch_entries_repo") as r:
        r.set_revenue = AsyncMock()
        r.set_refresh_error = AsyncMock()
        yield r


async def test_refresh_entry_writes_window_minus_baseline(repo):
    with patch.object(rev, "load_key", AsyncMock(return_value="rk_test_x")), \
         patch.object(rev, "sum_net_revenue", AsyncMock(return_value=(12_000, "usd"))) as summer:
        await rev.refresh_entry({"owner_id": "u1", "baseline_cents": 5_000}, S, now=NOW)
    summer.assert_awaited_once_with("rk_test_x", S.competition_start_at, NOW)
    repo.set_revenue.assert_awaited_once_with("u1", window_cents=12_000, delta_cents=7_000, currency="usd")


async def test_refresh_entry_clamps_window_end_to_competition_end(repo):
    late = S.competition_end_at + timedelta(days=3)
    with patch.object(rev, "load_key", AsyncMock(return_value="rk")), patch.object(rev, "sum_net_revenue", AsyncMock(return_value=(0, "usd"))) as summer:
        await rev.refresh_entry({"owner_id": "u1", "baseline_cents": 0}, S, now=late)
    assert summer.await_args.args[2] == S.competition_end_at


async def test_refresh_entry_records_errors_instead_of_raising(repo):
    with patch.object(rev, "load_key", AsyncMock(return_value="rk")), patch.object(rev, "sum_net_revenue", AsyncMock(side_effect=rev.KeyRejected("stripe_auth"))):
        await rev.refresh_entry({"owner_id": "u1", "baseline_cents": 0}, S, now=NOW)
    repo.set_refresh_error.assert_awaited_once_with("u1", "stripe_auth")
    with patch.object(rev, "load_key", AsyncMock(return_value=None)):
        await rev.refresh_entry({"owner_id": "u2", "baseline_cents": 0}, S, now=NOW)
    repo.set_refresh_error.assert_awaited_with("u2", "missing_key")


async def test_refresh_all_skips_fresh_rows_unless_forced(repo):
    fresh = (NOW - timedelta(minutes=10)).isoformat()
    stale = (NOW - timedelta(minutes=70)).isoformat()
    repo.list_all = AsyncMock(return_value=[{"owner_id": "a", "refreshed_at": fresh}, {"owner_id": "b", "refreshed_at": stale}, {"owner_id": "c"}])
    with patch.object(rev, "refresh_entry", AsyncMock()) as one, patch.object(rev.launch_phase, "schedule", return_value=S):
        assert await rev.refresh_all(now=NOW) == 2
        assert {c.args[0]["owner_id"] for c in one.await_args_list} == {"b", "c"}
        one.reset_mock()
        assert await rev.refresh_all(force=True, now=NOW) == 3


async def test_refresh_all_noop_outside_window(repo):
    repo.list_all = AsyncMock(return_value=[{"owner_id": "a"}])
    with patch.object(rev, "refresh_entry", AsyncMock()) as one, patch.object(rev.launch_phase, "schedule", return_value=S):
        assert await rev.refresh_all(now=DROP - timedelta(days=1)) == 0
        assert await rev.refresh_all(now=S.competition_end_at + timedelta(days=2)) == 0
        one.assert_not_awaited()
```

Add to `tests/unit/test_main_lifespan.py`:

```python
@pytest.mark.asyncio
async def test_lifespan_registers_launch_sweep_when_configured():
    """The revenue sweep is a lifespan task like running_gauges — registered in
    BACKGROUND_TASKS so /admin/system/health can see it — only when
    LAUNCH_DROP_AT is set."""
    from core.services import system_health
    from main import _start_launch_sweep

    with patch("main.settings.LAUNCH_DROP_AT", ""):
        assert _start_launch_sweep() is None
        assert "launch_sweep" not in system_health.BACKGROUND_TASKS
    with patch("main.settings.LAUNCH_DROP_AT", "2026-09-18T16:00:00Z"), patch("core.services.launch_revenue.sweep_loop", new_callable=AsyncMock):
        task = _start_launch_sweep()
        assert task is system_health.BACKGROUND_TASKS["launch_sweep"]
        task.cancel()
        system_health.BACKGROUND_TASKS.pop("launch_sweep", None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_launch_revenue_sweep.py tests/unit/test_main_lifespan.py -k "launch" -v`
Expected: FAIL — `AttributeError: refresh_entry` / `ImportError: _start_launch_sweep`.

- [ ] **Step 3: Append the sweep to `launch_revenue.py`**

Add imports `import asyncio`, `from datetime import timedelta, timezone`, `from core.repositories import launch_entries_repo`, `from core.services import launch_phase`, `from core.services.launch_phase import Schedule`, then append:

```python
# --- Sweep -----------------------------------------------------------------

STALE_AFTER_S = 55 * 60
SWEEP_GRACE_AFTER_END = timedelta(hours=24)
_PER_OWNER_PAUSE_S = 0.2


async def refresh_entry(entry: dict, s: Schedule, now: datetime | None = None) -> None:
    owner_id = entry["owner_id"]
    now = now or datetime.now(timezone.utc)
    end = min(now, s.competition_end_at)
    try:
        key = await load_key(owner_id)
        if not key:
            await launch_entries_repo.set_refresh_error(owner_id, "missing_key")
            return
        window, currency = await sum_net_revenue(key, s.competition_start_at, end)
    except KeyRejected as e:
        await launch_entries_repo.set_refresh_error(owner_id, e.reason)
        return
    except MixedCurrency:
        await launch_entries_repo.set_refresh_error(owner_id, "mixed_currency")
        return
    except Exception:  # noqa: BLE001 — one bad entry must not stop the sweep
        logger.warning("launch sweep: refresh failed for %s", owner_id, exc_info=True)
        await launch_entries_repo.set_refresh_error(owner_id, "refresh_failed")
        return
    delta = window - int(entry.get("baseline_cents", 0))
    await launch_entries_repo.set_revenue(owner_id, window_cents=window, delta_cents=delta, currency=currency)


def _is_stale(entry: dict, now: datetime) -> bool:
    raw = entry.get("refreshed_at")
    if not raw:
        return True
    return (now - launch_phase.parse_iso(raw)).total_seconds() > STALE_AFTER_S


async def refresh_all(*, force: bool = False, now: datetime | None = None) -> int:
    s = launch_phase.schedule()
    now = now or datetime.now(timezone.utc)
    if s is None or now < s.competition_start_at or now > s.competition_end_at + SWEEP_GRACE_AFTER_END:
        return 0
    entries = await launch_entries_repo.list_all()
    n = 0
    for entry in entries:
        if not force and not _is_stale(entry, now):
            continue
        await refresh_entry(entry, s, now=now)
        n += 1
        await asyncio.sleep(_PER_OWNER_PAUSE_S)
    return n


async def sweep_loop(interval_s: int = 3600) -> None:
    """Lifespan task. ponytail: in-process on the single backend task; a
    scheduled Lambda if the fleet ever runs more than one instance."""
    while True:
        try:
            n = await refresh_all()
            logger.info("launch sweep: refreshed %d entries", n)
        except Exception:  # noqa: BLE001
            logger.warning("launch sweep iteration failed", exc_info=True)
        await asyncio.sleep(interval_s)
```

- [ ] **Step 4: Wire the lifespan**

In `main.py`, next to `_running_count_gauge_loop`, add:

```python
def _start_launch_sweep() -> asyncio.Task | None:
    """Start the Launch Competition revenue sweep when the feature is configured.
    Registered like running_gauges so /admin/system/health can see it."""
    if not (settings.LAUNCH_DROP_AT or "").strip():
        return None
    from core.services import launch_revenue, system_health

    task = asyncio.create_task(launch_revenue.sweep_loop())
    system_health.BACKGROUND_TASKS["launch_sweep"] = task
    return task
```

and in `lifespan`, right after `system_health.BACKGROUND_TASKS["running_gauges"] = gauge_task`:

```python
    _start_launch_sweep()
```

(`settings` is already imported in `main.py`; confirm with `grep -n "^from core.config import settings" main.py`.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_launch_revenue_sweep.py tests/unit/services/test_launch_revenue.py tests/unit/test_main_lifespan.py -v && uv run ruff check core/services/launch_revenue.py main.py`
Expected: all pass; ruff clean.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/launch_revenue.py apps/backend/main.py apps/backend/tests/unit/services/test_launch_revenue_sweep.py apps/backend/tests/unit/test_main_lifespan.py
git commit -m "feat(launch): hourly revenue sweep as a lifespan task"
```

---

### Task 11: Admin — `GET /ops/launch`, `POST /ops/launch/refresh`

**Files:**
- Create: `apps/backend/core/services/ops/launch.py`
- Modify: `apps/backend/routers/ops.py` (append two routes)
- Test: `apps/backend/tests/unit/services/test_ops_launch.py`, `apps/backend/tests/unit/routers/test_ops_launch_routes.py`

**Interfaces:**
- Consumes: `launch_waitlist_repo.list_all`, `launch_entries_repo.list_all`, `billing_repo.get_by_owner_id`, `readiness_state.resolve_onboarding_state`, `launch_phase`, `launch_revenue.refresh_all`, `launch_service.referral_link/rank`, `env.envelope/read_source`.
- Produces: `ops.launch.compute() -> tuple[dict, str, list[dict]]` with `data` =
  ```
  {generated_at, phase, schedule: {drop_at, competition_start_at, competition_end_at, comp_slots},
   waitlist: {total, top_slots_activated, top_slots_claimed},
   entrants: {total, paid, comped, public},
   pot_cents, referrals: {signed_up, paid, months_banked},
   economics: {paid_profit_cents, comp_cost_cents, referral_cost_cents, pnl_cents, aws_credits_cents, net_cents},
   entries: [{owner_id, display_name, public, waitlist_position, currency, baseline_cents, window_cents, delta_cents, pct, refreshed_at, refresh_error, comped}],
   waitlist_rows: [{email, position, referral_link, created_at, user_id, activated, referred_by, referrals_signed_up, referrals_paid, free_months_banked}]}
  ```
  Constants in `ops/launch.py`: `CONTAINER_COST_CENTS = 1800`, `PLAN_PRICE_CENTS = 5000`, `AWS_CREDITS_CENTS = 500_000` (the sheet). `comped` for an entry = the owner's `billing.launch_comped` is not tracked — derive: `stripe_subscription_id` present AND the entrant's `waitlist_position` ≤ `comp_slots` AND activated by the deadline. (Honest approximation; the true signal — first invoice $0 — is a Stripe read per owner and is deliberately out of scope for a 60s-cached admin page.)

- [ ] **Step 1: Write the failing compute tests**

```python
# apps/backend/tests/unit/services/test_ops_launch.py
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from core.services import launch_phase
from core.services.ops import launch as ops_launch

DROP = datetime(2026, 9, 18, 16, tzinfo=timezone.utc)
S = launch_phase.Schedule(DROP, DROP + timedelta(hours=72), DROP + timedelta(hours=72, days=30), 2, 72, 30)  # 2 comp slots for the test


@pytest.fixture
def deps():
    waitlist = [
        {"email": "a@x.com", "position": 1, "referral_code": "AAAA2345", "user_id": "u_a", "created_at": "t", "referrals_signed_up": 1, "referrals_paid": 1, "free_months_banked": 0},
        {"email": "b@x.com", "position": 2, "referral_code": "BBBB2345", "user_id": None, "created_at": "t", "referrals_signed_up": 0, "referrals_paid": 0, "free_months_banked": 1},
        {"email": "c@x.com", "position": 3, "referral_code": "CCCC2345", "user_id": "u_c", "created_at": "t", "referred_by": "AAAA2345", "referrals_signed_up": 0, "referrals_paid": 0, "free_months_banked": 0},
    ]
    entries = [
        {"owner_id": "u_a", "display_name": "A", "public": True, "waitlist_position": 1, "currency": "usd", "baseline_cents": 100, "window_cents": 300, "delta_cents": 200, "refreshed_at": "t"},
        {"owner_id": "u_c", "display_name": "C", "public": False, "waitlist_position": 3, "currency": "usd", "baseline_cents": 0, "window_cents": 50, "delta_cents": 50, "refresh_error": "stripe_auth"},
    ]
    onboarding = {
        "u_a": {"bootstrap_state": "delivered", "bootstrap_delivered_at": (DROP + timedelta(hours=1)).isoformat()},
        "u_c": {"bootstrap_state": "delivered", "bootstrap_delivered_at": (DROP + timedelta(hours=100)).isoformat()},
    }
    with patch.object(ops_launch.launch_phase, "schedule", return_value=S), \
         patch.object(ops_launch.launch_waitlist_repo, "list_all", AsyncMock(return_value=waitlist)), \
         patch.object(ops_launch.launch_entries_repo, "list_all", AsyncMock(return_value=entries)), \
         patch.object(ops_launch.readiness_state, "resolve_onboarding_state", AsyncMock(side_effect=lambda o: onboarding.get(o, {"bootstrap_state": "not_started"}))), \
         patch.object(ops_launch.billing_repo, "get_by_owner_id", AsyncMock(side_effect=lambda o: {"stripe_subscription_id": f"sub_{o}"})), \
         patch.object(ops_launch.settings, "FRONTEND_URL", "https://dev.flickmeon.ai"):
        ops_launch._cache.clear()
        yield


async def test_compute_counts_and_economics(deps):
    data, state, errors = await ops_launch.compute()
    assert state == "ok" and errors == []
    assert data["waitlist"] == {"total": 3, "top_slots_activated": 1, "top_slots_claimed": 1}
    # u_a: position 1 ≤ 2 slots and activated inside 72h ⇒ comped. u_c: position 3 ⇒ paid.
    assert data["entrants"] == {"total": 2, "paid": 1, "comped": 1, "public": 1}
    assert data["pot_cents"] == 100_000 + 2 * 1_000
    assert data["referrals"] == {"signed_up": 1, "paid": 1, "months_banked": 1}
    econ = data["economics"]
    assert econ["paid_profit_cents"] == 1 * (5000 - 1800 - 1000)
    assert econ["comp_cost_cents"] == 1 * (1800 + 1000)
    assert econ["referral_cost_cents"] == 1 * (1800 + 1000)
    assert econ["net_cents"] == econ["pnl_cents"] + 500_000
    assert data["entries"][0]["owner_id"] == "u_a" and data["entries"][0]["pct"] == 2.0 and data["entries"][0]["comped"] is True
    assert data["entries"][1]["refresh_error"] == "stripe_auth"
    row = next(r for r in data["waitlist_rows"] if r["email"] == "a@x.com")
    assert row["referral_link"] == "https://dev.flickmeon.ai/launch?ref=AAAA2345" and row["activated"] is True


async def test_compute_is_cached_for_60s(deps):
    await ops_launch.compute()
    with patch.object(ops_launch.launch_waitlist_repo, "list_all", AsyncMock(side_effect=AssertionError("should be cached"))):
        await ops_launch.compute()


async def test_compute_unconfigured():
    with patch.object(ops_launch.launch_phase, "schedule", return_value=None):
        ops_launch._cache.clear()
        data, state, errors = await ops_launch.compute()
    assert data is None and state == "unavailable" and errors[0]["domain"] == "launch"
```

- [ ] **Step 2: Write the failing route tests**

```python
# apps/backend/tests/unit/routers/test_ops_launch_routes.py
from unittest.mock import AsyncMock, patch

import pytest

from core.auth import AuthContext, require_platform_admin


@pytest.fixture
def admin_client(app, async_client):
    app.dependency_overrides[require_platform_admin] = lambda: AuthContext(user_id="user_admin")
    yield async_client
    app.dependency_overrides.pop(require_platform_admin, None)


@patch("core.services.ops.launch.compute", AsyncMock(return_value=({"phase": "live"}, "ok", [])))
async def test_ops_launch_envelope(admin_client):
    r = await admin_client.get("/api/v1/ops/launch")
    assert r.status_code == 200 and r.json()["data"]["phase"] == "live" and r.json()["source_state"] == "ok"


@patch("core.services.launch_revenue.refresh_all", AsyncMock(return_value=7))
async def test_ops_launch_refresh_forces(admin_client):
    r = await admin_client.post("/api/v1/ops/launch/refresh")
    assert r.status_code == 200 and r.json() == {"refreshed": 7}


async def test_ops_launch_requires_admin(async_client):
    r = await async_client.get("/api/v1/ops/launch")
    assert r.status_code in (401, 403)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_ops_launch.py tests/unit/routers/test_ops_launch_routes.py -v`
Expected: FAIL — `ImportError` / 404.

- [ ] **Step 4: Write the compute module**

```python
# apps/backend/core/services/ops/launch.py
"""Launch Competition numbers for the admin console — the owner's economics
sheet with live inputs. Computed on read, 60s in-process cache."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

from core.config import settings
from core.dynamodb import utc_now_iso
from core.repositories import billing_repo, launch_entries_repo, launch_waitlist_repo
from core.services import launch_phase, launch_service, readiness_state
from core.services.ops import envelope as env

CONTAINER_COST_CENTS = 1800
PLAN_PRICE_CENTS = 5000
AWS_CREDITS_CENTS = 500_000
_CACHE_TTL_S = 60
_cache: dict[str, tuple[float, tuple]] = {}


def _activated_by_deadline(onboarding: dict, s: launch_phase.Schedule) -> bool:
    raw = onboarding.get("bootstrap_delivered_at")
    if onboarding.get("bootstrap_state") != "delivered" or not raw:
        return False
    return launch_phase.parse_iso(raw) <= s.competition_start_at


async def _compute(s: launch_phase.Schedule) -> dict:
    waitlist, entries = await asyncio.gather(launch_waitlist_repo.list_all(), launch_entries_repo.list_all())
    per_entrant = settings.LAUNCH_POT_PER_ENTRANT_CENTS

    claimed = [w for w in waitlist if w.get("user_id")]
    sem = asyncio.Semaphore(8)

    async def _onb(owner_id: str) -> dict:
        async with sem:
            return await readiness_state.resolve_onboarding_state(owner_id)

    onboarding = dict(zip([w["user_id"] for w in claimed], await asyncio.gather(*[_onb(w["user_id"]) for w in claimed])))
    activated = {uid for uid, o in onboarding.items() if _activated_by_deadline(o, s)}

    top = [w for w in waitlist if int(w["position"]) <= s.comp_slots]
    top_claimed = [w for w in top if w.get("user_id")]
    top_activated = [w for w in top_claimed if w["user_id"] in activated]

    billing_rows = await asyncio.gather(*[billing_repo.get_by_owner_id(e["owner_id"]) for e in entries])
    entry_rows = []
    comped = 0
    for e, billing in zip(entries, billing_rows):
        pos = e.get("waitlist_position")
        is_comped = pos is not None and int(pos) <= s.comp_slots and e["owner_id"] in activated
        comped += int(is_comped)
        baseline = int(e.get("baseline_cents", 0))
        delta = int(e.get("delta_cents", 0))
        entry_rows.append({
            "owner_id": e["owner_id"],
            "has_subscription": bool((billing or {}).get("stripe_subscription_id")),
            "display_name": e.get("display_name"),
            "public": bool(e.get("public", True)),
            "waitlist_position": pos,
            "currency": e.get("currency"),
            "baseline_cents": baseline,
            "window_cents": int(e.get("window_cents", 0)),
            "delta_cents": delta,
            "pct": round(delta / baseline, 4) if baseline > 0 else None,
            "refreshed_at": e.get("refreshed_at"),
            "refresh_error": e.get("refresh_error"),
            "comped": is_comped,
        })
    entry_rows.sort(key=lambda r: -r["delta_cents"])

    paid = len(entries) - comped
    referrals_paid = sum(int(w.get("referrals_paid", 0)) for w in waitlist)
    months_banked = sum(int(w.get("free_months_banked", 0)) for w in waitlist)
    paid_profit = paid * (PLAN_PRICE_CENTS - CONTAINER_COST_CENTS - per_entrant)
    comp_cost = comped * (CONTAINER_COST_CENTS + per_entrant)
    referral_cost = referrals_paid * (CONTAINER_COST_CENTS + per_entrant)
    pnl = paid_profit - comp_cost - referral_cost

    return {
        "generated_at": utc_now_iso(),
        "phase": launch_phase.phase(s),
        "schedule": {
            "drop_at": s.drop_at.isoformat(),
            "competition_start_at": s.competition_start_at.isoformat(),
            "competition_end_at": s.competition_end_at.isoformat(),
            "comp_slots": s.comp_slots,
        },
        "waitlist": {"total": len(waitlist), "top_slots_activated": len(top_activated), "top_slots_claimed": len(top_claimed)},
        "entrants": {"total": len(entries), "paid": paid, "comped": comped, "public": sum(1 for e in entries if e.get("public", True))},
        "pot_cents": launch_phase.pot_cents(len(entries)),
        "referrals": {
            "signed_up": sum(int(w.get("referrals_signed_up", 0)) for w in waitlist),
            "paid": referrals_paid,
            "months_banked": months_banked,
        },
        "economics": {
            "paid_profit_cents": paid_profit,
            "comp_cost_cents": comp_cost,
            "referral_cost_cents": referral_cost,
            "pnl_cents": pnl,
            "aws_credits_cents": AWS_CREDITS_CENTS,
            "net_cents": pnl + AWS_CREDITS_CENTS,
        },
        "entries": entry_rows,
        "waitlist_rows": sorted(
            [
                {
                    "email": w["email"],
                    "position": int(w["position"]),
                    "referral_link": launch_service.referral_link(w["referral_code"]),
                    "created_at": w.get("created_at"),
                    "user_id": w.get("user_id"),
                    "activated": w.get("user_id") in activated,
                    "referred_by": w.get("referred_by"),
                    "referrals_signed_up": int(w.get("referrals_signed_up", 0)),
                    "referrals_paid": int(w.get("referrals_paid", 0)),
                    "free_months_banked": int(w.get("free_months_banked", 0)),
                }
                for w in waitlist
            ],
            key=lambda r: r["position"],
        ),
    }


async def compute() -> tuple[dict | None, str, list[dict]]:
    s = launch_phase.schedule()
    if s is None:
        return None, env.UNAVAILABLE, [{"domain": "launch", "reason": "LAUNCH_DROP_AT is not set"}]
    hit = _cache.get("launch")
    if hit and time.monotonic() - hit[0] < _CACHE_TTL_S:
        return hit[1]
    data, state, errors = await env.read_source("launch", lambda: _compute(s), timeout_s=20.0)
    result = (data, state, errors)
    if state == env.OK:
        _cache["launch"] = (time.monotonic(), result)
    return result
```

- [ ] **Step 5: Append the routes to `routers/ops.py`**

```python
@router.get("/launch")
async def get_launch(_auth: AuthContext = Depends(require_platform_admin)):
    """Launch Competition — the economics sheet with live inputs, 60s cache."""
    from core.services.ops import launch as launch_ops

    data, state, errors = await launch_ops.compute()
    return env.envelope(data, source_state=state, errors=errors)


@router.post("/launch/refresh")
async def refresh_launch(_auth: AuthContext = Depends(require_platform_admin)):
    """Force-refresh every entrant's revenue now (judging day)."""
    from core.services import launch_revenue
    from core.services.ops import launch as launch_ops

    n = await launch_revenue.refresh_all(force=True)
    launch_ops._cache.clear()
    return {"refreshed": n}
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_ops_launch.py tests/unit/routers/test_ops_launch_routes.py -v && uv run ruff check core/services/ops/launch.py routers/ops.py`
Expected: all pass; ruff clean.

- [ ] **Step 7: Run the whole launch backend suite + the touched suites once**

Run: `cd apps/backend && uv run pytest tests/unit -k "launch or stripe_webhook_handlers or lifespan" -q && uv run ruff check .`
Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add apps/backend/core/services/ops/launch.py apps/backend/routers/ops.py apps/backend/tests/unit/services/test_ops_launch.py apps/backend/tests/unit/routers/test_ops_launch_routes.py
git commit -m "feat(launch): admin ops endpoint — the economics sheet with live inputs"
```

---

### Task 12: Public `/launch` page — hero, waitlist form, leaderboard

**Files:**
- Create: `apps/frontend/src/lib/launch.ts`
- Create: `apps/frontend/src/app/launch/page.tsx`
- Create: `apps/frontend/src/components/launch/LaunchPage.tsx`, `LaunchHero.tsx`, `WaitlistForm.tsx`, `Leaderboard.tsx`, `useLaunchRef.ts`
- Modify: `apps/frontend/src/lib/analytics.ts` (`AnalyticsEvent` union)
- Test: `apps/frontend/src/components/launch/__tests__/WaitlistForm.test.tsx`, `LaunchPage.test.tsx`, `apps/frontend/src/lib/__tests__/launch.test.ts`

**Interfaces:**
- Produces (`lib/launch.ts`):
  ```ts
  export type LaunchPhase = "waitlist" | "activation" | "live" | "ended";
  export interface LaunchInfo { phase: LaunchPhase; drop_at: string; competition_start_at: string; competition_end_at: string; pot_cents: number; entrants: number; waitlist_count: number; comp_slots: number }
  export interface LeaderboardRow { rank: number; display_name: string; delta_cents: number; pct: number | null; currency: string }
  export interface LaunchMe { phase: LaunchPhase; waitlist: { position: number; referral_code: string; in_comp_window: boolean; referral_link: string; referrals_signed_up: number; referrals_paid: number; free_months_banked: number } | null; activated: boolean; activated_at: string | null; entry: LaunchEntry | null; can_enter: boolean; reason: string | null }
  export interface LaunchEntry { display_name: string; public: boolean; entered_at: string | null; currency: string | null; baseline_cents: number; window_cents: number; delta_cents: number; refreshed_at: string | null; refresh_error: string | null }
  export async function fetchLaunchInfo(): Promise<LaunchInfo | null>      // null on 404 (feature off)
  export async function fetchLeaderboard(): Promise<{ entrants: number; rows: LeaderboardRow[] }>
  export async function joinWaitlist(email: string, ref: string | null): Promise<{ position: number; referral_code: string; in_comp_window: boolean }>  // throws Error(detail) on 4xx
  export function formatMoney(cents: number, currency: string): string      // "$1,234" / "€1.234"; no fractional
  export function formatPct(pct: number | null): string                     // "+25%", "−4%", "new" when null
  export const LAUNCH_REF_KEY = "launch_ref";
  ```
- `useLaunchRef.ts`: `export function useLaunchRef(): string | null` — on mount reads `?ref=` from `useSearchParams()`, stores it under `LAUNCH_REF_KEY` in `localStorage`, returns the stored value (query wins over storage).
- `components/launch/LaunchPage.tsx`: default export client component; polls `fetchLaunchInfo` every 30s; renders `LaunchHero` + phase body; renders `<MyLaunchCard />` (Task 13) when `useAuth().isSignedIn`. Renders a "Launch is not open yet" line when `fetchLaunchInfo` returns null.
- New analytics events: `"launch_waitlist_joined" | "launch_entered" | "launch_referral_copied"`.

- [ ] **Step 1: Write the failing tests**

```ts
// apps/frontend/src/lib/__tests__/launch.test.ts
import { describe, expect, it } from "vitest";
import { formatMoney, formatPct } from "../launch";

describe("launch formatters", () => {
  it("formats whole-currency money", () => {
    expect(formatMoney(123456, "usd")).toBe("$1,235");
    expect(formatMoney(-2000, "usd")).toBe("-$20");
    expect(formatMoney(0, "eur")).toBe("€0");
  });
  it("formats percent with sign, and 'new' for no baseline", () => {
    expect(formatPct(0.25)).toBe("+25%");
    expect(formatPct(-0.04)).toBe("−4%");
    expect(formatPct(null)).toBe("new");
  });
});
```

```tsx
// apps/frontend/src/components/launch/__tests__/WaitlistForm.test.tsx
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const joinWaitlist = vi.fn();
vi.mock("@/lib/launch", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/launch")>()),
  joinWaitlist: (...args: unknown[]) => joinWaitlist(...args),
}));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

import { WaitlistForm } from "../WaitlistForm";

describe("WaitlistForm", () => {
  it("submits the email with the stored ref and shows position + referral link", async () => {
    joinWaitlist.mockResolvedValue({ position: 47, referral_code: "P9AB3CDE", in_comp_window: true });
    render(<WaitlistForm compSlots={220} refCode="K7QX2MPP" />);
    fireEvent.change(screen.getByLabelText(/email/i), { target: { value: "me@x.com" } });
    fireEvent.click(screen.getByRole("button", { name: /join/i }));
    await waitFor(() => expect(joinWaitlist).toHaveBeenCalledWith("me@x.com", "K7QX2MPP"));
    expect(await screen.findByText(/#47/)).toBeInTheDocument();
    expect(screen.getByText(/first 220/i)).toBeInTheDocument();
    expect(screen.getByDisplayValue(/\/launch\?ref=P9AB3CDE$/)).toBeInTheDocument();
  });

  it("tells a late joiner they are past the comp window", async () => {
    joinWaitlist.mockResolvedValue({ position: 305, referral_code: "X", in_comp_window: false });
    render(<WaitlistForm compSlots={220} refCode={null} />);
    fireEvent.change(screen.getByLabelText(/email/i), { target: { value: "me@x.com" } });
    fireEvent.click(screen.getByRole("button", { name: /join/i }));
    expect(await screen.findByText(/#305/)).toBeInTheDocument();
    expect(screen.queryByText(/first 220/i)).not.toBeInTheDocument();
  });

  it("surfaces a backend rejection", async () => {
    joinWaitlist.mockRejectedValue(new Error("invalid_email"));
    render(<WaitlistForm compSlots={220} refCode={null} />);
    fireEvent.change(screen.getByLabelText(/email/i), { target: { value: "nope@" } });
    fireEvent.click(screen.getByRole("button", { name: /join/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/valid email/i);
  });
});
```

```tsx
// apps/frontend/src/components/launch/__tests__/LaunchPage.test.tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const info = vi.fn();
const board = vi.fn();
vi.mock("@/lib/launch", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/launch")>()),
  fetchLaunchInfo: () => info(),
  fetchLeaderboard: () => board(),
}));
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ isSignedIn: false }) }));
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams("") }));
vi.mock("../MyLaunchCard", () => ({ MyLaunchCard: () => <div data-testid="me" /> }));

import LaunchPage from "../LaunchPage";

const base = { drop_at: "2026-09-18T16:00:00+00:00", competition_start_at: "2026-09-21T16:00:00+00:00", competition_end_at: "2026-10-21T16:00:00+00:00", pot_cents: 103000, entrants: 3, waitlist_count: 40, comp_slots: 220 };

describe("LaunchPage phases", () => {
  it("waitlist: shows the form and the pot", async () => {
    info.mockResolvedValue({ ...base, phase: "waitlist" });
    render(<LaunchPage />);
    expect(await screen.findByLabelText(/email/i)).toBeInTheDocument();
    expect(screen.getByText("$1,030")).toBeInTheDocument();
    expect(screen.queryByTestId("me")).not.toBeInTheDocument();
  });
  it("activation: sign-up CTA, no form", async () => {
    info.mockResolvedValue({ ...base, phase: "activation" });
    render(<LaunchPage />);
    expect(await screen.findByRole("link", { name: /sign up/i })).toHaveAttribute("href", "/sign-up");
    expect(screen.queryByLabelText(/email/i)).not.toBeInTheDocument();
  });
  it("live: leaderboard rows", async () => {
    info.mockResolvedValue({ ...base, phase: "live" });
    board.mockResolvedValue({ entrants: 1, rows: [{ rank: 1, display_name: "Ada", delta_cents: 25000, pct: 0.5, currency: "usd" }] });
    render(<LaunchPage />);
    expect(await screen.findByText("Ada")).toBeInTheDocument();
    expect(screen.getByText("$250")).toBeInTheDocument();
    expect(screen.getByText("+50%")).toBeInTheDocument();
  });
  it("feature off: quiet message", async () => {
    info.mockResolvedValue(null);
    render(<LaunchPage />);
    expect(await screen.findByText(/not open yet/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/frontend && pnpm vitest run src/lib/__tests__/launch.test.ts src/components/launch`
Expected: FAIL — modules not found.

- [ ] **Step 3: Write `lib/launch.ts`**

```ts
// apps/frontend/src/lib/launch.ts
import { BACKEND_URL } from "@/lib/api";

export type LaunchPhase = "waitlist" | "activation" | "live" | "ended";

export interface LaunchInfo {
  phase: LaunchPhase;
  drop_at: string;
  competition_start_at: string;
  competition_end_at: string;
  pot_cents: number;
  entrants: number;
  waitlist_count: number;
  comp_slots: number;
}

export interface LeaderboardRow {
  rank: number;
  display_name: string;
  delta_cents: number;
  pct: number | null;
  currency: string;
}

export interface LaunchEntry {
  display_name: string;
  public: boolean;
  entered_at: string | null;
  currency: string | null;
  baseline_cents: number;
  window_cents: number;
  delta_cents: number;
  refreshed_at: string | null;
  refresh_error: string | null;
}

export interface LaunchMe {
  phase: LaunchPhase;
  waitlist: {
    position: number;
    referral_code: string;
    in_comp_window: boolean;
    referral_link: string;
    referrals_signed_up: number;
    referrals_paid: number;
    free_months_banked: number;
  } | null;
  activated: boolean;
  activated_at: string | null;
  entry: LaunchEntry | null;
  can_enter: boolean;
  reason: string | null;
}

export const LAUNCH_REF_KEY = "launch_ref";

// Public reads use plain fetch: the shared client refuses to send without a
// Clerk token, and these routes are meant for signed-out visitors.
export async function fetchLaunchInfo(): Promise<LaunchInfo | null> {
  const res = await fetch(`${BACKEND_URL}/launch`, { cache: "no-store" });
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`launch_info_${res.status}`);
  return (await res.json()) as LaunchInfo;
}

export async function fetchLeaderboard(): Promise<{ entrants: number; rows: LeaderboardRow[] }> {
  const res = await fetch(`${BACKEND_URL}/launch/leaderboard`, { cache: "no-store" });
  if (!res.ok) throw new Error(`leaderboard_${res.status}`);
  return (await res.json()) as { entrants: number; rows: LeaderboardRow[] };
}

export async function joinWaitlist(
  email: string,
  ref: string | null,
): Promise<{ position: number; referral_code: string; in_comp_window: boolean }> {
  const res = await fetch(`${BACKEND_URL}/launch/waitlist`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, ref: ref ?? undefined }),
  });
  if (!res.ok) {
    let detail = `http_${res.status}`;
    try {
      const body = (await res.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return (await res.json()) as { position: number; referral_code: string; in_comp_window: boolean };
}

const SYMBOL: Record<string, string> = { usd: "$", eur: "€", gbp: "£", cad: "CA$", aud: "A$" };

export function formatMoney(cents: number, currency: string): string {
  const sym = SYMBOL[currency.toLowerCase()] ?? `${currency.toUpperCase()} `;
  const whole = Math.round(Math.abs(cents) / 100).toLocaleString("en-US");
  return `${cents < 0 ? "-" : ""}${sym}${whole}`;
}

export function formatPct(pct: number | null): string {
  if (pct === null) return "new";
  const n = Math.round(pct * 100);
  return n < 0 ? `−${Math.abs(n)}%` : `+${n}%`;
}
```

- [ ] **Step 4: Add the analytics events**

In `apps/frontend/src/lib/analytics.ts`, inside the `AnalyticsEvent` union, add a group:

```ts
  // Launch competition
  | "launch_waitlist_joined"
  | "launch_entered"
  | "launch_referral_copied"
```

- [ ] **Step 5: Write `useLaunchRef.ts`**

```ts
// apps/frontend/src/components/launch/useLaunchRef.ts
"use client";

import * as React from "react";
import { useSearchParams } from "next/navigation";
import { LAUNCH_REF_KEY } from "@/lib/launch";

/** `?ref=` wins and is persisted; otherwise whatever a previous visit stored. */
export function useLaunchRef(): string | null {
  const params = useSearchParams();
  const fromQuery = params.get("ref")?.trim().toUpperCase() || null;
  const [ref, setRef] = React.useState<string | null>(fromQuery);
  React.useEffect(() => {
    try {
      if (fromQuery) {
        window.localStorage.setItem(LAUNCH_REF_KEY, fromQuery);
        setRef(fromQuery);
      } else {
        setRef(window.localStorage.getItem(LAUNCH_REF_KEY));
      }
    } catch {
      /* storage unavailable */
    }
  }, [fromQuery]);
  return ref;
}
```

- [ ] **Step 6: Write `WaitlistForm.tsx`**

```tsx
// apps/frontend/src/components/launch/WaitlistForm.tsx
"use client";

import * as React from "react";
import { capture } from "@/lib/analytics";
import { joinWaitlist } from "@/lib/launch";
import { ReferralLink } from "./ReferralLink";

const ERROR_COPY: Record<string, string> = {
  invalid_email: "Enter a valid email address.",
  waitlist_closed: "The waitlist has closed — sign up instead.",
  rate_limited: "Too many attempts. Try again in a minute.",
};

export function WaitlistForm({ compSlots, refCode }: { compSlots: number; refCode: string | null }) {
  const [email, setEmail] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [joined, setJoined] = React.useState<{ position: number; referral_code: string; in_comp_window: boolean } | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await joinWaitlist(email.trim(), refCode);
      setJoined(result);
      capture("launch_waitlist_joined", { position: result.position, referred: Boolean(refCode) });
    } catch (err) {
      const code = err instanceof Error ? err.message : "unknown";
      setError(ERROR_COPY[code] ?? "Something went wrong — try again.");
    } finally {
      setBusy(false);
    }
  };

  if (joined) {
    return (
      <div className="rounded-xl border border-amber-400/30 bg-amber-400/5 p-5" data-testid="waitlist-joined">
        <p className="text-2xl font-semibold">You&apos;re #{joined.position}.</p>
        {joined.in_comp_window ? (
          <p className="mt-1 text-sm opacity-80">
            You&apos;re in the first {compSlots} — your free competition month is yours if you finish onboarding within 3 days of the drop. Watch your inbox on drop day.
          </p>
        ) : (
          <p className="mt-1 text-sm opacity-80">The first {compSlots} spots are gone, but you can still compete — and every friend who subscribes earns you a month free.</p>
        )}
        <ReferralLink code={joined.referral_code} />
      </div>
    );
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-3 sm:flex-row" aria-label="Join the waitlist">
      <label className="sr-only" htmlFor="launch-email">Email</label>
      <input
        id="launch-email"
        type="email"
        required
        autoComplete="email"
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        placeholder="you@company.com"
        className="flex-1 rounded-lg border border-zinc-700 bg-zinc-900 px-4 py-3 text-base outline-none focus:border-amber-400"
      />
      <button type="submit" disabled={busy || email.trim().length < 3} className="rounded-lg bg-amber-400 px-5 py-3 font-semibold text-zinc-950 disabled:opacity-50">
        {busy ? "Joining…" : "Join the waitlist"}
      </button>
      {error ? <p role="alert" className="text-sm text-red-400 sm:basis-full">{error}</p> : null}
    </form>
  );
}
```

`ReferralLink` is shared with Task 13 — create it now:

```tsx
// apps/frontend/src/components/launch/ReferralLink.tsx
"use client";

import * as React from "react";
import { capture } from "@/lib/analytics";
import { SITE_URL } from "@/lib/domain";

export function ReferralLink({ code, link }: { code: string; link?: string }) {
  const href = link ?? `${SITE_URL}/launch?ref=${code}`;
  const [copied, setCopied] = React.useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(href);
      setCopied(true);
      capture("launch_referral_copied", {});
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard blocked — the input is selectable */
    }
  };
  return (
    <div className="mt-4">
      <p className="text-sm font-medium">Refer a friend who subscribes → you get a month free.</p>
      <div className="mt-2 flex gap-2">
        <input readOnly value={href} aria-label="Your referral link" className="flex-1 rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 font-mono text-xs" onFocus={(e) => e.currentTarget.select()} />
        <button type="button" onClick={copy} className="rounded-lg border border-zinc-600 px-3 py-2 text-sm">{copied ? "Copied" : "Copy"}</button>
      </div>
    </div>
  );
}
```

- [ ] **Step 7: Write `LaunchHero.tsx`, `Leaderboard.tsx`, `LaunchPage.tsx`, `app/launch/page.tsx`**

```tsx
// apps/frontend/src/components/launch/LaunchHero.tsx
"use client";

import * as React from "react";
import type { LaunchInfo } from "@/lib/launch";
import { formatMoney } from "@/lib/launch";

const HEADLINE: Record<LaunchInfo["phase"], string> = {
  waitlist: "The Lightbulb Launch Competition",
  activation: "Lightbulb AI is live. The competition starts soon.",
  live: "The competition is on.",
  ended: "The competition has ended.",
};

function useCountdown(iso: string): string {
  const [now, setNow] = React.useState(() => Date.now());
  React.useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  const ms = Math.max(0, new Date(iso).getTime() - now);
  const d = Math.floor(ms / 86_400_000);
  const h = Math.floor((ms % 86_400_000) / 3_600_000);
  const m = Math.floor((ms % 3_600_000) / 60_000);
  const s = Math.floor((ms % 60_000) / 1000);
  return d > 0 ? `${d}d ${h}h ${m}m` : `${h}h ${m}m ${s}s`;
}

export function LaunchHero({ info }: { info: LaunchInfo }) {
  const target = info.phase === "waitlist" ? info.drop_at : info.phase === "activation" ? info.competition_start_at : info.competition_end_at;
  const label = info.phase === "waitlist" ? "until the drop" : info.phase === "activation" ? "until the competition starts" : info.phase === "live" ? "left" : "";
  const countdown = useCountdown(target);
  return (
    <header className="text-center">
      <p className="text-xs uppercase tracking-[0.2em] opacity-60">Lightbulb AI</p>
      <h1 className="mt-3 text-4xl font-semibold sm:text-5xl">{HEADLINE[info.phase]}</h1>
      <p className="mt-4 text-lg opacity-80">
        Build a business with your own AI company. Biggest revenue lift over 30 days wins the pot.
      </p>
      <div className="mt-8 grid grid-cols-2 gap-4 sm:grid-cols-3">
        <div className="rounded-xl border border-zinc-800 p-4">
          <div className="text-3xl font-semibold tabular-nums">{formatMoney(info.pot_cents, "usd")}</div>
          <div className="text-xs opacity-60">prize pot · +$10 per entrant</div>
        </div>
        <div className="rounded-xl border border-zinc-800 p-4">
          <div className="text-3xl font-semibold tabular-nums">{info.phase === "waitlist" ? info.waitlist_count : info.entrants}</div>
          <div className="text-xs opacity-60">{info.phase === "waitlist" ? "on the waitlist" : "entrants"}</div>
        </div>
        {info.phase !== "ended" ? (
          <div className="col-span-2 rounded-xl border border-zinc-800 p-4 sm:col-span-1">
            <div className="text-3xl font-semibold tabular-nums">{countdown}</div>
            <div className="text-xs opacity-60">{label}</div>
          </div>
        ) : null}
      </div>
    </header>
  );
}
```

```tsx
// apps/frontend/src/components/launch/Leaderboard.tsx
"use client";

import * as React from "react";
import { fetchLeaderboard, formatMoney, formatPct, type LeaderboardRow } from "@/lib/launch";

export function Leaderboard() {
  const [rows, setRows] = React.useState<LeaderboardRow[] | null>(null);
  React.useEffect(() => {
    let alive = true;
    const load = () => fetchLeaderboard().then((b) => alive && setRows(b.rows)).catch(() => alive && setRows([]));
    load();
    const t = setInterval(load, 60_000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);
  if (rows === null) return <p className="text-sm opacity-60">Loading the board…</p>;
  if (rows.length === 0) return <p className="text-sm opacity-60">No public entrants yet.</p>;
  return (
    <table className="w-full text-sm">
      <thead className="text-left text-xs uppercase opacity-60">
        <tr><th className="py-2">#</th><th>Entrant</th><th className="text-right">Revenue lift</th><th className="text-right">Growth</th></tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.rank} className="border-t border-zinc-800">
            <td className="py-2 tabular-nums">{r.rank}</td>
            <td>{r.display_name}</td>
            <td className="text-right tabular-nums">{formatMoney(r.delta_cents, r.currency)}</td>
            <td className="text-right tabular-nums opacity-80">{formatPct(r.pct)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
```

```tsx
// apps/frontend/src/components/launch/LaunchPage.tsx
"use client";

import * as React from "react";
import Link from "next/link";
import { useAuth } from "@clerk/nextjs";
import { fetchLaunchInfo, type LaunchInfo } from "@/lib/launch";
import { LaunchHero } from "./LaunchHero";
import { Leaderboard } from "./Leaderboard";
import { MyLaunchCard } from "./MyLaunchCard";
import { WaitlistForm } from "./WaitlistForm";
import { useLaunchRef } from "./useLaunchRef";

const RULES = [
  "Sign up and finish onboarding — Friday's first run has to complete.",
  "Every entrant puts $10 into the pot; the first 220 on the waitlist get their month free.",
  "Connect a read-only Stripe key. The board ranks the dollar lift over your prior 30 days.",
  "Judges pick the winner from the board. Self-dealing gets you removed.",
];

export default function LaunchPage() {
  const { isSignedIn } = useAuth();
  const refCode = useLaunchRef();
  const [info, setInfo] = React.useState<LaunchInfo | null | undefined>(undefined);

  React.useEffect(() => {
    let alive = true;
    const load = () => fetchLaunchInfo().then((i) => alive && setInfo(i)).catch(() => alive && setInfo(null));
    load();
    const t = setInterval(load, 30_000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  if (info === undefined) return <main className="mx-auto max-w-3xl px-6 py-24 text-center opacity-60">Loading…</main>;
  if (info === null) return <main className="mx-auto max-w-3xl px-6 py-24 text-center opacity-60">The launch is not open yet.</main>;

  return (
    <main className="mx-auto max-w-3xl px-6 py-16 sm:py-24">
      <LaunchHero info={info} />
      <section className="mt-12">
        {info.phase === "waitlist" ? <WaitlistForm compSlots={info.comp_slots} refCode={refCode} /> : null}
        {info.phase === "activation" ? (
          <div className="text-center">
            <Link href="/sign-up" className="inline-block rounded-lg bg-amber-400 px-6 py-3 font-semibold text-zinc-950">Sign up</Link>
            <p className="mt-3 text-sm opacity-70">Got a code in your inbox? Finish onboarding first, then redeem it at checkout.</p>
          </div>
        ) : null}
        {info.phase === "live" || info.phase === "ended" ? <Leaderboard /> : null}
      </section>
      {isSignedIn ? (
        <section className="mt-12">
          <MyLaunchCard info={info} refCode={refCode} />
        </section>
      ) : null}
      <section className="mt-12">
        <h2 className="text-lg font-semibold">How it works</h2>
        <ol className="mt-3 list-decimal space-y-2 pl-5 text-sm opacity-80">
          {RULES.map((r) => <li key={r}>{r}</li>)}
        </ol>
      </section>
    </main>
  );
}
```

```tsx
// apps/frontend/src/app/launch/page.tsx
import type { Metadata } from "next";
import { Suspense } from "react";
import LaunchPage from "@/components/launch/LaunchPage";
import { pageMetadata } from "@/lib/seo";

export const metadata: Metadata = pageMetadata({
  title: "Lightbulb Launch Competition",
  description: "Join the waitlist, build a business with your own AI company, and compete for the pot on revenue lift.",
  path: "/launch",
});

export default function Page() {
  // useSearchParams (inside useLaunchRef) needs a Suspense boundary for static rendering.
  return (
    <Suspense fallback={null}>
      <LaunchPage />
    </Suspense>
  );
}
```

For Task 12's tests to compile, create a placeholder `MyLaunchCard.tsx` exporting `MyLaunchCard({ info, refCode }: { info: LaunchInfo; refCode: string | null })` that returns `null`; Task 13 replaces it.

- [ ] **Step 8: Run tests + typecheck**

Run: `cd apps/frontend && pnpm vitest run src/lib/__tests__/launch.test.ts src/components/launch && pnpm tsc --noEmit -p tsconfig.json && pnpm lint`
Expected: all pass; no type or lint errors.

- [ ] **Step 9: Commit**

```bash
git add apps/frontend/src/lib/launch.ts apps/frontend/src/lib/analytics.ts apps/frontend/src/app/launch apps/frontend/src/components/launch apps/frontend/src/lib/__tests__/launch.test.ts
git commit -m "feat(launch): public /launch page — hero, waitlist form, leaderboard"
```

---

### Task 13: Signed-in card — status, referral attribution, Enter form

**Files:**
- Replace: `apps/frontend/src/components/launch/MyLaunchCard.tsx`
- Create: `apps/frontend/src/components/launch/EnterForm.tsx`
- Test: `apps/frontend/src/components/launch/__tests__/MyLaunchCard.test.tsx`, `EnterForm.test.tsx`

**Interfaces:**
- Consumes: `useApi()` (`get`, `post`), `LaunchMe`/`LaunchEntry` types, `ReferralLink`, `LAUNCH_REF_KEY`, `formatMoney`, `capture`.
- Produces: `MyLaunchCard({ info, refCode })` — loads `GET /launch/me` with SWR (key `"/launch/me"`); on mount, if `refCode` and `me.waitlist` and no live entry, `POST /launch/referral {ref}` once then `localStorage.removeItem(LAUNCH_REF_KEY)`; renders position/comp copy, activation state, referral stats + link, and either the entry summary or `<EnterForm onEntered={mutate} />` when `can_enter`, else the reason copy. `EnterForm({ onEntered })` — display name, public toggle, key field with client-side prefix check, posts `POST /launch/enter`, maps `detail` reasons to copy, never puts the key in analytics.

- [ ] **Step 1: Write the failing tests**

```tsx
// apps/frontend/src/components/launch/__tests__/MyLaunchCard.test.tsx
import { render, screen, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import { describe, expect, it, vi, beforeEach } from "vitest";

const get = vi.fn();
const post = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ get, post, put: vi.fn(), del: vi.fn() }) }));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
vi.mock("../EnterForm", () => ({ EnterForm: () => <div data-testid="enter-form" /> }));

import { LAUNCH_REF_KEY, type LaunchMe } from "@/lib/launch";
import { MyLaunchCard } from "../MyLaunchCard";

const info = { phase: "live" as const, drop_at: "", competition_start_at: "", competition_end_at: "", pot_cents: 0, entrants: 0, waitlist_count: 0, comp_slots: 220 };
const me = (over: Partial<LaunchMe>): LaunchMe => ({
  phase: "live",
  waitlist: { position: 47, referral_code: "P9AB3CDE", in_comp_window: true, referral_link: "https://flickmeon.ai/launch?ref=P9AB3CDE", referrals_signed_up: 3, referrals_paid: 1, free_months_banked: 1 },
  activated: true, activated_at: "2026-09-19T00:00:00Z", entry: null, can_enter: true, reason: null, ...over,
});

function mount(refCode: string | null = null) {
  return render(<SWRConfig value={{ provider: () => new Map() }}><MyLaunchCard info={info} refCode={refCode} /></SWRConfig>);
}

describe("MyLaunchCard", () => {
  beforeEach(() => { get.mockReset(); post.mockReset(); });

  it("renders position, referral stats and the enter form when eligible", async () => {
    get.mockResolvedValue(me({}));
    mount();
    expect(await screen.findByText(/#47/)).toBeInTheDocument();
    expect(screen.getByText(/3 signed up/)).toBeInTheDocument();
    expect(screen.getByText(/1 paid/)).toBeInTheDocument();
    expect(screen.getByText(/1 free month banked/)).toBeInTheDocument();
    expect(screen.getByDisplayValue(/ref=P9AB3CDE/)).toBeInTheDocument();
    expect(screen.getByTestId("enter-form")).toBeInTheDocument();
  });

  it("explains why entry is blocked", async () => {
    get.mockResolvedValue(me({ activated: false, can_enter: false, reason: "not_activated" }));
    mount();
    expect(await screen.findByText(/finish onboarding/i)).toBeInTheDocument();
    expect(screen.queryByTestId("enter-form")).not.toBeInTheDocument();
  });

  it("shows the entry summary once entered", async () => {
    get.mockResolvedValue(me({ entry: { display_name: "Ada", public: true, entered_at: "t", currency: "usd", baseline_cents: 10000, window_cents: 15000, delta_cents: 5000, refreshed_at: "t", refresh_error: null } }));
    mount();
    expect(await screen.findByText(/You're in as Ada/)).toBeInTheDocument();
    expect(screen.getByText("$50")).toBeInTheDocument();
  });

  it("attributes a stored referral once and clears it", async () => {
    get.mockResolvedValue(me({}));
    post.mockResolvedValue({ attributed: true });
    localStorage.setItem(LAUNCH_REF_KEY, "K7QX2MPP");
    mount("K7QX2MPP");
    await waitFor(() => expect(post).toHaveBeenCalledWith("/launch/referral", { ref: "K7QX2MPP" }));
    await waitFor(() => expect(localStorage.getItem(LAUNCH_REF_KEY)).toBeNull());
  });
});
```

```tsx
// apps/frontend/src/components/launch/__tests__/EnterForm.test.tsx
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";

const post = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: vi.fn(), post, put: vi.fn(), del: vi.fn() }), ApiError: class extends Error { constructor(public status: number, public body: unknown, message?: string) { super(message); } } }));
const capture = vi.fn();
vi.mock("@/lib/analytics", () => ({ capture: (...a: unknown[]) => capture(...a) }));

import { EnterForm } from "../EnterForm";

function fill(name: string, key: string) {
  fireEvent.change(screen.getByLabelText(/display name/i), { target: { value: name } });
  fireEvent.change(screen.getByLabelText(/restricted key/i), { target: { value: key } });
}

describe("EnterForm", () => {
  beforeEach(() => { post.mockReset(); capture.mockReset(); });

  it("refuses a secret key client-side without calling the API", async () => {
    render(<EnterForm onEntered={vi.fn()} />);
    fill("Ada", "sk_live_" + "x".repeat(24));
    fireEvent.click(screen.getByRole("button", { name: /enter/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/restricted key/i);
    expect(post).not.toHaveBeenCalled();
  });

  it("posts and reports success without leaking the key to analytics", async () => {
    const onEntered = vi.fn();
    post.mockResolvedValue({ display_name: "Ada" });
    render(<EnterForm onEntered={onEntered} />);
    const key = "rk_live_" + "x".repeat(24);
    fill("Ada", key);
    fireEvent.click(screen.getByRole("button", { name: /enter/i }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/launch/enter", { display_name: "Ada", stripe_key: key, public: true }));
    expect(onEntered).toHaveBeenCalled();
    expect(JSON.stringify(capture.mock.calls)).not.toContain(key);
  });

  it("maps a backend reason to copy", async () => {
    const { ApiError } = await import("@/lib/api");
    post.mockRejectedValue(new ApiError(400, { detail: "stripe_permission" }, "stripe_permission"));
    render(<EnterForm onEntered={vi.fn()} />);
    fill("Ada", "rk_live_" + "x".repeat(24));
    fireEvent.click(screen.getByRole("button", { name: /enter/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Balance: Read/);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/frontend && pnpm vitest run src/components/launch`
Expected: the new tests FAIL (placeholder card renders nothing; EnterForm missing).

- [ ] **Step 3: Write `EnterForm.tsx`**

```tsx
// apps/frontend/src/components/launch/EnterForm.tsx
"use client";

import * as React from "react";
import { ApiError, useApi } from "@/lib/api";
import { capture } from "@/lib/analytics";

const KEY_RE = /^rk_(live|test)_[A-Za-z0-9]{16,}$/;

const REASON_COPY: Record<string, string> = {
  bad_prefix: "That's not a restricted key. Create one under Developers → API keys → Create restricted key.",
  stripe_auth: "Stripe rejected that key. Copy it again — it's only shown once.",
  stripe_permission: "That key can't read your balance. Give it Balance: Read and Charges: Read, then paste it again.",
  stripe_error: "Stripe didn't answer. Try again in a minute.",
  mixed_currency: "Your account settles in more than one currency — we can't rank it. Email support.",
  no_subscription: "You need an active subscription to enter.",
  not_activated: "Finish onboarding first — Friday's first run has to complete.",
  phase_waitlist: "Entries open on drop day.",
  phase_ended: "The competition has ended.",
};

export function EnterForm({ onEntered }: { onEntered: () => void }) {
  const api = useApi();
  const [name, setName] = React.useState("");
  const [key, setKey] = React.useState("");
  const [isPublic, setIsPublic] = React.useState(true);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    const trimmed = key.trim();
    if (!KEY_RE.test(trimmed)) {
      setError(REASON_COPY.bad_prefix);
      return;
    }
    setBusy(true);
    try {
      await api.post("/launch/enter", { display_name: name.trim(), stripe_key: trimmed, public: isPublic });
      capture("launch_entered", { public: isPublic });
      onEntered();
    } catch (err) {
      const reason = err instanceof ApiError && typeof err.body === "object" && err.body && "detail" in err.body ? String((err.body as { detail: unknown }).detail) : "stripe_error";
      setError(REASON_COPY[reason] ?? "Couldn't enter — try again.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className="space-y-4" aria-label="Enter the competition">
      <div>
        <label htmlFor="launch-name" className="text-sm font-medium">Display name</label>
        <input id="launch-name" required minLength={2} maxLength={40} value={name} onChange={(e) => setName(e.target.value)} className="mt-1 w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2" />
      </div>
      <div>
        <label htmlFor="launch-key" className="text-sm font-medium">Stripe restricted key</label>
        <input id="launch-key" required autoComplete="off" spellCheck={false} value={key} onChange={(e) => setKey(e.target.value)} placeholder="rk_live_…" className="mt-1 w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 font-mono text-sm" />
        <ol className="mt-2 list-decimal space-y-1 pl-5 text-xs opacity-70">
          <li>Stripe Dashboard → Developers → API keys → <b>Create restricted key</b></li>
          <li>Permissions: <b>Balance: Read</b> and <b>Charges: Read</b>. Nothing else.</li>
          <li>Paste it here. We only ever read totals; you can delete the key in Stripe any time.</li>
        </ol>
      </div>
      <label className="flex items-center gap-2 text-sm">
        <input type="checkbox" checked={isPublic} onChange={(e) => setIsPublic(e.target.checked)} />
        Show me on the public board
      </label>
      {error ? <p role="alert" className="text-sm text-red-400">{error}</p> : null}
      <button type="submit" disabled={busy} className="rounded-lg bg-amber-400 px-5 py-2.5 font-semibold text-zinc-950 disabled:opacity-50">{busy ? "Entering…" : "Enter the competition"}</button>
    </form>
  );
}
```

- [ ] **Step 4: Write `MyLaunchCard.tsx`**

```tsx
// apps/frontend/src/components/launch/MyLaunchCard.tsx
"use client";

import * as React from "react";
import useSWR from "swr";
import { useApi } from "@/lib/api";
import { LAUNCH_REF_KEY, formatMoney, type LaunchInfo, type LaunchMe } from "@/lib/launch";
import { EnterForm } from "./EnterForm";
import { ReferralLink } from "./ReferralLink";

const BLOCKED_COPY: Record<string, string> = {
  phase_waitlist: "Entries open on drop day.",
  phase_ended: "The competition has ended.",
  no_subscription: "Subscribe (or redeem your code at checkout) to enter.",
  not_activated: "Finish onboarding — Friday's first run has to complete — and come back.",
};

export function MyLaunchCard({ info, refCode }: { info: LaunchInfo; refCode: string | null }) {
  const api = useApi();
  const { data: me, mutate } = useSWR<LaunchMe>("/launch/me", (k: string) => api.get(k) as Promise<LaunchMe>);
  const attributed = React.useRef(false);

  React.useEffect(() => {
    if (!me || !refCode || attributed.current) return;
    if (me.waitlist && me.waitlist.referral_code === refCode) return;
    attributed.current = true;
    api
      .post("/launch/referral", { ref: refCode })
      .catch(() => undefined)
      .finally(() => {
        try {
          window.localStorage.removeItem(LAUNCH_REF_KEY);
        } catch {
          /* storage unavailable */
        }
      });
  }, [me, refCode, api]);

  if (!me) return <div className="rounded-xl border border-zinc-800 p-5 text-sm opacity-60">Loading your status…</div>;

  return (
    <div className="rounded-xl border border-zinc-800 p-5" data-testid="my-launch-card">
      <h2 className="text-lg font-semibold">Your launch</h2>
      {me.waitlist ? (
        <p className="mt-1 text-sm">
          Waitlist #{me.waitlist.position}
          {me.waitlist.in_comp_window ? ` — in the first ${info.comp_slots}. Check your inbox for your code on drop day.` : "."}
        </p>
      ) : null}
      <p className="mt-1 text-sm opacity-80">{me.activated ? "Onboarding complete ✓" : "Onboarding not finished yet."}</p>

      {me.waitlist ? (
        <div className="mt-4 text-sm">
          <span>{me.waitlist.referrals_signed_up} signed up</span> · <span>{me.waitlist.referrals_paid} paid</span> ·{" "}
          <span>{me.waitlist.free_months_banked} free month{me.waitlist.free_months_banked === 1 ? "" : "s"} banked</span>
          <ReferralLink code={me.waitlist.referral_code} link={me.waitlist.referral_link} />
        </div>
      ) : null}

      <div className="mt-6 border-t border-zinc-800 pt-4">
        {me.entry ? (
          <div className="text-sm">
            <p className="font-medium">You&apos;re in as {me.entry.display_name}.</p>
            <p className="mt-1 opacity-80">
              Lift so far: <span className="tabular-nums">{formatMoney(me.entry.delta_cents, me.entry.currency ?? "usd")}</span>
              {me.entry.refresh_error ? <span className="ml-2 text-red-400">— couldn&apos;t refresh ({me.entry.refresh_error}); re-enter with a fresh key.</span> : null}
            </p>
            {me.entry.refresh_error ? <div className="mt-4"><EnterForm onEntered={() => mutate()} /></div> : null}
          </div>
        ) : me.can_enter ? (
          <EnterForm onEntered={() => mutate()} />
        ) : (
          <p className="text-sm opacity-80">{BLOCKED_COPY[me.reason ?? ""] ?? "You can't enter right now."}</p>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 5: Run tests + typecheck**

Run: `cd apps/frontend && pnpm vitest run src/components/launch && pnpm tsc --noEmit -p tsconfig.json && pnpm lint`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add apps/frontend/src/components/launch
git commit -m "feat(launch): signed-in launch card, referral attribution, enter form"
```

---

### Task 14: Admin `/admin/launch`

**Files:**
- Modify: `apps/frontend/src/app/admin/_lib/api.ts` (types + `getLaunchOps`)
- Create: `apps/frontend/src/app/admin/_actions/launch.ts`
- Create: `apps/frontend/src/app/admin/launch/page.tsx`, `apps/frontend/src/app/admin/launch/LaunchAdminClient.tsx`
- Modify: `apps/frontend/src/app/admin/layout.tsx` (nav link after Growth)
- Test: `apps/frontend/src/app/admin/launch/__tests__/LaunchAdminClient.test.tsx`

**Interfaces:**
- Produces (`_lib/api.ts`): `export interface LaunchOpsData { … mirror of Task 11's data … }` and `export async function getLaunchOps(token: string): Promise<OpsEnvelope<LaunchOpsData> | null>` → `adminFetch(token, "/ops/launch")`.
- `_actions/launch.ts` (`"use server"`): `export async function refreshLaunch(): Promise<ActionResult>` → `adminPost("/ops/launch/refresh")`.
- `LaunchAdminClient({ data })`: KPI tiles, economics card, entries table, "Refresh all" button (calls the action then `router.refresh()`), "Export waitlist CSV" button building a Blob from `data.waitlist_rows` (`email,position,referral_link,created_at,activated`) and clicking a temporary `<a download="launch-waitlist.csv">`.

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/src/app/admin/launch/__tests__/LaunchAdminClient.test.tsx
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));
const refreshLaunch = vi.fn().mockResolvedValue({ ok: true, status: 200, data: { refreshed: 2 } });
vi.mock("../../_actions/launch", () => ({ refreshLaunch: () => refreshLaunch() }));

import type { LaunchOpsData } from "../../_lib/api";
import { LaunchAdminClient } from "../LaunchAdminClient";

const data: LaunchOpsData = {
  generated_at: "t", phase: "live",
  schedule: { drop_at: "d", competition_start_at: "s", competition_end_at: "e", comp_slots: 220 },
  waitlist: { total: 300, top_slots_activated: 150, top_slots_claimed: 200 },
  entrants: { total: 40, paid: 30, comped: 10, public: 35 },
  pot_cents: 140000,
  referrals: { signed_up: 50, paid: 12, months_banked: 3 },
  economics: { paid_profit_cents: 66000, comp_cost_cents: 28000, referral_cost_cents: 33600, pnl_cents: 4400, aws_credits_cents: 500000, net_cents: 504400 },
  entries: [{ owner_id: "u1", display_name: "Ada", public: true, waitlist_position: 3, currency: "usd", baseline_cents: 1000, window_cents: 3000, delta_cents: 2000, pct: 2, refreshed_at: "t", refresh_error: null, comped: true }],
  waitlist_rows: [{ email: "a@x.com", position: 1, referral_link: "https://flickmeon.ai/launch?ref=A", created_at: "t", user_id: "u1", activated: true, referred_by: null, referrals_signed_up: 0, referrals_paid: 0, free_months_banked: 0 }],
};

describe("LaunchAdminClient", () => {
  it("renders the sheet numbers and the entries table", () => {
    render(<LaunchAdminClient data={data} />);
    expect(screen.getByText("$1,400")).toBeInTheDocument();
    expect(screen.getByText("150 / 220")).toBeInTheDocument();
    expect(screen.getByText("Ada")).toBeInTheDocument();
    expect(screen.getByText("+200%")).toBeInTheDocument();
  });

  it("refresh calls the action", async () => {
    render(<LaunchAdminClient data={data} />);
    fireEvent.click(screen.getByRole("button", { name: /refresh all/i }));
    expect(refreshLaunch).toHaveBeenCalled();
    expect(await screen.findByText(/refreshed 2/i)).toBeInTheDocument();
  });

  it("builds a CSV from the waitlist rows", () => {
    const create = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:x");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    render(<LaunchAdminClient data={data} />);
    fireEvent.click(screen.getByRole("button", { name: /export waitlist csv/i }));
    const blob = create.mock.calls[0][0] as Blob;
    expect(blob.type).toBe("text/csv");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && pnpm vitest run src/app/admin/launch`
Expected: FAIL — module not found.

- [ ] **Step 3: Types + fetcher**

Append to `apps/frontend/src/app/admin/_lib/api.ts` (near `GrowthData` / `getGrowth`):

```ts
export interface LaunchOpsEntry {
  owner_id: string;
  display_name: string | null;
  public: boolean;
  waitlist_position: number | null;
  currency: string | null;
  baseline_cents: number;
  window_cents: number;
  delta_cents: number;
  pct: number | null;
  refreshed_at: string | null;
  refresh_error: string | null;
  comped: boolean;
}

export interface LaunchOpsWaitlistRow {
  email: string;
  position: number;
  referral_link: string;
  created_at: string | null;
  user_id: string | null;
  activated: boolean;
  referred_by: string | null;
  referrals_signed_up: number;
  referrals_paid: number;
  free_months_banked: number;
}

export interface LaunchOpsData {
  generated_at: string;
  phase: "waitlist" | "activation" | "live" | "ended";
  schedule: { drop_at: string; competition_start_at: string; competition_end_at: string; comp_slots: number };
  waitlist: { total: number; top_slots_activated: number; top_slots_claimed: number };
  entrants: { total: number; paid: number; comped: number; public: number };
  pot_cents: number;
  referrals: { signed_up: number; paid: number; months_banked: number };
  economics: {
    paid_profit_cents: number;
    comp_cost_cents: number;
    referral_cost_cents: number;
    pnl_cents: number;
    aws_credits_cents: number;
    net_cents: number;
  };
  entries: LaunchOpsEntry[];
  waitlist_rows: LaunchOpsWaitlistRow[];
}

export async function getLaunchOps(token: string): Promise<OpsEnvelope<LaunchOpsData> | null> {
  return adminFetch<OpsEnvelope<LaunchOpsData>>(token, "/ops/launch");
}
```

- [ ] **Step 4: Server action**

```ts
// apps/frontend/src/app/admin/_actions/launch.ts
"use server";

import { adminPost, type ActionResult } from "./_shared";

export async function refreshLaunch(): Promise<ActionResult> {
  return adminPost("/ops/launch/refresh");
}
```

- [ ] **Step 5: Page + client**

```tsx
// apps/frontend/src/app/admin/launch/page.tsx
import { auth } from "@clerk/nextjs/server";

import { ErrorBanner } from "@/components/admin/ErrorBanner";

import { getLaunchOps } from "../_lib/api";
import { LaunchAdminClient } from "./LaunchAdminClient";

export const dynamic = "force-dynamic";

export default async function AdminLaunchPage() {
  const { getToken } = await auth();
  const token = await getToken();
  const env = token ? await getLaunchOps(token) : null;
  const data = env?.data ?? null;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-zinc-100">Launch competition</h1>
        <p className="text-xs text-zinc-500">
          The economics sheet with live inputs. {data ? `Generated ${data.generated_at}, phase ${data.phase}.` : ""}
        </p>
      </div>
      {!env || env.source_state !== "ok" ? (
        <ErrorBanner
          error={
            !env
              ? "Launch read failed — nothing below is trustworthy."
              : `Not available (${env.source_state}): ${env.errors.map((e) => `${e.domain}: ${e.reason}`).join("; ")}`
          }
        />
      ) : null}
      {data ? <LaunchAdminClient data={data} /> : null}
    </div>
  );
}
```

```tsx
// apps/frontend/src/app/admin/launch/LaunchAdminClient.tsx
"use client";

import * as React from "react";
import { useRouter } from "next/navigation";

import { Card } from "@/components/admin/Card";
import { MetricStat } from "@/components/admin/MetricStat";
import { formatMoney, formatPct } from "@/lib/launch";

import { refreshLaunch } from "../_actions/launch";
import type { LaunchOpsData } from "../_lib/api";
import { Table } from "../growth/sections";

function csvEscape(v: unknown): string {
  const s = v === null || v === undefined ? "" : String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

export function LaunchAdminClient({ data }: { data: LaunchOpsData }) {
  const router = useRouter();
  const [note, setNote] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  const refresh = async () => {
    setBusy(true);
    const r = await refreshLaunch();
    setBusy(false);
    setNote(r.ok ? `Refreshed ${(r.data as { refreshed?: number })?.refreshed ?? 0} entries.` : `Refresh failed: ${r.error ?? r.status}`);
    router.refresh();
  };

  const exportCsv = () => {
    const head = ["email", "position", "referral_link", "created_at", "activated"];
    const lines = [head.join(","), ...data.waitlist_rows.map((r) => [r.email, r.position, r.referral_link, r.created_at, r.activated].map(csvEscape).join(","))];
    const blob = new Blob([lines.join("\n")], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "launch-waitlist.csv";
    a.click();
    URL.revokeObjectURL(url);
  };

  const e = data.economics;
  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <MetricStat label="pot" value={formatMoney(data.pot_cents, "usd")} />
        <MetricStat label="waitlist" value={data.waitlist.total} hint={`${data.waitlist.top_slots_claimed} of top ${data.schedule.comp_slots} claimed`} />
        <MetricStat label="top slots activated" value={`${data.waitlist.top_slots_activated} / ${data.schedule.comp_slots}`} />
        <MetricStat label="entrants" value={data.entrants.total} hint={`${data.entrants.paid} paid · ${data.entrants.comped} comped · ${data.entrants.public} public`} />
        <MetricStat label="referrals signed up" value={data.referrals.signed_up} />
        <MetricStat label="referrals paid" value={data.referrals.paid} hint={`${data.referrals.months_banked} months banked`} />
        <MetricStat label="P&L before credits" value={formatMoney(e.pnl_cents, "usd")} tone={e.pnl_cents < 0 ? "error" : "default"} />
        <MetricStat label="net after AWS credits" value={formatMoney(e.net_cents, "usd")} hint={`credits ${formatMoney(e.aws_credits_cents, "usd")}`} />
      </div>

      <Card
        title="Actions"
        action={
          <div className="flex gap-2">
            <button type="button" onClick={exportCsv} className="rounded-md border border-zinc-700 px-3 py-1.5 text-xs text-zinc-200 hover:bg-zinc-800">Export waitlist CSV</button>
            <button type="button" onClick={refresh} disabled={busy} className="rounded-md border border-zinc-700 px-3 py-1.5 text-xs text-zinc-200 hover:bg-zinc-800 disabled:opacity-50">{busy ? "Refreshing…" : "Refresh all"}</button>
          </div>
        }
      >
        <p className="text-xs text-zinc-500">{note ?? "Export the top rows for the launch email; refresh before judging."}</p>
      </Card>

      <Card title="Economics (sheet lines)">
        <Table
          head={["line", "amount"]}
          rows={[
            ["paid entrants profit", formatMoney(e.paid_profit_cents, "usd")],
            ["comped waitlist cost", formatMoney(-e.comp_cost_cents, "usd")],
            ["referral free months cost", formatMoney(-e.referral_cost_cents, "usd")],
            ["competition P&L", formatMoney(e.pnl_cents, "usd")],
            ["AWS credits", formatMoney(e.aws_credits_cents, "usd")],
            ["net", formatMoney(e.net_cents, "usd")],
          ]}
        />
      </Card>

      <Card title={`Entries (${data.entries.length})`}>
        <Table
          head={["#", "entrant", "owner", "pos", "comped", "public", "baseline", "window", "lift", "growth", "refreshed", "error"]}
          rows={data.entries.map((r, i) => [
            i + 1,
            r.display_name ?? "—",
            r.owner_id,
            r.waitlist_position ?? "—",
            r.comped ? "yes" : "no",
            r.public ? "yes" : "no",
            formatMoney(r.baseline_cents, r.currency ?? "usd"),
            formatMoney(r.window_cents, r.currency ?? "usd"),
            formatMoney(r.delta_cents, r.currency ?? "usd"),
            formatPct(r.pct),
            r.refreshed_at ?? "never",
            r.refresh_error ?? "",
          ])}
        />
      </Card>
    </div>
  );
}
```

Add the nav link in `apps/frontend/src/app/admin/layout.tsx` directly after the Growth link, same classes:

```tsx
              <Link
                href="/admin/launch"
                className="rounded-md px-3 py-1.5 text-zinc-300 hover:bg-zinc-800 hover:text-zinc-100"
              >
                Launch
              </Link>
```

- [ ] **Step 6: Run tests + typecheck + lint**

Run: `cd apps/frontend && pnpm vitest run src/app/admin/launch src/components/launch src/lib/__tests__/launch.test.ts && pnpm tsc --noEmit -p tsconfig.json && pnpm lint`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add apps/frontend/src/app/admin apps/frontend/src/app/admin/_lib/api.ts
git commit -m "feat(launch): /admin/launch — the sheet with live inputs, refresh, CSV export"
```

---

### Task 15: Full verification, docs, PR

**Files:**
- Modify: `docs/superpowers/specs/2026-08-28-launch-competition-design.md` (two reconciliations, below)
- Modify: `apps/infra/openclaw/skills/lightbulb/SKILL.md` (one short "Launch Competition" anchor — the fleet skill is the agents' knowledge of user-facing surfaces; per CLAUDE.md it must be updated when those change)
- No CLAUDE.md change: the feature is documented by the spec + plan while in flight; both get deleted when the competition ends.

- [ ] **Step 1: Reconcile the spec with what was built**

Edit the spec's Endpoints table: replace the `GET /ops/launch/waitlist.csv` row with "CSV is built client-side on `/admin/launch` from `waitlist_rows` in `GET /ops/launch`". Add to the `launch-waitlist` data table: `user_id`, `claimed_at` — "stamped on the first authed `/launch/me` so admin can resolve activation without an email index on `users`". Commit as `docs(launch): reconcile spec with implementation`.

- [ ] **Step 2: Add the fleet-skill anchor**

Append to `apps/infra/openclaw/skills/lightbulb/SKILL.md` under the surfaces section a 4-line block: what the competition is, that entry happens on `flickmeon.ai/launch` (never inside chat), that agents must never ask an owner for a Stripe key in chat (the page is the only door), and that revenue on the board is the owner's own Stripe account. Commit as `docs(skill): launch competition anchor`.

- [ ] **Step 3: Backend full targeted run + ruff**

Run: `cd apps/backend && uv run ruff check . && uv run ruff format --check . && uv run pytest tests/unit -q -x -k "launch or stripe_webhook or lifespan or ops"`
Expected: green. (The full backend suite runs in CI — do not run the 20-minute suite locally; owner directive.)

- [ ] **Step 4: Frontend full run**

Run: `cd apps/frontend && pnpm tsc --noEmit -p tsconfig.json && pnpm lint && NODE_OPTIONS='--max-old-space-size=8192' pnpm vitest run src/components/launch src/app/admin/launch src/lib/__tests__/launch.test.ts src/components/admin`
Expected: green.

- [ ] **Step 5: Infra**

Run: `cd apps/infra && npx jest && npx cdk synth > /dev/null && git status --short`
Expected: tests green; no untracked `deploy.yml` drift (or it is already committed).

- [ ] **Step 6: Rebase on `origin/main`, push, open the PR — ONLY after the owner says push**

```bash
git fetch origin main && git rebase origin/main
# re-run Steps 3–5 if the rebase pulled backend/infra changes
git push -u origin feat/launch-competition
gh pr create --repo Isol8AI/isol8 --base main --head feat/launch-competition \
  --title "feat(launch): waitlist, comp, referral free months, Stripe-revenue leaderboard, admin sheet" \
  --body-file - <<'EOF'
## What

The Lightbulb Launch Competition loop — spec: `docs/superpowers/specs/2026-08-28-launch-competition-design.md`.

- Public `/launch`: waitlist (uncapped, positions, referral links), phase-aware (waitlist → activation → live → ended), pot counter, leaderboard.
- Signed-in card: position + comp copy, activation state (reads the existing `bootstrap.state`), referral stats, **Enter** with a pasted Stripe *restricted* key (`rk_live_` only in prod; stored on the BYO-key rail, never in DDB or logs).
- Referral free month: applied from `invoice.paid` (`amount_paid > 0`) via the console-created coupon, once per referred person (conditional write), banked when it can't apply.
- Hourly revenue sweep (lifespan task, like running_gauges): net charges − refunds vs a 30-day baseline; `$ delta` ranks, `%` shown.
- `/admin/launch`: the owner's economics sheet with live inputs, force refresh, CSV export for the launch email.
- Two new tables `lightbulb-{env}-launch-waitlist` / `-launch-entries`, read by full name (prefix flip is mid-flight).

## Inert until configured

`LAUNCH_DROP_AT` is empty in both envs → every `/launch*` route 404s and the sweep never starts. Setting it (dev first) is the go-live.

## Drop-day runbook

In the spec. Stripe console: coupon 100% off once → promotion code `max_redemptions=220`, `expires_at=drop+72h`. Clerk: Restricted during the waitlist, Public at the drop.

## Tests

Backend: phase math, waitlist/entries repos (moto), routes, entry gate matrix, key validation, revenue summation, referral CAS + banking, sweep, ops compute. Frontend: form/page/card/enter/admin. Infra: tables + env.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
```

- [ ] **Step 7: Watch CI by name**

Run: `gh pr checks --repo Isol8AI/isol8 feat/launch-competition --watch` and confirm the named checks (`backend`, `frontend`, `infra-ci`, CodeRabbit) — never infer green from a quiet list. Then E2E on **dev** with Claude-in-Chrome after the dev deploy (set `LAUNCH_DROP_AT` for dev in a follow-up PR or via the owner): join with `?ref=`, sign up, activate, enter with a test-mode `rk_test_` key, see the board. Prod promotion is the owner's `workflow_dispatch`.

---

## Self-review (done while writing)

- **Spec coverage:** decisions table → Tasks 1 (phases), 5 (waitlist), 6 (attribution, `/me`), 7–8 (restricted key, baseline, entry, leaderboard), 9 (referral on first paid invoice, banking, personal-only), 10 (sweep), 11/14 (admin + CSV), 12–13 (page + card + rules copy), 2 (tables/env), 15 (runbook lives in the spec; skill anchor). Soft activation gate = no code, by design.
- **Placeholders:** none; every step carries its code.
- **Name consistency:** `require_launch`, `launch_service.me/enter/can_enter/rank/leaderboard/resolve_email/referral_link/LIVE_FOR_ENTRY/EnterRefused`, `launch_revenue.validate_key/sum_net_revenue/store_key/load_key/delete_key/KeyRejected/MixedCurrency/refresh_entry/refresh_all/sweep_loop`, `launch_referral.on_invoice_paid/grant_free_month/subscription_has_discount`, `launch_waitlist_repo.join/get/get_by_referral_code/claim_user/attribute/mark_rewarded/add_counter/list_all/count/normalize_email`, `launch_entries_repo.upsert/get/list_all/count/set_revenue/set_refresh_error/delete`, `ops.launch.compute/_cache`, frontend `fetchLaunchInfo/fetchLeaderboard/joinWaitlist/formatMoney/formatPct/LAUNCH_REF_KEY`, `getLaunchOps/refreshLaunch/LaunchAdminClient` — used with the same names everywhere above.
- **Known approximations, stated in code comments:** admin "comped" is position+activation-derived, not the $0 first invoice; the sweep is in-process; positions may have gaps under a same-email race; `count()` on entries is a scan.
