# iOS In-App Purchase Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A free-tier workspace admin can buy Base / Workspace / Ultra in the iPhone app via StoreKit 2 — or, on the US App Store, at the cheaper web price via Stripe Checkout in Safari — the workspace becomes paid exactly like a Stripe checkout, and the whole loop is testable in dev with Xcode StoreKit Testing — shipped dark in prod.

**Architecture:** The backend's existing Apple notification path (`core/services/apple_iap.py`) gets its deleted link half back (`link_purchase`, `owner_account_token`), a product→plan map, and a dev-only Xcode verifier. Two admin-only routes (`purchase-intent`, `link`) sit in `routers/apple_iap.py`. The mobile app gets a paywall screen, a Settings → Plans row, the paywall in place of the subscription gate, and a launch listener that links unfinished transactions. Visibility is server-driven: `/billing/account.apple_iap_product_ids` is empty unless `APPLE_IAP_SELLING` is on.

**Tech Stack:** FastAPI + DynamoDB (`billing_repo`), `app-store-server-library` (already a dependency), AWS CDK (`service-stack.ts`), Expo SDK 57 / React Native, `expo-iap@5.3.0` (already in the binary), jest, pytest.

**Spec:** `docs/superpowers/specs/2026-09-25-ios-in-app-purchase-design.md`

## Global Constraints

- Work in the worktree `/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/.worktrees/ios-iap` on branch `feat/ios-iap`. Run `pnpm install` at the worktree root and `cd apps/backend && uv sync` once before the first task.
- Products (exact ids → `billing_accounts.plan`): `company.thelightbulb.mobile.base.monthly` → `base`; `company.thelightbulb.mobile.plan.monthly` → `standard`; `company.thelightbulb.mobile.ultra.monthly` → `ultra_pro`.
- iOS prices are never in code. The app renders StoreKit's `displayPrice` only. The `.storekit` test file uses $23.99 / $59.99 / $179.99.
- Web prices, Stripe checkout behaviour for non-Apple workspaces, and the web frontend are unchanged.
- Xcode-signed transactions are accepted ONLY when `APPLE_IAP_ACCEPT_XCODE` is true AND `settings.ENVIRONMENT != "prod"`. Prod must refuse them even if misconfigured.
- Only an org **admin** may call purchase-intent or link (`require_org_admin`). Members get 403.
- No new dependencies (backend or mobile). No `app.json` / native change (the OTA fingerprint must stay unchanged).
- Mobile copy on iOS: the web / a web price may appear ONLY in the paywall's "Or pay $X/month on the web" option. The app is sold on the US App Store only (ASC availability), which is what makes that link allowed (3.1.1(a) US carve-out) — there is no in-app storefront check. Web prices come from `/billing/account` (`base_price_usd`, `workspace_price_usd`, `ultra_price_usd`), never from the binary. The IAP button is never less prominent than the web option. Checkout opens with `Linking.openURL` (Safari), never a WebView.
- Backend tests: run targeted files only, with `cd apps/backend && uv run python -m pytest <files> -q` (not `uv run pytest`, which picks a stray interpreter). Never chain a merge or commit after a pipe that hides pytest's exit code.
- Mobile tests: `cd apps/mobile && pnpm jest <paths>`; also `pnpm typecheck` and `pnpm lint` before each mobile commit.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.
- Any change under `apps/infra/lib/` requires `cd apps/infra && npx cdk synth` and committing the regenerated `.github/workflows/deploy.yml` in the same commit.

## Review Focus

1. **Apple charged, link failed** (network drop, backend 5xx, app killed): the transaction must stay unfinished and be linked on next launch by the listener — never lost, never finished before a 2xx. Tests in Task 6 (paywall) and Task 7 (listener).
2. **A Stripe-billed workspace admin opens the app**: no paywall anywhere, and a direct intent call returns 409 before StoreKit is reached. Tests in Task 3 and Task 6.
3. **An Xcode transaction reaching prod**: refused even if `APPLE_IAP_ACCEPT_XCODE` were set. Tests in Task 1 (backend guard) and Task 4 (CDK can't set it for prod).
4. **Double-tap Buy / tapping Buy while a purchase sheet is up**: one StoreKit request only. Test in Task 6.
5. **Apple-paid owner lands on `credits_required` or `provider_required` right after paying** (the latent `provider_choice` bug): link must leave `provider_choice` unset and write `plan`. Test in Task 2.
6. **Someone opens `flickmeon://…/checkout_redirect/success` without paying**: it only triggers a refetch; the plan comes from the Stripe webhook. Test in Task 9.

---

### Task 1: Product→plan map, account token, Xcode verifier, family-sharing refusal

**Files:**
- Modify: `apps/backend/core/services/apple_iap.py`
- Modify: `apps/backend/core/config.py:296-318` (Apple IAP block)
- Test: `apps/backend/tests/unit/services/test_apple_iap.py`

**Interfaces:**
- Produces:
  - `apple_iap.PRODUCT_PLANS: dict[str, str]` — the three ids above → `base` / `standard` / `ultra_pro`.
  - `apple_iap.owner_account_token(owner_id: str) -> str` — lowercase UUID string.
  - `AppleSubscription.owner_hint: str | None` and `AppleSubscription.plan: str` (new fields).
  - `apple_iap.verify_transaction(signed_transaction: str) -> AppleSubscription`.
  - `settings.APPLE_IAP_SELLING: bool = False`, `settings.APPLE_IAP_ACCEPT_XCODE: bool = False`.
  - `settings.APPLE_IAP_PRODUCT_ID` is **removed** (the map is a code constant; product ids never change).

- [ ] **Step 1: Write the failing tests** — append to `tests/unit/services/test_apple_iap.py`, and change the `_product_id` autouse fixture to a no-op (delete it; the map is a constant now).

```python
@pytest.mark.parametrize(
    "product_id,plan",
    [
        ("company.thelightbulb.mobile.base.monthly", "base"),
        ("company.thelightbulb.mobile.plan.monthly", "standard"),
        ("company.thelightbulb.mobile.ultra.monthly", "ultra_pro"),
    ],
)
def test_each_product_maps_to_its_plan(product_id, plan):
    sub = apple_iap._subscription_from_transaction(_transaction(productId=product_id), None)
    assert sub.plan == plan


def test_an_unknown_product_never_grants():
    with pytest.raises(AppleIapError):
        apple_iap._subscription_from_transaction(_transaction(productId="com.example.other"), None)


def test_family_shared_transactions_are_refused():
    with pytest.raises(AppleIapError, match="family"):
        apple_iap._subscription_from_transaction(_transaction(inAppOwnershipType="FAMILY_SHARED"), None)


def test_owner_hint_is_read_from_app_account_token():
    sub = apple_iap._subscription_from_transaction(_transaction(appAccountToken="ABC-123"), None)
    assert sub.owner_hint == "ABC-123"


def test_owner_account_token_is_a_stable_v5_shaped_uuid():
    import uuid

    token = apple_iap.owner_account_token("org_3JlId4cAIysT9sovRPThedSNY5S")
    assert token == apple_iap.owner_account_token("org_3JlId4cAIysT9sovRPThedSNY5S")
    parsed = uuid.UUID(token)
    assert str(parsed) == token and parsed.version == 5
    assert token != apple_iap.owner_account_token("org_other")


def _xcode_settings(monkeypatch, *, accept: bool, environment: str):
    monkeypatch.setattr(apple_iap.settings, "APPLE_IAP_ACCEPT_XCODE", accept)
    monkeypatch.setattr(apple_iap.settings, "ENVIRONMENT", environment)
    monkeypatch.setattr(apple_iap.settings, "APPLE_IAP_BUNDLE_ID", "co.isol8.mobile")
    monkeypatch.setattr(apple_iap.settings, "APPLE_IAP_ROOT_CERTS_DIR", "certs/apple")
    apple_iap._verifier.cache_clear()


def _xcode_jws() -> str:
    """An unsigned-by-Apple JWS shaped like an Xcode StoreKit Testing transaction."""
    import jwt

    payload = {
        "originalTransactionId": "0",
        "transactionId": "0",
        "productId": "company.thelightbulb.mobile.base.monthly",
        "bundleId": "co.isol8.mobile",
        "environment": "Xcode",
        "expiresDate": 4_000_000_000_000,
        "signedDate": int(time.time() * 1000),
        "type": "Auto-Renewable Subscription",
        "inAppOwnershipType": "PURCHASED",
        "appAccountToken": apple_iap.owner_account_token("org_x"),
    }
    return jwt.encode(payload, "local-xcode-key", algorithm="HS256")


def test_an_xcode_transaction_verifies_in_dev_when_enabled(monkeypatch):
    _xcode_settings(monkeypatch, accept=True, environment="dev")
    sub = apple_iap.verify_transaction(_xcode_jws())
    assert sub.environment == "xcode" and sub.plan == "base"


def test_an_xcode_transaction_is_refused_when_disabled(monkeypatch):
    _xcode_settings(monkeypatch, accept=False, environment="dev")
    with pytest.raises(AppleIapError):
        apple_iap.verify_transaction(_xcode_jws())


def test_prod_refuses_xcode_even_if_misconfigured(monkeypatch):
    _xcode_settings(monkeypatch, accept=True, environment="prod")
    with pytest.raises(AppleIapError):
        apple_iap.verify_transaction(_xcode_jws())
```

Also delete `test_product_id_must_be_configured_before_the_verifier_is_live` (the setting it tests no longer exists) and update `test_foreign_product_id_is_rejected` only if its match string referenced `APPLE_IAP_PRODUCT_ID`.

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/backend && uv run python -m pytest tests/unit/services/test_apple_iap.py -q`
Expected: FAIL (`AttributeError: ... 'plan'`, `owner_account_token` missing, Xcode tests error).

- [ ] **Step 3: Implement**

`core/config.py` — replace the `APPLE_IAP_PRODUCT_ID` block with:

```python
    # Selling switch. When False (prod until launch) /billing/account lists no
    # products, purchase-intent 404s, and the app renders no paywall. The
    # notification webhook works either way (historical subscribers).
    APPLE_IAP_SELLING: bool = False
    # DEV ONLY: accept Xcode StoreKit Testing transactions, which are signed by a
    # local Xcode key, not Apple. apple_iap refuses them whenever ENVIRONMENT is
    # "prod" regardless of this flag, and CDK never sets it for prod.
    APPLE_IAP_ACCEPT_XCODE: bool = False
```

`core/services/apple_iap.py`:

1. Add `import hashlib` and `from dataclasses import dataclass, replace` at the top.
2. After the exception classes add:

```python
#: Every product we sell, and the billing plan it buys. A code constant, not
#: config: the ids are fixed in App Store Connect forever, and a blank or
#: partial config was the one way the old single-id setting could grant a plan
#: for a product we never sold. Ranked in one subscription group in ASC
#: (Ultra > Workspace > Base) so Apple handles upgrades and downgrades.
PRODUCT_PLANS: dict[str, str] = {
    "company.thelightbulb.mobile.base.monthly": "base",
    "company.thelightbulb.mobile.plan.monthly": "standard",
    "company.thelightbulb.mobile.ultra.monthly": "ultra_pro",
}


def owner_account_token(owner_id: str) -> str:
    """The ``appAccountToken`` a purchase for ``owner_id`` must carry.

    SHA-256 of the owner id shaped into a v5-style UUID (StoreKit accepts only a
    UUID there, and ``org_…`` ids are not one). Derived, never stored: the
    server recomputes and compares. The client never computes it — it gets it
    from POST /billing/apple/purchase-intent. Never change this computation: it
    would unbind every existing purchase.
    """
    h = hashlib.sha256(owner_id.encode()).hexdigest()[:32]
    h = f"{h[:12]}5{h[13:16]}8{h[17:]}"
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
```

3. In `AppleSubscription` add, after `signed_date_ms`:

```python
    #: Billing plan this product buys (PRODUCT_PLANS). Always from the VERIFIED productId.
    plan: str = "standard"
    #: ``appAccountToken`` stamped at purchase. Apple echoes it inside the signed
    #: payload, so it cannot be forged or stripped by a replaying account.
    owner_hint: str | None = None
```

4. In `_verifier`, change the configuration check to drop the product id:

```python
    if not settings.APPLE_IAP_BUNDLE_ID or not settings.APPLE_IAP_ROOT_CERTS_DIR:
        raise AppleIapNotConfigured(
            "APPLE_IAP_BUNDLE_ID / APPLE_IAP_ROOT_CERTS_DIR unset — Apple IAP is off in this environment"
        )
```

and replace its environment selection with:

```python
    resolved = environment_name or settings.APPLE_IAP_ENVIRONMENT
    environment = {
        "production": Environment.PRODUCTION,
        "xcode": Environment.XCODE,
    }.get(resolved, Environment.SANDBOX)
```

Delete the comment block above that check that explains why the product id is required (it no longer applies).

5. In `_verify_signed`, after the fallback attempt and before the final `raise`, add the Xcode attempt:

```python
    # Xcode StoreKit Testing: dev acceptance without Apple's sandbox. The library
    # skips the chain check for Environment.XCODE (these are signed by a local
    # Xcode key), so this path is a free plan for anyone who can reach it — hence
    # BOTH conditions, and prod refuses even a misconfigured flag.
    if settings.APPLE_IAP_ACCEPT_XCODE and settings.ENVIRONMENT != "prod":
        xcode = _fallback_verifier("xcode")
        if xcode is not None:
            try:
                return getattr(xcode, method)(blob), "xcode"
            except Exception:  # noqa: BLE001 — report the primary's error
                pass
```

6. In `_subscription_from_transaction`, replace the whole `expected = settings.APPLE_IAP_PRODUCT_ID … raise AppleIapError(f"unexpected productId …")` block with:

```python
    plan = PRODUCT_PLANS.get(product_id)
    if plan is None:
        # A transaction for a product we do not sell must never grant a plan.
        raise AppleIapError(f"unexpected productId {product_id!r}")
    ownership = getattr(transaction, "inAppOwnershipType", None)
    if getattr(ownership, "value", ownership) == "FAMILY_SHARED":
        # One workspace per purchase; Family Sharing is off in ASC and refused here too.
        raise AppleIapError("family-shared purchases cannot be linked to a workspace")
```

and pass the new fields into the returned `AppleSubscription(...)`:

```python
        plan=plan,
        owner_hint=getattr(transaction, "appAccountToken", None) or None,
```

7. Add, after `_subscription_from_transaction`:

```python
def verify_transaction(signed_transaction: str) -> AppleSubscription:
    """Verify a client-supplied StoreKit 2 transaction JWS and project it."""
    payload, environment = _verify_signed("verify_and_decode_signed_transaction", signed_transaction)
    return _subscription_from_transaction(payload, None, environment)
```

8. Update the module docstring paragraph that says the purchase-link half was deleted to: "`link_purchase` (below) is the purchase half: the app sends the StoreKit transaction right after a purchase or restore. `apply_notification` is the lifecycle half."

- [ ] **Step 4: Run to verify they pass**

Run: `cd apps/backend && uv run python -m pytest tests/unit/services/test_apple_iap.py tests/unit/services/test_apple_iap_certs.py -q`
Expected: PASS. Then `uv run ruff check core/services/apple_iap.py core/config.py` → clean. Grep `git grep -n APPLE_IAP_PRODUCT_ID -- apps/backend` → only hits you will remove in Task 4 (none in backend).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/apple_iap.py apps/backend/core/config.py apps/backend/tests/unit/services/test_apple_iap.py
git commit -m "feat(apple-iap): three-product plan map, account token, dev-only Xcode verifier

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Entering paid via Apple — plan written, no provider, same side effects as Stripe

**Files:**
- Modify: `apps/backend/core/services/stripe_webhook_handlers.py` (the `if entering_live:` block, ~line 457)
- Modify: `apps/backend/core/services/apple_iap.py` (`_reconcile_active_subscription`, `_apply_verified_notification`)
- Test: `apps/backend/tests/unit/services/test_apple_iap.py`, `apps/backend/tests/unit/services/test_stripe_webhook_handlers.py`

**Interfaces:**
- Consumes: `AppleSubscription.plan` (Task 1).
- Produces: `stripe_webhook_handlers.on_entering_live(owner_id: str) -> None` (async) — referral activation + company-Stripe request. `apple_iap._reconcile_active_subscription(owner_id, *, subscription, was_live: bool)` — new keyword `was_live`.

- [ ] **Step 1: Write the failing tests** (`test_apple_iap.py`)

```python
@pytest.mark.asyncio
async def test_an_active_apple_sub_writes_the_plan_and_never_a_provider(monkeypatch):
    row = {"owner_id": "org_x", "subscription_status": "active", "apple_subscription_status": "active"}
    monkeypatch.setattr(apple_iap.billing_repo, "get_by_owner_id", AsyncMock(return_value=row))
    set_plan = AsyncMock()
    set_provider = AsyncMock()
    monkeypatch.setattr(apple_iap.billing_repo, "set_plan", set_plan)
    monkeypatch.setattr(apple_iap.billing_repo, "set_provider_choice", set_provider)
    dispatch = AsyncMock(return_value=True)
    monkeypatch.setattr("core.services.provision_dispatch.dispatch_reconcile", dispatch)
    monkeypatch.setattr("core.services.owner_facts.write_owner_facts", AsyncMock())
    entering = AsyncMock()
    monkeypatch.setattr("core.services.stripe_webhook_handlers.on_entering_live", entering)

    sub = apple_iap._subscription_from_transaction(
        _transaction(productId="company.thelightbulb.mobile.ultra.monthly"), None
    )
    await apple_iap._reconcile_active_subscription("org_x", subscription=sub, was_live=False)

    set_plan.assert_awaited_once()
    assert set_plan.await_args.kwargs["plan"] == "ultra_pro"
    set_provider.assert_not_awaited()
    entering.assert_awaited_once_with("org_x")
    dispatch.assert_awaited_once_with("org_x")


@pytest.mark.asyncio
async def test_a_renewal_does_not_refire_the_entering_paid_effects(monkeypatch):
    row = {"owner_id": "org_x", "subscription_status": "active"}
    monkeypatch.setattr(apple_iap.billing_repo, "get_by_owner_id", AsyncMock(return_value=row))
    monkeypatch.setattr(apple_iap.billing_repo, "set_plan", AsyncMock())
    monkeypatch.setattr("core.services.provision_dispatch.dispatch_reconcile", AsyncMock(return_value=True))
    monkeypatch.setattr("core.services.owner_facts.write_owner_facts", AsyncMock())
    entering = AsyncMock()
    monkeypatch.setattr("core.services.stripe_webhook_handlers.on_entering_live", entering)

    sub = apple_iap._subscription_from_transaction(_transaction(), "DID_RENEW")
    await apple_iap._reconcile_active_subscription("org_x", subscription=sub, was_live=True)
    entering.assert_not_awaited()


def test_an_upgrade_notification_is_granting_not_informational():
    # DID_CHANGE_RENEWAL_PREF/UPGRADE carries the NEW product's transaction, effective now.
    assert apple_iap._effective_type("DID_CHANGE_RENEWAL_PREF", "UPGRADE") == "DID_RENEW"
    assert apple_iap._effective_type("DID_CHANGE_RENEWAL_PREF", "DOWNGRADE") == "DID_CHANGE_RENEWAL_PREF"
```

In `test_stripe_webhook_handlers.py` add one test that `handle_subscription_state` entering live calls `on_entering_live` once (patch `stripe_webhook_handlers.on_entering_live` with an `AsyncMock`, reuse that file's existing trial_local→active fixture/event builder — find it with `grep -n "trial_local" tests/unit/services/test_stripe_webhook_handlers.py`), and assert `launch_referral.on_subscription_activated` is no longer patched directly by any assertion you change.

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/backend && uv run python -m pytest tests/unit/services/test_apple_iap.py tests/unit/services/test_stripe_webhook_handlers.py -q`
Expected: FAIL (`was_live` unexpected kwarg, `_effective_type` missing, `on_entering_live` missing).

- [ ] **Step 3: Implement**

`stripe_webhook_handlers.py` — add a module-level function and use it:

```python
async def on_entering_live(owner_id: str) -> None:
    """Side effects of a workspace's FIRST move onto a paid subscription, from either
    provider (Stripe here, Apple in apple_iap). Both calls are internally best-effort/deduped."""
    from core.services import launch_referral

    await launch_referral.on_subscription_activated(owner_id)
    dispatch_company_stripe_request(owner_id)
```

Replace the body of `if entering_live:` (the `from core.services import launch_referral`, `await launch_referral.on_subscription_activated(...)`, and `dispatch_company_stripe_request(...)` lines plus the NOTE comment about Apple) with `await on_entering_live(account["owner_id"])`, keeping the Metronome comment above it.

`apple_iap.py`:

1. Add near the type sets:

```python
def _effective_type(notification_type: str | None, subtype: str | None) -> str | None:
    """An UPGRADE takes effect immediately and carries the new product's transaction,
    so it grants like a renewal. Downgrades and crossgrades wait for the next DID_RENEW."""
    if notification_type == "DID_CHANGE_RENEWAL_PREF" and subtype == "UPGRADE":
        return "DID_RENEW"
    return notification_type
```

2. At the top of `_apply_verified_notification`, before the `_INFORMATIONAL_TYPES` check:

```python
    subtype = getattr(notification, "subtype", None)
    notification_type = _effective_type(notification_type, getattr(subtype, "value", subtype))
```

3. In `_apply_verified_notification`, capture liveness before the write: immediately before `written = await billing_repo.set_apple_subscription(`, add

```python
    from core.services.provision_gate import LIVE_SUBSCRIPTION_STATUSES

    was_live = row.get("subscription_status") in LIVE_SUBSCRIPTION_STATUSES
```

and change the call to `await _reconcile_active_subscription(owner_id, subscription=subscription, was_live=was_live)`.

4. Replace `_reconcile_active_subscription` entirely:

```python
async def _reconcile_active_subscription(owner_id: str, *, subscription: AppleSubscription, was_live: bool) -> None:
    """An active Apple subscription: make the workspace match it, on EVERY such event.

    Mirrors what a Stripe checkout does (stripe_webhook_handlers.handle_subscription_state):
    first-entry side effects, the plan, the OpenRouter limit, owner facts, then a reconcile.

    ``provider_choice`` is deliberately NOT written. An unset provider on a paid plan is
    the capped managed default (trial_cap.is_managed_capped) — what a Stripe buyer gets.
    Writing ``openrouter_managed`` sent an Apple-only owner (no Stripe customer, so no
    Metronome contract) to ``credits_required`` the moment they paid.

    Raises when the dispatch fails, so the caller (link → 5xx, notification → Apple
    redelivers) runs it again; every step is idempotent.
    """
    if subscription.status != "active":
        return
    row = await billing_repo.get_by_owner_id(owner_id, consistent_read=True)
    if not row:
        return

    from core.dynamodb import utc_now_iso
    from core.services import stripe_webhook_handlers
    from core.services.owner_facts import write_owner_facts
    from core.services.provision_dispatch import dispatch_reconcile

    if not was_live:
        await stripe_webhook_handlers.on_entering_live(owner_id)
    await billing_repo.set_plan(owner_id, plan=subscription.plan, now_iso=utc_now_iso())
    if settings.OPENROUTER_WORKSPACE_ID:
        try:
            from core.services.openrouter_keys import relimit_owner_key

            await relimit_owner_key(owner_id)
        except Exception:
            logger.exception("apple_iap.relimit_failed owner_id=%s", owner_id)
    await write_owner_facts(owner_id, account_overrides={"subscription_status": "active", "plan": subscription.plan})
    if not await dispatch_reconcile(owner_id):
        raise RuntimeError(f"reconcile dispatch failed for owner {owner_id}")
```

5. Remove `set_provider_choice` from the `_no_trial_conversion` fixture only if a test now fails because of it; otherwise leave the fixture (it is harmless) and add `set_plan` to it:

```python
    monkeypatch.setattr(apple_iap.billing_repo, "set_plan", AsyncMock())
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd apps/backend && uv run python -m pytest tests/unit/services/test_apple_iap.py tests/unit/services/test_stripe_webhook_handlers.py -q`
Expected: PASS. `uv run ruff check core/services/` clean.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/apple_iap.py apps/backend/core/services/stripe_webhook_handlers.py apps/backend/tests/unit/services/test_apple_iap.py apps/backend/tests/unit/services/test_stripe_webhook_handlers.py
git commit -m "fix(apple-iap): an Apple sub writes its plan, never a provider, and fires Stripe's entering-paid effects

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `link_purchase`, purchase-intent + link routes, account field, checkout refusal

**Files:**
- Modify: `apps/backend/core/services/apple_iap.py` (add `link_purchase`)
- Modify: `apps/backend/routers/apple_iap.py` (replace the 410 tombstone; add intent route)
- Modify: `apps/backend/schemas/billing.py` (`BillingAccountResponse`)
- Modify: `apps/backend/routers/billing.py:120-150` (`/billing/account`) and `routers/billing.py` checkout route (~line 286)
- Test: `apps/backend/tests/unit/services/test_apple_iap.py`, `apps/backend/tests/unit/routers/test_apple_iap_router.py`, `apps/backend/tests/unit/routers/test_billing_workspace_checkout.py`

**Interfaces:**
- Consumes: `verify_transaction`, `owner_account_token`, `PRODUCT_PLANS`, `_reconcile_active_subscription(..., was_live=)` (Tasks 1–2).
- Produces (HTTP, used by mobile Tasks 5–7):
  - `POST /api/v1/billing/apple/purchase-intent` → `200 {"app_account_token": str, "product_ids": [str, str, str]}`; `403` non-admin; `404` selling off; `409 {"detail": "stripe_managed"}` Stripe live.
  - `POST /api/v1/billing/apple/link` body `{"signed_transaction": str}` → `200 {"status": str, "product_id": str, "plan": str, "expires_at": int|null}`; `400` rejected; `403` non-admin; `409 stripe_managed`; `503` not configured.
  - `GET /api/v1/billing/account` gains `apple_iap_product_ids: list[str]` (empty unless selling).
  - `POST /api/v1/billing/checkout` → `409 {"detail": "apple_managed"}` when Apple is live.
- `apple_iap.link_purchase(*, owner_id: str, signed_transaction: str) -> AppleSubscription`
- `apple_iap.stripe_is_live(row: dict | None) -> bool`, `apple_iap.apple_is_live(row: dict | None) -> bool`

- [ ] **Step 1: Write the failing service tests** (`test_apple_iap.py`)

```python
def _linkable(monkeypatch, *, row=None, existing=None, token_owner="org_x", **tx):
    tx.setdefault("appAccountToken", apple_iap.owner_account_token(token_owner))
    monkeypatch.setattr(apple_iap, "_verify_signed", lambda method, blob: (_transaction(**tx), "sandbox"))
    monkeypatch.setattr(apple_iap.billing_repo, "get_by_apple_original_transaction_id", AsyncMock(return_value=existing))
    monkeypatch.setattr(
        apple_iap.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value=row if row is not None else {"owner_id": "org_x", "subscription_status": "trial_local"}),
    )
    write = AsyncMock(return_value={"owner_id": "org_x"})
    monkeypatch.setattr(apple_iap.billing_repo, "set_apple_subscription", write)
    monkeypatch.setattr(apple_iap, "_reconcile_active_subscription", AsyncMock())
    return write


@pytest.mark.asyncio
async def test_link_binds_a_purchase_to_its_workspace(monkeypatch):
    write = _linkable(monkeypatch)
    sub = await apple_iap.link_purchase(owner_id="org_x", signed_transaction="jws")
    assert sub.status == "active" and sub.plan == "standard"
    write.assert_awaited_once()
    apple_iap._reconcile_active_subscription.assert_awaited_once()


@pytest.mark.asyncio
async def test_link_refuses_a_purchase_made_for_another_workspace(monkeypatch):
    _linkable(monkeypatch, token_owner="org_other")
    with pytest.raises(AppleIapError, match="different"):
        await apple_iap.link_purchase(owner_id="org_x", signed_transaction="jws")


@pytest.mark.asyncio
async def test_link_refuses_a_transaction_without_an_account_token(monkeypatch):
    _linkable(monkeypatch, appAccountToken=None)
    with pytest.raises(AppleIapError, match="binding"):
        await apple_iap.link_purchase(owner_id="org_x", signed_transaction="jws")


@pytest.mark.asyncio
async def test_link_refuses_a_subscription_already_linked_elsewhere(monkeypatch):
    _linkable(monkeypatch, existing={"owner_id": "org_other"})
    with pytest.raises(AppleIapError, match="another"):
        await apple_iap.link_purchase(owner_id="org_x", signed_transaction="jws")


@pytest.mark.asyncio
async def test_link_refuses_when_stripe_already_bills_the_workspace(monkeypatch):
    _linkable(monkeypatch, row={"owner_id": "org_x", "stripe_subscription_status": "active"})
    with pytest.raises(apple_iap.StripeManaged):
        await apple_iap.link_purchase(owner_id="org_x", signed_transaction="jws")


@pytest.mark.asyncio
async def test_link_refuses_a_revoked_transaction(monkeypatch):
    _linkable(monkeypatch)
    monkeypatch.setattr(apple_iap.webhook_dedup, "has_event", AsyncMock(return_value=True))
    with pytest.raises(AppleIapError, match="refunded"):
        await apple_iap.link_purchase(owner_id="org_x", signed_transaction="jws")


@pytest.mark.asyncio
async def test_a_superseded_link_reports_apples_status_not_the_blobs(monkeypatch):
    write = _linkable(monkeypatch)
    write.return_value = None
    apple_iap.billing_repo.get_by_owner_id.side_effect = [
        {"owner_id": "org_x", "subscription_status": "active"},
        {"owner_id": "org_x", "apple_original_transaction_id": "2000000000000001", "apple_subscription_status": "canceled"},
    ]
    sub = await apple_iap.link_purchase(owner_id="org_x", signed_transaction="jws")
    assert sub.status == "canceled"
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/backend && uv run python -m pytest tests/unit/services/test_apple_iap.py -q -k link`
Expected: FAIL (`link_purchase` / `StripeManaged` missing).

- [ ] **Step 3: Implement the service**

In `apple_iap.py` add after `AppleIapNotConfigured`:

```python
class StripeManaged(AppleIapError):
    """The workspace already pays through Stripe; a second subscription is refused."""


def stripe_is_live(row: dict | None) -> bool:
    from core.services.provision_gate import LIVE_SUBSCRIPTION_STATUSES

    return bool(row) and row.get("stripe_subscription_status") in LIVE_SUBSCRIPTION_STATUSES


def apple_is_live(row: dict | None) -> bool:
    from core.services.provision_gate import LIVE_SUBSCRIPTION_STATUSES

    return bool(row) and row.get("apple_subscription_status") in LIVE_SUBSCRIPTION_STATUSES
```

Add `link_purchase` after `verify_transaction`. It is `24edcd0b6^:apps/backend/core/services/apple_iap.py` lines 596–721 (`git show 24edcd0b6^:apps/backend/core/services/apple_iap.py | sed -n 596,721p`) with exactly these changes:
- Delete the `is_org_owner_id` fence and its comment block (orgs are now the only buyers; admin-ness is enforced in the router).
- Replace `before = await billing_repo.get_by_owner_id(owner_id)` with:

```python
    before = await billing_repo.get_by_owner_id(owner_id, consistent_read=True)
    if stripe_is_live(before):
        raise StripeManaged("stripe_managed")
    from core.services.provision_gate import LIVE_SUBSCRIPTION_STATUSES

    was_live = (before or {}).get("subscription_status") in LIVE_SUBSCRIPTION_STATUSES
```

  These lines stay where `before` was: after the revoked-tombstone check, before `set_apple_subscription`.
- Replace the `await _convert_trial_if_needed(...)` call with `await _reconcile_active_subscription(owner_id, subscription=subscription, was_live=was_live)`.
- Keep the `replace(...)` superseded branch, the token checks, the GSI cross-account check, the tombstone check and the `apple_iap.linked` log line verbatim.
- Change the error text "this Apple transaction was purchased by a different account" to "this Apple purchase belongs to a different workspace"; the tests match on "different".

- [ ] **Step 4: Run the service tests** — `uv run python -m pytest tests/unit/services/test_apple_iap.py -q` → PASS.

- [ ] **Step 5: Write the failing router tests** (`tests/unit/routers/test_apple_iap_router.py`)

Delete `test_retired_link_route_is_always_gone_before_verification` (and its parametrization). Keep `test_retired_link_route_still_requires_authentication` but rename it `test_link_requires_authentication`. Read the file's `client` fixture and the portal tests (`test_the_mobile_portal_refuses_a_non_admin_org_member`) and build auth the same way they do. Add:

```python
@pytest.mark.asyncio
async def test_intent_is_hidden_while_selling_is_off(admin_client, monkeypatch):
    monkeypatch.setattr(apple_router.settings, "APPLE_IAP_SELLING", False)
    r = await admin_client.post("/api/v1/billing/apple/purchase-intent")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_intent_returns_the_workspace_token_and_products(admin_client, monkeypatch):
    monkeypatch.setattr(apple_router.settings, "APPLE_IAP_SELLING", True)
    monkeypatch.setattr(apple_router.billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": ORG}))
    r = await admin_client.post("/api/v1/billing/apple/purchase-intent")
    assert r.status_code == 200
    body = r.json()
    assert body["app_account_token"] == apple_iap.owner_account_token(ORG)
    assert sorted(body["product_ids"]) == sorted(apple_iap.PRODUCT_PLANS)


@pytest.mark.asyncio
async def test_intent_refuses_a_stripe_billed_workspace(admin_client, monkeypatch):
    monkeypatch.setattr(apple_router.settings, "APPLE_IAP_SELLING", True)
    monkeypatch.setattr(
        apple_router.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": ORG, "stripe_subscription_status": "active"}),
    )
    r = await admin_client.post("/api/v1/billing/apple/purchase-intent")
    assert r.status_code == 409 and r.json()["detail"] == "stripe_managed"


@pytest.mark.asyncio
async def test_intent_and_link_refuse_a_member(member_client, monkeypatch):
    monkeypatch.setattr(apple_router.settings, "APPLE_IAP_SELLING", True)
    assert (await member_client.post("/api/v1/billing/apple/purchase-intent")).status_code == 403
    assert (await member_client.post("/api/v1/billing/apple/link", json={"signed_transaction": "x"})).status_code == 403


@pytest.mark.asyncio
async def test_link_returns_the_plan(admin_client, monkeypatch):
    sub = apple_iap.AppleSubscription(
        original_transaction_id="1", product_id="company.thelightbulb.mobile.base.monthly",
        expires_at_ms=None, status="active", plan="base",
    )
    monkeypatch.setattr(apple_router, "link_purchase", AsyncMock(return_value=sub))
    r = await admin_client.post("/api/v1/billing/apple/link", json={"signed_transaction": "jws"})
    assert r.status_code == 200 and r.json()["plan"] == "base"


@pytest.mark.asyncio
async def test_link_maps_stripe_managed_to_409(admin_client, monkeypatch):
    monkeypatch.setattr(apple_router, "link_purchase", AsyncMock(side_effect=apple_iap.StripeManaged("stripe_managed")))
    r = await admin_client.post("/api/v1/billing/apple/link", json={"signed_transaction": "jws"})
    assert r.status_code == 409
```

(`ORG`, `admin_client`, `member_client`, and `apple_router` — `import routers.apple_iap as apple_router` — are fixtures/constants you add to this file following its existing auth-override pattern.)

In `tests/unit/routers/test_billing_workspace_checkout.py` add a test that `POST /billing/checkout` returns `409 {"detail": "apple_managed"}` when the billing row has `apple_subscription_status: "active"` — copy that file's existing happy-path test setup and change only the row.

For `/billing/account`, add to an existing account test file (`tests/unit/routers/test_billing.py`): with `APPLE_IAP_SELLING` False the response has `apple_iap_product_ids == []`; with True it lists the three ids.

- [ ] **Step 6: Run to verify they fail** — `uv run python -m pytest tests/unit/routers/test_apple_iap_router.py tests/unit/routers/test_billing_workspace_checkout.py tests/unit/routers/test_billing.py -q` → FAIL.

- [ ] **Step 7: Implement the routes**

`routers/apple_iap.py` — update the module docstring's first bullet to describe the two live routes, import `from core.config import settings` and `from core.services.apple_iap import AppleIapError, AppleIapNotConfigured, StripeManaged, link_purchase, owner_account_token, PRODUCT_PLANS, stripe_is_live`, and replace the tombstone with:

```python
@router.post(
    "/billing/apple/purchase-intent",
    summary="Start an in-app purchase for this workspace",
    description="Admin only. Returns the appAccountToken the StoreKit purchase must carry.",
)
async def apple_purchase_intent(auth: AuthContext = Depends(get_current_user)):
    # Runs BEFORE StoreKit charges: every refusal here costs the customer nothing.
    require_org_admin(auth)
    if not settings.APPLE_IAP_SELLING:
        raise HTTPException(status_code=404, detail="In-app purchase is not available")
    owner_id = resolve_owner_id(auth)
    if stripe_is_live(await billing_repo.get_by_owner_id(owner_id)):
        raise HTTPException(status_code=409, detail="stripe_managed")
    return {"app_account_token": owner_account_token(owner_id), "product_ids": list(PRODUCT_PLANS)}


@router.post(
    "/billing/apple/link",
    summary="Link a StoreKit purchase to this workspace",
    description="Admin only. Verifies the signed transaction and records the subscription. Idempotent.",
)
async def link_apple_purchase(payload: dict, auth: AuthContext = Depends(get_current_user)):
    # Deliberately NOT gated on APPLE_IAP_SELLING: a purchase Apple already charged
    # must always be linkable (restore, relaunch), even after selling is switched off.
    require_org_admin(auth)
    owner_id = resolve_owner_id(auth)
    signed_transaction = (payload or {}).get("signed_transaction")
    if not isinstance(signed_transaction, str) or not signed_transaction:
        raise HTTPException(status_code=400, detail="signed_transaction is required")
    try:
        subscription = await link_purchase(owner_id=owner_id, signed_transaction=signed_transaction)
    except AppleIapNotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except StripeManaged as e:
        raise HTTPException(status_code=409, detail="stripe_managed") from e
    except AppleIapError as e:
        logger.warning("apple_iap.link_rejected owner_id=%s: %s", owner_id, e)
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {
        "status": subscription.status,
        "product_id": subscription.product_id,
        "plan": subscription.plan,
        "expires_at": subscription.expires_at_ms,
    }
```

`schemas/billing.py` — add to `BillingAccountResponse` after `subscription_source`:

```python
    # App Store product ids the iOS app may offer. Empty unless APPLE_IAP_SELLING:
    # the app renders no paywall when this is empty, which keeps IAP dark.
    apple_iap_product_ids: list[str] = []
```

`routers/billing.py` `/billing/account` return — add
`apple_iap_product_ids=list(apple_iap.PRODUCT_PLANS) if settings.APPLE_IAP_SELLING else [],`
(import `from core.services import apple_iap`).

`routers/billing.py` checkout route (`create_workspace_checkout`, ~line 286) — after `owner_id` is resolved and before `create_checkout(...)`:

```python
    from core.services.apple_iap import apple_is_live

    if apple_is_live(await billing_repo.get_by_owner_id(owner_id)):
        # Billed through the App Store; a web checkout would start a second subscription.
        raise HTTPException(status_code=409, detail="apple_managed")
```

(If `billing_repo` is not imported in that router, import it.)

- [ ] **Step 8: Run to verify they pass**

Run: `cd apps/backend && uv run python -m pytest tests/unit/services/test_apple_iap.py tests/unit/routers/test_apple_iap_router.py tests/unit/routers/test_billing_workspace_checkout.py tests/unit/routers/test_billing.py -q`
Expected: PASS. `uv run ruff check routers schemas core` clean.

- [ ] **Step 9: Commit**

```bash
git add apps/backend/core/services/apple_iap.py apps/backend/routers/apple_iap.py apps/backend/routers/billing.py apps/backend/schemas/billing.py apps/backend/tests/unit
git commit -m "feat(apple-iap): admin purchase-intent + link routes; web checkout refuses an Apple-billed workspace

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: CDK env — selling on in dev, Xcode in dev only, product-id env removed

**Files:**
- Modify: `apps/infra/lib/stacks/service-stack.ts:1488-1512`
- Test: `apps/infra/test/service-stack.test.ts`
- Regenerate: `.github/workflows/deploy.yml`

**Interfaces:**
- Produces: backend env `APPLE_IAP_SELLING` (`"true"` dev / `"false"` prod) and `APPLE_IAP_ACCEPT_XCODE` (`"true"` dev / `"false"` prod); `APPLE_IAP_PRODUCT_ID` no longer set.

- [ ] **Step 1: Write the failing test** — in `service-stack.test.ts`, following the existing `Environment: Match.arrayWith([...])` assertions (see ~line 109):

```ts
describe("Apple IAP env", () => {
  it.each([
    ["dev", "true", "true"],
    ["prod", "false", "false"],
  ] as const)("%s: selling=%s, xcode=%s", (env, selling, xcode) => {
    const template = buildServiceStack(env);
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            { Name: "APPLE_IAP_SELLING", Value: selling },
            { Name: "APPLE_IAP_ACCEPT_XCODE", Value: xcode },
          ]),
        }),
      ]),
    });
  });

  it("prod never accepts Xcode transactions", () => {
    const json = JSON.stringify(buildServiceStack("prod").toJSON());
    expect(json).not.toMatch(/"APPLE_IAP_ACCEPT_XCODE","Value":"true"/);
  });
});
```

- [ ] **Step 2: Run to verify it fails** — `cd apps/infra && npx jest test/service-stack.test.ts -t "Apple IAP"` → FAIL.

- [ ] **Step 3: Implement** — in `service-stack.ts` replace the comment block starting "Historical Apple subscription lifecycle only" and the `APPLE_IAP_PRODUCT_ID` line with:

```ts
      // Apple IAP (StoreKit 2). Product ids live in apple_iap.PRODUCT_PLANS.
      // SELLING is the one switch that makes the iOS app offer purchases; prod
      // stays off until the Paid Apps Agreement is active. ACCEPT_XCODE lets
      // dev accept Xcode StoreKit Testing purchases (signed locally, not by
      // Apple) — it is a free plan for anyone who can reach it, so it is
      // hard-wired off for prod here AND refused by the backend in prod.
      APPLE_IAP_SELLING: env === "prod" ? "false" : "true",
      APPLE_IAP_ACCEPT_XCODE: env === "prod" ? "false" : "true",
```

Keep `APPLE_IAP_BUNDLE_ID`, `APPLE_IAP_APP_APPLE_ID`, `APPLE_IAP_ENVIRONMENT`, `APPLE_IAP_ROOT_CERTS_DIR` and their comments.

- [ ] **Step 4: Run tests + synth**

Run: `cd apps/infra && npx jest test/service-stack.test.ts && npx cdk synth > /dev/null`
Expected: PASS, synth exits 0 and rewrites `.github/workflows/deploy.yml` only if it changed. `git status` shows the diff.

- [ ] **Step 5: Commit**

```bash
git add apps/infra/lib/stacks/service-stack.ts apps/infra/test/service-stack.test.ts .github/workflows/deploy.yml
git commit -m "feat(infra): Apple IAP selling on in dev, Xcode test purchases dev-only

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Mobile IAP client module

**Files:**
- Create: `apps/mobile/src/lib/appleIap.ts`
- Test: `apps/mobile/src/lib/__tests__/appleIap.test.ts`

**Interfaces:**
- Consumes: HTTP routes from Task 3.
- Produces:

```ts
export type ApiPost = (path: string, body: unknown) => Promise<unknown>;
export type PurchaseIntent = { appAccountToken: string; productIds: string[] };
export function paywallProductIds(account: { apple_iap_product_ids?: string[] | null } | null | undefined): string[];
export async function startPurchaseIntent(post: ApiPost): Promise<PurchaseIntent>; // throws PurchaseRefused
export class PurchaseRefused extends Error { reason: "stripe_managed" | "not_admin" | "unavailable" }
export async function linkApplePurchase(post: ApiPost, signedTransaction: string): Promise<{ plan: string; status: string }>;
export const PLAN_ORDER: Record<string, number>; // product id → display order (base 0, plan 1, ultra 2)
```

- [ ] **Step 1: Write the failing test**

```ts
import {
  linkApplePurchase,
  paywallProductIds,
  PurchaseRefused,
  startPurchaseIntent,
} from "../appleIap";

describe("appleIap", () => {
  it("offers nothing when the server lists no products", () => {
    expect(paywallProductIds(null)).toEqual([]);
    expect(paywallProductIds({ apple_iap_product_ids: [] })).toEqual([]);
  });

  it("orders products Base, Workspace, Ultra", () => {
    expect(
      paywallProductIds({
        apple_iap_product_ids: [
          "company.thelightbulb.mobile.ultra.monthly",
          "company.thelightbulb.mobile.base.monthly",
          "company.thelightbulb.mobile.plan.monthly",
        ],
      }),
    ).toEqual([
      "company.thelightbulb.mobile.base.monthly",
      "company.thelightbulb.mobile.plan.monthly",
      "company.thelightbulb.mobile.ultra.monthly",
    ]);
  });

  it("maps a 409 stripe_managed intent to a refusal", async () => {
    const err = Object.assign(new Error("API 409"), { status: 409, body: { detail: "stripe_managed" } });
    await expect(startPurchaseIntent(jest.fn().mockRejectedValue(err))).rejects.toMatchObject({
      reason: "stripe_managed",
    });
    await expect(startPurchaseIntent(jest.fn().mockRejectedValue(err))).rejects.toBeInstanceOf(PurchaseRefused);
  });

  it("returns the server token", async () => {
    const post = jest.fn().mockResolvedValue({ app_account_token: "t", product_ids: ["a"] });
    await expect(startPurchaseIntent(post)).resolves.toEqual({ appAccountToken: "t", productIds: ["a"] });
    expect(post).toHaveBeenCalledWith("/billing/apple/purchase-intent", {});
  });

  it("links a signed transaction", async () => {
    const post = jest.fn().mockResolvedValue({ plan: "base", status: "active" });
    await expect(linkApplePurchase(post, "jws")).resolves.toEqual({ plan: "base", status: "active" });
    expect(post).toHaveBeenCalledWith("/billing/apple/link", { signed_transaction: "jws" });
  });
});
```

Before writing `startPurchaseIntent`, check how `@lightbulb/shared`'s `createApiClient` surfaces HTTP errors: `grep -n "status" packages/shared/src/api*.ts` (or wherever `createApiClient` lives — `git grep -n "export function createApiClient" packages/shared`). Match the test's error shape (`status`, `body.detail`) to what it really throws, and adjust the test if the field names differ.

- [ ] **Step 2: Run to verify it fails** — `cd apps/mobile && pnpm jest src/lib/__tests__/appleIap.test.ts` → FAIL (module missing).

- [ ] **Step 3: Implement** `src/lib/appleIap.ts`:

```ts
/**
 * Apple In-App Purchase — the two server calls and the product list.
 *
 * Prices are never here: the paywall renders StoreKit's `displayPrice` for the
 * viewer's storefront. Which products exist is decided by the SERVER
 * (/billing/account.apple_iap_product_ids, empty while selling is off), so IAP
 * stays dark in any build until the backend switches it on.
 *
 * The client never decides a purchase is valid: it forwards StoreKit's signed
 * transaction to /billing/apple/link, which verifies it against Apple's root CA.
 * The appAccountToken comes from the server too (purchase-intent), so the
 * binding between a purchase and a workspace has one implementation.
 */
export type ApiPost = (path: string, body: unknown) => Promise<unknown>;
export type PurchaseIntent = { appAccountToken: string; productIds: string[] };

export const PLAN_ORDER: Record<string, number> = {
  "company.thelightbulb.mobile.base.monthly": 0,
  "company.thelightbulb.mobile.plan.monthly": 1,
  "company.thelightbulb.mobile.ultra.monthly": 2,
};

export function paywallProductIds(
  account: { apple_iap_product_ids?: string[] | null } | null | undefined,
): string[] {
  const ids = account?.apple_iap_product_ids ?? [];
  return [...ids].sort((a, b) => (PLAN_ORDER[a] ?? 99) - (PLAN_ORDER[b] ?? 99));
}

export class PurchaseRefused extends Error {
  constructor(public reason: "stripe_managed" | "not_admin" | "unavailable") {
    super(reason);
  }
}

export async function startPurchaseIntent(post: ApiPost): Promise<PurchaseIntent> {
  try {
    const body = (await post("/billing/apple/purchase-intent", {})) as {
      app_account_token: string;
      product_ids: string[];
    };
    return { appAccountToken: body.app_account_token, productIds: body.product_ids };
  } catch (e) {
    const status = (e as { status?: number }).status;
    const detail = (e as { body?: { detail?: string } }).body?.detail;
    if (status === 409 && detail === "stripe_managed") throw new PurchaseRefused("stripe_managed");
    if (status === 403) throw new PurchaseRefused("not_admin");
    if (status === 404) throw new PurchaseRefused("unavailable");
    throw e;
  }
}

export async function linkApplePurchase(
  post: ApiPost,
  signedTransaction: string,
): Promise<{ plan: string; status: string }> {
  return (await post("/billing/apple/link", { signed_transaction: signedTransaction })) as {
    plan: string;
    status: string;
  };
}
```

- [ ] **Step 4: Run** — `pnpm jest src/lib/__tests__/appleIap.test.ts && pnpm typecheck && pnpm lint` → PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/mobile/src/lib/appleIap.ts apps/mobile/src/lib/__tests__/appleIap.test.ts
git commit -m "feat(mobile): Apple IAP client (server-driven products, intent, link)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: PaywallScreen and the `/plans` route

**Files:**
- Create: `apps/mobile/src/components/billing/PaywallScreen.tsx`
- Create: `apps/mobile/src/app/(app)/plans.tsx`
- Test: `apps/mobile/src/components/billing/__tests__/PaywallScreen.test.tsx`

**Interfaces:**
- Consumes: Task 5 module; `expo-iap` (`useIAP`, `getAvailablePurchases`, `ErrorCode`) — the same API the deleted paywall used: `git show c9fa53261^:apps/mobile/src/components/billing/PaywallScreen.tsx` (read lines 95–300 for the working `useIAP` wiring, the `finishRef` pattern and the restore-via-module-`getAvailablePurchases` fix; reuse them).
- Produces: `<PaywallScreen api={{ get, post }} productIds={string[]} currentProductId={string | null} onPurchased={() => void} />` — renders `null` when `productIds` is empty or StoreKit returns none of them.

Behaviour (write it exactly this way):
- `useIAP({ onPurchaseSuccess, onPurchaseError })`; when `connected`, `fetchProducts({ skus: productIds, type: "subs" })`.
- Rows sorted by `PLAN_ORDER`; each shows the product's `title` (fallback: Base / Workspace / Ultra by id) and `displayPrice` + "/month". The current product shows "Current plan" and no button.
- **Buy**: guarded by `busy` (a second tap while busy is a no-op). `startPurchaseIntent(api.post)` → `requestPurchase({ type: "subs", request: { apple: { sku, appAccountToken } } })`. A `PurchaseRefused("stripe_managed")` shows "This workspace is billed outside the App Store." and makes no StoreKit call.
- **onPurchaseSuccess**: no `purchaseToken` → error "That purchase couldn't be confirmed. Contact support@thelightbulb.company." Otherwise `linkApplePurchase` → then `finishTransaction({ purchase, isConsumable: false })` → `onPurchased()`. If link throws: do NOT finish; show "Your purchase went through, but we couldn't activate it yet. It will finish the next time you open the app."
- **onPurchaseError**: `ErrorCode.UserCancelled` is silent; anything else shows "The purchase didn't complete. Nothing was charged."
- **Restore** button: `restorePurchases()`, then module-level `getAvailablePurchases()`; link + finish each purchase whose `productId`/`id` is in `productIds` and has a `purchaseToken`; ≥1 linked → `onPurchased()`; none found → "No previous purchase found for this Apple Account."
- Copy: header "Choose a plan", footer "Billed monthly to your Apple Account. Cancel anytime in iOS Settings." Nothing about the web, flickmeon.ai or other prices.
- Styling: use `colors`, `fonts`, `spacing`, `radii` from `src/config/theme` like other screens; a plain `ScrollView` of rows is fine.

`src/app/(app)/plans.tsx`: a screen (`Stack.Screen options={{ title: "Plans" }}`) that loads `/billing/account` with the same `createApiClient(makeAdapter(...))` pattern `settings.tsx` uses (lines ~95–105), and renders `<PaywallScreen productIds={paywallProductIds(account)} currentProductId={…} api={api} onPurchased={() => router.back()} />`. `currentProductId`: when `account.subscription_source === "apple_iap"` and `account.plan` is set, map plan→product id with the inverse of the Task 1 table; otherwise null.

- [ ] **Step 1: Write the failing tests** — mock `expo-iap` at the top of the test file:

```tsx
const mockRequestPurchase = jest.fn();
const mockFinish = jest.fn();
const mockFetchProducts = jest.fn();
let mockHandlers: { onPurchaseSuccess?: (p: unknown) => void; onPurchaseError?: (e: unknown) => void } = {};
let mockSubscriptions: Array<{ id: string; title: string; displayPrice: string }> = [];

jest.mock("expo-iap", () => ({
  ErrorCode: { UserCancelled: "user-cancelled" },
  getAvailablePurchases: jest.fn().mockResolvedValue([]),
  useIAP: (handlers: typeof mockHandlers) => {
    mockHandlers = handlers;
    return {
      connected: true,
      subscriptions: mockSubscriptions,
      fetchProducts: mockFetchProducts,
      requestPurchase: mockRequestPurchase,
      finishTransaction: mockFinish,
      restorePurchases: jest.fn(),
    };
  },
}));
```

Tests (use `@testing-library/react-native` like `src/components/**/__tests__` do; `render`, `fireEvent`, `waitFor`, `screen`):

1. `renders nothing when the server offers no products` — `productIds={[]}` → `toJSON()` is null.
2. `renders nothing when StoreKit returns none of the products` — ids given, `mockSubscriptions = []` → null.
3. `lists StoreKit prices in plan order and never mentions the web` — three products → texts "$23.99", "$59.99", "$179.99" in order; `queryByText(/flickmeon|web/i)` is null.
4. `buys with the server's account token` — api.post resolves intent `{app_account_token: "tok", product_ids: [...]}`; press Base "Buy" → `mockRequestPurchase` called with `{ type: "subs", request: { apple: { sku: BASE, appAccountToken: "tok" } } }`.
5. `a second tap while busy starts one purchase` — press Buy twice quickly (intent promise unresolved) → `mockRequestPurchase` called once.
6. `finishes only after the link succeeds` — api.post: intent ok, link resolves; call `mockHandlers.onPurchaseSuccess({ purchaseToken: "jws" })` → link called with `{ signed_transaction: "jws" }`, then `mockFinish` called, then `onPurchased` called.
7. `a failed link leaves the transaction unfinished` — link rejects → `mockFinish` not called, "It will finish the next time you open the app" visible, `onPurchased` not called.
8. `a stripe-billed workspace never reaches StoreKit` — intent rejects with `{status: 409, body: {detail: "stripe_managed"}}` → `mockRequestPurchase` not called, "billed outside the App Store" visible.
9. `cancelling the Apple sheet shows no error` — `mockHandlers.onPurchaseError({ code: "user-cancelled" })` → no error text.
10. `the current plan has no buy button` — `currentProductId={BASE}` → "Current plan" visible, only two "Buy" buttons.

- [ ] **Step 2: Run to verify they fail** — `pnpm jest src/components/billing` → FAIL.
- [ ] **Step 3: Implement** `PaywallScreen.tsx` and `plans.tsx` per the behaviour list above.
- [ ] **Step 4: Run** — `pnpm jest src/components/billing && pnpm typecheck && pnpm lint` → PASS.
- [ ] **Step 5: Commit**

```bash
git add apps/mobile/src/components/billing apps/mobile/src/app/'(app)'/plans.tsx
git commit -m "feat(mobile): StoreKit paywall + Plans screen (dark until the server lists products)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Wire it in — Settings → Plans, the subscription gate, the launch listener

**Files:**
- Modify: `apps/mobile/src/app/(app)/settings.tsx` (billing sections, ~line 480–545; `BillingAccount` type ~line 50–80)
- Modify: `apps/mobile/src/components/provision/ProvisionGate.tsx` (~line 326–375)
- Create: `apps/mobile/src/components/billing/PurchaseListener.tsx`
- Modify: `apps/mobile/src/app/(app)/_layout.tsx` (mount the listener)
- Modify: `apps/mobile/src/lib/purchaseCta.ts` (header comment only)
- Test: `apps/mobile/src/app/(app)/__tests__/settings.test.tsx`, `apps/mobile/src/components/provision/__tests__/ProvisionGate.test.tsx`, `apps/mobile/src/components/billing/__tests__/PurchaseListener.test.tsx`

**Interfaces:**
- Consumes: `paywallProductIds`, `linkApplePurchase` (Task 5), `PaywallScreen` (Task 6).
- Produces: `<PurchaseListener />` — no UI.

Behaviour:
- **Settings**: add `apple_iap_product_ids?: string[] | null` to `BillingAccount`. Inside the billing sections (only rendered for admins / non-restricted — the existing `orgRestricted` gate already hides billing from members), when `paywallProductIds(account).length > 0` AND the workspace is not Stripe-billed (`!(account.is_subscribed && !isAppleManaged(account.subscription_source))`), render a `Section` with `<NavigationRow title="Plans" icon="creditcard" onPress={() => router.push("/plans")} />`. Stripe-billed workspaces keep today's "Billed on the web" text.
- **ProvisionGate**: the org-owner subscription-gate branch (`isSubscriptionGate(status.code) && isOrgOwner`): if `status.ownerRole === "admin"`, fetch `/billing/account` (small `useEffect` + state, or reuse any account already available in the gate — check first) and, when `paywallProductIds(account)` is non-empty, render `<PaywallScreen … onPurchased={refetch} />` above a "Sign out" link, instead of the neutral interstitial. Members, and admins with no products, get today's interstitial unchanged, except a member's body becomes "Ask your workspace admin to choose a plan." when products are offered.
- **PurchaseListener**: calls `useIAP({ onPurchaseSuccess })`; for each purchase delivered at launch or later whose `productId` is one of the three ids and that has a `purchaseToken`, `linkApplePurchase` then `finishTransaction({ purchase, isConsumable: false })`; link failure → leave unfinished, `console.warn` only. Do nothing when the user is not an org admin (read `useOrganization().membership?.role === "org:admin"`), since link would 403. Mount it in `(app)/_layout.tsx` inside `WorkspaceGate` (so an org is active), next to `LiveEventsProvider`.
- Two components now handle `onPurchaseSuccess` (the paywall and the listener). Guard double-linking: the listener skips a purchase whose `transactionId` the paywall is currently linking — keep a module-level `Set<string>` `inFlight` in `appleIap.ts` (`markInFlight(id) / clearInFlight(id) / isInFlight(id)`); the paywall marks before linking and clears after; the listener checks it. Add these three functions to Task 5's module with a unit test.
- `purchaseCta.ts` header: replace the 3.1.3(f) paragraph's claim with: "The app sells plans through In-App Purchase (3.1.1); these gates therefore show the StoreKit paywall to admins when the server offers products, and neutral local copy otherwise. Never the server's web-purchase copy."

- [ ] **Step 1: Write the failing tests**
  - settings: Plans row shown for an admin on the free tier with products; hidden with `apple_iap_product_ids: []`; hidden for a Stripe-billed admin (`is_subscribed: true, subscription_source: null`); shown for an Apple-billed admin. Pressing it pushes `/plans` (mock `expo-router` the way the existing settings test does for `/workspaces`).
  - ProvisionGate: `trial_expired` + admin + products → paywall rendered (mock `PaywallScreen` to a `Text` "PAYWALL"); + member → "Ask your workspace admin to choose a plan."; + admin, no products → today's local copy.
  - PurchaseListener: a delivered purchase with a known product id and token → link then finish; link rejects → finish not called; unknown product id → neither called; in-flight id → neither called; member → neither called.
- [ ] **Step 2: Run to verify they fail** — `pnpm jest "src/app/(app)/__tests__/settings.test.tsx" src/components/provision src/components/billing src/lib/__tests__/appleIap.test.ts` → FAIL.
- [ ] **Step 3: Implement** per the behaviour list.
- [ ] **Step 4: Run** the same jest command + `pnpm typecheck && pnpm lint` → PASS. Also run the whole mobile suite once: `pnpm jest` → all pass (this task touches shared screens).
- [ ] **Step 5: Verify the OTA fingerprint is unchanged** — this branch adds no dependency and touches no `app.json`, so check that directly: `git diff origin/main --stat -- apps/mobile/app.json apps/mobile/package.json pnpm-lock.yaml` must print nothing. Then `cd apps/mobile && npx expo-updates fingerprint:generate --platform ios` on this branch and on `origin/main` (checked out in a second temporary worktree under the scratchpad, removed afterwards with `git worktree remove`) — the `hash` fields must be equal.
- [ ] **Step 6: Commit**

```bash
git add apps/mobile
git commit -m "feat(mobile): Plans in Settings, paywall at the subscription gate, launch purchase listener

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Stripe Checkout that returns to the app

**Files:**
- Modify: `apps/backend/routers/billing.py` (`CheckoutRequest`, `create_workspace_checkout` route ~line 277–310)
- Modify: `apps/backend/core/services/billing_service.py` (`create_workspace_checkout` ~line 154; the `Session.create` call with `success_url=f"{settings.FRONTEND_URL}/settings?panel=billing&checkout=success"`; the open-pending-session reuse check)
- Test: `apps/backend/tests/unit/services/test_workspace_checkout.py`, `apps/backend/tests/unit/routers/test_billing_workspace_checkout.py`

**Interfaces:**
- Produces: `POST /api/v1/billing/checkout` body `{"plan": "base"|"workspace"|"ultra", "return_to": "web"|"app"}` (`return_to` optional, default `"web"`) → `{"checkout_url": str}` as today. `billing_service.create_workspace_checkout(*, owner_id, clerk_user_id, plan, return_to: Literal["web", "app"] = "web")`.

- [ ] **Step 1: Write the failing tests** — in `test_workspace_checkout.py`, copy the existing new-session happy-path test (find it with `grep -n "Session.create\|checkout_url" tests/unit/services/test_workspace_checkout.py`) into two tests:
  - `return_to="app"`: the mocked `stripe.checkout.Session.create` receives `success_url == f"{FRONTEND_URL}/checkout_redirect/success"`, `cancel_url == f"{FRONTEND_URL}/checkout_redirect/cancel"`, `origin_context == "mobile_app"`, and the session metadata includes `return_to: "app"`.
  - default: unchanged web URLs, no `origin_context` key, metadata `return_to: "web"`.
  - An open pending session whose metadata `return_to` is `"web"` is NOT reused for a `return_to="app"` request (a new session is created), and IS reused for a `"web"` request. Base it on the existing open-session-reuse test in that file.
  In `test_billing_workspace_checkout.py`: the route passes `return_to` through (patch the service and assert the kwarg); an unknown `return_to` value → 422.
- [ ] **Step 2: Run to verify they fail** — `cd apps/backend && uv run python -m pytest tests/unit/services/test_workspace_checkout.py tests/unit/routers/test_billing_workspace_checkout.py -q` → FAIL.
- [ ] **Step 3: Implement**
  - `CheckoutRequest`: add `return_to: Literal["web", "app"] = "web"`; pass `return_to=body.return_to` to the service.
  - Service: add the `return_to` kwarg. Where the session is created:

```python
        if return_to == "app":
            # Stripe's app-to-web flow: the app opens this in Safari; these URLs are
            # claimed by the iOS app's universal link (apple-app-site-association
            # /checkout_redirect/*) and fall back to the web page of the same path.
            urls = {
                "success_url": f"{settings.FRONTEND_URL}/checkout_redirect/success",
                "cancel_url": f"{settings.FRONTEND_URL}/checkout_redirect/cancel",
                "origin_context": "mobile_app",
            }
        else:
            urls = {
                "success_url": f"{settings.FRONTEND_URL}/settings?panel=billing&checkout=success",
                "cancel_url": f"{settings.FRONTEND_URL}/settings?panel=billing&checkout=cancel",
            }
```

    and spread `**urls` into the create call in place of the two literal URL kwargs; add `"return_to": return_to` to the session `metadata` dict; in the open-pending-session reuse condition add `and (metadata.get("return_to") or "web") == return_to`.
  - If the installed `stripe` SDK rejects `origin_context` (check `uv run python -c "import stripe; print(stripe.VERSION)"`; the param is a plain request field, so it passes through), keep it — Stripe's docs name it for this flow.
- [ ] **Step 4: Run** the same pytest command → PASS; `uv run ruff check routers core` clean.
- [ ] **Step 5: Commit**

```bash
git add apps/backend/routers/billing.py apps/backend/core/services/billing_service.py apps/backend/tests/unit
git commit -m "feat(billing): workspace Checkout can return to the iOS app (app-to-web)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: "Or pay on the web" in the paywall

**Files:**
- Modify: `apps/mobile/src/components/billing/PaywallScreen.tsx`
- Modify: `apps/mobile/src/lib/appleIap.ts` (add `webPriceFor`, `startWebCheckout`)
- Test: `apps/mobile/src/components/billing/__tests__/PaywallScreen.test.tsx`, `apps/mobile/src/lib/__tests__/appleIap.test.ts`

**Interfaces:**
- Consumes: Task 8 route; `/billing/account` price fields; `parseCheckoutReturn` from `src/lib/deepLink.ts`.
- Produces:

```ts
export const WEB_PLAN_FOR_PRODUCT: Record<string, "base" | "workspace" | "ultra">;
export function webPriceFor(productId: string, account: { base_price_usd?: number; workspace_price_usd?: number; ultra_price_usd?: number } | null): number | null;
export async function startWebCheckout(post: ApiPost, productId: string): Promise<string>; // POST /billing/checkout {plan, return_to: "app"} → checkout_url
```

- `PaywallScreen` gains prop `account` (the `/billing/account` body it already has in `plans.tsx` / `ProvisionGate`) — pass it from both call sites.

Behaviour:
- Under each purchasable plan's IAP "Buy" button (same row), a secondary, smaller text-style button: `Or pay $${price}/month on the web` where `price = webPriceFor(productId, account)`; hidden if `price` is null.
- Tap: `busy`-guarded like Buy. `startWebCheckout(api.post, productId)` → `Linking.openURL(url)`. A 409 `apple_managed` / `checkout_in_progress` shows "A checkout is already open, or this workspace is billed through the App Store." Any other failure shows "Couldn't open checkout. Try again in a moment."
- Return: subscribe to `Linking.addEventListener("url")`; when `parseCheckoutReturn(url)?.status === "success"` call `onPurchased()` (which refetches — it never grants). Also call `onPurchased()`'s refetch on `AppState` → `active` after a web checkout was opened in this screen (covers the fallback page path).

- [ ] **Step 1: Write the failing tests**
  - `appleIap.test.ts`: `webPriceFor` maps base/plan/ultra product ids to `base_price_usd` / `workspace_price_usd` / `ultra_price_usd`, null for unknown id or missing account; `startWebCheckout` posts `{ plan: "workspace", return_to: "app" }` for the Workspace product and returns `checkout_url`.
  - `PaywallScreen.test.tsx` (mock `react-native`'s `Linking.openURL`):
    1. account prices → "Or pay $20/month on the web", "$50", "$150" visible; with no account → no web option.
    2. tapping the Workspace web option → `api.post("/billing/checkout", { plan: "workspace", return_to: "app" })` then `Linking.openURL(<checkout_url>)`; `mockRequestPurchase` not called.
    3. a `url` event with `…/checkout_redirect/success` → `onPurchased` called; with `…/checkout_redirect/cancel` → not called.
    4. the IAP "Buy" button is rendered before the web option in each row (order in `getAllByRole("button")`).
- [ ] **Step 2: Run to verify they fail** — `cd apps/mobile && pnpm jest src/components/billing src/lib/__tests__/appleIap.test.ts` → FAIL.
- [ ] **Step 3: Implement** per the behaviour list; update both `PaywallScreen` call sites (`plans.tsx`, `ProvisionGate.tsx`) to pass `account`.
- [ ] **Step 4: Run** the same jest command + `pnpm typecheck && pnpm lint` → PASS.
- [ ] **Step 5: Commit**

```bash
git add apps/mobile/src
git commit -m "feat(mobile): web checkout option beside the App Store purchase

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: Web billing page knows about App Store billing

**Files:**
- Modify: the web billing panel. Find it with `git grep -n "billing/portal\|/billing/checkout" -- apps/frontend/src ':!**/__tests__/**'` — the settings billing panel component that renders the plan, the "Manage billing" (portal) button and the plan-change buttons.
- Test: that component's existing test file (same grep, in `__tests__`), or a new one beside it.

**Interfaces:**
- Consumes: `/billing/account.subscription_source` (already returned; `"apple_iap"` for App Store billing).

Behaviour: when `subscription_source === "apple_iap"` and the workspace is subscribed, the panel shows the current plan name and the line "Billed through the App Store. Change or cancel on your iPhone: Settings → Apple Account → Subscriptions." It renders no Stripe portal button and no subscribe/change-plan buttons. Credit top-up UI, if present, is hidden too (it needs a card). Every other state renders exactly as today.

- [ ] **Step 1: Write the failing test** — render the panel with a mocked account `{ is_subscribed: true, subscription_status: "active", plan: "standard", subscription_source: "apple_iap", … }` (copy the existing test's mock shape) → the App Store line is visible; queries for the portal button and plan buttons return null. A second test with `subscription_source: null` still shows the portal button (guards today's behaviour).
- [ ] **Step 2: Run to verify it fails** — `cd apps/frontend && pnpm test <that test file>` → FAIL.
- [ ] **Step 3: Implement** — one early branch in the panel for the Apple-billed case. Colours/fonts only via the brand tokens (`src/__tests__/brandTokens.test.ts` enforces it).
- [ ] **Step 4: Run** the test file + `pnpm test src/__tests__/brandTokens.test.ts` + `pnpm lint` → PASS.
- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src
git commit -m "feat(web): billing panel shows App Store billing instead of Stripe controls

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 11: StoreKit test configuration, docs, and dev acceptance

**Files:**
- Create: `apps/mobile/storekit/Lightbulb.storekit`
- Create: `docs/runbooks/ios-iap-local-storekit-testing.md`
- Modify: `CLAUDE.md` (Billing System → "Apple IAP no longer accepts…" paragraph; Mobile App → "Apple subscriptions" bullet) and `AGENTS.md` if it carries the same paragraphs (`git grep -n "Apple IAP no longer" -- AGENTS.md CLAUDE.md`)

- [ ] **Step 1: StoreKit configuration file** — `apps/mobile/storekit/Lightbulb.storekit` (Xcode's JSON format, version 3):

```json
{
  "identifier" : "0D4C5E1A",
  "nonRenewingSubscriptions" : [],
  "products" : [],
  "settings" : { "_storeKitErrors" : [] },
  "subscriptionGroups" : [
    {
      "id" : "21000001",
      "localizations" : [],
      "name" : "Lightbulb Workspace",
      "subscriptions" : [
        {
          "adHocOffers" : [], "codeOffers" : [], "displayPrice" : "179.99", "familyShareable" : false,
          "groupNumber" : 1, "internalID" : "21000004", "introductoryOffer" : null,
          "localizations" : [ { "description" : "Ultra workspace plan", "displayName" : "Ultra", "locale" : "en_US" } ],
          "productID" : "company.thelightbulb.mobile.ultra.monthly", "recurringSubscriptionPeriod" : "P1M",
          "referenceName" : "Ultra", "subscriptionGroupID" : "21000001", "type" : "RecurringSubscription"
        },
        {
          "adHocOffers" : [], "codeOffers" : [], "displayPrice" : "59.99", "familyShareable" : false,
          "groupNumber" : 2, "internalID" : "21000003", "introductoryOffer" : null,
          "localizations" : [ { "description" : "Workspace plan", "displayName" : "Workspace", "locale" : "en_US" } ],
          "productID" : "company.thelightbulb.mobile.plan.monthly", "recurringSubscriptionPeriod" : "P1M",
          "referenceName" : "Workspace", "subscriptionGroupID" : "21000001", "type" : "RecurringSubscription"
        },
        {
          "adHocOffers" : [], "codeOffers" : [], "displayPrice" : "23.99", "familyShareable" : false,
          "groupNumber" : 3, "internalID" : "21000002", "introductoryOffer" : null,
          "localizations" : [ { "description" : "Base workspace plan", "displayName" : "Base", "locale" : "en_US" } ],
          "productID" : "company.thelightbulb.mobile.base.monthly", "recurringSubscriptionPeriod" : "P1M",
          "referenceName" : "Base", "subscriptionGroupID" : "21000001", "type" : "RecurringSubscription"
        }
      ]
    }
  ],
  "version" : { "major" : 3, "minor" : 0 }
}
```

(`groupNumber` 1 = highest service level. If Xcode rejects the file on open, recreate it in Xcode — File → New → StoreKit Configuration File — with the same ids, prices and ranking, and commit Xcode's output.)

- [ ] **Step 2: Runbook** `docs/runbooks/ios-iap-local-storekit-testing.md` — write these steps:
  1. `cd apps/mobile && EXPO_PUBLIC_API_URL=https://api-dev.flickmeon.ai/api/v1 npx expo prebuild -p ios` (the `ios/` dir is gitignored).
  2. `open ios/*.xcworkspace`; Product → Scheme → Edit Scheme → Run → Options → StoreKit Configuration → `storekit/Lightbulb.storekit` (drag it into the project if it is not listed).
  3. Run on an iOS simulator from Xcode. Sign in as the dev test admin via the Clerk ticket recipe (CLAUDE.md "Signing a Chrome session in…" — use the ticket in the app's sign-in or the simulator recipe in memory `mobile-nav-insets-keyboard-pass-2026-09-15`).
  4. Debug → StoreKit → Manage Transactions is the Transaction Manager (refund, expire, upgrade, "ask to buy").
  5. The dev backend accepts these because `APPLE_IAP_ACCEPT_XCODE=true` in dev only. Prod refuses them.
  6. The acceptance script (Step 4 below) and what each step must show.
  7. Note: Xcode purchases never reach `/webhooks/apple-iap` — renewals/expiry arrive only via relink/Restore in this mode. Real notifications need Apple's sandbox (after the Paid Apps Agreement).

- [ ] **Step 3: Docs** — in `CLAUDE.md` replace the "**Apple IAP no longer accepts new purchases or restores.** …" paragraph with a short paragraph: IAP sells Base/Workspace/Ultra via StoreKit 2 (`apple_iap.PRODUCT_PLANS`), admin-only `purchase-intent` + `link`, `APPLE_IAP_SELLING` switch (on in dev, off in prod until the Paid Apps Agreement is active), `APPLE_IAP_ACCEPT_XCODE` dev-only, iOS prices live in ASC ($23.99/$59.99/$179.99, web unchanged), web checkout 409s an Apple-billed workspace, link never writes `provider_choice`. Replace the Mobile App "**Apple subscriptions:**" bullet with: paywall at Settings → Plans and at the subscription gate for admins, server-driven product list, launch `PurchaseListener`, the "Or pay on the web" Stripe Checkout (allowed because the app is US-only in ASC — gate it by storefront before ever widening availability) (`return_to: "app"`, Safari, `/checkout_redirect/*` universal link), runbook link. Keep both under ~120 words each.

- [ ] **Step 4: Dev acceptance** (after the branch is merged and the dev deploy has landed — verify with `aws ecs describe-services --profile isol8-admin --cluster lightbulb-dev-container --services lightbulb-dev-backend --query 'services[0].taskDefinition'` and check the task def env has `APPLE_IAP_SELLING=true`). Use a fresh free-tier dev org whose admin you sign in as (create via the Clerk admin API as in memory `money-freetier-ux-audit-2026-09-24`). Capture a screenshot or response for each and post them on the PR:
  1. `GET /billing/account` → `apple_iap_product_ids` has 3 ids.
  2. Settings → Plans shows $23.99 / $59.99 / $179.99.
  3. Buy Base → row (`aws dynamodb get-item --table-name lightbulb-dev-billing-accounts …`): `apple_subscription_status=active`, `plan=base`, `subscription_status=active`, `subscription_source=apple_iap`, no `provider_choice`; a reconcile SFN execution started; `/container/status` ready (no `credits_required`); an agent replies in `#general`.
  4. Transaction Manager → upgrade to Ultra → the paywall/listener links it → row `plan=ultra_pro`.
  5. Buy with the backend unreachable (toggle the simulator's network off after the Apple sheet) → relaunch with network → the listener links it (row updated, transaction finished in Transaction Manager).
  6. Transaction Manager → expire/refund → Settings → Plans → Restore → row `apple_subscription_status=canceled`; the gate shows the paywall again.
  7. A member of the same org: no Plans row; the gate says "Ask your workspace admin…"; `curl` purchase-intent with the member's token → 403.
  8. `POST /billing/checkout` with the admin's token on the Apple-billed org → 409 `apple_managed`; the web billing page for that org shows "Billed through the App Store" and no Stripe buttons.
  9. On a second fresh free-tier org: the paywall shows "Or pay $20/month on the web" under Base → tap → Safari opens Stripe Checkout (dev = Stripe test mode; card 4242 4242 4242 4242) → pay → the universal link (or the `/checkout_redirect/success` fallback page's "Open the app" link) returns to the app → the account refetches → row `stripe_subscription_status=active`, `plan=base`, `subscription_source` absent; the paywall no longer offers either option.

- [ ] **Step 5: Commit** (Steps 1–3; Step 4 happens after merge and is recorded on the PR)

```bash
git add apps/mobile/storekit docs/runbooks/ios-iap-local-storekit-testing.md CLAUDE.md AGENTS.md
git commit -m "docs(iap): local StoreKit testing runbook, test config, CLAUDE.md billing/mobile paragraphs

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

## After the tasks

- Open one PR from `feat/ios-iap`. Merging ships a mobile OTA (`mobile-ota.yml`) — safe, because prod `/billing/account` returns no products; dev gets the feature on the next dev deploy.
- Do not promote to prod as part of this work; prod stays `APPLE_IAP_SELLING=false` regardless.
- Delete this plan and the spec when the feature launches in prod (CLAUDE.md "Planning docs are disposable").
