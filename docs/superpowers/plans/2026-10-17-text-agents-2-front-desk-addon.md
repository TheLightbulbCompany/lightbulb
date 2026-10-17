# Text agents, plan 2 of 3: the Front desk add-on and the number door

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A business clicks "Get a phone number", pays for Team plus the Front desk add-on, and the Sendblue line exists and is shown the moment the subscription is live; when the add-on item or the subscription goes away, the line is released.

**Architecture:** The add-on is a third Stripe subscription item on the workspace subscription, handled like the seat item: filtered out by the plan detector, added by checkout or by `SubscriptionItem.create` on a live subscription. The subscription webhook is the one writer that provisions or releases the line, so a retry, a portal change or a dashboard edit all converge.

**Tech Stack:** Stripe (subscriptions, checkout), FastAPI, DynamoDB billing row, Next.js, CDK (GitHub secrets at synth).

**Spec:** `docs/superpowers/specs/2026-10-16-text-agents-design.md` (Flow 3; Rules "Getting the number is where the business starts paying"; Mechanisms: Billing, Numbers; Calls: add-on at $100, charged then, no trial carried over).

**Depends on:** plan 1 Task 2 (`sendblue.provision_line/deprovision_line`), Task 3 (`agent_email_repo.put_text_line`, `text_line_for_owner`), Task 6 (`GET /agent-text/line`).

## Ponytail cuts (2026-10-17)

- No in-app Remove: no `remove_front_desk_item`, no `DELETE /billing/front-desk`, no Remove button. The Stripe billing portal (`billing_service.py:146`) and the dashboard already remove an item, and the webhook releases the line whichever way the item disappears. Add the door when a customer asks to drop just the add-on in-app.
- No `GetNumberButton.tsx`: the button is inline in the Mail panel header.

## Global Constraints

- Team stays `WORKSPACE_PRICE_USD` (50). The add-on is `FRONT_DESK_PRICE_USD = 100`, display only; the Stripe Price id is `STRIPE_FRONT_DESK_PRICE_ID` from GitHub secrets `STRIPE_FRONT_DESK_PRICE_ID_{DEV,PROD}`.
- Empty `STRIPE_FRONT_DESK_PRICE_ID` fails closed: the door returns 400 `front_desk_price_unconfigured`, nothing is charged.
- A front-desk checkout carries **no** Stripe trial, whatever the workspace's `trial_end` (the line costs us $100 the moment it exists).
- Apple-billed workspaces get 409 `apple_managed`, as checkout does.
- Only org admins can buy the add-on (`require_org_admin`).
- Never bake a price into a client: web reads `front_desk_price_usd` from `/billing/prices`.

## Review Focus

1. A Stripe event whose subscription carries plan + seat + front-desk items must still resolve the plan (`standard`) and must not emit `stripe.subscription.invalid_shape`. Test in Task 2.
2. Two deliveries of the same `customer.subscription.updated` must provision one line. Test in Task 5 (row exists → `unchanged`).
3. A provisioning failure (`daily_limit_reached`, `no_lines_available`) must leave the billing row consistent and make the webhook 5xx so Stripe retries. Test in Task 5.
4. A workspace on an unexpired free trial buying the add-on must be charged now, not on day 31. Test in Task 3.
5. The line must be released only after Stripe confirms the item is gone (through the webhook), never from a click. Test in Task 5.

---

### Task 1: Price id, display price, prices and account

**Files:**
- Modify: `apps/backend/core/config.py:296-317` (`STRIPE_FRONT_DESK_PRICE_ID: str = ""`, `FRONT_DESK_PRICE_USD: int = 100`)
- Modify: `apps/infra/lib/stacks/service-stack.ts:1580-1584` (env entry like the seat one), `apps/infra/lib/app.ts:181-183` (`STRIPE_FRONT_DESK_PRICE_ID_DEV/PROD` from secrets); regenerate `.github/workflows/deploy.yml`
- Modify: `apps/backend/routers/billing.py:121-140` (`/prices` adds `front_desk_price_usd`), `:143-207` (`/account` passes `front_desk`, `front_desk_price_usd`), `apps/backend/schemas/billing.py:14-74` (`front_desk: bool = False`, `front_desk_price_usd: int`)
- Modify: `apps/frontend/src/lib/pricing.ts:24-45` (`frontDeskPriceUsd`), `apps/frontend/src/hooks/useBilling.ts:41-42`
- Test: `apps/backend/tests/unit/routers/test_billing.py`, `apps/frontend/src/lib/__tests__/pricing.test.ts`, `apps/infra/test/service-stack.test.ts`

**Interfaces:**
- Produces: `settings.STRIPE_FRONT_DESK_PRICE_ID`, `settings.FRONT_DESK_PRICE_USD`; `/billing/prices.front_desk_price_usd`; `/billing/account.front_desk` (bool, from the billing row's `front_desk` attribute, Task 5) and `.front_desk_price_usd`; `Pricing.frontDeskPriceUsd`.

- [ ] **Step 1: Write the failing tests** (`test_prices_include_front_desk`, `test_account_reports_front_desk_flag`, pricing.test.ts reads `front_desk_price_usd`, service-stack test finds `STRIPE_FRONT_DESK_PRICE_ID` in the task env)
- [ ] **Step 2: Run** → FAIL
- [ ] **Step 3: Implement; run `cd apps/infra && npx cdk synth` and commit the regenerated `deploy.yml`**
- [ ] **Step 4: Run** backend, frontend and infra tests → PASS
- [ ] **Step 5: Commit** — `git commit -m "front desk add-on: price id, display price, prices/account"`

### Task 2: The plan detector tolerates the add-on item

**Files:**
- Modify: `apps/backend/core/services/stripe_webhook_handlers.py:300-336` (`_workspace_plan_from_subscription_items`), `apps/backend/core/services/billing_service.py:541` (add `_is_front_desk_item`)
- Test: `apps/backend/tests/unit/services/test_seat_billing.py` (beside `test_subscription_events_accept_the_seat_item`)

**Interfaces:**
- Produces: `def _is_front_desk_item(item: dict) -> bool` (price id equals `settings.STRIPE_FRONT_DESK_PRICE_ID`, False when unset); the detector filters out seat and front-desk items before the one-item check.

- [ ] **Step 1: Write the failing test**

```python
def test_subscription_events_accept_the_front_desk_item(monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_WORKSPACE_PRICE_ID", "price_workspace"); monkeypatch.setattr(settings, "STRIPE_SEAT_PRICE_ID", "price_seat"); monkeypatch.setattr(settings, "STRIPE_FRONT_DESK_PRICE_ID", "price_fd")
    items = {"items": {"data": [{"price": {"id": "price_workspace"}}, {"price": {"id": "price_seat"}}, {"price": {"id": "price_fd"}}]}}
    assert _workspace_plan_from_subscription_items(items) == "standard"
```

- [ ] **Step 2: Run** → FAIL (`None`)
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** `tests/unit/services/test_seat_billing.py tests/unit/services/test_stripe_webhook_handlers.py -q` → PASS
- [ ] **Step 5: Commit** — `git commit -m "stripe webhook: the front desk item is an allowed third item"`

### Task 3: Checkout with the add-on, and adding it on a live subscription

**Files:**
- Modify: `apps/backend/core/services/billing_service.py:163-413` (`create_workspace_checkout(..., front_desk: bool = False)`), add `add_front_desk_item`
- Test: `apps/backend/tests/unit/services/test_seat_billing.py`, `tests/unit/services/test_trial_checkout.py`

**Interfaces:**
- Produces:
  - `create_workspace_checkout(*, owner_id, clerk_user_id, plan, return_to="web", confirm=False, front_desk: bool = False)`: when `front_desk`: refuse `BillingServiceError("front_desk_price_unconfigured")` if the price is empty; append `{"price": settings.STRIPE_FRONT_DESK_PRICE_ID, "quantity": 1}` after the seat item; force `trial_end = None`; the idempotency key gains a trailing `:fd1` (`:fd0` otherwise).
  - `async def add_front_desk_item(owner_id: str) -> str` → `added | already | not_live | front_desk_price_unconfigured`: the live-subscription half of `sync_seat_quantity` (retrieve, `LIVE_SUBSCRIPTION_STATUSES`, `subscription_item_on_price`), `stripe.SubscriptionItem.create(subscription=sub_id, price=FD, quantity=1, proration_behavior="create_prorations", idempotency_key=f"front-desk-item:v1:{sub_id}")`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_checkout_with_front_desk_adds_the_item_and_no_trial(checkout, monkeypatch): ...   # trial_local row with trial_end in the future; line_items ends with price_fd qty 1; subscription_data has no trial_end
async def test_checkout_without_front_desk_is_unchanged(checkout): ...                            # key ends with :fd0
async def test_front_desk_checkout_refuses_without_a_price(): ...
async def test_add_front_desk_item_creates_once(): ...                                             # idempotency_key front-desk-item:v1:sub_1; second call -> already
```

- [ ] **Step 2: Run** → FAIL
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** the billing test files → PASS
- [ ] **Step 5: Commit** — `git commit -m "billing: front desk add-on in checkout and on live subscriptions"`

### Task 4: The door: `POST /billing/front-desk`

**Files:**
- Modify: `apps/backend/routers/billing.py` (after `POST /checkout`, ~line 388)
- Test: `apps/backend/tests/unit/routers/test_billing_front_desk.py` (pattern: `test_billing_workspace_checkout.py`)

**Interfaces:**
- Consumes: Task 3 functions; `apple_iap.apple_is_live`; `stripe_is_live`; `BillingService().create_customer_for_owner` (as checkout does).
- Produces: `POST /billing/front-desk` body `{return_to: "web" | "app" = "web"}`, admin only → Apple live: 409 `apple_managed`; Stripe live: `add_front_desk_item` → `{"status": "added" | "already"}`; otherwise `create_workspace_checkout(..., front_desk=True)` → `{"status": "checkout", "url": <session url>}`; `BillingServiceError` maps as checkout does (`checkout_in_progress` → 409, else 400 with the code).

- [ ] **Step 1: Write the failing tests** (`test_post_on_live_subscription_adds`, `test_post_on_trial_returns_checkout_url`, `test_post_apple_is_409`, `test_post_member_is_403`)
- [ ] **Step 2: Run** → FAIL (404)
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** → PASS
- [ ] **Step 5: Commit** — `git commit -m "billing: the front desk door"`

### Task 5: The webhook provisions and releases the line

**Files:**
- Create: `apps/backend/core/services/text_lines.py`
- Modify: `apps/backend/core/repositories/billing_repo.py` (add `set_front_desk`, template `set_founding_team` at :1169), `apps/backend/core/services/stripe_webhook_handlers.py:594-606` (call `sync_front_desk` after `sync_seat_quantity`)
- Test: `apps/backend/tests/unit/services/test_text_lines.py`, `tests/unit/services/test_stripe_webhook_handlers.py`

**Interfaces:**
- Consumes: `sendblue.provision_line`, `deprovision_line`, `SendblueError` (plan 1 Task 2); `agent_email_repo.put_text_line`, `text_line_for_owner`, `delete_all_for_owner`-style single delete via `_get_table().delete_item` (plan 1 Task 3); `PaperclipRepo.resolve_owner_row(...).starter_agent_ids["customer_operations"]`; `hermes_provisioning.refresh_agent_soul(owner_id, agent_id)`; `billing_service._is_front_desk_item`.
- Produces:
  - `billing_repo.set_front_desk(owner_id: str, value: bool) -> None`
  - `async def ensure_line(owner_id: str) -> str` → `unchanged` (row exists) | `provisioned`: `number = await sendblue.provision_line()`; `put_text_line(number=number, owner_id=owner_id, agent_id=<customer_operations id>)`; `set_front_desk(owner_id, True)`; `await refresh_agent_soul(owner_id, agent_id)` (best effort). No `customer_operations` on the roster → `BillingServiceError("no_front_desk_agent")` before any Sendblue call. On `SendblueError`: `put_metric("text_line.provision_failed", dimensions={"code": e.code})` and re-raise.
  - `async def release_line(owner_id: str) -> str` → `absent` | `released`: `deprovision_line` best effort (logged), delete the row, `set_front_desk(False)`, `refresh_agent_soul` best effort.
  - `async def sync_front_desk(owner_id: str, *, status: str, items: list[dict]) -> str`: `has = any(_is_front_desk_item(i) for i in items)`; live status and `has` → `ensure_line`; `not has` or status `canceled` → `release_line` when a row exists else `absent`; otherwise `skipped`. Raises through from `ensure_line`.
  - `handle_subscription_state` calls `await sync_front_desk(owner_id, status=new_status, items=event_data["items"]["data"])` right after `sync_seat_quantity`, letting exceptions propagate (Stripe redelivers).

- [ ] **Step 1: Write the failing tests**

```python
async def test_ensure_line_provisions_once_and_refreshes_the_soul(): ...
async def test_ensure_line_refuses_without_customer_operations(): ...    # no provision_line call
async def test_provision_failure_records_metric_and_raises(): ...
async def test_sync_with_item_on_live_provisions(): ...
async def test_sync_without_item_releases_existing_line(): ...
async def test_sync_canceled_releases(): ...
async def test_subscription_handler_calls_sync_front_desk_after_seats(): ...   # AsyncMock order
```

- [ ] **Step 2: Run** → FAIL
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** `tests/unit/services/test_text_lines.py tests/unit/services/test_stripe_webhook_handlers.py tests/unit/services/test_seat_billing.py -q` → PASS
- [ ] **Step 5: Commit** — `git commit -m "front desk: the subscription webhook provisions and releases the line"`

### Task 6: The number in the app

**Files:**
- Modify: `apps/frontend/src/components/mail/MailPanel.tsx` (header: the number from `GET /agent-text/line`, else for admins a "Get a phone number · $<price>/month" button, neutral "Get a phone number" when pricing is unavailable), `apps/frontend/src/app/settings/...BillingPanel` (one display line "Front desk · $<price>/month" when `account.front_desk`; no test, it is one conditional line)
- Test: `apps/frontend/src/components/mail/__tests__/MailPanel.number.test.tsx`

**Interfaces:**
- Consumes: `post("/billing/front-desk", {return_to: "web"})` → on `checkout` navigate to `url`; on `added`/`already` refetch `/agent-text/line` and `/billing/account`; `getPricing().frontDeskPriceUsd`.

- [ ] **Step 1: Write the failing tests** (`shows the number when the line exists`, `shows the price from pricing`, `checkout status navigates to url`, `added status refetches the line`)
- [ ] **Step 2: Run** `pnpm test -- MailPanel.number` → FAIL
- [ ] **Step 3: Implement with ladder tokens; no colour literals**
- [ ] **Step 4: Run** tests and lint → PASS
- [ ] **Step 5: Commit** — `git commit -m "app: get a phone number"`

### Task 7: Dev acceptance

- [ ] **Step 1:** In the Stripe sandbox create a recurring $100 Price "Front desk" and set `STRIPE_FRONT_DESK_PRICE_ID_DEV` in GitHub secrets; merge deploys dev.
- [ ] **Step 2:** On a dev trial workspace click "Get a phone number", pay with a test card, confirm: three subscription items, the webhook provisioned a line (row in `lightbulb-dev-agent-email-addresses` with `kind: text`, number in the Mail panel header), `/billing/account.front_desk` true, the invoice charged today.
- [ ] **Step 3:** Delete the add-on item in the Stripe sandbox dashboard; confirm the next webhook released the line and the header shows the button again. Receipts on the PR.

## Self-review notes

- Spec coverage: Flow 3 (paid step, line on the spot, shown in the workspace), Rules (never free), Mechanisms Billing and Numbers (provision on live, release on removal; a wipe is plan 1 Task 9), Calls (add-on at Sendblue's rate, no trial). Sendblue's default 3-a-day/10-a-month line limit is an account setting: raise it before launch.
