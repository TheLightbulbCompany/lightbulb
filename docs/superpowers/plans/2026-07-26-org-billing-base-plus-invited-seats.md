# Org billing: $50 base + $20 per invited seat — Implementation Plan (PR 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An org owner pays `$50 base + $20 × (members + pending invitations)` at checkout, the bill is legible before they pay, and the seat quantity can never be applied to the base line item.

**Architecture:** Stripe subscriptions gain a second line item. The single checkout builder in `billing_service.py` moves from a `price_id`/`quantity` pair to a list of line items; the team path emits two elements and the personal/Ultra paths emit one. Everywhere the webhook previously reached for `items[0]`, it now selects the seat item by price ID. The seat count's source changes from Clerk accepted memberships to accepted memberships **plus pending invitations**.

**Tech Stack:** FastAPI + Pydantic settings (`apps/backend`), AWS CDK TypeScript (`apps/infra`), Next.js 16 + React 19 + Vitest (`apps/frontend`), Stripe Python SDK, Clerk Backend API over `httpx`.

**Spec:** `docs/superpowers/specs/2026-07-26-org-billing-base-plus-invited-seats-design.md`

## Global Constraints

- **Principle: no free members.** `seats = accepted_members + pending_invitations`. An invitation is billed from the moment it is sent.
- **Pricing:** `$50` base (`TEAM_BASE_PRICE_USD = 50`) + `$20` per seat (`TEAM_SEAT_PRICE_USD = 20`). A solo admin pays **$70/mo**.
- **Never select a subscription item by position.** Stripe does not guarantee line-item order. Always select by price ID.
- **Personal and Ultra checkout behaviour must not change.** Both continue to produce exactly one line item with quantity 1.
- **A Clerk read failure must never reduce a seat count.** Existing helpers return `0` on failure and `sync_team_seat_quantity` treats `0` as a no-op; preserve that posture.
- Branch off `origin/main` **after** PR #1461 (merged 2026-07-26T01:33Z) — `TeamBillingCard.tsx` in that PR is the file Task 7 edits.
- Backend tests: `cd apps/backend && uv run pytest <path> -v`. Frontend: `cd apps/frontend && pnpm test <path>`. Infra: `cd apps/infra && pnpm test`.
- Commit after every task. Never push to `main`; this plan produces one PR.

## Prerequisites (performed by the orchestrator, not a task)

These are writes to live external systems and are done before Task 1 is dispatched:

1. Create a **test-mode** Stripe price: `$50.00 USD`, `recurring`, `licensed`, every `1 month`, on the test-mode `Isol8 Teams (per seat)` product. (Prod already has `price_1TxGYyI54BysGS3rcf0lxymj`, audited and correct.)
2. `gh secret set STRIPE_TEAM_BASE_PRICE_ID_DEV` — the new test price id.
3. `gh secret set STRIPE_TEAM_BASE_PRICE_ID_PROD` — `price_1TxGYyI54BysGS3rcf0lxymj`.

## File Structure

**Modified — backend**
- `core/config.py` — two new settings beside their seat-price siblings.
- `core/services/billing_service.py` — checkout builder signature; three call sites; team price guard; Ultra team-predicate; seat-count source.
- `core/services/stripe_webhook_handlers.py` — seat-item selection by price id (fixes two read sites via one derivation).
- `core/services/clerk_admin.py` — new `get_organization_seat_count`.
- `routers/billing.py` + `schemas/billing.py` — expose `team_base_price_usd`.
- `routers/orgs.py` — seat sync after a successful invite.

**Modified — infra**
- `lib/app.ts` — new `_DEV`/`_PROD` secret pair in the workflow env.
- `lib/stacks/service-stack.ts` — new container env var.

**Modified — frontend**
- `components/chat/TeamBillingCard.tsx` — base + seats math, theme tokens.
- `hooks/useBilling.ts` — new field on the account type.

---

### Task 1: Wire `STRIPE_TEAM_BASE_PRICE_ID` through config and CDK

**Files:**
- Modify: `apps/backend/core/config.py:239` (after `STRIPE_TEAM_SEAT_PRICE_ID`) and `:247` (after `TEAM_SEAT_PRICE_USD`)
- Modify: `apps/infra/lib/app.ts:57-58`
- Modify: `apps/infra/lib/stacks/service-stack.ts:1168-1171`
- Test: `apps/infra/test/service-stack.test.ts`

**Interfaces:**
- Consumes: nothing.
- Produces: `settings.STRIPE_TEAM_BASE_PRICE_ID: str` (empty string when unconfigured) and `settings.TEAM_BASE_PRICE_USD: int = 50`, both used by Tasks 3, 4 and 7.

- [ ] **Step 1: Write the failing infra test**

Append to `apps/infra/test/service-stack.test.ts`, inside the existing top-level `describe` block:

```typescript
test("backend task definition carries STRIPE_TEAM_BASE_PRICE_ID", () => {
  const template = Template.fromStack(stack);
  template.hasResourceProperties("AWS::ECS::TaskDefinition", {
    ContainerDefinitions: Match.arrayWith([
      Match.objectLike({
        Environment: Match.arrayWith([
          Match.objectLike({ Name: "STRIPE_TEAM_BASE_PRICE_ID" }),
        ]),
      }),
    ]),
  });
});
```

If `Match` is not already imported in this file, add it: `import { Match, Template } from "aws-cdk-lib/assertions";`. If the file builds its stack under a different variable than `stack`, reuse whatever the neighbouring tests use — do not introduce a new fixture.

- [ ] **Step 2: Run it to confirm it fails**

Run: `cd apps/infra && pnpm test -- service-stack`
Expected: FAIL — no environment entry named `STRIPE_TEAM_BASE_PRICE_ID`.

- [ ] **Step 3: Add the two backend settings**

In `apps/backend/core/config.py`, immediately after the `STRIPE_TEAM_SEAT_PRICE_ID` declaration:

```python
    # Flat monthly base for the Teams (org) subscription, charged once
    # alongside the per-seat item. Empty default so the app boots without it;
    # CDK-wired in dev/prod. This is the billing source of truth for "$50 base".
    STRIPE_TEAM_BASE_PRICE_ID: str = ""
```

And immediately after `TEAM_SEAT_PRICE_USD: int = 20`:

```python
    # Display dollars for the Teams base, echoed to the frontend via
    # /billing/account so the paywall renders "$50 + N × $20" without
    # hardcoding the number in the UI. Kept in lockstep with
    # STRIPE_TEAM_BASE_PRICE_ID's unit amount in Stripe.
    TEAM_BASE_PRICE_USD: int = 50
```

- [ ] **Step 4: Reference the secrets in the deploy workflow env**

In `apps/infra/lib/app.ts`, directly after the two `STRIPE_TEAM_SEAT_PRICE_ID_*` lines:

```typescript
      // Stripe Teams BASE Price IDs — the $50/mo flat component charged
      // alongside the per-seat item. Same synth-time bake + per-env live/test
      // split as the seat price above.
      STRIPE_TEAM_BASE_PRICE_ID_DEV: "${{ secrets.STRIPE_TEAM_BASE_PRICE_ID_DEV }}",
      STRIPE_TEAM_BASE_PRICE_ID_PROD: "${{ secrets.STRIPE_TEAM_BASE_PRICE_ID_PROD }}",
```

- [ ] **Step 5: Set the container env var**

In `apps/infra/lib/stacks/service-stack.ts`, directly after the `STRIPE_TEAM_SEAT_PRICE_ID` entry, matching its exact ternary shape:

```typescript
      STRIPE_TEAM_BASE_PRICE_ID:
        props.envName === "prod"
          ? (process.env.STRIPE_TEAM_BASE_PRICE_ID_PROD ?? "")
          : (process.env.STRIPE_TEAM_BASE_PRICE_ID_DEV ?? ""),
```

Read the surrounding lines first — if the neighbouring entry uses a different prop name than `props.envName`, mirror that one exactly.

- [ ] **Step 6: Run the infra tests**

Run: `cd apps/infra && pnpm test`
Expected: PASS. Other snapshot tests may need updating — if a snapshot diff shows **only** the added env var, update it with `pnpm test -- -u`.

- [ ] **Step 7: Commit**

```bash
git add apps/backend/core/config.py apps/infra/lib/app.ts apps/infra/lib/stacks/service-stack.ts apps/infra/test/
git commit -m "feat(billing): wire STRIPE_TEAM_BASE_PRICE_ID through config and CDK"
```

---

### Task 2: `_build_subscription_checkout` accepts a line-item list

Pure refactor. No externally observable behaviour changes in this task.

**Files:**
- Modify: `apps/backend/core/services/billing_service.py:306-348` (the builder), `:250` (flat), `:362` (team), `:580` (ultra)
- Test: `apps/backend/tests/unit/services/test_billing_service.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `_build_subscription_checkout(*, owner_id: str, line_items: list[dict], metadata: dict, success_url: str, cancel_url: str, idempotency_key: str)`. `line_items` is passed verbatim to Stripe as `line_items`; each element is `{"price": str, "quantity": int}`. The `price_id` and `quantity` parameters are **removed**.

- [ ] **Step 1: Write the failing test**

Add to `apps/backend/tests/unit/services/test_billing_service.py`:

```python
@pytest.mark.asyncio
async def test_build_subscription_checkout_passes_line_items_verbatim(monkeypatch):
    """The builder forwards its line_items list to Stripe untouched."""
    captured: dict = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return {"id": "cs_test_1", "url": "https://checkout.stripe.com/x"}

    monkeypatch.setattr(billing_service.stripe.checkout.Session, "create", fake_create)
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "org_1", "stripe_customer_id": "cus_1"}),
    )

    items = [{"price": "price_base", "quantity": 1}, {"price": "price_seat", "quantity": 3}]
    await billing_service._build_subscription_checkout(
        owner_id="org_1",
        line_items=items,
        metadata={"owner_id": "org_1"},
        success_url="https://x/ok",
        cancel_url="https://x/no",
        idempotency_key="k1",
    )

    assert captured["line_items"] == items
```

Match the file's existing import style for `AsyncMock` and `billing_service`; do not add a second import if one is already present.

- [ ] **Step 2: Run it to confirm it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_billing_service.py::test_build_subscription_checkout_passes_line_items_verbatim -v`
Expected: FAIL — `TypeError: _build_subscription_checkout() got an unexpected keyword argument 'line_items'`.

- [ ] **Step 3: Change the builder signature**

In `billing_service.py`, replace the `price_id: str,` and `quantity: int,` parameters with `line_items: list[dict],`, and replace the hardcoded line in `kwargs`:

```python
        line_items=[{"price": price_id, "quantity": quantity}],
```

with:

```python
        line_items=line_items,
```

Update the docstring's closing sentence — it currently reads "The price-id existence guard stays in each caller because the config key differs." Extend it:

```
    Callers pass a list so the team path can charge a flat base alongside the
    per-seat item. Single-element lists are the norm; price-id existence
    guards stay in each caller because the config key differs.
```

- [ ] **Step 4: Update all three call sites**

At `:250` (flat fee), replace `price_id=settings.STRIPE_FLAT_PRICE_ID,` and `quantity=1,` with:

```python
            line_items=[{"price": settings.STRIPE_FLAT_PRICE_ID, "quantity": 1}],
```

At `:580` (Ultra), replace `price_id=settings.STRIPE_ULTRA_PRICE_ID,` and `quantity=1,` with:

```python
            line_items=[{"price": settings.STRIPE_ULTRA_PRICE_ID, "quantity": 1}],
```

At `:362` (team), replace `price_id=settings.STRIPE_TEAM_SEAT_PRICE_ID,` and `quantity=quantity,` with:

```python
        line_items=[{"price": settings.STRIPE_TEAM_SEAT_PRICE_ID, "quantity": quantity}],
```

The team path still emits one item here — Task 3 adds the base.

- [ ] **Step 5: Run the billing tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_billing_service.py tests/unit/routers/test_billing_change_provider.py -v`
Expected: PASS. Any test asserting on `price_id=`/`quantity=` kwargs must be updated to assert on `line_items` — that is the intended change, not a regression.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/billing_service.py apps/backend/tests/
git commit -m "refactor(billing): checkout builder takes a line-item list"
```

---

### Task 3: Team checkout emits base + seat

**Files:**
- Modify: `apps/backend/core/services/billing_service.py:350-380` (`create_team_seat_checkout`)
- Test: `apps/backend/tests/unit/services/test_billing_service.py`

**Interfaces:**
- Consumes: `settings.STRIPE_TEAM_BASE_PRICE_ID` (Task 1); `_build_subscription_checkout(..., line_items=...)` (Task 2).
- Produces: team checkout sessions carrying exactly two line items — base at quantity 1, seat at quantity N.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_team_checkout_charges_base_plus_seats(monkeypatch):
    """Team checkout emits [base x1, seat xN] — a solo admin is $50 + 1x$20."""
    captured: dict = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return {"id": "cs_test_2", "url": "https://checkout.stripe.com/y"}

    monkeypatch.setattr(billing_service.stripe.checkout.Session, "create", fake_create)
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "org_1", "stripe_customer_id": "cus_1"}),
    )
    monkeypatch.setattr(billing_service.settings, "STRIPE_TEAM_BASE_PRICE_ID", "price_base")
    monkeypatch.setattr(billing_service.settings, "STRIPE_TEAM_SEAT_PRICE_ID", "price_seat")

    await billing_service.create_team_seat_checkout(owner_id="org_1", member_count=1)

    assert captured["line_items"] == [
        {"price": "price_base", "quantity": 1},
        {"price": "price_seat", "quantity": 1},
    ]


@pytest.mark.asyncio
async def test_team_checkout_requires_base_price(monkeypatch):
    """An unconfigured base price fails loudly rather than silently under-billing."""
    monkeypatch.setattr(billing_service.settings, "STRIPE_TEAM_BASE_PRICE_ID", "")
    monkeypatch.setattr(billing_service.settings, "STRIPE_TEAM_SEAT_PRICE_ID", "price_seat")

    with pytest.raises(billing_service.BillingServiceError, match="STRIPE_TEAM_BASE_PRICE_ID"):
        await billing_service.create_team_seat_checkout(owner_id="org_1", member_count=1)
```

- [ ] **Step 2: Run to confirm both fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_billing_service.py -k "team_checkout" -v`
Expected: FAIL — one line item emitted; no base-price guard.

- [ ] **Step 3: Add the base to the checkout**

In `create_team_seat_checkout`, after the existing seat-price guard, add:

```python
    if not settings.STRIPE_TEAM_BASE_PRICE_ID:
        raise BillingServiceError("STRIPE_TEAM_BASE_PRICE_ID not configured")
```

and replace the single-element `line_items` from Task 2 with:

```python
        # Flat base + per-seat, in that order. NOTHING may select these by
        # position — the webhook picks the seat item by price id precisely
        # because Stripe does not guarantee line-item order.
        line_items=[
            {"price": settings.STRIPE_TEAM_BASE_PRICE_ID, "quantity": 1},
            {"price": settings.STRIPE_TEAM_SEAT_PRICE_ID, "quantity": quantity},
        ],
```

Update the function's docstring first line to: `"""Teams Checkout: flat base at quantity 1 plus per-seat at the seat count."""`

- [ ] **Step 4: Run to confirm they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_billing_service.py -k "team_checkout" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/billing_service.py apps/backend/tests/
git commit -m "feat(billing): team checkout charges \$50 base plus per-seat"
```

---

### Task 4: Select the seat item by price ID, never by position

The highest-stakes task in this plan. With two line items, the existing `items[0]` read can store the **base** item's id as `subscription_item_id`, after which `sync_team_seat_quantity` drives the base item's quantity — turning a 4-member org into `$50 × 4 + $20 = $220/mo`.

**Files:**
- Modify: `apps/backend/core/services/stripe_webhook_handlers.py:755-770` and `:934-935`
- Modify: `apps/backend/core/services/billing_service.py:522-533` (Ultra team-predicate)
- Test: `apps/backend/tests/unit/services/test_stripe_webhook_handlers.py`

**Interfaces:**
- Consumes: `settings.STRIPE_TEAM_SEAT_PRICE_ID`.
- Produces: nothing new; both existing read sites share one derivation, so replacing it fixes both.

- [ ] **Step 1: Write the failing regression test**

The base is placed **first** deliberately — that is the failure mode.

```python
@pytest.mark.asyncio
async def test_seat_persistence_selects_seat_item_not_position_zero(monkeypatch):
    """With the base at items[0], the SEAT item's id and quantity are stored.

    Regression guard: storing the base item's id makes sync_team_seat_quantity
    drive the base quantity, billing a 4-seat org $50x4 + $20 = $220/mo.
    """
    monkeypatch.setattr(handlers.settings, "STRIPE_TEAM_SEAT_PRICE_ID", "price_seat")
    recorded: dict = {}

    async def fake_set_team_subscription(**kwargs):
        recorded.update(kwargs)

    monkeypatch.setattr(handlers.billing_repo, "set_team_subscription", fake_set_team_subscription)

    event_data = {
        "id": "sub_1",
        "status": "active",
        "metadata": {"owner_id": "org_1", "purpose": "team_seat"},
        "items": {"data": [
            {"id": "si_base", "quantity": 1, "price": {"id": "price_base"}},
            {"id": "si_seat", "quantity": 4, "price": {"id": "price_seat"}},
        ]},
    }

    await handlers.handle_subscription_event(event_data)

    assert recorded["subscription_item_id"] == "si_seat"
    assert recorded["seat_count"] == 4
```

Read the top of `test_stripe_webhook_handlers.py` first and mirror how existing tests import the module and stub `billing_repo`, `get_by_owner_id`, and the entry-point function name — reuse the file's fixtures rather than inventing new ones, and adapt the call above to the real handler name and signature.

- [ ] **Step 2: Run to confirm it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_stripe_webhook_handlers.py -k "position_zero" -v`
Expected: FAIL — `si_base` stored instead of `si_seat`, `seat_count == 1`.

- [ ] **Step 3: Replace the positional derivation**

In `stripe_webhook_handlers.py`, replace lines 755-757:

```python
    items = (event_data.get("items") or {}).get("data") or []
    first_item = items[0] if items else {}
    first_price_id = (first_item.get("price") or {}).get("id")
```

with:

```python
    items = (event_data.get("items") or {}).get("data") or []
    first_item = items[0] if items else {}
    first_price_id = (first_item.get("price") or {}).get("id")
    # A Teams subscription carries TWO items (flat base + per-seat) and Stripe
    # does not guarantee their order. Select the seat item BY PRICE ID: storing
    # the base item's id here would make sync_team_seat_quantity drive the base
    # quantity, billing a 4-seat org $50x4 + $20 instead of $50 + $20x4.
    seat_item = next(
        (
            item
            for item in items
            if (item.get("price") or {}).get("id") == settings.STRIPE_TEAM_SEAT_PRICE_ID
        ),
        {},
    ) if settings.STRIPE_TEAM_SEAT_PRICE_ID else {}
```

Keep `first_item`/`first_price_id` — the Ultra branch below still legitimately uses them for single-item personal subscriptions.

- [ ] **Step 4: Use `seat_item` at both persistence sites**

At `:762-769`, change the guard and both fields:

```python
    if is_team_seat and seat_item:
        try:
            await billing_repo.set_team_subscription(
                owner_id=account["owner_id"],
                subscription_id=event_data["id"],
                subscription_item_id=seat_item.get("id"),
                status=new_status,
                seat_count=int(seat_item.get("quantity") or 0),
            )
```

At `:934-935`:

```python
        facts_overrides["subscription_item_id"] = seat_item.get("id")
        facts_overrides["seat_count"] = int(seat_item.get("quantity") or 0)
```

- [ ] **Step 5: Make the Ultra team-predicate explicit**

In `billing_service.py:529-533`, `len(items_data) > 1` currently means "this is a team sub". It stays correct with two items but only coincidentally. Replace the condition with an explicit price-id check:

```python
        first_price_id = (first_item.get("price") or {}).get("id")
        # Explicitly: a Teams sub is one carrying the seat price on ANY item.
        # (This previously leaned on `len(items_data) > 1`, which happens to
        # hold now that Teams has base + seat, but says nothing true.)
        is_team_sub = bool(settings.STRIPE_TEAM_SEAT_PRICE_ID) and any(
            (item.get("price") or {}).get("id") == settings.STRIPE_TEAM_SEAT_PRICE_ID
            for item in items_data
        )
        if is_team_sub:
            raise BillingServiceError("Ultra Pro isn't available for per-seat team plans yet")
```

- [ ] **Step 6: Run the affected suites**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_stripe_webhook_handlers.py tests/unit/services/test_billing_service.py tests/unit/services/test_seat_sync.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/backend/core/services/stripe_webhook_handlers.py apps/backend/core/services/billing_service.py apps/backend/tests/
git commit -m "fix(billing): select the seat subscription item by price id, never by position"
```

---

### Task 5: Seat count becomes members + pending invitations

**Files:**
- Modify: `apps/backend/core/services/clerk_admin.py` (new function after `get_organization_member_count`, which ends at :258)
- Modify: `apps/backend/core/services/billing_service.py:411` (`sync_team_seat_quantity`)
- Modify: `apps/backend/routers/billing.py:140` (the `/billing/account` display count)
- Modify: `apps/backend/routers/billing.py:678` (**the checkout quantity** — inside `create_team_checkout`)
- Test: `apps/backend/tests/unit/services/test_seat_sync.py`, `apps/backend/tests/unit/routers/test_billing_team_checkout.py`

**There are exactly three call sites.** Missing `routers/billing.py:678` would charge the owner at checkout for members only and then proration-bump them on the very first sync — a surprise charge minutes after paying.

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `async def get_organization_seat_count(org_id: str) -> int` in `clerk_admin` — returns `members_count + pending_invitations_count`, or `0` when the member read fails, or the member count alone when only the invitations read fails.

- [ ] **Step 1: Write the failing tests**

Add to `apps/backend/tests/unit/services/test_seat_sync.py`:

```python
@pytest.mark.asyncio
async def test_seat_count_bills_pending_invitations(monkeypatch):
    """No free members: 1 admin + 3 unaccepted invites = 4 seats."""
    monkeypatch.setattr(clerk_admin, "get_organization_member_count", AsyncMock(return_value=1))
    monkeypatch.setattr(clerk_admin, "_pending_invitation_count", AsyncMock(return_value=3))

    assert await clerk_admin.get_organization_seat_count("org_1") == 4


@pytest.mark.asyncio
async def test_seat_count_unchanged_when_an_invite_is_accepted(monkeypatch):
    """Acceptance moves pending -> member; the total must not move."""
    monkeypatch.setattr(clerk_admin, "get_organization_member_count", AsyncMock(return_value=2))
    monkeypatch.setattr(clerk_admin, "_pending_invitation_count", AsyncMock(return_value=2))

    assert await clerk_admin.get_organization_seat_count("org_1") == 4


@pytest.mark.asyncio
async def test_seat_count_falls_back_to_members_when_invites_unreadable(monkeypatch):
    """An invitations-read failure must never under-bill into a downgrade."""
    monkeypatch.setattr(clerk_admin, "get_organization_member_count", AsyncMock(return_value=3))
    monkeypatch.setattr(clerk_admin, "_pending_invitation_count", AsyncMock(return_value=None))

    assert await clerk_admin.get_organization_seat_count("org_1") == 3


@pytest.mark.asyncio
async def test_seat_count_zero_when_member_read_fails(monkeypatch):
    """0 keeps sync_team_seat_quantity's existing no-op-on-Clerk-outage posture."""
    monkeypatch.setattr(clerk_admin, "get_organization_member_count", AsyncMock(return_value=0))
    monkeypatch.setattr(clerk_admin, "_pending_invitation_count", AsyncMock(return_value=5))

    assert await clerk_admin.get_organization_seat_count("org_1") == 0
```

Import `clerk_admin` and `AsyncMock` following the file's existing style.

- [ ] **Step 2: Run to confirm they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_seat_sync.py -k "seat_count" -v`
Expected: FAIL — `AttributeError: module 'core.services.clerk_admin' has no attribute 'get_organization_seat_count'`.

- [ ] **Step 3: Implement both functions**

Append to `apps/backend/core/services/clerk_admin.py`, after `get_organization_member_count`:

```python
async def _pending_invitation_count(org_id: str) -> int | None:
    """Count an org's pending invitations. None signals an unreadable result.

    None is distinct from 0: 0 means "no pending invitations", None means the
    read failed and the caller must not treat the absence as a reduction.
    Filtering on status=pending is what makes revoked and expired invitations
    fall out of the seat count on their own.
    """
    if not settings.CLERK_SECRET_KEY:
        return 0

    headers = {"Authorization": f"Bearer {settings.CLERK_SECRET_KEY}"}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.get(
                f"{_CLERK_API_BASE}/organizations/{org_id}/invitations",
                headers=headers,
                params={"status": "pending", "limit": 1},
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("clerk_admin._pending_invitation_count network error: %s", e)
        return None

    if response.status_code >= 400:
        logger.warning(
            "clerk_admin._pending_invitation_count HTTP %s org=%s", response.status_code, org_id
        )
        return None

    try:
        payload = response.json()
    except ValueError:
        return None
    total = payload.get("total_count") if isinstance(payload, dict) else None
    return total if isinstance(total, int) else None


async def get_organization_seat_count(org_id: str) -> int:
    """Billable seats for an org: accepted members PLUS pending invitations.

    The platform principle is "no free members" — an invitation is billed from
    the moment it is sent. Because acceptance moves a person from pending to
    member, this total does NOT change when an invite is accepted; it moves
    only on invite, revoke, expire, and remove.

    Returns 0 when the member read fails, preserving sync_team_seat_quantity's
    treat-0-as-a-no-op posture during a Clerk outage. When only the invitations
    read fails, returns the member count alone rather than under-billing.
    """
    members = await get_organization_member_count(org_id)
    if members <= 0:
        return 0
    pending = await _pending_invitation_count(org_id)
    return members + (pending or 0)
```

- [ ] **Step 4: Run to confirm they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_seat_sync.py -k "seat_count" -v`
Expected: PASS.

- [ ] **Step 5: Switch all three consumers to the new source**

In `billing_service.py:411` inside `sync_team_seat_quantity`:

```python
    member_count = await clerk_admin.get_organization_seat_count(owner_id)
```

In `routers/billing.py:140` (the `/billing/account` display count):

```python
        live = await clerk_admin.get_organization_seat_count(owner_id)
```

Update the comment above it — it currently says "LIVE Clerk member count"; make it "LIVE billable seat count (members + pending invitations)".

In `routers/billing.py:678` (the checkout quantity, inside `create_team_checkout`):

```python
    member_count = await clerk_admin.get_organization_seat_count(owner_id)
```

Leave the `:680` comment's meaning intact but correct the name — `get_organization_seat_count` also returns `0` on any Clerk error/outage, so the existing zero-guard below it still holds.

- [ ] **Step 6: Update the team-checkout router test**

`tests/unit/routers/test_billing_team_checkout.py` stubs the old name at least once:

```python
    monkeypatch.setattr(billing.clerk_admin, "get_organization_member_count", AsyncMock(return_value=4))
```

Re-point every such stub in that file to `get_organization_seat_count`. Then add a test proving invited-but-unaccepted people are charged:

```python
@pytest.mark.asyncio
async def test_team_checkout_quantity_includes_pending_invitations(monkeypatch, _stub_set_provider_choice):
    """No free members: 1 admin + 3 pending invites checks out at 4 seats."""
    monkeypatch.setattr(
        billing,
        "_get_billing_account",
        AsyncMock(return_value={"owner_id": "org_1", "stripe_customer_id": "cus_1"}),
    )
    monkeypatch.setattr(billing.clerk_admin, "get_organization_seat_count", AsyncMock(return_value=4))
    fake_session = MagicMock(url="https://checkout/team")
    with patch(
        "core.services.billing_service.create_team_seat_checkout",
        new=AsyncMock(return_value=fake_session),
    ) as mock_create:
        await billing.create_team_checkout(auth=_admin_ctx())

    mock_create.assert_awaited_once_with(owner_id="org_1", member_count=4)
```

- [ ] **Step 7: Run the full seat and billing router suites**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_seat_sync.py tests/unit/services/test_billing_service.py tests/unit/routers/test_billing_team_checkout.py tests/unit/routers/test_billing_account_seats.py -v`
Expected: PASS. Any other test stubbing `get_organization_member_count` to drive a seat assertion must be re-pointed — that is the intended change, not a regression.

- [ ] **Step 8: Commit**

```bash
git add apps/backend/core/services/clerk_admin.py apps/backend/core/services/billing_service.py apps/backend/routers/billing.py apps/backend/tests/
git commit -m "feat(billing): bill pending invitations as seats"
```

---

### Task 6: Sync seats the moment an invite is sent

**Files:**
- Modify: `apps/backend/routers/orgs.py:87-104`
- Test: `apps/backend/tests/unit/routers/test_orgs_invitations.py` (**already exists** — append to it, and reuse its existing fixtures and auth-context helpers rather than adding new ones)

**Interfaces:**
- Consumes: `billing_service.sync_team_seat_quantity(owner_id)`; `clerk_admin.get_organization_seat_count` (Task 5).
- Produces: nothing.

- [ ] **Step 1: Write the failing tests**

Read `test_orgs_invitations.py` first. It calls the router coroutine directly with an `AuthContext`, the same pattern as `test_billing_team_checkout.py`:

```python
from core.auth import AuthContext

def _admin_ctx():
    return AuthContext(user_id="user_admin", org_id="org_test", org_role="org:admin", email="admin@example.com")
```

Append these two tests, adapting the helper name and the request-body model to whatever the file already uses:

```python
@pytest.mark.asyncio
async def test_successful_invite_syncs_seat_quantity(monkeypatch):
    """The bill moves on the admin's action, not on the next poll.

    Clerk membership webhooks are configured nowhere today (see #1465), so
    without this the seat quantity lags until someone happens to open a tab.
    """
    calls: list[str] = []

    async def fake_sync(owner_id: str) -> int:
        calls.append(owner_id)
        return 2

    monkeypatch.setattr(orgs.clerk_admin, "find_user_by_email", AsyncMock(return_value=None))
    monkeypatch.setattr(
        orgs.clerk_admin,
        "create_organization_invitation",
        AsyncMock(return_value={"id": "orginv_1"}),
    )
    monkeypatch.setattr("core.services.billing_service.sync_team_seat_quantity", fake_sync)

    result = await orgs.create_invitation(
        org_id="org_test",
        body=orgs.CreateInvitationRequest(email="new@example.com", role="org:member"),
        auth=_admin_ctx(),
    )

    assert result.invitation_id == "orginv_1"
    assert calls == ["org_test"]


@pytest.mark.asyncio
async def test_invite_still_succeeds_when_seat_sync_fails(monkeypatch):
    """The invite already committed in Clerk; a Stripe blip must not 5xx it."""

    async def boom(owner_id: str) -> int:
        raise RuntimeError("stripe down")

    monkeypatch.setattr(orgs.clerk_admin, "find_user_by_email", AsyncMock(return_value=None))
    monkeypatch.setattr(
        orgs.clerk_admin,
        "create_organization_invitation",
        AsyncMock(return_value={"id": "orginv_2"}),
    )
    monkeypatch.setattr("core.services.billing_service.sync_team_seat_quantity", boom)

    result = await orgs.create_invitation(
        org_id="org_test",
        body=orgs.CreateInvitationRequest(email="new@example.com", role="org:member"),
        auth=_admin_ctx(),
    )

    assert result.invitation_id == "orginv_2"
```

`sync_team_seat_quantity` is imported inside the function body, so patch the **source** module path (`"core.services.billing_service.sync_team_seat_quantity"`), not an attribute on `orgs` — the same reason documented in `test_billing_change_provider.py`'s `patch_background_provision` fixture.

- [ ] **Step 2: Run to confirm the first fails**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_orgs_invitations.py -k "seat_quantity or seat_sync_fails" -v`
Expected: the first FAILs on `calls == []`; the second passes vacuously (nothing calls sync yet) and becomes meaningful after Step 3.

- [ ] **Step 3: Sync after a successful invite**

In `routers/orgs.py`, immediately before `return CreateInvitationResponse(...)`:

```python
    # Seats include pending invitations ("no free members"), so the bill must
    # move now. Clerk's membership webhooks are configured nowhere today
    # (#1465), and the /container/status poll would otherwise be the only
    # thing that notices. Failure is logged, never surfaced: the invite
    # already succeeded in Clerk, and the poll reconciles.
    try:
        from core.services.billing_service import sync_team_seat_quantity

        await sync_team_seat_quantity(org_id)
    except Exception:
        logger.exception("orgs.invitation seat sync failed org_id=%s", org_id)
```

Use the module-local import style already used at `routers/webhooks.py:394`.

- [ ] **Step 4: Run to confirm it passes**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_orgs_invitations.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/routers/orgs.py apps/backend/tests/
git commit -m "feat(billing): sync seat quantity when an invitation is sent"
```

---

### Task 7: Show the real math

**Files:**
- Modify: `apps/backend/schemas/billing.py:33`
- Modify: `apps/backend/routers/billing.py:141`
- Modify: `apps/frontend/src/components/chat/TeamBillingCard.tsx`
- Modify: `apps/frontend/src/hooks/useBilling.ts`
- Test: `apps/frontend/src/components/chat/__tests__/TeamBillingCard.test.tsx`

**Interfaces:**
- Consumes: `settings.TEAM_BASE_PRICE_USD` (Task 1); the seat count now including pending invitations (Task 5).
- Produces: `team_base_price_usd: int | None` on `BillingAccountResponse`; `basePriceUsd?: number | null` on `TeamBillingCardProps`.

- [ ] **Step 1: Write the failing frontend test**

The existing tests pass props explicitly (no shared fixture) and the per-seat prop is named **`seatPriceUsd`**, not `priceUsd`. Append to `TeamBillingCard.test.tsx`, matching that style:

```tsx
it("prices base plus seats, not seats alone", () => {
  render(<TeamBillingCard seatCount={1} seatPriceUsd={20} basePriceUsd={50} ownerRole="admin" />);
  expect(screen.getByText(/\$70\/mo/)).toBeInTheDocument();
});

it("counts an invited-but-unjoined person as a billable seat", () => {
  // seatCount already includes pending invitations — 1 admin + 3 invites.
  render(<TeamBillingCard seatCount={4} seatPriceUsd={20} basePriceUsd={50} ownerRole="admin" />);
  expect(screen.getByText(/\$130\/mo/)).toBeInTheDocument();
});

it("falls back to seats-only when the base price is absent", () => {
  render(<TeamBillingCard seatCount={2} seatPriceUsd={20} basePriceUsd={null} ownerRole="admin" />);
  expect(screen.getByText(/\$40\/mo/)).toBeInTheDocument();
});
```

Note the existing `"renders N seats × $price = total for an admin"` test asserts `$80` for `seatCount={4} seatPriceUsd={20}`. Once the base is threaded it must be updated — either pass `basePriceUsd={50}` and assert `$130`, or leave the prop off and keep `$80` via the null fallback. Pick the former; it is the real rendering path.

- [ ] **Step 2: Run to confirm it fails**

Run: `cd apps/frontend && pnpm test TeamBillingCard`
Expected: FAIL — renders `$20/mo`.

- [ ] **Step 3: Add the backend field**

In `apps/backend/schemas/billing.py`, after `team_seat_price_usd: int | None = None`:

```python
    # Flat monthly base charged alongside the per-seat item. None for personal
    # owners. Frontend renders "$50 + N x $20" from these two numbers.
    team_base_price_usd: int | None = None
```

In `apps/backend/routers/billing.py`, beside the existing `team_seat_price_usd = settings.TEAM_SEAT_PRICE_USD` assignment:

```python
        team_base_price_usd = settings.TEAM_BASE_PRICE_USD
```

Initialise `team_base_price_usd = None` next to the other `None` initialisers above the `if is_org_owner_id(owner_id):` block, and add `team_base_price_usd=team_base_price_usd,` to the `BillingAccountResponse(...)` construction.

- [ ] **Step 4: Thread it through the frontend**

In `useBilling.ts`, add `team_base_price_usd?: number | null;` beside the existing `team_seat_price_usd` field.

Find every place `TeamBillingCard` is mounted (`ProvisioningStepper.tsx` for the blocked gate, `app/settings/page.tsx:311` for the settings panel) and pass `basePriceUsd={account?.team_base_price_usd ?? null}` at each, matching how each already sources `seatPriceUsd`.

In `TeamBillingCard.tsx`, add `basePriceUsd?: number | null;` to `TeamBillingCardProps`, accept it in the destructured props, and replace:

```tsx
  const total = seats * price;
```

with:

```tsx
  // "No free members": seats counts joined members AND pending invitations.
  const base = basePriceUsd ?? 0;
  const total = base + seats * price;
```

Update the description line at `:86` so it states the base explicitly, e.g.:

```tsx
  ${base} base + {seats} {seats === 1 ? "seat" : "seats"} × ${price} ={" "}
  <strong className="font-semibold text-[var(--ink)]">${total}/mo</strong>. Everyone
  you invite is a seat, from the moment you invite them.
```

- [ ] **Step 5: Swap the off-palette literals for theme tokens**

The one non-blocking finding on PR #1461: this card uses `settings.css`'s original literals while the rest of the same screen uses tokens. Replace throughout `TeamBillingCard.tsx`:

- `#1c1917` → `var(--ink)`
- `#6b655e` → `var(--mute)`
- `#b63025` → `var(--danger)`

as Tailwind arbitrary values (`text-[var(--ink)]`). Leave `#faf7f2`, `#e0dbd0` and `#06402B` alone — those already match `--cream`, `--edge` and `--forest`. Do **not** weaken the existing "emits no `settings-*` class" regression assertion.

- [ ] **Step 6: Run both suites**

Run: `cd apps/frontend && pnpm test TeamBillingCard ProvisioningStepper`
Then: `cd apps/backend && uv run pytest tests/unit/routers/ -k billing -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/backend/schemas/billing.py apps/backend/routers/billing.py apps/frontend/src apps/backend/tests
git commit -m "feat(billing): show \$50 base + N x \$20 on the team gate card"
```

---

### Task 8: Full verification

**Files:** none modified unless a failure is found.

- [ ] **Step 1: Full backend suite**

Run: `cd apps/backend && uv run pytest tests/ -q`
Expected: PASS. Note the scope is `tests/`, not `tests/unit/` — that is CI's scope.

- [ ] **Step 2: Frontend tests and typecheck**

Run: `cd apps/frontend && pnpm test && pnpm exec tsc --noEmit && pnpm run lint`
Expected: PASS. `tsc` typechecks test files too, so a prop added to a component must also be added anywhere tests construct it.

- [ ] **Step 3: Infra tests**

Run: `cd apps/infra && pnpm test`
Expected: PASS.

- [ ] **Step 4: Confirm the personal path is untouched**

Run: `cd apps/backend && uv run pytest tests/ -k "flat or trial or ultra" -q`
Expected: PASS — personal and Ultra checkout still emit exactly one line item at quantity 1.

- [ ] **Step 5: Re-commit any linter auto-edits, then open the PR**

```bash
git status --short
git add -A && git commit -m "chore: linter auto-edits" || true
```

Open a PR against `main`. Do not merge without approval.

## Manual dev verification (after deploy, before prod)

Not a subagent task — requires a browser and a real Clerk org.

1. Create a Clerk org on dev with a fresh account (never `isol8-e2e-testing@mailsac.com`).
2. Hit the `team_billing_required` gate; confirm the card reads **`$50 base + 1 seat × $20 = $70/mo`** and the CTA is a real styled button.
3. Complete Stripe Checkout in test mode.
4. In the Stripe dashboard, confirm the subscription has **two** line items and that the **seat** item — not the base — carries the quantity.
5. Confirm the DynamoDB billing row's `subscription_item_id` matches the **seat** item id.
6. Invite one person; **without accepting**, confirm the seat item's quantity moves to 2 and the base item stays at 1. This is the whole principle — verify it before prod.

### Prod cutover for an existing seat-only subscription

There is no migration. An org already holding a seat-only $20 subscription keeps a single line item and never acquires a base, because the base is only added at checkout. If the founder org is in that state, **cancel and resubscribe it** — do not hand-edit the Stripe subscription, because adding a base item out of band without re-running the webhook leaves `subscription_item_id` pointing at whatever `items[0]` was. Confirm afterwards that the DDB row's `subscription_item_id` matches the seat item.

---

## PR 2 (separate plan, blocked)

Clerk-native team management is **not** in this plan. It depends on #1465 (authenticate `/webhooks/clerk`) because every Clerk-native remove/revoke writes to Clerk rather than to us, making webhooks the only timely billing signal. Write that plan once #1465 ships.
