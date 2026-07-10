# Teams Billing (per-seat) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give an org owner a per-seat ($20/seat/month, quantity = every member, no trial) Teams subscription that satisfies the `team_billing_required` provision gate, keeps seat quantity synced automatically as members join/leave, and renders a real checkout paywall where the admin is blocked.

**Architecture:** Backend adds a per-seat Stripe Checkout path that reuses one shared `_build_subscription_checkout` builder (also used by the existing flat-fee path), records the subscription item id + seat count on the billing row, and exposes one `sync_team_seat_quantity(owner_id)` reconciler that is the *only* writer of seat quantity — called from the two Clerk membership webhooks and opportunistically from `GET /container/status`. The frontend consumes the gate's `team_billing_required` blocked code and renders one `TeamBillingCard` (seat math + Subscribe) in both the `/chat` blocked screen and the settings billing panel.

**Tech Stack:** FastAPI + DynamoDB (boto3) + Stripe SDK (backend, `uv`/pytest); Next.js 16 App Router + React 19 + SWR + Clerk (frontend, vitest + tsc).

## Global Constraints

Binding rules, copied verbatim from the design (`docs/superpowers/specs/2026-07-10-teams-billing-design.md`). Every task's requirements implicitly include this section.

- **DRY spine — three single-sources-of-truth.** (1) ONE checkout-session builder: extract `_build_subscription_checkout(*, owner_id, price_id, quantity, trial_days, metadata, ...)`; both flat (personal) and team paths call it — no copy of the session kwargs, idempotency-key shape, `automatic_tax`/`customer_update` block. (2) ONE seat-quantity reconciler: a single `sync_team_seat_quantity(owner_id) -> int` sets the Stripe subscription item to the **absolute** live member count (idempotent), and is the ONLY place quantity is written — called from all three triggers (membership webhooks, reconcile safety-net, post-checkout). No delta math. (3) ONE seat price: backend `STRIPE_TEAM_SEAT_PRICE_ID` is the billing source of truth; the frontend reads the display price from the billing/account API response (backend echoes it) — the number is defined once server-side, never hardcoded in two UIs.
- **$20/seat/month.** Quantity = **every org member** (admin included). **No trial** for teams.
- **Grandfather existing orgs.** New orgs use per-seat; existing org subscriptions are left untouched. No migration script. An org with an existing non-seat subscription has no `subscription_item_id` → `sync_team_seat_quantity` no-ops for it (guard), so we never touch a grandfathered sub.
- **Owner model unchanged.** Org subscription keys on `owner_id = org_id` (`resolve_owner_id`, `is_org_owner_id`).
- **Do NOT modify `provision_gate` gate logic.** This feature *consumes* the `team_billing_required` code that #1201 adds; it adds nothing to `provision_gate.py`.
- **Shared-file boundary is additive + function-scoped.** Both this stream and the free-tier stream touch `billing_service.py`, `provision_gate.py`, `webhooks.py`, `config.py`. Keep every change additive and scoped to the functions this plan names. `provision_gate.py` is theirs (consume only). Coordinate the one `create_flat_fee_checkout` refactor.
- **Backend tests:** `cd apps/backend && uv run pytest <path> -v` (never run from repo root).
- **Frontend tests:** single-file vitest only (`pnpm vitest run <file>`) — NEVER the full `pnpm test`; run `pnpm tsc --noEmit` (or `pnpm exec tsc --noEmit`) after adding tests.

## Rebase note (READ FIRST — applies to Tasks 1, 2, 6, 8, 10)

This branch will REBASE onto `main` after PR #1201 merges. #1201 adds the `team_billing_required` gate code and also touches `billing_service.py`, `webhooks.py`, `provision_gate.py`, and `config.py`. For every task below that **modifies an existing shared function** (`create_flat_fee_checkout`, `handle_subscription_state`, `_handle_organization_membership_created/_deleted`, `get_billing_account`, and the `config.py` Stripe block), **open the current post-rebase file and read the target function before editing** — the surrounding code and line numbers may have shifted. The change description + tests below are exact; the line ranges are approximate. Net-new files/functions (Tasks 3, 4, 5, 7, 9, 11) are fully specified and unaffected by the rebase.

---

## File Structure

**Backend**
- `apps/backend/core/config.py` — add `STRIPE_TEAM_SEAT_PRICE_ID` + `TEAM_SEAT_PRICE_USD` settings (Task 1).
- `apps/backend/core/services/billing_service.py` — add `_checkout_idempotency_key`, `_build_subscription_checkout`, refactor `create_flat_fee_checkout` onto it, add `create_team_seat_checkout` (Task 2), add `sync_team_seat_quantity` (Task 7).
- `apps/backend/core/repositories/billing_repo.py` — add `set_team_subscription` (Task 3).
- `apps/backend/core/services/clerk_admin.py` — add `get_organization_member_count` (Task 4).
- `apps/backend/routers/billing.py` — add `POST /team-checkout`; extend `get_billing_account` payload (Tasks 5, 10).
- `apps/backend/core/services/stripe_webhook_handlers.py` — extend `handle_subscription_state` to record team seat state (Task 6).
- `apps/backend/routers/webhooks.py` — call `sync_team_seat_quantity` from both membership handlers (Task 8).
- `apps/backend/routers/container.py` — opportunistic reconcile on `/container/status` (Task 8).
- `apps/backend/schemas/billing.py` — add `seat_count` + `team_seat_price_usd` to `BillingAccountResponse` (Task 10).

**Frontend**
- `apps/frontend/src/app/settings/billing/page.tsx` — new `/settings/billing` route (Task 9).
- `apps/frontend/src/hooks/useBilling.ts` — add `seat_count`/`team_seat_price_usd` to `BillingAccount`, add `startTeamCheckout` (Task 10).
- `apps/frontend/src/components/chat/TeamBillingCard.tsx` — new component (Task 11).
- `apps/frontend/src/components/chat/ProvisioningStepper.tsx` — early-return `TeamBillingCard` on `team_billing_required` (Task 11).
- `apps/frontend/src/app/settings/page.tsx` — render `TeamBillingCard` in `BillingPanel` for org context (Task 11).

---

## Task 1: Config — team seat price id + display price

**Files:**
- Modify: `apps/backend/core/config.py:215-218` (the "Billing / Stripe" block; approximate — see Rebase note)
- Test: `apps/backend/tests/unit/test_config.py`

**Interfaces:**
- Produces: `settings.STRIPE_TEAM_SEAT_PRICE_ID: str` (empty default, CDK-wired), `settings.TEAM_SEAT_PRICE_USD: int = 20`.

- [ ] **Step 1: Write the failing test**

Add to `apps/backend/tests/unit/test_config.py`:

```python
def test_team_seat_settings_have_expected_defaults():
    from core.config import Settings

    s = Settings()
    # Empty default so the app boots without the CDK-wired price id.
    assert s.STRIPE_TEAM_SEAT_PRICE_ID == ""
    # Per-seat display dollars — the single server-side source of the "$20".
    assert s.TEAM_SEAT_PRICE_USD == 20
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/test_config.py::test_team_seat_settings_have_expected_defaults -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'STRIPE_TEAM_SEAT_PRICE_ID'`

- [ ] **Step 3: Add the settings**

In `config.py`, directly below `STRIPE_FLAT_PRICE_ID: str = ""`:

```python
    STRIPE_FLAT_PRICE_ID: str = ""
    # Per-seat Stripe price for the Teams (org) subscription. Empty default so
    # the app boots without it; CDK-wired in dev/prod. Quantity = org member
    # count, no trial. This is the billing source of truth for "$20/seat".
    STRIPE_TEAM_SEAT_PRICE_ID: str = ""
    # Display dollars for one seat, echoed to the frontend via /billing/account
    # so the paywall renders "N × $20" without hardcoding the number in the UI.
    # Kept in lockstep with STRIPE_TEAM_SEAT_PRICE_ID's unit amount in Stripe.
    TEAM_SEAT_PRICE_USD: int = 20
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/test_config.py::test_team_seat_settings_have_expected_defaults -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/config.py apps/backend/tests/unit/test_config.py
git commit -m "feat(billing): add STRIPE_TEAM_SEAT_PRICE_ID + TEAM_SEAT_PRICE_USD settings"
```

---

## Task 2: Checkout builder — extract `_build_subscription_checkout`, refactor flat, add team

**Files:**
- Modify: `apps/backend/core/services/billing_service.py:164-238` (`create_flat_fee_checkout` — approximate, see Rebase note; add new helpers directly below it)
- Test: `apps/backend/tests/unit/services/test_billing_service.py`

**Interfaces:**
- Consumes: `settings.STRIPE_FLAT_PRICE_ID`, `settings.STRIPE_TEAM_SEAT_PRICE_ID` (Task 1), `billing_repo.get_by_owner_id`, `FRONTEND_URL`, `BillingServiceError`.
- Produces:
  - `_checkout_idempotency_key(flow: str, owner_id: str, extra: str | None = None) -> str`
  - `_build_subscription_checkout(*, owner_id: str, price_id: str, quantity: int, trial_days: int | None, metadata: dict, success_url: str, cancel_url: str, idempotency_key: str) -> stripe.checkout.Session`
  - `create_team_seat_checkout(*, owner_id: str, member_count: int) -> stripe.checkout.Session`
  - `create_flat_fee_checkout(...)` — same signature and observable behavior as today, now delegating to the builder.

- [ ] **Step 1: Write the failing tests**

Add to `apps/backend/tests/unit/services/test_billing_service.py` (inside the file, module level):

```python
class TestBuildSubscriptionCheckoutSharedSpine:
    """The flat and team paths must both go through _build_subscription_checkout
    so the session kwargs / tax block / idempotency shape exist in exactly one
    place (DRY spine, design §DRY spine item 1)."""

    @pytest.mark.asyncio
    async def test_flat_checkout_delegates_to_shared_builder(self, monkeypatch):
        monkeypatch.setattr(billing_service.settings, "STRIPE_FLAT_PRICE_ID", "price_flat_test")
        seen = {}

        async def _fake_builder(**kwargs):
            seen.update(kwargs)
            return MagicMock(url="https://checkout/flat", id="cs_flat")

        monkeypatch.setattr(billing_service, "_build_subscription_checkout", _fake_builder)
        with patch.object(
            billing_service.billing_repo, "get_by_owner_id",
            new=AsyncMock(return_value={"stripe_customer_id": "cus_test"}),
        ):
            result = await billing_service.create_flat_fee_checkout(owner_id="u_1", provider_choice="bedrock_claude")

        assert result.url == "https://checkout/flat"
        assert seen["price_id"] == "price_flat_test"
        assert seen["quantity"] == 1
        assert seen["trial_days"] == 14
        assert seen["metadata"]["owner_id"] == "u_1"
        assert seen["metadata"]["provider_choice"] == "bedrock_claude"
        assert seen["idempotency_key"].startswith("flat_checkout:u_1:")

    @pytest.mark.asyncio
    async def test_team_checkout_uses_seat_price_quantity_and_no_trial(self, monkeypatch):
        monkeypatch.setattr(billing_service.settings, "STRIPE_TEAM_SEAT_PRICE_ID", "price_seat_test")
        seen = {}

        async def _fake_builder(**kwargs):
            seen.update(kwargs)
            return MagicMock(url="https://checkout/team", id="cs_team")

        monkeypatch.setattr(billing_service, "_build_subscription_checkout", _fake_builder)
        result = await billing_service.create_team_seat_checkout(owner_id="org_1", member_count=4)

        assert result.url == "https://checkout/team"
        assert seen["price_id"] == "price_seat_test"
        assert seen["quantity"] == 4
        assert seen["trial_days"] is None
        assert seen["metadata"] == {"owner_id": "org_1", "purpose": "team_seat"}
        assert seen["idempotency_key"].startswith("team_checkout:org_1:")

    @pytest.mark.asyncio
    async def test_team_checkout_raises_without_seat_price_id(self, monkeypatch):
        monkeypatch.setattr(billing_service.settings, "STRIPE_TEAM_SEAT_PRICE_ID", "")
        with pytest.raises(BillingServiceError, match="STRIPE_TEAM_SEAT_PRICE_ID not configured"):
            await billing_service.create_team_seat_checkout(owner_id="org_1", member_count=4)

    @pytest.mark.asyncio
    async def test_team_checkout_floors_quantity_at_one(self, monkeypatch):
        monkeypatch.setattr(billing_service.settings, "STRIPE_TEAM_SEAT_PRICE_ID", "price_seat_test")
        seen = {}

        async def _fake_builder(**kwargs):
            seen.update(kwargs)
            return MagicMock(url="https://checkout/team")

        monkeypatch.setattr(billing_service, "_build_subscription_checkout", _fake_builder)
        await billing_service.create_team_seat_checkout(owner_id="org_1", member_count=0)
        assert seen["quantity"] == 1
```

Keep the existing `TestCreateFlatFeeCheckout` class — it asserts the concrete Stripe kwargs (`line_items`, `automatic_tax`, `customer_update`, `idempotency_key` prefix) still flow through, which pins that the builder preserves flat behavior end-to-end.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_billing_service.py::TestBuildSubscriptionCheckoutSharedSpine -v`
Expected: FAIL with `AttributeError: module 'core.services.billing_service' has no attribute '_build_subscription_checkout'`

- [ ] **Step 3: Implement the builder + refactor flat + add team**

In `billing_service.py`, replace the body of `create_flat_fee_checkout` (keep its `def` signature and docstring) and add the two helpers + `create_team_seat_checkout`. Read the current post-rebase `create_flat_fee_checkout` first, then make it delegate:

```python
def _checkout_idempotency_key(flow: str, owner_id: str, extra: str | None = None) -> str:
    """The single idempotency-key shape for subscription Checkout sessions.

    5-minute time bucket collapses rapid duplicate clicks / multi-tab retries
    to one session before the customer.subscription.created webhook + the
    duplicate-sub guard catch up. ``flow`` namespaces flat vs team; ``extra``
    carries the flat path's provider_choice so a config change bypasses the
    cached response naturally.
    """
    bucket = int(time.time() // 300)
    suffix = f":{extra}" if extra is not None else ""
    return f"{flow}:{owner_id}{suffix}:{bucket}"


async def _build_subscription_checkout(
    *,
    owner_id: str,
    price_id: str,
    quantity: int,
    trial_days: int | None,
    metadata: dict,
    success_url: str,
    cancel_url: str,
    idempotency_key: str,
) -> stripe.checkout.Session:
    """Single builder for subscription-mode Stripe Checkout sessions.

    Owns the session kwargs, the automatic_tax / customer_update block, and
    the subscription_data (trial + metadata) assembly so neither the flat
    (personal) nor the team path copies them. The price-id existence guard
    stays in each caller because the config key differs.
    """
    account = await billing_repo.get_by_owner_id(owner_id)
    if not account or not account.get("stripe_customer_id"):
        raise BillingServiceError(f"No Stripe customer for owner_id={owner_id}")

    subscription_data: dict = {}
    if trial_days is not None and trial_days > 0:
        subscription_data["trial_period_days"] = trial_days
    subscription_data["metadata"] = metadata

    kwargs: dict = dict(
        customer=account["stripe_customer_id"],
        mode="subscription",
        line_items=[{"price": price_id, "quantity": quantity}],
        success_url=success_url,
        cancel_url=cancel_url,
        allow_promotion_codes=True,
        automatic_tax={"enabled": True},
        customer_update={"address": "auto"},
        subscription_data=subscription_data,
        idempotency_key=idempotency_key,
    )
    with timing("stripe.api.latency", {"op": "checkout.session.create"}):
        session = stripe.checkout.Session.create(**kwargs)
    return session
```

Then rewrite `create_flat_fee_checkout`'s body (after the `if not settings.STRIPE_FLAT_PRICE_ID:` guard) to build metadata + delegate:

```python
    if not settings.STRIPE_FLAT_PRICE_ID:
        raise BillingServiceError("STRIPE_FLAT_PRICE_ID not configured")

    metadata: dict = {"owner_id": owner_id}
    if provider_choice:
        metadata["provider_choice"] = provider_choice
    if byo_provider:
        metadata["byo_provider"] = byo_provider
    if clerk_user_id:
        metadata["clerk_user_id"] = clerk_user_id

    return await _build_subscription_checkout(
        owner_id=owner_id,
        price_id=settings.STRIPE_FLAT_PRICE_ID,
        quantity=1,
        trial_days=trial_days,
        metadata=metadata,
        success_url=(
            f"{FRONTEND_URL}/chat?checkout=success"
            + (f"&provider={provider_choice}" if provider_choice else "")
        ),
        cancel_url=f"{FRONTEND_URL}/?checkout=cancel",
        idempotency_key=_checkout_idempotency_key("flat_checkout", owner_id, provider_choice or "_"),
    )
```

Add `create_team_seat_checkout` directly below `create_flat_fee_checkout`:

```python
async def create_team_seat_checkout(*, owner_id: str, member_count: int) -> stripe.checkout.Session:
    """Per-seat Teams Checkout: quantity = org member count, NO trial.

    Threads ``purpose="team_seat"`` into subscription metadata so the
    customer.subscription.* webhook records the subscription item id + seat
    count via set_team_subscription. Quantity is floored at 1 so a transient
    member-count read of 0 never creates a zero-quantity subscription.
    """
    if not settings.STRIPE_TEAM_SEAT_PRICE_ID:
        raise BillingServiceError("STRIPE_TEAM_SEAT_PRICE_ID not configured")

    quantity = max(1, member_count)
    return await _build_subscription_checkout(
        owner_id=owner_id,
        price_id=settings.STRIPE_TEAM_SEAT_PRICE_ID,
        quantity=quantity,
        trial_days=None,
        metadata={"owner_id": owner_id, "purpose": "team_seat"},
        success_url=f"{FRONTEND_URL}/chat?checkout=success",
        cancel_url=f"{FRONTEND_URL}/?checkout=cancel",
        idempotency_key=_checkout_idempotency_key("team_checkout", owner_id),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_billing_service.py -v`
Expected: PASS (new `TestBuildSubscriptionCheckoutSharedSpine` class AND the pre-existing `TestCreateFlatFeeCheckout` class both green — the flat kwargs are unchanged).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/billing_service.py apps/backend/tests/unit/services/test_billing_service.py
git commit -m "feat(billing): extract _build_subscription_checkout + add create_team_seat_checkout"
```

---

## Task 3: Repo — `set_team_subscription`

**Files:**
- Modify: `apps/backend/core/repositories/billing_repo.py` (add after `set_subscription`, ~line 226)
- Test: `apps/backend/tests/unit/repositories/test_billing_repo.py`

**Interfaces:**
- Consumes: `get_by_owner_id`, `_get_table`, `run_in_thread`, `utc_now_iso`.
- Produces: `set_team_subscription(owner_id: str, *, subscription_id: str | None, subscription_item_id: str, status: str, seat_count: int) -> dict | None` — read-then-put; returns `None` if no row exists (never upserts a phantom row).

- [ ] **Step 1: Write the failing test**

Add to `apps/backend/tests/unit/repositories/test_billing_repo.py`:

```python
@pytest.mark.asyncio
async def test_set_team_subscription_writes_item_id_and_seats(dynamodb_table):
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("org_seat", "cus_seat", owner_type="org")
    result = await billing_repo.set_team_subscription(
        "org_seat",
        subscription_id="sub_seat",
        subscription_item_id="si_seat",
        status="active",
        seat_count=5,
    )
    assert result is not None
    assert result["stripe_subscription_id"] == "sub_seat"
    assert result["subscription_item_id"] == "si_seat"
    assert result["subscription_status"] == "active"
    assert result["seat_count"] == 5

    stored = await billing_repo.get_by_owner_id("org_seat")
    assert stored["subscription_item_id"] == "si_seat"
    assert stored["seat_count"] == 5


@pytest.mark.asyncio
async def test_set_team_subscription_noops_without_row(dynamodb_table):
    from core.repositories import billing_repo

    # Never upsert a phantom row for an owner with no billing account.
    result = await billing_repo.set_team_subscription(
        "org_missing",
        subscription_id="sub_x",
        subscription_item_id="si_x",
        status="active",
        seat_count=2,
    )
    assert result is None
    assert await billing_repo.get_by_owner_id("org_missing") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/repositories/test_billing_repo.py::test_set_team_subscription_writes_item_id_and_seats -v`
Expected: FAIL with `AttributeError: module 'core.repositories.billing_repo' has no attribute 'set_team_subscription'`

- [ ] **Step 3: Implement**

Add after `set_subscription` in `billing_repo.py`:

```python
async def set_team_subscription(
    owner_id: str,
    *,
    subscription_id: str | None,
    subscription_item_id: str,
    status: str,
    seat_count: int,
) -> dict | None:
    """Persist Teams per-seat subscription identity + seat count.

    Mirrors ``set_subscription``'s read-then-put: reads the existing row and
    re-puts it with the seat fields added. Returns None (no write) when no
    billing row exists, so a stray team webhook can never upsert a phantom
    ``{owner_id, ...}`` row with no stripe_customer_id.
    """
    existing = await get_by_owner_id(owner_id)
    if existing is None:
        return None

    existing["stripe_subscription_id"] = subscription_id
    existing["subscription_status"] = status
    existing["subscription_item_id"] = subscription_item_id
    existing["seat_count"] = seat_count
    existing["updated_at"] = utc_now_iso()

    table = _get_table()
    await run_in_thread(table.put_item, Item=existing)
    return existing
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/repositories/test_billing_repo.py -k team_subscription -v`
Expected: PASS (both tests)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/repositories/billing_repo.py apps/backend/tests/unit/repositories/test_billing_repo.py
git commit -m "feat(billing): add billing_repo.set_team_subscription with seat fields"
```

---

## Task 4: Clerk — `get_organization_member_count`

**Files:**
- Modify: `apps/backend/core/services/clerk_admin.py` (add after `get_organization`, ~line 168)
- Test: `apps/backend/tests/unit/services/test_clerk_admin.py`

**Interfaces:**
- Consumes: `get_organization` (same module), `settings.CLERK_SECRET_KEY`, `httpx`, `_CLERK_API_BASE`, `_TIMEOUT_S`.
- Produces: `get_organization_member_count(org_id: str) -> int` — reads `members_count` off `get_organization`, falls back to `GET /organizations/{id}/memberships` `total_count`, returns `0` when the key is unset or on any error (stub-safe).

- [ ] **Step 1: Write the failing tests**

Add to `apps/backend/tests/unit/services/test_clerk_admin.py`:

```python
@pytest.mark.asyncio
async def test_member_count_reads_members_count(monkeypatch):
    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", "sk_test_xyz")
    from core.services import clerk_admin

    monkeypatch.setattr(clerk_admin, "get_organization", AsyncMock(return_value={"members_count": 7}))
    assert await clerk_admin.get_organization_member_count("org_1") == 7


@pytest.mark.asyncio
async def test_member_count_falls_back_to_memberships_total_count(monkeypatch):
    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", "sk_test_xyz")
    from core.services import clerk_admin

    # get_organization returns no usable members_count -> fall back to the
    # memberships list endpoint's total_count.
    monkeypatch.setattr(clerk_admin, "get_organization", AsyncMock(return_value={}))
    response = _make_response(200, json_body={"data": [], "total_count": 3})
    with patch(
        "core.services.clerk_admin.httpx.AsyncClient",
        new=lambda *a, **k: _fake_client(response),
    ):
        assert await clerk_admin.get_organization_member_count("org_1") == 3


@pytest.mark.asyncio
async def test_member_count_stubs_to_zero_without_secret_key(monkeypatch):
    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", None)
    from core.services import clerk_admin

    monkeypatch.setattr(clerk_admin, "get_organization", AsyncMock(return_value=None))
    assert await clerk_admin.get_organization_member_count("org_1") == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_clerk_admin.py -k member_count -v`
Expected: FAIL with `AttributeError: module 'core.services.clerk_admin' has no attribute 'get_organization_member_count'`

- [ ] **Step 3: Implement**

Add after `get_organization` in `clerk_admin.py`:

```python
async def get_organization_member_count(org_id: str) -> int:
    """Live member count for a Clerk organization (seat quantity source).

    Prefers ``members_count`` off the org object (one call). Falls back to the
    memberships list endpoint's ``total_count``. Returns 0 when CLERK_SECRET_KEY
    is unset or on any network/HTTP error — same defensive posture as the other
    helpers, so a Clerk outage yields a no-op seat sync rather than a crash.
    """
    org = await get_organization(org_id)
    if org and isinstance(org.get("members_count"), int):
        return org["members_count"]

    if not settings.CLERK_SECRET_KEY:
        return 0

    headers = {"Authorization": f"Bearer {settings.CLERK_SECRET_KEY}"}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.get(
                f"{_CLERK_API_BASE}/organizations/{org_id}/memberships",
                headers=headers,
                params={"limit": 1},
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("clerk_admin.get_organization_member_count network error: %s", e)
        return 0

    if response.status_code >= 400:
        logger.warning("clerk_admin.get_organization_member_count HTTP %s", response.status_code)
        return 0

    try:
        payload = response.json()
    except ValueError:
        return 0
    total = payload.get("total_count") if isinstance(payload, dict) else None
    return total if isinstance(total, int) else 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_clerk_admin.py -k member_count -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/clerk_admin.py apps/backend/tests/unit/services/test_clerk_admin.py
git commit -m "feat(billing): add clerk_admin.get_organization_member_count"
```

---

## Task 5: Endpoint — `POST /billing/team-checkout`

**Files:**
- Modify: `apps/backend/routers/billing.py` (add a new route after `create_trial_checkout`, ~line 436; add `is_org_owner_id` to the `core.auth` import at line 12)
- Test: `apps/backend/tests/unit/routers/test_billing_team_checkout.py` (new)

**Interfaces:**
- Consumes: `require_org_admin`, `resolve_owner_id`, `is_org_owner_id` (core.auth); `_get_billing_account`; `is_trial_blocked` (already imported from provision_gate); `clerk_admin.get_organization_member_count` (Task 4); `create_team_seat_checkout`, `BillingService`, `BillingServiceError` (Task 2); `create_customer_for_owner`.
- Produces: `POST /api/v1/billing/team-checkout` → `{"checkout_url": <str>}`. 400 if not org context / missing seat price; 403 non-admin (via `require_org_admin`); 409 already-subscribed.

- [ ] **Step 1: Write the failing tests**

Create `apps/backend/tests/unit/routers/test_billing_team_checkout.py`:

```python
"""POST /billing/team-checkout — per-seat Teams subscription."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from core.auth import AuthContext  # noqa: E402
from routers import billing  # noqa: E402


def _admin_ctx():
    return AuthContext(user_id="user_admin", org_id="org_1", org_role="org:admin", email="admin@example.com")


def _member_ctx():
    return AuthContext(user_id="user_mem", org_id="org_1", org_role="org:member", email="mem@example.com")


@pytest.mark.asyncio
async def test_team_checkout_happy_path(monkeypatch):
    monkeypatch.setattr(
        billing, "_get_billing_account",
        AsyncMock(return_value={"owner_id": "org_1", "stripe_customer_id": "cus_1"}),
    )
    monkeypatch.setattr(
        billing.clerk_admin, "get_organization_member_count", AsyncMock(return_value=4)
    )
    fake_session = MagicMock(url="https://checkout/team")
    with patch(
        "core.services.billing_service.create_team_seat_checkout",
        new=AsyncMock(return_value=fake_session),
    ) as mock_create:
        result = await billing.create_team_checkout(auth=_admin_ctx())

    assert result == {"checkout_url": "https://checkout/team"}
    mock_create.assert_awaited_once_with(owner_id="org_1", member_count=4)


@pytest.mark.asyncio
async def test_team_checkout_rejects_non_admin(monkeypatch):
    with pytest.raises(HTTPException) as exc:
        await billing.create_team_checkout(auth=_member_ctx())
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_team_checkout_rejects_personal_context():
    personal = AuthContext(user_id="user_p", email="p@example.com")
    with pytest.raises(HTTPException) as exc:
        await billing.create_team_checkout(auth=personal)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_team_checkout_409_when_already_subscribed(monkeypatch):
    monkeypatch.setattr(
        billing, "_get_billing_account",
        AsyncMock(return_value={"owner_id": "org_1", "stripe_customer_id": "cus_1", "subscription_status": "active"}),
    )
    with pytest.raises(HTTPException) as exc:
        await billing.create_team_checkout(auth=_admin_ctx())
    assert exc.value.status_code == 409
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_billing_team_checkout.py -v`
Expected: FAIL with `AttributeError: module 'routers.billing' has no attribute 'create_team_checkout'`

- [ ] **Step 3: Implement**

In `billing.py`, extend the `core.auth` import (line 12) to include `is_org_owner_id`:

```python
from core.auth import AuthContext, get_current_user, get_owner_type, is_org_owner_id, resolve_owner_id, require_org_admin
```

Add the route after `create_trial_checkout` (before the "Post-onboarding provider switch" section):

```python
@router.post(
    "/team-checkout",
    summary="Start a per-seat Teams subscription via Stripe Checkout",
    description=(
        "Org admins only. Creates a subscription-mode Checkout on the per-seat "
        "price with quantity = live org member count and NO trial. Returns the "
        "Checkout URL — frontend redirects the browser to it. 409 if the org "
        "already has any subscription; 403 for non-admin members."
    ),
    operation_id="create_team_checkout",
)
async def create_team_checkout(
    auth: AuthContext = Depends(get_current_user),
):
    from core.services.billing_service import (
        BillingService,
        BillingServiceError,
        create_team_seat_checkout,
    )

    owner_id = resolve_owner_id(auth)
    # Team billing keys on the org owner_id. A personal caller has no org to
    # bill per-seat — refuse rather than mint a personal seat subscription.
    if not auth.is_org_context or not is_org_owner_id(owner_id):
        raise HTTPException(status_code=400, detail="team-checkout requires an organization context")

    # Only org admins can subscribe the org (mirrors /trial-checkout). 403.
    require_org_admin(auth)

    account = await _get_billing_account(auth)
    if not account:
        billing_service = BillingService()
        account = await billing_service.create_customer_for_owner(
            owner_id=owner_id,
            owner_type="org",
            email=auth.email,
        )

    # Refuse a second subscription for any state that has ever billed / might
    # bill — Checkout in subscription mode creates a NEW subscription on every
    # successful return. Same guard set as /trial-checkout.
    if is_trial_blocked(account):
        raise HTTPException(
            status_code=409,
            detail=f"already_subscribed:{account.get('subscription_status')}",
        )

    member_count = await clerk_admin.get_organization_member_count(owner_id)
    try:
        session = await create_team_seat_checkout(owner_id=owner_id, member_count=member_count)
    except BillingServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"checkout_url": session.url}
```

`clerk_admin` and `is_trial_blocked` are already imported at the top of `billing.py`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_billing_team_checkout.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/routers/billing.py apps/backend/tests/unit/routers/test_billing_team_checkout.py
git commit -m "feat(billing): add POST /billing/team-checkout endpoint"
```

---

## Task 6: Webhook — record `subscription_item_id` + `seat_count` for team subs

**Files:**
- Modify: `apps/backend/core/services/stripe_webhook_handlers.py` — add `from core.config import settings` to imports (~line 30); extend `handle_subscription_state` (~line 196-269, see Rebase note)
- Test: `apps/backend/tests/unit/services/test_stripe_webhook_handlers.py`

**Interfaces:**
- Consumes: `billing_repo.set_team_subscription` (Task 3); `settings.STRIPE_TEAM_SEAT_PRICE_ID` (Task 1); the existing `resolve_owner_account` + `set_subscription` flow.
- Produces: after the generic `set_subscription`, when the sub is a team seat sub (metadata `purpose == "team_seat"` OR first item price id == `STRIPE_TEAM_SEAT_PRICE_ID`), also calls `set_team_subscription(owner_id, subscription_id=<id>, subscription_item_id=<items.data[0].id>, status=<new_status>, seat_count=<items.data[0].quantity>)`. Personal flat path unchanged.

- [ ] **Step 1: Write the failing tests**

Add to `apps/backend/tests/unit/services/test_stripe_webhook_handlers.py`:

```python
class TestTeamSeatWebhook:
    """handle_subscription_state records item id + seat count for team subs."""

    @staticmethod
    def _team_event(**item_overrides):
        item = {"id": "si_1", "quantity": 4, "price": {"id": "price_seat_test"}}
        item.update(item_overrides)
        return {
            "id": "evt_team",
            "type": "customer.subscription.created",
            "data": {
                "object": {
                    "id": "sub_team",
                    "customer": "cus_team",
                    "status": "active",
                    "metadata": {"owner_id": "org_team", "purpose": "team_seat"},
                    "items": {"data": [item]},
                }
            },
        }

    @pytest.mark.asyncio
    async def test_records_team_seat_state(self, monkeypatch):
        monkeypatch.setattr("core.config.settings.STRIPE_TEAM_SEAT_PRICE_ID", "price_seat_test")
        with patch.object(handlers, "billing_repo") as repo, patch.object(handlers, "stripe") as mock_stripe:
            repo.get_by_owner_id = AsyncMock(return_value={"owner_id": "org_team", "stripe_customer_id": "cus_team"})
            repo.set_subscription = AsyncMock()
            repo.set_provider_choice = AsyncMock()
            repo.set_team_subscription = AsyncMock()
            mock_stripe.Customer.retrieve.return_value = {"invoice_settings": {"default_payment_method": "pm"}}

            await handlers.handle_subscription_state(self._team_event(), MagicMock())

        repo.set_team_subscription.assert_awaited_once_with(
            owner_id="org_team",
            subscription_id="sub_team",
            subscription_item_id="si_1",
            status="active",
            seat_count=4,
        )

    @pytest.mark.asyncio
    async def test_flat_sub_does_not_call_set_team_subscription(self, monkeypatch):
        monkeypatch.setattr("core.config.settings.STRIPE_TEAM_SEAT_PRICE_ID", "price_seat_test")
        flat_event = {
            "id": "evt_flat", "type": "customer.subscription.created",
            "data": {"object": {
                "id": "sub_flat", "customer": "cus_flat", "status": "trialing",
                "metadata": {"owner_id": "user_flat"},
                "items": {"data": [{"id": "si_flat", "quantity": 1, "price": {"id": "price_flat_test"}}]},
            }},
        }
        with patch.object(handlers, "billing_repo") as repo, patch.object(handlers, "stripe") as mock_stripe:
            repo.get_by_owner_id = AsyncMock(return_value={"owner_id": "user_flat", "stripe_customer_id": "cus_flat"})
            repo.set_subscription = AsyncMock()
            repo.set_provider_choice = AsyncMock()
            repo.set_team_subscription = AsyncMock()
            mock_stripe.Customer.retrieve.return_value = {"invoice_settings": {"default_payment_method": "pm"}}

            await handlers.handle_subscription_state(flat_event, MagicMock())

        repo.set_team_subscription.assert_not_awaited()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_stripe_webhook_handlers.py::TestTeamSeatWebhook -v`
Expected: FAIL — `set_team_subscription.assert_awaited_once_with(...)` not called (or `AttributeError` if `settings` import missing).

- [ ] **Step 3: Implement**

Add the import near the top of `stripe_webhook_handlers.py` (with the other `from core...` imports, ~line 30-34):

```python
from core.config import settings
```

In `handle_subscription_state`, read the current post-rebase body and insert the team-seat recording **after** the existing `await billing_repo.set_subscription(...)` call and the provider_choice block (right before the `_bridge_subscription_state(...)` call at the end):

```python
    # Teams per-seat subscriptions additionally record the subscription item
    # id + seat quantity so sync_team_seat_quantity has an item to modify.
    # Detect by explicit metadata purpose OR by the seat price id. Personal
    # flat subs skip this entirely.
    items = (event_data.get("items") or {}).get("data") or []
    first_item = items[0] if items else {}
    first_price_id = (first_item.get("price") or {}).get("id")
    is_team_seat = metadata.get("purpose") == "team_seat" or (
        bool(settings.STRIPE_TEAM_SEAT_PRICE_ID) and first_price_id == settings.STRIPE_TEAM_SEAT_PRICE_ID
    )
    if is_team_seat and first_item:
        try:
            await billing_repo.set_team_subscription(
                owner_id=account["owner_id"],
                subscription_id=event_data["id"],
                subscription_item_id=first_item.get("id"),
                status=new_status,
                seat_count=int(first_item.get("quantity") or 0),
            )
        except Exception:
            logger.exception(
                "Failed to record team seat state for owner %s", account["owner_id"]
            )
```

Note: `metadata` and `new_status` are already local variables in `handle_subscription_state` (the provider block reads `metadata = event_data.get("metadata") or {}` and `new_status = event_data.get("status", "active")`). If the post-rebase code renamed them, reuse whatever it binds — do not re-derive.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_stripe_webhook_handlers.py -v`
Expected: PASS (new `TestTeamSeatWebhook` + all pre-existing default-PM tests).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/stripe_webhook_handlers.py apps/backend/tests/unit/services/test_stripe_webhook_handlers.py
git commit -m "feat(billing): record subscription_item_id + seat_count for team subs in webhook"
```

---

## Task 7: The one reconciler — `sync_team_seat_quantity`

**Files:**
- Modify: `apps/backend/core/services/billing_service.py` (add a module-level function near the other module-level helpers, e.g. after `create_team_seat_checkout`)
- Test: `apps/backend/tests/unit/services/test_seat_sync.py` (new)

**Interfaces:**
- Consumes: `billing_repo.get_by_owner_id`, `billing_repo.set_team_subscription` (Task 3); `clerk_admin.get_organization_member_count` (Task 4); `is_subscription_active` (provision_gate); `stripe.SubscriptionItem.modify`.
- Produces: `sync_team_seat_quantity(owner_id: str) -> int` — sets the Stripe subscription item to the absolute live member count. Idempotent. No-ops (returns the current stored `seat_count`, or 0) when there is no active subscription, no `subscription_item_id` (grandfathered / not-yet-webhooked), or the count is unchanged. The ONLY writer of seat quantity.

- [ ] **Step 1: Write the failing tests**

Create `apps/backend/tests/unit/services/test_seat_sync.py`:

```python
"""sync_team_seat_quantity — the single seat-quantity reconciler."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, patch  # noqa: E402

import pytest  # noqa: E402

from core.services import billing_service  # noqa: E402


@pytest.mark.asyncio
async def test_sets_absolute_member_count_on_mismatch(monkeypatch):
    monkeypatch.setattr(
        billing_service.billing_repo, "get_by_owner_id",
        AsyncMock(return_value={
            "owner_id": "org_1", "subscription_status": "active",
            "stripe_subscription_id": "sub_1", "subscription_item_id": "si_1", "seat_count": 3,
        }),
    )
    monkeypatch.setattr("core.services.provision_gate.is_subscription_active", lambda a: True)
    monkeypatch.setattr(
        "core.services.clerk_admin.get_organization_member_count", AsyncMock(return_value=5)
    )
    set_team = AsyncMock()
    monkeypatch.setattr(billing_service.billing_repo, "set_team_subscription", set_team)

    with patch.object(billing_service.stripe, "SubscriptionItem") as item:
        result = await billing_service.sync_team_seat_quantity("org_1")

    assert result == 5
    item.modify.assert_called_once()
    _, kwargs = item.modify.call_args
    assert kwargs["quantity"] == 5
    assert item.modify.call_args.args[0] == "si_1"
    set_team.assert_awaited_once()
    assert set_team.await_args.kwargs["seat_count"] == 5


@pytest.mark.asyncio
async def test_noop_when_count_unchanged(monkeypatch):
    monkeypatch.setattr(
        billing_service.billing_repo, "get_by_owner_id",
        AsyncMock(return_value={
            "owner_id": "org_1", "subscription_status": "active",
            "subscription_item_id": "si_1", "seat_count": 4,
        }),
    )
    monkeypatch.setattr("core.services.provision_gate.is_subscription_active", lambda a: True)
    monkeypatch.setattr(
        "core.services.clerk_admin.get_organization_member_count", AsyncMock(return_value=4)
    )
    set_team = AsyncMock()
    monkeypatch.setattr(billing_service.billing_repo, "set_team_subscription", set_team)

    with patch.object(billing_service.stripe, "SubscriptionItem") as item:
        result = await billing_service.sync_team_seat_quantity("org_1")

    assert result == 4
    item.modify.assert_not_called()
    set_team.assert_not_awaited()


@pytest.mark.asyncio
async def test_noop_without_active_subscription(monkeypatch):
    monkeypatch.setattr(
        billing_service.billing_repo, "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "org_1", "subscription_status": "canceled"}),
    )
    monkeypatch.setattr("core.services.provision_gate.is_subscription_active", lambda a: False)
    with patch.object(billing_service.stripe, "SubscriptionItem") as item:
        result = await billing_service.sync_team_seat_quantity("org_1")
    assert result == 0
    item.modify.assert_not_called()


@pytest.mark.asyncio
async def test_noop_for_grandfathered_sub_without_item_id(monkeypatch):
    # Active sub but no subscription_item_id -> grandfathered non-seat sub.
    # Never touch it.
    monkeypatch.setattr(
        billing_service.billing_repo, "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "org_1", "subscription_status": "active", "seat_count": 0}),
    )
    monkeypatch.setattr("core.services.provision_gate.is_subscription_active", lambda a: True)
    count = AsyncMock()
    monkeypatch.setattr("core.services.clerk_admin.get_organization_member_count", count)
    with patch.object(billing_service.stripe, "SubscriptionItem") as item:
        result = await billing_service.sync_team_seat_quantity("org_1")
    assert result == 0
    item.modify.assert_not_called()
    count.assert_not_awaited()  # guarded out before the Clerk call
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_seat_sync.py -v`
Expected: FAIL with `AttributeError: module 'core.services.billing_service' has no attribute 'sync_team_seat_quantity'`

- [ ] **Step 3: Implement**

Add to `billing_service.py` (module level, e.g. below `create_team_seat_checkout`):

```python
async def sync_team_seat_quantity(owner_id: str) -> int:
    """Set the team subscription's seat quantity to the live org member count.

    The SINGLE writer of seat quantity — called from the membership webhooks,
    the reconcile safety-net, and post-checkout. Absolute set (no delta math),
    idempotent. Returns the resulting seat count.

    No-ops (returns the stored seat_count, or 0) when:
      - there's no active subscription, or
      - there's no ``subscription_item_id`` (grandfathered non-seat sub, or a
        team sub whose creation webhook hasn't landed yet), or
      - the live count already equals the stored count.

    Stripe errors are logged and swallowed (returns the stored count) so a
    caller in a webhook path still 200s; the reconcile net heals the drift.
    """
    from core.services import clerk_admin
    from core.services.provision_gate import is_subscription_active

    account = await billing_repo.get_by_owner_id(owner_id)
    if not account or not is_subscription_active(account):
        return 0

    item_id = account.get("subscription_item_id")
    if not item_id:
        # Grandfathered / not-yet-webhooked — never touch a non-seat sub.
        return int(account.get("seat_count") or 0)

    current = int(account.get("seat_count") or 0)
    member_count = await clerk_admin.get_organization_member_count(owner_id)
    if member_count <= 0 or member_count == current:
        # 0 = Clerk read failed/stubbed; don't zero a real subscription.
        return current

    try:
        with timing("stripe.api.latency", {"op": "subscription_item.modify"}):
            stripe.SubscriptionItem.modify(
                item_id,
                quantity=member_count,
                proration_behavior="create_prorations",
                idempotency_key=f"seat_sync:{item_id}:{member_count}",
            )
    except Exception:
        logger.exception("sync_team_seat_quantity: Stripe modify failed for owner %s", owner_id)
        return current

    await billing_repo.set_team_subscription(
        owner_id=owner_id,
        subscription_id=account.get("stripe_subscription_id"),
        subscription_item_id=item_id,
        status=account.get("subscription_status"),
        seat_count=member_count,
    )
    return member_count
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_seat_sync.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/billing_service.py apps/backend/tests/unit/services/test_seat_sync.py
git commit -m "feat(billing): add sync_team_seat_quantity reconciler"
```

---

## Task 8: Wire seat sync into membership webhooks + `/container/status` reconcile

**Files:**
- Modify: `apps/backend/routers/webhooks.py` — `_handle_organization_membership_created` (~line 285-346) and `_handle_organization_membership_deleted` (~line 349-...), see Rebase note
- Modify: `apps/backend/routers/container.py` — `container_status` (~line 92-99); add `is_org_owner_id` to the `core.auth` import (line 12)
- Test: `apps/backend/tests/unit/routers/test_webhooks_seat_sync.py` (new), `apps/backend/tests/unit/routers/test_container_status_seat_reconcile.py` (new)

**Interfaces:**
- Consumes: `sync_team_seat_quantity` (Task 7); `is_org_owner_id` (core.auth).
- Produces: both membership handlers best-effort call `sync_team_seat_quantity(org_id)` after their existing work (errors swallowed → webhook still 200s). `container_status` dispatches `asyncio.create_task(sync_team_seat_quantity(owner_id))` for org owners (fire-and-forget; the reconciler's internal guards make it a single DDB read for non-team orgs).

- [ ] **Step 1: Write the failing tests**

Create `apps/backend/tests/unit/routers/test_webhooks_seat_sync.py`:

```python
"""Membership webhooks trigger the seat-quantity reconciler."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, patch  # noqa: E402

import pytest  # noqa: E402

from routers import webhooks  # noqa: E402


@pytest.mark.asyncio
async def test_membership_created_calls_seat_sync(monkeypatch):
    monkeypatch.setattr(webhooks.billing_repo, "get_by_owner_id", AsyncMock(return_value=None))
    sync = AsyncMock(return_value=3)
    with patch("core.services.billing_service.sync_team_seat_quantity", new=sync):
        await webhooks._handle_organization_membership_created({
            "organization": {"id": "org_1", "created_by": "user_owner"},
            "public_user_data": {"user_id": "user_new"},
            "email_address": "new@example.com",
        })
    sync.assert_awaited_once_with("org_1")


@pytest.mark.asyncio
async def test_membership_deleted_calls_seat_sync(monkeypatch):
    monkeypatch.setattr(webhooks, "build_paperclip_provisioning", AsyncMock(return_value=AsyncMock()))
    sync = AsyncMock(return_value=2)
    with patch("core.services.billing_service.sync_team_seat_quantity", new=sync):
        await webhooks._handle_organization_membership_deleted({
            "organization": {"id": "org_1"},
            "public_user_data": {"user_id": "user_gone"},
        })
    sync.assert_awaited_once_with("org_1")


@pytest.mark.asyncio
async def test_seat_sync_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr(webhooks.billing_repo, "get_by_owner_id", AsyncMock(return_value=None))
    sync = AsyncMock(side_effect=RuntimeError("stripe down"))
    with patch("core.services.billing_service.sync_team_seat_quantity", new=sync):
        # Must not raise — webhook has to keep returning 200.
        await webhooks._handle_organization_membership_created({
            "organization": {"id": "org_1", "created_by": "user_owner"},
            "public_user_data": {"user_id": "user_new"},
            "email_address": "new@example.com",
        })
```

Note: if the post-rebase `_handle_organization_membership_created` reads the member email through `_extract_org_member_email(data)`, pass whatever key that helper expects (grep it in the file) so the "missing fields" early-return doesn't fire before the seat-sync call. Adjust the test payload's email field accordingly.

Create `apps/backend/tests/unit/routers/test_container_status_seat_reconcile.py`:

```python
"""GET /container/status opportunistically reconciles team seats for org owners."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

from core.auth import AuthContext  # noqa: E402
from routers import container  # noqa: E402


@pytest.mark.asyncio
async def test_status_dispatches_seat_sync_for_org_owner(monkeypatch):
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=({"status": "running", "service_name": "svc"}, "1.2.3.4"))
    ecs.get_service_status = AsyncMock(return_value=None)
    monkeypatch.setattr(container, "get_ecs_manager", lambda: ecs)
    # Isolate from gateway-pool internals — the status endpoint's phase resolver
    # is not under test here.
    monkeypatch.setattr(container, "_safe_phase", lambda c, o: "ready")

    dispatched = []
    monkeypatch.setattr(container.asyncio, "create_task", lambda coro: dispatched.append(coro) or MagicMock())
    # sync_team_seat_quantity is a plain MagicMock so calling it returns a
    # sentinel we can assert was scheduled (not awaited inline).
    sentinel = MagicMock(name="seat_sync_coro")
    with patch("core.services.billing_service.sync_team_seat_quantity", return_value=sentinel) as mock_sync:
        auth = AuthContext(user_id="user_admin", org_id="org_1", org_role="org:admin")
        await container.container_status(auth=auth)

    mock_sync.assert_called_once_with("org_1")
    assert sentinel in dispatched
    # Close any real coroutine objects (e.g. ensure_user_ready) that the org
    # branch also scheduled, to silence "coroutine never awaited" warnings.
    for coro in dispatched:
        if hasattr(coro, "close"):
            coro.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_webhooks_seat_sync.py tests/unit/routers/test_container_status_seat_reconcile.py -v`
Expected: FAIL (seat sync not called).

- [ ] **Step 3a: Wire the membership webhooks**

In `webhooks.py`, at the END of `_handle_organization_membership_created` (after the existing `logger.info("membership.created: ...")`):

```python
    # Keep the org's per-seat subscription quantity in step with membership.
    # Best-effort: the reconciler self-guards (no-op unless the org has an
    # active team sub with an item id), and any error is swallowed so the
    # webhook still 200s — the /container/status reconcile net heals drift.
    try:
        from core.services.billing_service import sync_team_seat_quantity

        await sync_team_seat_quantity(org_id)
    except Exception:
        logger.exception("seat sync on membership.created failed for org=%s", org_id)
```

In `_handle_organization_membership_deleted`, read the org id from the payload near the top (the handler currently only extracts `user_id` — add the org id) and call the reconciler at the end. Add near the existing `user_id` extraction:

```python
    org_id = (data.get("organization") or {}).get("id", "")
```

And at the END of the handler (after the Paperclip archive/disable work, before `return`):

```python
    if org_id:
        try:
            from core.services.billing_service import sync_team_seat_quantity

            await sync_team_seat_quantity(org_id)
        except Exception:
            logger.exception("seat sync on membership.deleted failed for org=%s", org_id)
```

- [ ] **Step 3b: Wire the `/container/status` reconcile**

In `container.py`, extend the `core.auth` import (line 12):

```python
from core.auth import AuthContext, get_current_user, is_org_owner_id, resolve_owner_id
```

In `container_status`, right after `owner_id = resolve_owner_id(auth)` (~line 95), before the ECS resolve:

```python
    owner_id = resolve_owner_id(auth)
    # Opportunistic seat reconcile for org owners: converges the Stripe seat
    # quantity to the live member count without a cron, closing any missed
    # membership-webhook gap. Fire-and-forget — the reconciler self-guards to
    # a single DDB read for non-team orgs, and must never add latency or 500
    # the status poll.
    if is_org_owner_id(owner_id):
        from core.services.billing_service import sync_team_seat_quantity

        asyncio.create_task(sync_team_seat_quantity(owner_id))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_webhooks_seat_sync.py tests/unit/routers/test_container_status_seat_reconcile.py -v`
Expected: PASS. Then run the existing webhook + container regressions to confirm no break:
Run: `cd apps/backend && uv run pytest tests/unit/routers/test_webhooks.py tests/unit/routers/test_webhooks_paperclip.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/backend/routers/webhooks.py apps/backend/routers/container.py apps/backend/tests/unit/routers/test_webhooks_seat_sync.py apps/backend/tests/unit/routers/test_container_status_seat_reconcile.py
git commit -m "feat(billing): trigger sync_team_seat_quantity from membership webhooks + status reconcile"
```

---

## Task 9: Frontend — `/settings/billing` route

**Files:**
- Create: `apps/frontend/src/app/settings/billing/page.tsx`
- Test: `apps/frontend/src/app/settings/__tests__/billing-route.test.tsx` (new)

**Interfaces:**
- Produces: a client route at `/settings/billing` that redirects to `/settings?panel=billing` so the gate's `action_href="/settings/billing"` and the Stripe portal `return_url` (`create_portal_session` → `{FRONTEND_URL}/settings/billing`) both resolve.

- [ ] **Step 1: Write the failing test**

Create `apps/frontend/src/app/settings/__tests__/billing-route.test.tsx`:

```tsx
import { describe, it, expect, vi } from "vitest";
import { render } from "@testing-library/react";

const replace = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
}));

import BillingSettingsPage from "../billing/page";

describe("/settings/billing route", () => {
  it("redirects to the billing panel on /settings", () => {
    render(<BillingSettingsPage />);
    expect(replace).toHaveBeenCalledWith("/settings?panel=billing");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && pnpm vitest run src/app/settings/__tests__/billing-route.test.tsx`
Expected: FAIL — cannot resolve `../billing/page`.

- [ ] **Step 3: Implement**

Create `apps/frontend/src/app/settings/billing/page.tsx`:

```tsx
"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

/**
 * /settings/billing — a stable landing URL for the provision gate's
 * `action_href` and the Stripe billing-portal `return_url`. Both point at
 * /settings/billing; the settings page itself keys the billing pane off
 * `?panel=billing`, so this route just forwards there.
 */
export default function BillingSettingsPage() {
  const router = useRouter();
  useEffect(() => {
    router.replace("/settings?panel=billing");
  }, [router]);
  return null;
}
```

- [ ] **Step 4: Run test + typecheck**

Run: `cd apps/frontend && pnpm vitest run src/app/settings/__tests__/billing-route.test.tsx`
Expected: PASS
Run: `cd apps/frontend && pnpm exec tsc --noEmit`
Expected: no errors

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/app/settings/billing/page.tsx apps/frontend/src/app/settings/__tests__/billing-route.test.tsx
git commit -m "feat(billing): add /settings/billing route forwarding to the billing panel"
```

---

## Task 10: Billing account payload — seat data + `startTeamCheckout`

**Files:**
- Modify: `apps/backend/schemas/billing.py:6-20` (`BillingAccountResponse`)
- Modify: `apps/backend/routers/billing.py` — `get_billing_account` (~line 82-120, see Rebase note); add `is_org_owner_id` to import if not already added in Task 5
- Modify: `apps/frontend/src/hooks/useBilling.ts` — `BillingAccount` interface (line 13-25) + add `startTeamCheckout`
- Test: `apps/backend/tests/unit/routers/test_billing_account_seats.py` (new), extend `apps/frontend/src/hooks/__tests__/useBilling.test.tsx`

**Interfaces:**
- Consumes: `settings.TEAM_SEAT_PRICE_USD` (Task 1), `clerk_admin.get_organization_member_count` (Task 4), `is_org_owner_id`.
- Produces:
  - `BillingAccountResponse` gains `seat_count: int | None = None` and `team_seat_price_usd: int | None = None`.
  - For org owners, `get_billing_account` sets `seat_count` = live member count (falling back to the stored `account.seat_count`) and `team_seat_price_usd` = `settings.TEAM_SEAT_PRICE_USD`. Personal owners: both stay `None`.
  - `useBilling()` returns `startTeamCheckout(): Promise<void>` (POST `/billing/team-checkout` → redirect to `checkout_url`); `BillingAccount` gains `seat_count: number | null` and `team_seat_price_usd: number | null`.

**Decision flagged:** `seat_count` on the payload echoes the LIVE Clerk member count for org owners (not the stored Stripe quantity), so the paywall can price "N × $20" correctly *before* the first subscription exists. Post-subscribe the stored and live counts converge via `sync_team_seat_quantity`. Double-check this is the intended source.

- [ ] **Step 1: Write the failing tests (backend)**

Create `apps/backend/tests/unit/routers/test_billing_account_seats.py`:

```python
"""GET /billing/account exposes seat data for org owners."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock  # noqa: E402

import pytest  # noqa: E402

from core.auth import AuthContext  # noqa: E402
from routers import billing  # noqa: E402


@pytest.mark.asyncio
async def test_account_includes_seats_for_org_owner(monkeypatch):
    monkeypatch.setattr(billing.settings, "TEAM_SEAT_PRICE_USD", 20)
    monkeypatch.setattr(
        billing, "_get_billing_account",
        AsyncMock(return_value={"owner_id": "org_1", "subscription_status": "active", "seat_count": 3}),
    )
    monkeypatch.setattr(billing, "get_usage_summary", AsyncMock(
        return_value={"total_spend": 0.0, "lifetime_spend": 0.0}
    ))
    monkeypatch.setattr(billing.clerk_admin, "get_organization_member_count", AsyncMock(return_value=5))

    resp = await billing.get_billing_account(auth=AuthContext(user_id="u", org_id="org_1", org_role="org:admin"))
    assert resp.seat_count == 5
    assert resp.team_seat_price_usd == 20


@pytest.mark.asyncio
async def test_account_omits_seats_for_personal_owner(monkeypatch):
    monkeypatch.setattr(
        billing, "_get_billing_account",
        AsyncMock(return_value={"owner_id": "user_p", "subscription_status": "active"}),
    )
    monkeypatch.setattr(billing, "get_usage_summary", AsyncMock(
        return_value={"total_spend": 0.0, "lifetime_spend": 0.0}
    ))
    resp = await billing.get_billing_account(auth=AuthContext(user_id="user_p"))
    assert resp.seat_count is None
    assert resp.team_seat_price_usd is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_billing_account_seats.py -v`
Expected: FAIL — `BillingAccountResponse` has no `seat_count` field / not populated.

- [ ] **Step 3a: Extend the schema**

In `schemas/billing.py`, add to `BillingAccountResponse` (after `byo_provider`):

```python
    # Teams (org) per-seat billing. seat_count is the live org member count for
    # org owners (drives the paywall's "N × $20" math); team_seat_price_usd is
    # the single server-side source of the per-seat display price. Both None
    # for personal owners.
    seat_count: int | None = None
    team_seat_price_usd: int | None = None
```

- [ ] **Step 3b: Populate in `get_billing_account`**

In `billing.py`, read the current post-rebase `get_billing_account`. Ensure `is_org_owner_id` is imported (Task 5 added it; if executing this task standalone, add it to the `core.auth` import). Before the `return BillingAccountResponse(...)`, compute seat data:

```python
    seat_count: int | None = None
    team_seat_price_usd: int | None = None
    if is_org_owner_id(owner_id):
        # Live member count so the paywall prices correctly even pre-subscribe;
        # fall back to the stored Stripe quantity if Clerk is unavailable.
        live = await clerk_admin.get_organization_member_count(owner_id)
        stored = int(account.get("seat_count") or 0) if account else 0
        seat_count = live if live > 0 else (stored or None)
        team_seat_price_usd = settings.TEAM_SEAT_PRICE_USD
```

Then add the two fields to the `BillingAccountResponse(...)` call:

```python
        provider_choice=account.get("provider_choice") if account else None,
        byo_provider=account.get("byo_provider") if account else None,
        seat_count=seat_count,
        team_seat_price_usd=team_seat_price_usd,
    )
```

- [ ] **Step 4a: Run backend tests**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_billing_account_seats.py -v`
Expected: PASS (2 tests). Also run the existing account test to confirm no regression:
Run: `cd apps/backend && uv run pytest tests/unit/routers/test_billing_account_provider_choice.py -v`
Expected: PASS

- [ ] **Step 4b: Write the failing frontend test**

Add to `apps/frontend/src/hooks/__tests__/useBilling.test.tsx`:

```tsx
  it("startTeamCheckout POSTs /billing/team-checkout and redirects", async () => {
    const orig = window.location;
    // jsdom: make location.href assignable for the assertion.
    Object.defineProperty(window, "location", { value: { href: "" }, writable: true });
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ checkout_url: "https://checkout/team" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const { result } = renderHook(() => useBilling(), { wrapper });
    await result.current.startTeamCheckout();
    const call = (global.fetch as ReturnType<typeof vi.fn>).mock.calls.find((c) =>
      String(c[0]).includes("/billing/team-checkout"),
    );
    expect(call).toBeTruthy();
    expect(window.location.href).toBe("https://checkout/team");
    Object.defineProperty(window, "location", { value: orig, writable: true });
  });
```

Run: `cd apps/frontend && pnpm vitest run src/hooks/__tests__/useBilling.test.tsx`
Expected: FAIL — `result.current.startTeamCheckout is not a function`.

- [ ] **Step 5a: Implement the frontend hook changes**

In `useBilling.ts`, extend the `BillingAccount` interface (after `byo_provider`):

```typescript
  byo_provider: string | null;
  // Teams per-seat billing (org owners only; null for personal). seat_count is
  // the live org member count; team_seat_price_usd is echoed from backend
  // config so the paywall never hardcodes the per-seat price.
  seat_count: number | null;
  team_seat_price_usd: number | null;
```

Add `startTeamCheckout` inside `useBilling` (near `openPortal`):

```typescript
  const startTeamCheckout = useCallback(async () => {
    const { checkout_url } = (await api.post("/billing/team-checkout", {})) as {
      checkout_url?: string;
    };
    if (!checkout_url) throw new Error("No checkout_url returned");
    window.location.href = checkout_url;
  }, [api]);
```

Add it to the returned object:

```typescript
    openPortal,
    startTeamCheckout,
    fetchUsage,
```

- [ ] **Step 5b: Run frontend test + typecheck**

Run: `cd apps/frontend && pnpm vitest run src/hooks/__tests__/useBilling.test.tsx`
Expected: PASS (existing + new test)
Run: `cd apps/frontend && pnpm exec tsc --noEmit`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add apps/backend/schemas/billing.py apps/backend/routers/billing.py apps/backend/tests/unit/routers/test_billing_account_seats.py apps/frontend/src/hooks/useBilling.ts apps/frontend/src/hooks/__tests__/useBilling.test.tsx
git commit -m "feat(billing): expose seat_count + per-seat price on /billing/account; add startTeamCheckout"
```

---

## Task 11: `TeamBillingCard` — paywall in ProvisioningStepper + BillingPanel

**Files:**
- Create: `apps/frontend/src/components/chat/TeamBillingCard.tsx`
- Modify: `apps/frontend/src/components/chat/ProvisioningStepper.tsx` — early-return before the generic blocked card (~line 804)
- Modify: `apps/frontend/src/app/settings/page.tsx` — render `TeamBillingCard` in `BillingPanel` for org context (~line 166-216)
- Test: `apps/frontend/src/components/chat/__tests__/TeamBillingCard.test.tsx` (new)

**Interfaces:**
- Consumes: `useBilling` (`startTeamCheckout`, `openPortal`, `account`) — Task 10.
- Produces: `TeamBillingCard` component:

```typescript
export interface TeamBillingCardProps {
  seatCount: number | null;
  seatPriceUsd: number | null;
  ownerRole: "admin" | "member";
  isSubscribed?: boolean;
  onSubscribe?: () => Promise<void>;
  onManage?: () => Promise<void>;
}
```

Renders `"{N} seats × ${price} = ${N*price}/mo"`. Admin + not subscribed → "Subscribe your team" (calls `onSubscribe`); admin + subscribed → "Manage billing" (calls `onManage`); member → "Ask your org admin to set up Teams billing." (no CTA).

- [ ] **Step 1: Write the failing tests**

Create `apps/frontend/src/components/chat/__tests__/TeamBillingCard.test.tsx`:

```tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { TeamBillingCard } from "../TeamBillingCard";

describe("TeamBillingCard", () => {
  it("renders N seats × $price = total for an admin", () => {
    render(<TeamBillingCard seatCount={4} seatPriceUsd={20} ownerRole="admin" />);
    expect(screen.getByText(/4 seats/i)).toBeInTheDocument();
    expect(screen.getByText(/\$20/)).toBeInTheDocument();
    expect(screen.getByText(/\$80/)).toBeInTheDocument();
  });

  it("calls onSubscribe when an unsubscribed admin clicks subscribe", () => {
    const onSubscribe = vi.fn().mockResolvedValue(undefined);
    render(
      <TeamBillingCard seatCount={2} seatPriceUsd={20} ownerRole="admin" isSubscribed={false} onSubscribe={onSubscribe} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /subscribe your team/i }));
    expect(onSubscribe).toHaveBeenCalled();
  });

  it("shows the ask-admin message and no CTA for a member", () => {
    render(<TeamBillingCard seatCount={3} seatPriceUsd={20} ownerRole="member" />);
    expect(screen.getByText(/ask your org admin/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /subscribe/i })).toBeNull();
  });

  it("shows Manage for a subscribed admin", () => {
    const onManage = vi.fn().mockResolvedValue(undefined);
    render(
      <TeamBillingCard seatCount={5} seatPriceUsd={20} ownerRole="admin" isSubscribed onManage={onManage} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /manage billing/i }));
    expect(onManage).toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/frontend && pnpm vitest run src/components/chat/__tests__/TeamBillingCard.test.tsx`
Expected: FAIL — cannot resolve `../TeamBillingCard`.

- [ ] **Step 3: Implement the component**

Create `apps/frontend/src/components/chat/TeamBillingCard.tsx`:

```tsx
"use client";

import { useCallback, useState } from "react";

export interface TeamBillingCardProps {
  seatCount: number | null;
  seatPriceUsd: number | null;
  ownerRole: "admin" | "member";
  isSubscribed?: boolean;
  onSubscribe?: () => Promise<void>;
  onManage?: () => Promise<void>;
}

/**
 * The one Teams-billing paywall card. Rendered in two places (the /chat
 * blocked screen and the settings billing panel), so it owns no data fetching
 * — seat count + price are passed in from the /billing/account payload, and
 * the actions are injected by the caller. Admin-gated: members see an
 * ask-your-admin message with no CTA.
 */
export function TeamBillingCard({
  seatCount,
  seatPriceUsd,
  ownerRole,
  isSubscribed = false,
  onSubscribe,
  onManage,
}: TeamBillingCardProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const seats = seatCount ?? 0;
  const price = seatPriceUsd ?? 0;
  const total = seats * price;
  const isAdmin = ownerRole === "admin";

  const run = useCallback(
    async (fn?: () => Promise<void>) => {
      if (!fn) return;
      setBusy(true);
      setError(null);
      try {
        await fn();
      } catch (err) {
        setBusy(false);
        setError(err instanceof Error ? err.message : "Something went wrong. Please try again.");
      }
    },
    [],
  );

  return (
    <div className="settings-card" style={{ maxWidth: 440 }}>
      <div className="settings-card-title">Teams — ${price} / seat / month</div>
      <div className="settings-card-desc" style={{ marginTop: 4 }}>
        {seats} {seats === 1 ? "seat" : "seats"} × ${price} = <strong>${total}/mo</strong>. Every member of your
        organization is a seat.
      </div>

      {error && (
        <div className="settings-alert settings-alert-error" style={{ marginTop: 12 }}>
          {error}
        </div>
      )}

      <div style={{ marginTop: 16 }}>
        {!isAdmin ? (
          <p className="settings-card-desc">Ask your org admin to set up Teams billing.</p>
        ) : isSubscribed ? (
          <button type="button" className="settings-btn-outline" disabled={busy} onClick={() => run(onManage)}>
            Manage billing
          </button>
        ) : (
          <button type="button" className="settings-btn-save" disabled={busy} onClick={() => run(onSubscribe)}>
            {busy ? "Starting checkout…" : "Subscribe your team"}
          </button>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Run component tests to verify they pass**

Run: `cd apps/frontend && pnpm vitest run src/components/chat/__tests__/TeamBillingCard.test.tsx`
Expected: PASS (4 tests)

- [ ] **Step 5: Wire into ProvisioningStepper**

In `ProvisioningStepper.tsx`, add the import near the other chat-component imports (~line 22):

```tsx
import { TeamBillingCard } from "@/components/chat/TeamBillingCard";
```

The component already destructures `account` from `useBilling()` and `startTeamCheckout` is available on that hook — add `startTeamCheckout` to the `useBilling()` destructure in `ProvisioningStepper` (find the existing `const { ... } = useBilling();` call and add `startTeamCheckout`).

Add an early return **immediately before** the generic blocked card (`if (gatePhase === "blocked" && blocked && providerChoice === null) {` at ~line 804):

```tsx
  // Teams billing paywall: the gate returns team_billing_required for an org
  // owner with no billing row. Render the real per-seat checkout card instead
  // of the generic blocked card. Admin-gated inside TeamBillingCard (members
  // get "ask your org admin").
  if (gatePhase === "blocked" && blocked && blocked.code === "team_billing_required") {
    return (
      <div className="flex-1 flex items-center justify-center bg-[#faf7f2]">
        <TeamBillingCard
          seatCount={account?.seat_count ?? null}
          seatPriceUsd={account?.team_seat_price_usd ?? null}
          ownerRole={blocked.owner_role}
          isSubscribed={false}
          onSubscribe={startTeamCheckout}
        />
      </div>
    );
  }
```

- [ ] **Step 6: Wire into BillingPanel**

In `apps/frontend/src/app/settings/page.tsx`, import the card (with the other component imports near line 11):

```tsx
import { TeamBillingCard } from "@/components/chat/TeamBillingCard";
```

In `BillingPanel`, destructure `startTeamCheckout` from `useBilling()` (add to the existing destructure at ~line 107-113). Replace the member "Billing restricted" early-return block AND the main `$50/month` card with org-aware rendering: for an org context, render `TeamBillingCard`. Concretely, after the `isLoading` guard, add:

```tsx
  const isOrgContext = Boolean(membership);
  const orgRole: "admin" | "member" = membership?.role === "org:admin" ? "admin" : "member";

  if (isOrgContext) {
    return (
      <div className="settings-panel" id="panel-billing" role="tabpanel">
        <h1 className="settings-page-title">Billing</h1>
        <p className="settings-page-desc">Manage your team&apos;s subscription.</p>
        {accountError && (
          <div className="settings-alert settings-alert-error">
            Failed to load billing data. Please refresh the page.
          </div>
        )}
        <TeamBillingCard
          seatCount={account?.seat_count ?? null}
          seatPriceUsd={account?.team_seat_price_usd ?? null}
          ownerRole={orgRole}
          isSubscribed={isSubscribed}
          onSubscribe={startTeamCheckout}
          onManage={openPortal}
        />
      </div>
    );
  }
```

Leave the existing personal `$50/month` card render as the fall-through for personal owners (the block below this early return). Delete the old `membership && membership.role !== "org:admin"` restricted-card block since org members now get the member view of `TeamBillingCard` (its ask-admin message replaces the restricted card).

- [ ] **Step 7: Typecheck + regression-run the touched frontend tests**

Run: `cd apps/frontend && pnpm exec tsc --noEmit`
Expected: no errors
Run: `cd apps/frontend && pnpm vitest run src/components/chat/__tests__/TeamBillingCard.test.tsx src/components/chat/__tests__/ProvisioningStepper.test.tsx`
Expected: PASS (the ProvisioningStepper regression suite must still pass with the new early-return; if a fixture drives `team_billing_required`, update its expectation to the card).

- [ ] **Step 8: Commit**

```bash
git add apps/frontend/src/components/chat/TeamBillingCard.tsx apps/frontend/src/components/chat/__tests__/TeamBillingCard.test.tsx apps/frontend/src/components/chat/ProvisioningStepper.tsx apps/frontend/src/app/settings/page.tsx
git commit -m "feat(billing): TeamBillingCard paywall in ProvisioningStepper + settings billing panel"
```

---

## Final verification

- [ ] **Backend suite** (targeted, per Global Constraints):

```bash
cd apps/backend && uv run pytest tests/ -k "billing or seat or clerk_admin or webhook or config or container_status" -v
```

Expected: PASS. This spans Tasks 1-8 and 10.

- [ ] **Frontend touched files + typecheck**:

```bash
cd apps/frontend && pnpm vitest run \
  src/hooks/__tests__/useBilling.test.tsx \
  src/components/chat/__tests__/TeamBillingCard.test.tsx \
  src/app/settings/__tests__/billing-route.test.tsx \
  src/components/chat/__tests__/ProvisioningStepper.test.tsx
cd apps/frontend && pnpm exec tsc --noEmit
```

Expected: PASS, no type errors.

---

## Spec coverage map (self-review)

| Design item | Task |
|---|---|
| B1 config `STRIPE_TEAM_SEAT_PRICE_ID` (+ price echo source) | 1 |
| DRY spine #1 `_build_subscription_checkout` + flat refactor | 2 |
| B3 `create_team_seat_checkout` (seat price, qty=members, no trial) | 2 |
| B2 `set_team_subscription` + seat fields | 3 |
| B5 `get_organization_member_count` | 4 |
| B4 `POST /billing/team-checkout` (admin-gate, 409, 400) | 5 |
| B6 webhook records `subscription_item_id` + `seat_count` | 6 |
| DRY spine #2 `sync_team_seat_quantity` (absolute, guarded) | 7 |
| B7 wire into membership webhooks | 8 |
| B8 reconcile net (checkout via webhook + `/container/status`) | 8 |
| F1 `/settings/billing` route | 9 |
| F4 `/billing/account` seat_count + price; DRY spine #3 | 10 |
| F2 `useTeamCheckout` (`startTeamCheckout` on useBilling) | 10 |
| F3 `TeamBillingCard` in ProvisioningStepper + BillingPanel | 11 |
| Grandfather (no item_id → no-op) | 7 (guard) + tested |
| Consume `team_billing_required`, never modify provision_gate | 11 (frontend) — no gate edits anywhere |
