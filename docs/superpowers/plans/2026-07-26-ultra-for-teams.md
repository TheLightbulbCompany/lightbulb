# Ultra for teams — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An organization can run the Ultra container size (2048 CPU / 8192 MiB) while keeping per-seat billing, by swapping its subscription's **base** line item to the existing $150 Ultra price and leaving the seat item untouched.

**Architecture:** A team subscription is `[base, seat × N]`. Ultra replaces the base item's price, exactly as it replaces the flat item's price for a personal owner. Seat billing is never touched, because every line item is selected by price ID via the existing `subscription_item_on_price` helper. No new Stripe price, secret, or config is needed — the existing `STRIPE_ULTRA_PRICE_ID` serves both roles.

**Tech Stack:** FastAPI, Stripe Python SDK, pytest. Backend only — no frontend or infra changes.

**Spec:** `docs/superpowers/specs/2026-07-26-ultra-for-teams-design.md`

## Global Constraints

- Ultra is **only** a container size: `ULTRA_CPU = "2048"` / `ULTRA_MEMORY_MIB = "8192"` vs `PER_USER_CPU = "1024"` / `PER_USER_MEMORY_MIB = "4096"`. It changes nothing else.
- Pricing: team standard = `$50 base + $20/seat`; **team ultra = `$150 base + $20/seat`** (a 4-person Ultra team pays $230/mo). Personal ultra remains a single `$150` item.
- **No new Stripe price.** Reuse `settings.STRIPE_ULTRA_PRICE_ID` (prod `price_1Tv1mXI54BysGS3r28E4hWOS`, dev `price_1Tv1lSI54BysGS3rTDizqnP2`, both audited $150 / recurring / licensed / monthly).
- **Never select a subscription line item by position.** Always `subscription_item_on_price(items, price_id)`.
- **The seat item must never be touched by a tier change** — not its id, not its quantity.
- **Personal Ultra behaviour must not change.** It is the regression this work must not cause.
- Backend tests: `cd apps/backend && uv run pytest <path> -v`. Commit after every task. Never push. Never touch `main`.

## File Structure

**Modified**
- `apps/backend/core/services/billing_service.py` — `create_ultra_upgrade`: pick the item to swap by price, relax the two team guards, refuse the one genuinely unsupported case.
- `apps/backend/core/services/stripe_webhook_handlers.py` — stop excluding team subscriptions from tier logic.

**Tests**
- `apps/backend/tests/unit/services/test_ultra_checkout.py` — upgrade-path behaviour.
- `apps/backend/tests/unit/services/test_webhook_ultra_tier.py` — tier/resize behaviour.

---

### Task 1: A live team subscription can upgrade to Ultra

**Files:**
- Modify: `apps/backend/core/services/billing_service.py` — `create_ultra_upgrade` (starts at `:484`); the local-row guard (~`:547`), the live guard (~`:571`), and the item selection (~`:575`)
- Test: `apps/backend/tests/unit/services/test_ultra_checkout.py`

**Interfaces:**
- Consumes: `subscription_item_on_price(items: list[dict], price_id: str) -> dict` (already in `billing_service.py`) — returns the item carrying `price_id`, or `{}`.
- Produces: no new public names. `create_ultra_upgrade(owner_id, clerk_user_id=None)` keeps its signature and its `{"upgraded": True}` return.

**Three guards change, and each for a different reason — read all three before editing:**

1. **Local-row guard** (`if account and account.get("subscription_item_id"): raise`). `subscription_item_id` is set for every live per-seat team sub, so this refuses all teams. **Delete it** — teams are now supported.
2. **Live guard** (`if len(items_data) > 1 or is_team_sub: raise`). Must stop refusing the *recognised* team shape while still refusing shapes we cannot explain. Do **not** simply delete it.
3. **Item selection** (`first_item = items_data[0]`, then `item_id = first_item["id"]`). Positional. Unreachable today only because the multi-item guard rejects everything with two items — relaxing guard 2 makes it reachable, so it must be fixed in the same change.

- [ ] **Step 1: Write the failing tests**

Read the top of `test_ultra_checkout.py` first and reuse its existing fixtures, Stripe stubs and account-builder helpers; adapt the bodies below to that file's real conventions rather than inventing new ones.

```python
@pytest.mark.asyncio
async def test_team_upgrade_swaps_the_base_item_and_leaves_seats_alone(monkeypatch):
    """Team ultra = $150 base + $20/seat. The seat item must not be touched."""
    monkeypatch.setattr(billing_service.settings, "STRIPE_ULTRA_PRICE_ID", "price_ultra")
    monkeypatch.setattr(billing_service.settings, "STRIPE_TEAM_BASE_PRICE_ID", "price_base")
    monkeypatch.setattr(billing_service.settings, "STRIPE_TEAM_SEAT_PRICE_ID", "price_seat")
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={
            "owner_id": "org_1",
            "stripe_subscription_id": "sub_1",
            "subscription_status": "active",
            "subscription_item_id": "si_seat",
        }),
    )
    # Base deliberately at index 1 — selection must be by price, not position.
    sub = {"items": {"data": [
        {"id": "si_seat", "quantity": 4, "price": {"id": "price_seat"}},
        {"id": "si_base", "quantity": 1, "price": {"id": "price_base"}},
    ]}}
    monkeypatch.setattr(billing_service.stripe.Subscription, "retrieve", lambda *_a, **_k: sub)
    captured: dict = {}
    monkeypatch.setattr(
        billing_service.stripe.Subscription, "modify",
        lambda sub_id, **kw: captured.update({"sub_id": sub_id, **kw}),
    )

    result = await billing_service.create_ultra_upgrade(owner_id="org_1")

    assert result == {"upgraded": True}
    # Exactly one item modified: the BASE, swapped to the ultra price.
    assert captured["items"] == [{"id": "si_base", "price": "price_ultra"}]


@pytest.mark.asyncio
async def test_unrecognised_multi_item_sub_still_refused(monkeypatch):
    """The multi-item guard stays as defence-in-depth for shapes we can't explain."""
    monkeypatch.setattr(billing_service.settings, "STRIPE_ULTRA_PRICE_ID", "price_ultra")
    monkeypatch.setattr(billing_service.settings, "STRIPE_TEAM_BASE_PRICE_ID", "price_base")
    monkeypatch.setattr(billing_service.settings, "STRIPE_TEAM_SEAT_PRICE_ID", "price_seat")
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={
            "owner_id": "user_1",
            "stripe_subscription_id": "sub_1",
            "subscription_status": "active",
        }),
    )
    sub = {"items": {"data": [
        {"id": "si_flat", "quantity": 1, "price": {"id": "price_flat"}},
        {"id": "si_mystery", "quantity": 1, "price": {"id": "price_unknown_addon"}},
    ]}}
    monkeypatch.setattr(billing_service.stripe.Subscription, "retrieve", lambda *_a, **_k: sub)

    with pytest.raises(billing_service.BillingServiceError):
        await billing_service.create_ultra_upgrade(owner_id="user_1")


@pytest.mark.asyncio
async def test_org_without_a_live_subscription_is_refused(monkeypatch):
    """An org must buy its team plan first; the fresh-checkout branch mints a
    personal-shaped single-item Ultra sub with NO seat item, which would leave
    an org paying $150 for zero seats."""
    monkeypatch.setattr(billing_service.settings, "STRIPE_ULTRA_PRICE_ID", "price_ultra")
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "org_1", "subscription_status": "canceled"}),
    )

    with pytest.raises(billing_service.BillingServiceError, match="team"):
        await billing_service.create_ultra_upgrade(owner_id="org_1")
```

- [ ] **Step 2: Run them to confirm they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_ultra_checkout.py -k "team_upgrade_swaps or unrecognised_multi_item or org_without" -v`
Expected: the first FAILs on the local-row guard raising; the third FAILs because no such guard exists yet. The second may already pass — it must still pass at the end.

- [ ] **Step 3: Delete the local-row team guard**

Remove this block entirely (and its comment):

```python
    if account and account.get("subscription_item_id"):
        raise BillingServiceError("Ultra Pro isn't available for per-seat team plans yet")
```

- [ ] **Step 4: Refuse an org with no live subscription**

Immediately after `sub_id` / `status` are read, before the `if sub_id and status in LIVE_SUBSCRIPTION_STATUSES:` branch:

```python
    # An org with no live subscription must buy its team plan first. The
    # fresh-checkout branch below emits a single-item Ultra subscription with
    # NO seat item — correct for a personal owner, but it would leave an org
    # paying $150/mo for zero seats and with no item for
    # sync_team_seat_quantity to drive.
    from core.auth import is_org_owner_id

    if is_org_owner_id(owner_id) and not (sub_id and status in LIVE_SUBSCRIPTION_STATUSES):
        raise BillingServiceError("Subscribe your team before upgrading to Ultra Pro")
```

- [ ] **Step 5: Relax the live guard and select the item by price**

Replace the `is_team_sub` / `len(items_data) > 1` guard and the positional `first_item` read with:

```python
        seat_item = subscription_item_on_price(items_data, settings.STRIPE_TEAM_SEAT_PRICE_ID)
        base_item = subscription_item_on_price(items_data, settings.STRIPE_TEAM_BASE_PRICE_ID)
        ultra_item = subscription_item_on_price(items_data, settings.STRIPE_ULTRA_PRICE_ID)
        is_team_sub = bool(seat_item)

        # Which item carries the tier? A team's tier lives on its BASE item
        # (either the standard base, or the ultra price once upgraded); a
        # personal sub's tier is its only item. Never by position — Stripe does
        # not guarantee line-item order.
        if is_team_sub:
            target_item = base_item or ultra_item
            recognised = len(items_data) == 2 and bool(target_item)
        else:
            target_item = items_data[0] if len(items_data) == 1 else {}
            recognised = len(items_data) == 1

        # Defence in depth: never reprice an item on a subscription whose shape
        # we cannot fully explain (an unknown add-on, a coupon item, a
        # hand-edited sub). Recognised shapes are exactly: one item (personal),
        # or base+seat (team).
        if not recognised or not target_item:
            raise BillingServiceError("This subscription's plan can't be changed automatically")

        item_id = target_item["id"]
```

Leave the `stripe.Subscription.modify(...)` call below unchanged — it already
uses `item_id` and the existing idempotency key.

- [ ] **Step 6: Run the tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_ultra_checkout.py -v`
Expected: PASS. Pre-existing tests asserting the old refusal messages must be updated to the new behaviour — that is the intended change. A test that asserted "a live multi-item sub is rejected" using an unrecognised shape should still pass; one that used a real `[base, seat]` team shape now expects success.

- [ ] **Step 7: Commit**

```bash
git add apps/backend/core/services/billing_service.py apps/backend/tests/unit/services/test_ultra_checkout.py
git commit -m "feat(billing): let a team subscription upgrade to Ultra by swapping its base item"
```

---

### Task 2: The webhook tiers and resizes team subscriptions

**Files:**
- Modify: `apps/backend/core/services/stripe_webhook_handlers.py:808` — the `if items and not is_team_seat:` condition
- Test: `apps/backend/tests/unit/services/test_webhook_ultra_tier.py`

**Interfaces:**
- Consumes: `is_ultra` (already computed above the branch via `subscription_item_on_price(items, settings.STRIPE_ULTRA_PRICE_ID)` — **no change needed**, it already answers correctly for a team).
- Produces: nothing new.

**Why this is the whole change:** the tier branch currently excludes team subs outright, so a team could never *become* Ultra even once sold one. Dropping the exclusion is sufficient because `is_ultra` is already price-based.

- [ ] **Step 1: Write the failing tests**

Read the file's `_ultra_event` / `_drive` / `_account` helpers first and reuse them; the bodies below assume that shape.

```python
@pytest.mark.asyncio
async def test_team_sub_on_the_ultra_price_is_tiered_and_resized_up():
    """A team's tier lives on its base item. Excluding team subs from tier
    logic is what made Ultra unavailable to orgs."""
    event = _ultra_event(item_price=ULTRA_PRICE)
    event["data"]["object"]["metadata"]["purpose"] = "team_seat"
    event["data"]["object"]["items"]["data"] = [
        {"id": "si_ultra", "quantity": 1, "price": {"id": ULTRA_PRICE}},
        {"id": "si_seat", "quantity": 4, "price": {"id": SEAT_PRICE}},
    ]

    repo, mgr = await _drive(event, account=_account(plan="standard"))

    assert repo.set_plan.await_args.kwargs["plan"] == "ultra_pro"
    mgr.resize_user_container.assert_awaited_once_with("org_x", new_cpu="2048", new_memory="8192")


@pytest.mark.asyncio
async def test_ultra_team_persists_seat_state_and_tier_together():
    """Both webhook branches now fire on one subscription for the first time.
    They must not fight: seat persistence writes subscription_item_id +
    seat_count from the SEAT item; tier persistence writes plan + resizes."""
    event = _ultra_event(item_price=ULTRA_PRICE)
    event["data"]["object"]["metadata"]["purpose"] = "team_seat"
    event["data"]["object"]["items"]["data"] = [
        {"id": "si_ultra", "quantity": 1, "price": {"id": ULTRA_PRICE}},
        {"id": "si_seat", "quantity": 4, "price": {"id": SEAT_PRICE}},
    ]

    repo, _mgr = await _drive(event, account=_account(plan="standard"))

    # Seat state comes from the SEAT item, never the ultra/base item.
    seat_kwargs = repo.set_team_subscription.await_args.kwargs
    assert seat_kwargs["subscription_item_id"] == "si_seat"
    assert seat_kwargs["seat_count"] == 4
    # Tier state persisted on the same event.
    assert repo.set_plan.await_args.kwargs["plan"] == "ultra_pro"
```

Plus the downgrade direction — the same branch handles it, and it must resize
**down**:

```python
@pytest.mark.asyncio
async def test_team_sub_on_the_standard_base_is_tiered_down():
    """Downgrade happens in the Stripe portal (there is no downgrade endpoint);
    the webhook reacts. A team back on the standard base is plan="standard" and
    resizes down, honouring the >10-agent deferral like any other owner."""
    from core.containers.ecs_manager import PER_USER_CPU, PER_USER_MEMORY_MIB

    event = _ultra_event(item_price=BASE_PRICE)
    event["data"]["object"]["metadata"]["purpose"] = "team_seat"
    event["data"]["object"]["items"]["data"] = [
        {"id": "si_base", "quantity": 1, "price": {"id": BASE_PRICE}},
        {"id": "si_seat", "quantity": 4, "price": {"id": SEAT_PRICE}},
    ]

    repo, mgr = await _drive(
        event,
        account=_account(plan="ultra_pro"),
        agents=[{"id": f"a{i}"} for i in range(5)],
    )

    assert repo.set_plan.await_args.kwargs["plan"] == "standard"
    mgr.resize_user_container.assert_awaited_once_with(
        "org_x", new_cpu=PER_USER_CPU, new_memory=PER_USER_MEMORY_MIB
    )
```

`SEAT_PRICE` and `BASE_PRICE` must be module constants monkeypatched into
`settings.STRIPE_TEAM_SEAT_PRICE_ID` and `settings.STRIPE_TEAM_BASE_PRICE_ID`,
mirroring how the file's existing `ultra_price_configured` autouse fixture sets
the Ultra price. Add a sibling fixture if one does not already exist.

- [ ] **Step 2: Run them to confirm they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_webhook_ultra_tier.py -k "team_sub_on_the_ultra_price or persists_seat_state_and_tier" -v`
Expected: FAIL — `set_plan` is never awaited, because the tier branch skips team subs.

- [ ] **Step 3: Drop the team exclusion**

At `stripe_webhook_handlers.py:808`, change:

```python
    if items and not is_team_seat:
```

to:

```python
    if items:
```

and rewrite the comment beneath it. It currently claims *"Team-seat subs are excluded outright — their per-seat pricing is a different axis (never the ultra price), and their container sizing is managed by the seat flow, not the tier flow."* That is no longer true. Replace with:

```python
        # Only act when the payload actually carries the subscription item. A
        # partial `.updated` can arrive with an empty items.data; treating that
        # absence as "not ultra" would wrongly downgrade + shrink an Ultra owner.
        #
        # Team subs are INCLUDED: tier and headcount are independent axes. A
        # team's tier rides its BASE item (the ultra price once upgraded) while
        # its seat item is untouched, so this branch and the seat-persistence
        # branch above operate on different items and different fields.
```

- [ ] **Step 4: Run the tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_webhook_ultra_tier.py tests/unit/services/test_stripe_webhook_handlers.py -v`
Expected: PASS. A pre-existing test asserting a team-seat event never reaches tier logic (e.g. `test_team_seat_event_never_reaches_tier_logic`) now contradicts the intended behaviour — update it to assert the opposite: a team-seat event on the **standard** base price sets `plan="standard"`, and on the **ultra** price sets `plan="ultra_pro"`.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/stripe_webhook_handlers.py apps/backend/tests/unit/services/test_webhook_ultra_tier.py apps/backend/tests/unit/services/test_stripe_webhook_handlers.py
git commit -m "feat(billing): tier and resize team subscriptions like any other owner"
```

---

### Task 3: Full verification

**Files:** none modified unless a check fails.

- [ ] **Step 1: Full backend suite**

Run: `cd apps/backend && uv run pytest tests/ -q`
Expected: PASS. Scope is `tests/`, not `tests/unit/` — that is CI's scope. Takes roughly 25-30 minutes; let it finish rather than sampling it.

- [ ] **Step 2: Personal-path regression check**

Run: `cd apps/backend && uv run pytest tests/ -k "ultra or flat or trial" -q`
Expected: PASS. This is the guard on the one thing this work must not break — personal Ultra still swaps its single item, and personal flat/trial checkout is untouched.

- [ ] **Step 3: Seat-billing regression check**

Run: `cd apps/backend && uv run pytest tests/ -k "seat or team" -q`
Expected: PASS — seat sync still targets the seat item after a tier change.

- [ ] **Step 4: Lint**

Run: `cd apps/backend && uv run ruff check core routers tests && uv run ruff format --check core routers tests`
Expected: clean.

- [ ] **Step 5: Re-commit any linter auto-edits**

```bash
git status --short
git add -A && git commit -m "chore: linter auto-edits" || true
```

## Manual verification (after deploy, not a subagent task)

1. On dev, subscribe an org to the team plan; confirm two line items.
2. Upgrade that org to Ultra. In Stripe, confirm the **base** item now carries the $150 Ultra price and the **seat** item's id and quantity are unchanged.
3. Confirm the container resized to 2048/8192.
4. Invite a member; confirm the seat quantity moves and the base item stays untouched.

## Out of scope

- A Stripe customer-portal downgrade path for an Ultra team (portal price configuration, not code).
- Any frontend surfacing of Ultra for orgs.
- Personal → org conversion, which consumes this.
