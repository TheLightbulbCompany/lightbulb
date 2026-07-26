# Budget-Exhaustion UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A user who exhausts their Isol8 budget — free-tier daily cap, trial window, or paid credits — sees an accurate limit card with a working upgrade/top-up CTA instead of "Provider rate limit reached".

**Architecture:** Three changes. (A) Every backend/UI budget predicate adopts the bedrock-gate's *admission* threshold rather than the raw cap, closing a $0.30 dead zone present on both plans. (B) The gateway's chat-error branch reclassifies a mid-run `rate_limit` into the real money-reason by re-reading budget state, provider-aware, with no OpenClaw fork change. (C) `ChatErrorCard` gains correct copy, the two missing money codes, and a cross-route CTA that actually navigates.

**Tech Stack:** FastAPI + DynamoDB (backend, `uv`/pytest), Next.js 16 App Router + React 19 (frontend, vitest + Testing Library), AWS CDK (TypeScript).

## Global Constraints

- `CAP_MICROCENTS = 3_000_000` — do NOT change. Three lockstep copies: `apps/backend/core/services/trial_cap.py`, `apps/infra/lambda/bedrock-usage-meter/index.py`, `apps/infra/lib/stacks/bedrock-gate-stack.ts` env.
- `RESERVE_ESTIMATE_MICROCENTS = 300_000` — the value this plan introduces backend-side. Lockstep with `bedrock-gate-stack.ts:173`. Moving one copy alone desynchronizes admission from every predicate that predicts it.
- The gate denies trial admission on `spent + reserved + estimate > cap` (strictly greater). Backend predicates MUST use `>` so `spent == CAP - RESERVE` still admits.
- No changes to the OpenClaw fork (`~/Desktop/agent`) or the Paperclip fork. Explicit design constraint.
- Never relabel a `rate_limit` for a ChatGPT-OAuth or BYO-key owner — they consume no Isol8 budget, so their 429 is a real third-party throttle.
- Wall copy carries **no dollar figure**. Settings keeps "$3/day of included usage" as the headline.
- Backend CI test scope is `tests/`, not `tests/unit/`.

---

## File Structure

**Backend**
- `apps/backend/core/services/trial_cap.py` — add `RESERVE_ESTIMATE_MICROCENTS`; trial predicate adopts admission threshold.
- `apps/backend/core/services/metronome_gate.py` — paid predicate adopts admission threshold.
- `apps/backend/core/gateway/connection_pool.py` — shared budget-message constants; mid-run reclassification in the chat-error branch.
- `apps/backend/routers/billing.py` — `/credits/balance` returns server-computed `is_exhausted`.

**Frontend**
- `apps/frontend/src/components/chat/ChatErrorCard.tsx` — copy, two new codes, retry suppression, cross-route CTA.
- `apps/frontend/src/lib/planLabel.ts` *(new)* — the single `subscription_status` → display-label mapping.
- `apps/frontend/src/app/settings/page.tsx`, `components/workspace/WorkspaceSidebar.tsx`, `components/chat/ChatLayout.tsx` — consume `planLabel`.
- `apps/frontend/src/components/chat/OutOfCreditsBanner.tsx` — render on `is_exhausted`.
- `apps/frontend/src/hooks/useCredits.ts` — surface `is_exhausted`.

---

### Task 1: Trial predicate adopts the gate's admission threshold

**Files:**
- Modify: `apps/backend/core/services/trial_cap.py:45` (constant block), `:106` (predicate)
- Test: `apps/backend/tests/unit/services/test_trial_cap.py`

**Interfaces:**
- Produces: `trial_cap.RESERVE_ESTIMATE_MICROCENTS: int` — consumed by Task 2 and Task 3.

- [ ] **Step 1: Update the existing test that this change invalidates**

`test_trial_block_reason_false_for_trial_local_under_cap` currently uses `CAP_MICROCENTS - 1` and asserts `None`. That spend is now inside the refusal band. Change it to a value genuinely below the admission threshold, and add explicit boundary cases:

```python
@pytest.mark.asyncio
async def test_trial_block_reason_false_well_under_admission_threshold(monkeypatch):
    """Spend below CAP - RESERVE is admissible: the gate would take the next
    request, so no door may pretend otherwise."""

    async def fake_get_by_owner_id(owner_id):
        return {"owner_id": owner_id, "subscription_status": "trial_local"}

    async def fake_get_trial_daily_spend(owner_id, date):
        return trial_cap.CAP_MICROCENTS - trial_cap.RESERVE_ESTIMATE_MICROCENTS - 1

    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_by_owner_id", fake_get_by_owner_id)
    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_trial_daily_spend", fake_get_trial_daily_spend)

    assert await trial_cap.trial_block_reason("owner1") is None


@pytest.mark.asyncio
async def test_trial_block_reason_admits_exactly_at_threshold(monkeypatch):
    """The gate denies on `spent + reserved + estimate > cap` — strictly
    greater — so spend of exactly CAP - RESERVE is still ADMITTED. Blocking
    here would re-open the dead zone in the other direction."""

    async def fake_get_by_owner_id(owner_id):
        return {"owner_id": owner_id, "subscription_status": "trial_local"}

    async def fake_get_trial_daily_spend(owner_id, date):
        return trial_cap.CAP_MICROCENTS - trial_cap.RESERVE_ESTIMATE_MICROCENTS

    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_by_owner_id", fake_get_by_owner_id)
    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_trial_daily_spend", fake_get_trial_daily_spend)

    assert await trial_cap.trial_block_reason("owner1") is None


@pytest.mark.asyncio
async def test_trial_block_reason_blocks_one_microcent_past_threshold(monkeypatch):
    """One microcent past the threshold the gate refuses admission. This is the
    prod bug: spend froze at $2.7585 inside this band, generated no tokens, so
    it could never reach CAP and the backend never blocked."""

    async def fake_get_by_owner_id(owner_id):
        return {"owner_id": owner_id, "subscription_status": "trial_local"}

    async def fake_get_trial_daily_spend(owner_id, date):
        return trial_cap.CAP_MICROCENTS - trial_cap.RESERVE_ESTIMATE_MICROCENTS + 1

    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_by_owner_id", fake_get_by_owner_id)
    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_trial_daily_spend", fake_get_trial_daily_spend)

    assert await trial_cap.trial_block_reason("owner1") == trial_cap.DAILY_CAP


@pytest.mark.asyncio
async def test_trial_block_reason_blocks_at_the_observed_prod_spend(monkeypatch):
    """Regression pin for the 2026-07-26 prod incident: $2.7585 spent, which the
    old `spent >= CAP` predicate reported as fine."""

    async def fake_get_by_owner_id(owner_id):
        return {"owner_id": owner_id, "subscription_status": "trial_local"}

    async def fake_get_trial_daily_spend(owner_id, date):
        return 2_758_514

    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_by_owner_id", fake_get_by_owner_id)
    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_trial_daily_spend", fake_get_trial_daily_spend)

    assert await trial_cap.trial_block_reason("owner1") == trial_cap.DAILY_CAP
```

Delete the old `test_trial_block_reason_false_for_trial_local_under_cap`; the first new test replaces it.

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_trial_cap.py -v`
Expected: `test_trial_block_reason_blocks_one_microcent_past_threshold` and `test_trial_block_reason_blocks_at_the_observed_prod_spend` FAIL (they return `None`); the two admit-side tests PASS; `AttributeError: RESERVE_ESTIMATE_MICROCENTS` on the tests referencing it.

- [ ] **Step 3: Add the constant**

In `trial_cap.py`, directly below the `CAP_MICROCENTS` block:

```python
# The bedrock-gate's per-request reservation hold (apps/infra/lambda/
# bedrock-gate/ledger.mjs). Admission is `spent + reserved + estimate <= cap`,
# so the LAST admissible spend is CAP - RESERVE — not CAP. Every predicate that
# claims to answer "will the next request be admitted?" must subtract it, or it
# opens a dead zone where the backend forwards a turn the gate then refuses.
#
# Lockstep with RESERVE_ESTIMATE_MICROCENTS in
# apps/infra/lib/stacks/bedrock-gate-stack.ts — moving one alone desynchronizes
# admission from every door that predicts it.
RESERVE_ESTIMATE_MICROCENTS = 300_000
```

- [ ] **Step 4: Adopt the admission threshold in the predicate**

In `reason_for_account`, replace the cap comparison:

```python
    spent = await billing_repo.get_trial_daily_spend(owner_id, utc_today())
    # `>` (not `>=`) mirrors the gate's own inequality exactly, so spend of
    # precisely CAP - RESERVE still admits. `reserved` is deliberately NOT read
    # here: holds are transient and released on settle, so folding them in would
    # over-block an owner with two runs legitimately in flight. `spent` is the
    # durable floor — and it is what makes the refusal band permanent, which is
    # the bug this closes. A turn that crosses the line mid-run is caught by the
    # gateway's reclassification instead.
    if spent + RESERVE_ESTIMATE_MICROCENTS > CAP_MICROCENTS:
        return DAILY_CAP
    return None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_trial_cap.py -v`
Expected: all PASS.

- [ ] **Step 6: Run the wider suites that consume this predicate**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_provision_gate.py tests/unit/services/test_provision_gate_credits.py -v`
Expected: all PASS. If a test fails because it seeded a spend between `CAP - RESERVE` and `CAP`, update its fixture value the same way Step 1 did and note it in the commit body.

- [ ] **Step 7: Commit**

```bash
git add apps/backend/core/services/trial_cap.py apps/backend/tests/unit/services/test_trial_cap.py
git commit -m "fix(billing): trial predicate adopts the gate's admission threshold

The bedrock-gate refuses admission at spent + reserved + estimate > cap
(\$2.70) while this predicate blocked at spent >= cap (\$3.00), opening a
\$0.30 band where the backend forwarded turns the gate then 429'd. Refused
turns generate no tokens, so spend froze inside the band and could never
reach the cap — the correct limit card was unreachable until UTC rollover."
```

---

### Task 2: Paid credit predicate adopts the same threshold

**Files:**
- Modify: `apps/backend/core/services/metronome_gate.py` (`is_owner_blocked`, `_row_blocked`)
- Test: `apps/backend/tests/unit/services/test_metronome_gate.py`

**Interfaces:**
- Consumes: `trial_cap.RESERVE_ESTIMATE_MICROCENTS` (Task 1).

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_row_blocked_at_reserve_estimate():
    """The gate denies paid admission on `remaining - reserved - estimate <= 0`,
    so an owner holding exactly one reservation's worth is already refused. The
    old `remaining <= 0` predicate left the same $0.30 dead zone the trial side
    had: a paying customer with $0.20 left got 'Provider rate limit reached'
    and no out-of-credits banner."""
    from core.services.metronome_gate import _row_blocked
    from core.services.trial_cap import RESERVE_ESTIMATE_MICROCENTS

    assert _row_blocked({"remaining_microcents": RESERVE_ESTIMATE_MICROCENTS}) is True
    assert _row_blocked({"remaining_microcents": RESERVE_ESTIMATE_MICROCENTS - 1}) is True
    assert _row_blocked({"remaining_microcents": RESERVE_ESTIMATE_MICROCENTS + 1}) is False


@pytest.mark.asyncio
async def test_row_blocked_missing_row_still_admits():
    """A brand-new owner with no projection row must not be gated by its
    absence — unchanged by the threshold move."""
    from core.services.metronome_gate import _row_blocked

    assert _row_blocked(None) is False
    assert _row_blocked({}) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_metronome_gate.py -k row_blocked -v`
Expected: FAIL — `_row_blocked({"remaining_microcents": 300000})` returns `False`.

- [ ] **Step 3: Adopt the threshold**

In `metronome_gate.py`, add the import at module level:

```python
from core.services.trial_cap import RESERVE_ESTIMATE_MICROCENTS
```

Replace `_row_blocked`'s comparison:

```python
def _row_blocked(row: dict | None) -> bool:
    if not row:
        return False
    remaining = row.get("remaining_microcents")
    if remaining is None:
        return bool(row.get("blocked", False))
    # The gate admits only while `remaining - reserved - estimate > 0`, so a
    # balance at or below one reservation is already unspendable. Matching it
    # here keeps the banner, the chat gate and the provision gate agreeing with
    # the only component that actually decides.
    return int(remaining) <= RESERVE_ESTIMATE_MICROCENTS
```

And in `is_owner_blocked`, the post-refresh comparison:

```python
    refreshed = await _refresh_from_metronome(owner_id, gate)
    if refreshed is not None:
        return refreshed <= RESERVE_ESTIMATE_MICROCENTS
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_metronome_gate.py -v`
Expected: all PASS. If an existing test seeds `remaining_microcents` between 1 and 300,000 and expects not-blocked, update it — that balance is genuinely unspendable.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/metronome_gate.py apps/backend/tests/unit/services/test_metronome_gate.py
git commit -m "fix(billing): paid credit predicate adopts the gate's admission threshold

Same \$0.30 dead zone as the trial side: the gate refuses paid admission at
remaining <= \$0.30 while this blocked at remaining <= 0."
```

---

### Task 3: `/credits/balance` returns a server-computed `is_exhausted`

**Files:**
- Modify: `apps/backend/routers/billing.py:930-935` (the `get_credits_balance` return dict)
- Test: `apps/backend/tests/unit/routers/test_billing_credits_balance.py` *(create if absent)*

**Interfaces:**
- Produces: `/billing/credits/balance` response gains `is_exhausted: bool` — consumed by Task 8.

Rationale: `OutOfCreditsBanner` needs the admission threshold. Computing it server-side avoids a third TypeScript copy of `RESERVE_ESTIMATE_MICROCENTS` that could drift silently.

- [ ] **Step 1: Write the failing test**

```python
import pytest

from routers import billing


@pytest.mark.asyncio
async def test_balance_is_exhausted_at_or_below_reserve(monkeypatch):
    """A balance at or below one reservation is unspendable — the gate would
    refuse the next request — so the banner must show."""
    from core.services.trial_cap import RESERVE_ESTIMATE_MICROCENTS

    for balance, expected in [
        (RESERVE_ESTIMATE_MICROCENTS - 1, True),
        (RESERVE_ESTIMATE_MICROCENTS, True),
        (RESERVE_ESTIMATE_MICROCENTS + 1, False),
    ]:
        assert billing._is_exhausted(balance) is expected


def test_balance_unknown_is_never_exhausted():
    """Metronome down / no customer yet reads as null. Never show a false
    out-of-credits banner on an indeterminate balance."""
    assert billing._is_exhausted(None) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_billing_credits_balance.py -v`
Expected: FAIL with `AttributeError: module 'routers.billing' has no attribute '_is_exhausted'`.

- [ ] **Step 3: Implement**

Add near the other module-level helpers in `routers/billing.py`:

```python
def _is_exhausted(balance_microcents: int | None) -> bool:
    """Whether the owner's credits are unspendable at the bedrock-gate.

    The gate admits paid traffic only while `remaining - reserved - estimate
    > 0`, so a balance at or below one reservation buys nothing. Surfacing the
    gate's own threshold keeps the frontend from needing a copy of the
    constant. A null balance (Metronome outage / no customer) is INDETERMINATE
    and never exhausted — a false banner is worse than a late one.
    """
    from core.services.trial_cap import RESERVE_ESTIMATE_MICROCENTS

    if balance_microcents is None:
        return False
    return balance_microcents <= RESERVE_ESTIMATE_MICROCENTS
```

Add the field to the `get_credits_balance` return dict:

```python
        "is_exhausted": _is_exhausted(balance_uc),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_billing_credits_balance.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/routers/billing.py apps/backend/tests/unit/routers/test_billing_credits_balance.py
git commit -m "feat(billing): /credits/balance returns is_exhausted at the gate threshold"
```

---

### Task 4: Mid-run reclassification in the gateway error branch

**Files:**
- Modify: `apps/backend/core/gateway/connection_pool.py:1797-1831` (chat-error branch), `:2478-2539` (`gate_chat`), plus a new module-level constant block
- Test: `apps/backend/tests/unit/gateway/test_budget_reclassify.py` *(create)*

**Interfaces:**
- Consumes: `trial_cap.reason_for_account`, `metronome_gate.is_owner_blocked`, `self.owner_id` (available on `GatewayConnection`, set at `connection_pool.py:195`).
- Produces: `BUDGET_BLOCK_MESSAGES: dict[str, str]`; `GatewayConnection._budget_error_code() -> tuple[str, str] | None`.

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from core.gateway import connection_pool
from core.gateway.connection_pool import GatewayConnection


def _conn(owner_id="owner1"):
    """A GatewayConnection with only the attributes _budget_error_code touches.

    __new__ skips __init__ deliberately — the constructor opens sockets and
    registers liveness callbacks, none of which this pure read needs.
    """
    conn = GatewayConnection.__new__(GatewayConnection)
    conn.owner_id = owner_id
    return conn


@pytest.mark.asyncio
async def test_reclassifies_capped_trial_owner(monkeypatch):
    async def fake_account(owner_id):
        return {"owner_id": owner_id, "subscription_status": "trial_local"}

    async def fake_reason(owner_id, account):
        return "trial_daily_cap"

    monkeypatch.setattr("core.repositories.billing_repo.get_by_owner_id", fake_account)
    monkeypatch.setattr("core.services.trial_cap.reason_for_account", fake_reason)

    assert await _conn()._budget_error_code() == (
        "trial_daily_cap",
        connection_pool.BUDGET_BLOCK_MESSAGES["trial_daily_cap"],
    )


@pytest.mark.asyncio
async def test_reclassifies_expired_trial_owner(monkeypatch):
    async def fake_account(owner_id):
        return {"owner_id": owner_id, "subscription_status": "trial_local"}

    async def fake_reason(owner_id, account):
        return "trial_expired"

    monkeypatch.setattr("core.repositories.billing_repo.get_by_owner_id", fake_account)
    monkeypatch.setattr("core.services.trial_cap.reason_for_account", fake_reason)

    assert await _conn()._budget_error_code() == (
        "trial_expired",
        connection_pool.BUDGET_BLOCK_MESSAGES["trial_expired"],
    )


@pytest.mark.asyncio
async def test_reclassifies_paid_bedrock_owner_out_of_credits(monkeypatch):
    async def fake_account(owner_id):
        return {
            "owner_id": owner_id,
            "subscription_status": "active",
            "provider_choice": "bedrock_claude",
        }

    async def fake_blocked(owner_id):
        return True

    monkeypatch.setattr("core.repositories.billing_repo.get_by_owner_id", fake_account)
    monkeypatch.setattr("core.services.metronome_gate.is_owner_blocked", fake_blocked)

    assert await _conn()._budget_error_code() == (
        "out_of_credits",
        connection_pool.BUDGET_BLOCK_MESSAGES["out_of_credits"],
    )


@pytest.mark.asyncio
async def test_oauth_owner_is_never_reclassified(monkeypatch):
    """THE load-bearing negative case. An OAuth owner consumes no Isol8 budget,
    so their 429 is a genuine OpenAI throttle. Relabelling it would send them to
    an Isol8 billing page to fix someone else's quota."""

    async def fake_account(owner_id):
        return {
            "owner_id": owner_id,
            "subscription_status": "active",
            "provider_choice": "chatgpt_oauth",
        }

    monkeypatch.setattr("core.repositories.billing_repo.get_by_owner_id", fake_account)

    assert await _conn()._budget_error_code() is None


@pytest.mark.asyncio
async def test_byo_key_owner_is_never_reclassified(monkeypatch):
    async def fake_account(owner_id):
        return {
            "owner_id": owner_id,
            "subscription_status": "active",
            "provider_choice": "byo_key",
        }

    monkeypatch.setattr("core.repositories.billing_repo.get_by_owner_id", fake_account)

    assert await _conn()._budget_error_code() is None


@pytest.mark.asyncio
async def test_funded_bedrock_owner_keeps_the_real_throttle(monkeypatch):
    """A real Bedrock throttle for a funded owner must stay `rate_limit` —
    otherwise we mask an AWS incident as a billing problem."""

    async def fake_account(owner_id):
        return {
            "owner_id": owner_id,
            "subscription_status": "active",
            "provider_choice": "bedrock_claude",
        }

    async def fake_blocked(owner_id):
        return False

    monkeypatch.setattr("core.repositories.billing_repo.get_by_owner_id", fake_account)
    monkeypatch.setattr("core.services.metronome_gate.is_owner_blocked", fake_blocked)

    assert await _conn()._budget_error_code() is None


@pytest.mark.asyncio
async def test_missing_billing_row_is_not_reclassified(monkeypatch):
    async def fake_account(owner_id):
        return None

    monkeypatch.setattr("core.repositories.billing_repo.get_by_owner_id", fake_account)

    assert await _conn()._budget_error_code() is None


@pytest.mark.asyncio
async def test_read_failure_leaves_the_error_untouched(monkeypatch):
    """The budget read is a network call on an error path. A failure must
    degrade to today's behaviour (show the upstream error), never raise into
    event forwarding and drop the terminal frame — that would hang the run."""

    async def boom(owner_id):
        raise RuntimeError("ddb down")

    monkeypatch.setattr("core.repositories.billing_repo.get_by_owner_id", boom)

    assert await _conn()._budget_error_code() is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/gateway/test_budget_reclassify.py -v`
Expected: FAIL — `AttributeError: 'GatewayConnection' object has no attribute '_budget_error_code'`.

- [ ] **Step 3: Extract the shared budget messages**

Add at module level in `connection_pool.py`, above `class GatewayConnection`:

```python
# The user-facing text for each way an owner can run out of Isol8 budget.
# ONE copy, shared by gate_chat's pre-send refusal and the gateway's mid-run
# reclassification: a turn refused before it starts and a turn refused halfway
# through are the same event to the user and must never drift into two wordings.
# The web card renders from `code` alone (see ChatErrorCard); these strings are
# what non-web surfaces — Paperclip run rows, logs, the wake refusal frame — show.
BUDGET_BLOCK_MESSAGES = {
    "trial_expired": "Your 3-day free trial has ended. Pick a plan to keep your agent.",
    "trial_daily_cap": "You've used today's free allowance — resets tomorrow. Upgrade to keep going.",
    "out_of_credits": "You're out of Claude credits. Top up to continue.",
    "subscription_inactive": "Your subscription is not active. Reactivate to continue chatting.",
}
```

Then replace the four inline literals in `gate_chat` with lookups, e.g.:

```python
            return {
                "blocked": True,
                "code": "subscription_inactive",
                "message": BUDGET_BLOCK_MESSAGES["subscription_inactive"],
            }
```

…and identically for `trial_expired`, `trial_daily_cap`, and `out_of_credits`.

- [ ] **Step 4: Implement `_budget_error_code`**

Add as a method on `GatewayConnection`, directly above the `_transform_agent_event` handler:

```python
    async def _budget_error_code(self) -> tuple[str, str] | None:
        """Is this owner's `rate_limit` actually an Isol8 budget refusal?

        The bedrock-gate returns budget denials as `429 ThrottlingException`
        so the container never learns it's behind a proxy. OpenClaw classifies
        that as `errorKind: "rate_limit"` and — at two or more model attempts —
        discards the gate's own message (`model-fallback.ts:741`). So by the
        time the error reaches us the money reason is gone, and the only honest
        way to recover it is to re-read the budget.

        PROVIDER-AWARE BY CONSTRUCTION. Only owners whose inference Isol8 funds
        (`trial_local`, or paid `bedrock_claude`) can be reclassified. A
        ChatGPT-OAuth or BYO-key owner's 429 comes from THEIR provider on THEIR
        account; relabelling it would route them to an Isol8 billing page to fix
        someone else's quota. Same reasoning that made the wake-admission gate
        reuse gate_chat rather than a trial-only predicate.

        Returns `(code, message)` to substitute, or None to leave the upstream
        error exactly as it arrived — which is also the answer for a genuine
        Bedrock throttle against a funded owner.

        Never raises: this runs on the terminal error frame, and an exception
        here would drop the frame and hang the run in the UI.
        """
        from core.repositories import billing_repo
        from core.services import trial_cap
        from core.services.metronome_gate import is_owner_blocked

        try:
            account = await billing_repo.get_by_owner_id(self.owner_id)
            if not account:
                return None

            if account.get("subscription_status") == "trial_local":
                reason = await trial_cap.reason_for_account(self.owner_id, account)
                if reason in BUDGET_BLOCK_MESSAGES:
                    return reason, BUDGET_BLOCK_MESSAGES[reason]
                return None

            if account.get("provider_choice") != "bedrock_claude":
                return None

            if await is_owner_blocked(self.owner_id):
                return "out_of_credits", BUDGET_BLOCK_MESSAGES["out_of_credits"]
            return None
        except Exception:
            logger.warning(
                "Budget reclassification read failed for owner %s — forwarding the upstream error unchanged",
                self.owner_id,
                exc_info=True,
            )
            return None
```

- [ ] **Step 5: Wire it into the chat-error branch**

In the `elif state == "error":` branch, after `err_msg` is resolved and **before** `put_metric`:

```python
                    # A budget refusal reaches us disguised as a provider
                    # throttle (see _budget_error_code). Recover the real reason
                    # so the user gets a limit card with an upgrade path instead
                    # of "wait for your quota to reset", which for a capped
                    # free-tier owner is advice they cannot act on.
                    upstream_msg: Optional[str] = None
                    if err_kind == "rate_limit":
                        money = await self._budget_error_code()
                        if money is not None:
                            upstream_msg = err_msg
                            err_kind, err_msg = money

                    put_metric(
                        "chat.error",
                        dimensions={
                            "reason": err_kind or "agent_error",
                            # Makes mislabelling VISIBLE. If we ever start
                            # reclassifying real AWS throttles, this spikes
                            # instead of failing silently.
                            "reclassified": "true" if upstream_msg else "false",
                        },
                    )
                    fwd = {"type": "error", "message": err_msg}
                    if err_kind:
                        fwd["code"] = err_kind
                    if upstream_msg:
                        # Keep the true upstream cause on the wire for logs and
                        # support. The card renders from `code`, so this never
                        # reaches the user as copy.
                        fwd["upstream_message"] = upstream_msg
```

Delete the original `put_metric("chat.error", ...)` call and the `fwd = {...}` / `if err_kind:` lines this block replaces.

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/gateway/test_budget_reclassify.py -v`
Expected: all PASS.

- [ ] **Step 7: Run the gateway and gate_chat suites**

Run: `cd apps/backend && uv run pytest tests/unit/gateway/ tests/unit/services/test_provision_gate.py tests/unit/routers/test_websocket_chat_wake_gate.py tests/unit/routers/teams/ -v`
Expected: all PASS.

`test_websocket_chat_wake_gate.py:334-337` already pins all four strings as a
parametrized table, and several `tests/unit/routers/teams/` tests embed the
daily-cap string in fixtures. The extraction keeps the strings byte-identical,
so none should fail. If any does, the extraction changed a string — fix the
constant, do not edit the assertion. Update that parametrized table to read from
`BUDGET_BLOCK_MESSAGES` so it does not become a fifth copy.

- [ ] **Step 8: Commit**

```bash
git add apps/backend/core/gateway/connection_pool.py apps/backend/tests/unit/gateway/test_budget_reclassify.py
git commit -m "fix(gateway): recover the money reason from a mid-run budget refusal

The bedrock-gate shapes budget denials as Bedrock ThrottlingExceptions, and
OpenClaw's fallback summary discards the gate's message at >=2 attempts, so a
turn that crosses the cap mid-run surfaced as a provider rate limit. Re-read
budget state on rate_limit errors and substitute the real code. Only for
owners whose inference Isol8 funds — an OAuth/BYOK 429 is their provider's."
```

---

### Task 5: `ChatErrorCard` — correct copy, the two missing codes, retry suppression

**Files:**
- Modify: `apps/frontend/src/components/chat/ChatErrorCard.tsx`
- Test: `apps/frontend/src/components/chat/__tests__/ChatErrorCard.test.tsx`

**Interfaces:**
- Produces: `MONEY_CODES: ReadonlySet<string>` (module-local); `ErrorPresentation.action.crossRoute?: boolean` — consumed by Task 6.

- [ ] **Step 1: Write the failing tests**

```tsx
  it("renders the trial_daily_cap wall with an upgrade CTA and no dollar figure", () => {
    render(<ChatErrorCard code="trial_daily_cap" message="server copy" />);
    expect(screen.getByText("Daily free limit reached")).toBeInTheDocument();
    expect(screen.getByText(/resets at midnight UTC/i)).toBeInTheDocument();
    // No dollar figure at the wall: after the admission-threshold fix the wall
    // fires at $2.70 of usable spend while Settings advertises "$3/day", so any
    // number printed here would contradict it.
    expect(screen.queryByText(/\$/)).not.toBeInTheDocument();
    // The card renders from `code`, ignoring the server message, so the
    // pre-send and mid-run refusals are pixel-identical.
    expect(screen.queryByText("server copy")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Upgrade to keep going" })).toHaveAttribute(
      "href",
      "/settings?panel=billing",
    );
  });

  it("omits the reset line for trial_expired — expiry is permanent", () => {
    render(<ChatErrorCard code="trial_expired" message="" />);
    expect(screen.getByText("Your free trial has ended")).toBeInTheDocument();
    expect(screen.queryByText(/resets at midnight/i)).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /choose a plan/i })).toHaveAttribute(
      "href",
      "/settings?panel=billing",
    );
  });

  it("renders out_of_credits with a top-up CTA instead of Failed to generate", () => {
    render(<ChatErrorCard code="out_of_credits" message="" />);
    expect(screen.getByText("Out of credits")).toBeInTheDocument();
    expect(screen.queryByText("Failed to generate")).not.toBeInTheDocument();
    // Routes into CreditsStep, the same destination OutOfCreditsBanner uses.
    expect(screen.getByRole("link", { name: "Top up now" })).toHaveAttribute(
      "href",
      "/chat?provider=bedrock_claude",
    );
  });

  it("renders subscription_inactive with a reactivate CTA", () => {
    render(<ChatErrorCard code="subscription_inactive" message="" />);
    expect(screen.getByText("Subscription inactive")).toBeInTheDocument();
    expect(screen.queryByText("Failed to generate")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Reactivate" })).toHaveAttribute(
      "href",
      "/settings?panel=billing",
    );
  });

  it("suppresses Retry on money codes but keeps it elsewhere", () => {
    const onRetry = vi.fn();
    for (const code of ["trial_daily_cap", "trial_expired", "out_of_credits", "subscription_inactive"]) {
      const { unmount } = render(<ChatErrorCard code={code} message="" onRetry={onRetry} />);
      // Retry against a budget wall is guaranteed to fail until reset or
      // top-up; offering it reads as product flakiness, not an exhausted
      // allowance.
      expect(screen.queryByRole("button", { name: /retry/i })).not.toBeInTheDocument();
      unmount();
    }
    render(<ChatErrorCard code="timeout" message="" onRetry={onRetry} />);
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/frontend && pnpm vitest run src/components/chat/__tests__/ChatErrorCard.test.tsx`
Expected: FAIL — "Daily free limit reached" not found (current copy differs), `out_of_credits` renders "Failed to generate", Retry present on money codes.

- [ ] **Step 3: Implement**

Add above `describeError`:

```tsx
/**
 * Codes where the run died because the owner is out of Isol8 budget, not
 * because anything malfunctioned. Retry is suppressed for all of them: it
 * cannot succeed until the cap resets or the owner pays, and offering it
 * makes an exhausted allowance look like a flaky product.
 */
const MONEY_CODES: ReadonlySet<string> = new Set([
  "trial_daily_cap",
  "trial_expired",
  "out_of_credits",
  "subscription_inactive",
]);
```

**No `resetPhrase` / countdown.** A round-1 draft of this task added a
client-side `resetPhrase(nowMs)` helper computing "in about N hours" until
the next UTC midnight (via a `React.useState(() => Date.now())` snapshot).
Dropped in round-2 review: it's arithmetically correct but surprising near
the rollover (re-hitting the cap at 00:20 UTC reads "in about 24 hours"),
and removing it also removes the client-clock dependency and the whole
class of rounding edges rather than special-casing them. `describeError`
keeps its original two-argument shape (`code`, `message`) — no clock is
threaded through it. The `trial_daily_cap` description is the fixed string
`"You've used today's free usage. Resets at midnight UTC."`

Extend the action type:

```tsx
interface ErrorPresentation {
  title: string;
  description: string;
  /** `crossRoute` marks a target on a DIFFERENT Next route (e.g. /settings).
   *  Those must navigate via <Link>; handleShellLinkClick only swaps query
   *  params on the route the shell is already on. See Task 6. */
  action?: { label: string; href: string; crossRoute?: boolean };
}
```

Replace the four money branches (`describeError`'s signature stays two-argument — no clock):

```tsx
function describeError(code: string | undefined, message: string): ErrorPresentation {
  switch (code) {
    // ... rate_limit / context_length / timeout / refusal unchanged ...
    case "trial_daily_cap":
      // Copy is derived from `code` alone — the server message is ignored, so
      // the pre-send refusal (gate_chat) and the mid-run reclassification
      // render an identical card. No dollar figure: the wall fires at
      // CAP - RESERVE while Settings advertises the headline $3/day. No
      // countdown either — see the "No `resetPhrase` / countdown" note above.
      return {
        title: "Daily free limit reached",
        description: "You've used today's free usage. Resets at midnight UTC.",
        action: {
          label: "Upgrade to keep going",
          href: "/settings?panel=billing",
          crossRoute: true,
        },
      };
    case "trial_expired":
      // No reset line: trial_cap._is_expired never self-clears.
      return {
        title: "Your free trial has ended",
        description: "Your free trial has ended. Choose a plan to keep your agent running.",
        action: {
          label: "Choose a plan",
          href: "/settings?panel=billing",
          crossRoute: true,
        },
      };
    case "out_of_credits":
      // Previously fell through to `default` — a paying customer saw the same
      // "Failed to generate" a crash would produce, with no way to top up.
      return {
        title: "Out of credits",
        description:
          "Your Claude credit balance is used up. Top up to keep your agents running.",
        // In-shell: routes into CreditsStep, same as OutOfCreditsBanner.
        action: { label: "Top up now", href: "/chat?provider=bedrock_claude" },
      };
    case "subscription_inactive":
      return {
        title: "Subscription inactive",
        description:
          "Your subscription isn't active, so agent runs are paused. Reactivate to continue.",
        action: {
          label: "Reactivate",
          href: "/settings?panel=billing",
          crossRoute: true,
        },
      };
    case "unknown":
    default:
      return {
        title: "Failed to generate",
        description: message || "Your agent encountered an error. Please try again.",
      };
  }
}
```

In the component, gate Retry (no clock snapshot — there is no `resetPhrase` to feed):

```tsx
export function ChatErrorCard({ message, code, onRetry }: ChatErrorCardProps) {
  const { title, description, action } = describeError(code, message);
  const showRetry = Boolean(onRetry) && !MONEY_CODES.has(code ?? "");
```

…and change the Retry block's condition from `{onRetry && (` to `{showRetry && onRetry && (`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/frontend && pnpm vitest run src/components/chat/__tests__/ChatErrorCard.test.tsx`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/chat/ChatErrorCard.tsx apps/frontend/src/components/chat/__tests__/ChatErrorCard.test.tsx
git commit -m "feat(chat): accurate limit cards for every budget-exhaustion code

out_of_credits and subscription_inactive were emitted by gate_chat but had no
branch, so a paying customer out of credits got 'Failed to generate' with no
top-up path. Retry is suppressed on money codes — it cannot succeed."
```

---

### Task 6: Cross-route CTA actually navigates

**Files:**
- Modify: `apps/frontend/src/components/chat/ChatErrorCard.tsx` (render block, ~line 136-146)
- Test: `apps/frontend/src/__tests__/components/chat/ChatErrorCard.shellNav.test.tsx`

**Interfaces:**
- Consumes: `ErrorPresentation.action.crossRoute` (Task 5).

`handleShellLinkClick` unconditionally calls `e.preventDefault()` then `window.history.pushState`. That is correct for in-shell targets (`?panel`, `?agent`, `?detailId` on the route the shell is already on) — all 19 other call sites. `ChatErrorCard` is the only one passing a cross-route URL, and `pushState` to a different pathname updates the URL bar without rendering the route.

- [ ] **Step 1: Write the failing test**

```tsx
  it("navigates cross-route CTAs with a real Link, not pushState", () => {
    // /settings is a DIFFERENT Next route. pushState would change the URL bar
    // while leaving the chat shell mounted — the upgrade CTA would silently do
    // nothing. Only in-shell ?panel swaps may use pushState.
    const push = vi.spyOn(window.history, "pushState").mockImplementation(() => {});
    render(<ChatErrorCard code="trial_daily_cap" message="" />);
    const link = screen.getByRole("link", { name: "Upgrade to keep going" });
    expect(link).toHaveAttribute("href", "/settings?panel=billing");
    fireEvent.click(link, { button: 0 });
    expect(push).not.toHaveBeenCalled();
  });

  it("keeps pushState for in-shell CTAs", () => {
    const push = vi.spyOn(window.history, "pushState").mockImplementation(() => {});
    render(<ChatErrorCard code="out_of_credits" message="" />);
    fireEvent.click(screen.getByRole("link", { name: "Top up now" }), { button: 0 });
    expect(push).toHaveBeenCalledWith(null, "", "/chat?provider=bedrock_claude");
  });
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && pnpm vitest run src/__tests__/components/chat/ChatErrorCard.shellNav.test.tsx`
Expected: FAIL — `pushState` IS called for the cross-route CTA.

- [ ] **Step 3: Implement**

Add the import:

```tsx
import Link from "next/link";
```

Replace the action render block:

```tsx
            {action && (
              <Button
                asChild
                size="sm"
                variant="outline"
                className="h-7 px-3 text-xs border-[#c0392b] text-[#7a2018] hover:bg-[#fbd6d2]"
              >
                {action.crossRoute ? (
                  // Different Next route — must go through the router. A bare
                  // pushState would move the URL bar and leave the shell mounted.
                  <Link href={action.href}>{action.label}</Link>
                ) : (
                  <a href={action.href} onClick={(e) => handleShellLinkClick(e, action.href)}>
                    {action.label}
                  </a>
                )}
              </Button>
            )}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/frontend && pnpm vitest run src/__tests__/components/chat/ChatErrorCard.shellNav.test.tsx src/components/chat/__tests__/ChatErrorCard.test.tsx`
Expected: all PASS, including the pre-existing in-shell `rate_limit` test.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/chat/ChatErrorCard.tsx apps/frontend/src/__tests__/components/chat/ChatErrorCard.shellNav.test.tsx
git commit -m "fix(chat): cross-route error-card CTAs navigate instead of only changing the URL

handleShellLinkClick exists for in-shell ?panel swaps; ChatErrorCard was the
only call site passing a cross-route URL (/settings?panel=billing), so the
upgrade CTA preventDefault'd and pushState'd without rendering /settings."
```

---

### Task 7: One plan-label mapping, used everywhere

**Files:**
- Create: `apps/frontend/src/lib/planLabel.ts`
- Modify: `apps/frontend/src/components/workspace/WorkspaceSidebar.tsx:304`, `apps/frontend/src/components/chat/ChatLayout.tsx:466`, `apps/frontend/src/app/settings/page.tsx:251-256`
- Test: `apps/frontend/src/lib/__tests__/planLabel.test.ts` *(create)* — this repo has two parallel frontend test trees (issue #979); `src/lib/__tests__/` is the established location for `lib/` helpers (`approvalDisplayPath.test.ts`, `models.test.ts`, `teamsSubscription.test.ts`). Use it.

**Interfaces:**
- Produces: `planLabel(status: string | null, opts?: { expired?: boolean }): string | null`.

- [ ] **Step 1: Write the failing test**

```ts
import { describe, it, expect } from "vitest";
import { planLabel } from "../planLabel";

describe("planLabel", () => {
  it("never leaks the raw trial_local enum", () => {
    // CSS uppercases the plan badge, so an unmapped enum rendered as
    // "TRIAL_LOCAL" in the sidebar — developer-facing text in a user surface.
    expect(planLabel("trial_local")).toBe("Free tier");
  });

  it("distinguishes an elapsed free-tier window", () => {
    expect(planLabel("trial_local", { expired: true })).toBe("Free tier ended");
  });

  it("passes other statuses through for the caller's capitalize treatment", () => {
    expect(planLabel("active")).toBe("active");
    expect(planLabel("trialing")).toBe("trialing");
  });

  it("returns null for an absent status so callers can skip the chip", () => {
    expect(planLabel(null)).toBeNull();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && pnpm vitest run src/lib/__tests__/planLabel.test.ts`
Expected: FAIL — cannot resolve `@/lib/planLabel`.

- [ ] **Step 3: Implement**

```ts
/**
 * The single `subscription_status` → display-label mapping.
 *
 * The raw enum must never reach a user surface: the plan badge is
 * `text-transform: uppercase`, so `trial_local` rendered as "TRIAL_LOCAL".
 * Settings already had this mapping inline; the two chat shells did not, which
 * is exactly the drift one shared helper prevents.
 *
 * Statuses other than the free tier pass through unchanged — callers apply
 * their own `capitalize` treatment, as Settings does.
 */
export function planLabel(
  status: string | null | undefined,
  opts?: { expired?: boolean },
): string | null {
  if (!status) return null;
  if (status === "trial_local") {
    return opts?.expired ? "Free tier ended" : "Free tier";
  }
  return status;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/frontend && pnpm vitest run src/lib/__tests__/planLabel.test.ts`
Expected: PASS.

- [ ] **Step 5: Adopt it in all three call sites**

In `WorkspaceSidebar.tsx` and `ChatLayout.tsx`, add `import { planLabel } from "@/lib/planLabel";` and replace the badge line:

```tsx
          {planLabel(subscriptionStatus) && (
            <span className="plan-badge">{planLabel(subscriptionStatus)}</span>
          )}
```

In `settings/page.tsx`, replace the inline `statusChipLabel` computation with:

```tsx
  const statusChipLabel = planLabel(subscriptionStatus, { expired: isTrialExpired });
```

Leave the surrounding `textTransform: isTrialLocal ? undefined : "capitalize"` logic untouched.

- [ ] **Step 6: Run the affected suites**

Run: `cd apps/frontend && pnpm vitest run src/lib/__tests__/planLabel.test.ts src/components/chat/__tests__/ChatLayout.test.tsx`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/frontend/src/lib/planLabel.ts apps/frontend/src/lib/__tests__/planLabel.test.ts apps/frontend/src/components/workspace/WorkspaceSidebar.tsx apps/frontend/src/components/chat/ChatLayout.tsx apps/frontend/src/app/settings/page.tsx
git commit -m "fix(web): stop rendering the raw TRIAL_LOCAL enum in the shells

Settings mapped trial_local to 'Free tier'; both chat shells rendered the raw
value into an uppercased badge. One shared helper, three call sites."
```

---

### Task 8: `OutOfCreditsBanner` uses the gate's threshold

**Files:**
- Modify: `apps/frontend/src/hooks/useCredits.ts` (type + return), `apps/frontend/src/components/chat/OutOfCreditsBanner.tsx`
- Test: `apps/frontend/src/components/chat/__tests__/OutOfCreditsBanner.test.tsx` *(create if absent)*

**Interfaces:**
- Consumes: `is_exhausted` from `/billing/credits/balance` (Task 3).

- [ ] **Step 1: Write the failing test**

```tsx
// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

import { OutOfCreditsBanner } from "../OutOfCreditsBanner";

const mockCredits = vi.fn();
const mockSwr = vi.fn();
vi.mock("@/hooks/useCredits", () => ({ useCredits: () => mockCredits() }));
vi.mock("swr", () => ({ default: () => mockSwr() }));
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: vi.fn() }) }));

beforeEach(() => {
  mockSwr.mockReturnValue({ data: { provider_choice: "bedrock_claude" } });
});

describe("OutOfCreditsBanner", () => {
  it("shows when the balance is unspendable at the gate, not only at zero", () => {
    // The gate refuses paid admission at remaining <= one reservation, so a
    // $0.20 balance buys nothing. The old `> 0` check hid the banner for
    // exactly the owners already being refused.
    mockCredits.mockReturnValue({ balance: { balance_microcents: 200_000, is_exhausted: true } });
    render(<OutOfCreditsBanner />);
    expect(screen.getByText(/out of Claude credits/i)).toBeInTheDocument();
  });

  it("stays hidden on a spendable balance", () => {
    mockCredits.mockReturnValue({ balance: { balance_microcents: 5_000_000, is_exhausted: false } });
    const { container } = render(<OutOfCreditsBanner />);
    expect(container).toBeEmptyDOMElement();
  });

  it("stays hidden on an indeterminate balance", () => {
    // Metronome outage / no customer yet. A false out-of-credits banner is
    // worse than a late one.
    mockCredits.mockReturnValue({ balance: { balance_microcents: null, is_exhausted: false } });
    const { container } = render(<OutOfCreditsBanner />);
    expect(container).toBeEmptyDOMElement();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && pnpm vitest run src/components/chat/__tests__/OutOfCreditsBanner.test.tsx`
Expected: FAIL — the first case renders nothing (`balance_microcents > 0` returns null).

- [ ] **Step 3: Implement**

In `useCredits.ts`, add to the `CreditsBalance` type:

```ts
  /** Server-computed: is this balance unspendable at the bedrock-gate? The
   *  gate admits only while remaining exceeds one reservation hold, so a small
   *  positive balance still buys nothing. Computed backend-side so the reserve
   *  constant has no TypeScript copy to drift from. Absent (not-yet-redeployed
   *  backend) is treated as not exhausted. */
  is_exhausted?: boolean;
```

In `OutOfCreditsBanner.tsx`, replace the balance guard:

```tsx
  if (me?.provider_choice !== "bedrock_claude") return null;
  if (!balance || balance.is_exhausted !== true) return null;
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/frontend && pnpm vitest run src/components/chat/__tests__/OutOfCreditsBanner.test.tsx`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/hooks/useCredits.ts apps/frontend/src/components/chat/OutOfCreditsBanner.tsx apps/frontend/src/components/chat/__tests__/OutOfCreditsBanner.test.tsx
git commit -m "fix(billing): out-of-credits banner uses the gate's admission threshold"
```

---

### Task 9: Full suites and live browser verification

**Files:** none modified unless a failure is found.

- [ ] **Step 1: Run the full backend suite**

Run: `cd apps/backend && uv run pytest tests/ -q`
Expected: PASS. Backend CI scope is `tests/`, not `tests/unit/` — chaos and integration tests live outside `unit/`.

- [ ] **Step 2: Run the full frontend suite and typecheck**

Run: `cd apps/frontend && pnpm test && pnpm run lint && pnpm exec tsc --noEmit`
Expected: PASS. `tsc` typechecks test files too, so a mistyped mock fails the build.

- [ ] **Step 3: Verify the subscribe path live in a browser**

This is the user's stated acceptance criterion and must be confirmed by running, not by test.

1. Start the frontend against dev: `cd apps/frontend && pnpm run dev`
2. Sign in as a `trial_local` account on the desktop `WorkspaceShell` (not mobile `ChatLayout`).
3. Render the wall card without waiting for a real cap. In the browser console
   on `/chat`, the gateway's frontend socket dispatches on the parsed frame, so
   inject a terminal error the same shape `_transform_agent_event` forwards:

   ```js
   // Adjust the agent_id to a real one from the sidebar.
   window.dispatchEvent(new MessageEvent("message", {
     data: JSON.stringify({ type: "error", code: "trial_daily_cap", message: "", agent_id: "<agent_id>" }),
   }));
   ```

   If `useGateway` does not surface a window-level hook, fall back to flipping
   the prod billing row for a scratch dev account:
   `aws dynamodb update-item --table-name isol8-dev-billing-accounts --key '{"owner_id":{"S":"<owner>"}}' --update-expression 'SET trial_daily_spend_microcents.#d = :v' --expression-attribute-names '{"#d":"<UTC-date>"}' --expression-attribute-values '{":v":{"N":"2900000"}}' --profile isol8-admin`
   then send a message. **Dev only — never mutate a prod billing row to test UI.**
4. Confirm: title "Daily free limit reached", description reads exactly "You've used today's free usage. Resets at midnight UTC." (no countdown — dropped in review, see Task 5), no dollar figure, no Retry button.
5. Click **Upgrade to keep going**. Confirm the browser actually renders `/settings?panel=billing` — the Billing panel, not the chat shell with a changed URL.
6. Confirm the card there reads "Isol8 — Free tier" with a **Subscribe — $50/mo** button.
7. Click it and confirm redirection to a Stripe Checkout URL. **Do not complete payment.**
8. Screenshot steps 4, 5 and 7 for the PR description.

- [ ] **Step 4: File the deferred issue**

```bash
gh issue create --repo Isol8AI/isol8 \
  --title "Paperclip routine auto-pause survives the budget reset" \
  --body "A budget-refused wake parks the run, but the routine circuit breaker still counts it toward consecutiveFailureCount, so enough refusals set autoPauseReason=\"consecutive_failures\" and the routine stays paused after the cap resets at UTC midnight — needing a manual resume.

Flagged in #1426's closing note as needing its own issue. Out of scope for the budget-exhaustion UX work (docs/superpowers/specs/2026-07-26-budget-exhaustion-ux-design.md), which fixes the chat-side wall; the daily counters are date-keyed maps and reset with no worker, so chat genuinely recovers at midnight while automation may not.

Arming Paperclip's own pre-run-insert fence (budgets.getInvocationBlock via companies.status) is the durable fix."
```

- [ ] **Step 5: Commit any fixes and open the PR**

```bash
git push -u origin feat/budget-exhaustion-ux
gh pr create --repo Isol8AI/isol8 --fill
```

---

## Deferred (not in this plan)

**Paperclip routine auto-pause after budget refusals** — filed in Task 9 Step 4. A distinct mechanism in the Paperclip fork; cannot change the wall card designed here.
