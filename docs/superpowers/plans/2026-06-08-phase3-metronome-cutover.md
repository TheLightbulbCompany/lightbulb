# Metronome Cutover + DDB-Ledger Deletion (Phase 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

---

## ⛔ CRITICAL MERGE GATE — READ FIRST, DO NOT MERGE WITHOUT IT ⛔

**Merging this PR makes Metronome the ONLY ledger. There is NO DDB fallback after Phase 3.** This plan flips `LEDGER_BACKEND` default to `"metronome"`, flips `METER_IS_LIVE` default to `True` (the AWS invocation-log meter becomes the **sole biller**), deletes `core/services/credit_ledger.py`, deletes `core/billing/ddb_ledger_backend.py`, and drops the `credits` + `credit-transactions` DDB tables. **This is the point of no return.**

**The unit tests pass because they mock Metronome (httpx) + DDB (moto). A real production deploy without a live Metronome account configured would break billing AND credit gating entirely** — `get_ledger().get_balance` (the chat/provision gate) and `get_ledger().deplete` (the meter) would both hit an unconfigured Metronome and fail closed or error.

**This PR is "ready, DO NOT MERGE" until ALL of the following are true (operator-confirmed, not test-confirmed):**

1. **A PROD Metronome account is provisioned** (sales-gated — the public docs surface only a sandbox; prod requires the sales engagement, spec §4 decision 2). The prod `isol8/prod/metronome_api_key` secret holds a **real prod token**, and `METRONOME_CREDITS_CONTRACT_ID` / `METRONOME_CREDITS_PRODUCT_ID` are set per-env from the Metronome dashboard.
2. **The dev sandbox cutover is validated end-to-end** — flip dev to `LEDGER_BACKEND=metronome` + `METER_IS_LIVE=true` against the sandbox token and confirm: meter `deplete` → fast-gate decrement → chat gate blocks at ≤0 → low-balance webhook re-syncs → Stripe top-up funds a payment-gated commit → fast-gate re-arms → next message unblocks (spec §3.5 Phase 2 validation list).
3. **The balance backfill (Task 12) has been run against prod** (dry-run reviewed, then `--apply`) BEFORE the tables are dropped, so every owner's DDB balance is granted as an equal Metronome prepaid credit. Running the backfill AFTER the table drop is impossible — the source data is gone.
   - **Run it with admin creds, NOT ECS-exec on the post-deploy task role.** Task 11 removes the `credits` table `grantReadWriteData` from the backend task role, so once the table-drop CDK change deploys the running task can no longer read the source table. Sequence: run the backfill (admin creds) → merge → deploy. Run order is enforced by the sequencing line below.
   - **Negative balances are NOT carried over.** The legacy ledger's deduct path could leave an owner with a negative (overdraft) balance. There is no negative prepaid credit in Metronome, so that debt is **intentionally forgiven** at cutover. The dry-run summary surfaces `negative_skipped` + `negative_microcents` so the operator can review the (small) forgiven amount before `--apply`. This is a deliberate dropped responsibility, not an oversight.
   - **Idempotency is marker-before-grant.** The script claims the `backfill_done_at` marker with a conditional `attribute_not_exists` write BEFORE granting, and releases it if the grant fails — so the marker (not Metronome) is the at-most-once gate. Metronome's `/contracts/edit add_commits` does NOT dedup on commit metadata; the `backfill_key` is traceability only. A re-run after a crash is safe.

**Sequencing of the real-world cutover (NOT the code commits — those keep the suite green throughout):**
deploy dev → validate sandbox → engage Metronome sales → provision prod account → set prod secrets/contract/product → **run backfill (Task 12) on prod** → merge this PR (flips defaults + deletes the DDB ledger) → deploy prod → confirm billing/gating live → (a follow-up deletes the now-empty `credits`/`credit-transactions` tables once the backfill is proven and a retention window has passed; Task 11 prepares the CDK removal but the table `removalPolicy` honors the per-env `config.removalPolicy`).

---

**Goal:** Cut over to Metronome as the sole ledger and delete the home-grown DDB credit ledger. Flip the `LEDGER_BACKEND` default to `"metronome"`; flip `METER_IS_LIVE` default to `True` so the AWS invocation-log meter's `get_ledger().deplete()` is the single biller; migrate the four remaining direct `credit_ledger` callers off the legacy module onto the `LedgerBackend` interface (`get_balance`) or the Phase-2 Metronome funding/auto-recharge helpers (`top_up`, `set_auto_reload`); delete the legacy `credit_ledger.py` + the interim `ddb_ledger_backend.py` + their tests; remove the `"ddb"` branch from `get_ledger()`; drop the `credits` + `credit-transactions` CDK tables + their grants/outputs; and ship a one-time, idempotent balance-backfill script that grants every owner's surviving DDB balance as a Metronome prepaid credit. After Phase 3 **no code path imports `credit_ledger`** and Metronome is the only money store.

**Architecture:** Phases 1 (DDB seam + meter + enforcement) and 2 (Metronome adapter) are ALREADY MERGED on this branch. The `LedgerBackend` Protocol (`deplete(event)` + `get_balance(owner_id, *, consistent)`) and `get_ledger()` factory are the swap seam. Phase 3 flips the factory default to Metronome, deletes the `"ddb"` branch + the `DdbLedgerBackend` import, and removes `credit_ledger.py`. The four legacy callers are migrated by kind: `routers/billing.py` balance read → `get_ledger().get_balance` (the Metronome backend's `get_balance` already reads the local fast-gate cache, so the synchronous hot path stays off Metronome's no-SLA balance API — verified at `metronome_ledger_backend.py:83-100`); `routers/billing.py` auto-reload → a new `metronome_funding.set_auto_recharge` helper that configures Metronome's `credit_balance_threshold_configuration`; `stripe_webhook_handlers.py` top-up → the existing `metronome_funding.fund_top_up` (the DDB `top_up` shadow-write is removed, leaving only the Metronome funding); `connection_pool.py` legacy deduct → **removed entirely** (the meter `deplete()` is the only biller, so the `if not settings.METER_IS_LIVE: _maybe_deduct_credits(...)` block + the `_maybe_deduct_credits` method are deleted).

**INTERFACE DECISION (documented, load-bearing):** The `LedgerBackend` Protocol stays exactly `deplete(event)` + `get_balance(owner_id, *, consistent)` — it is genuinely the **per-event ledger seam** (the meter's hot path). `top_up` / `set_auto_reload` / `adjustment` are **administrative / webhook-time funding operations**, not per-event ledger operations, so they are **NOT added to the Protocol**. Instead the two surviving funding callers route **directly to the Phase-2 Metronome funding helpers** (`metronome_funding.fund_top_up`, and a new `metronome_funding.set_auto_recharge`). Rationale: extending the Protocol would force `DdbLedgerBackend` (being deleted) and every future backend to implement Stripe-gated commits + Metronome auto-recharge config — funding is a Metronome-specific money-in operation, not a generic ledger contract. `adjustment` (the operator-only manual credit) has **zero callers** (verified: `grep -rn "credit_ledger.adjustment\|\.adjustment(" apps/backend` returns only the definition) so it is simply deleted with the module — no migration target needed. **After this phase, no caller imports `credit_ledger`** (the merge gate verifies via grep).

**Tech Stack:** Python 3.12, `httpx>=0.28` (mocked via `unittest.mock` patching of `httpx.AsyncClient` — `respx==0.20.2` is known-incompatible with httpx 0.28, see `tests/unit/services/test_posthog_admin.py`), `boto3` + `moto` for DDB (the backfill source table is moto-mocked in its test), `pytest` + `pytest-asyncio==0.23.5`. CDK (TypeScript) for the table removal. **No live Metronome calls anywhere** — every HTTP request shape is asserted against a mock.

---

## Context for the implementer (read before Task 1)

This is **Phase 3** of the Bedrock metering remediation. **Phases 1 + 2 are already merged on this branch** (`git log --oneline` shows the Phase-2 commits `b2f0520c`..`41673a39`). Read these before starting — Phase 3 deletes/migrates against their exact shapes:

- `apps/backend/core/billing/ledger_interface.py` — the `LedgerBackend` Protocol + `get_ledger()` factory. Phase 3 flips its default and deletes the `"ddb"` branch.
- `apps/backend/core/billing/metronome_ledger_backend.py` — the Metronome backend. Its `get_balance` reads the **local fast-gate cache** (lines 83-100), NOT Metronome's no-SLA `getNetBalance`, so migrating the synchronous `routers/billing.py` balance read onto the interface keeps it off the hot path.
- `apps/backend/core/billing/metronome_funding.py` — Phase-2 `fund_top_up` (Stripe-payment-gated commit + fast-gate re-arm). Phase 3 adds `set_auto_recharge` here and wires `fund_top_up` into the Stripe webhook as the SOLE funding call.
- `apps/backend/core/billing/metronome_client.py` — the four HTTP shapes. Phase 3 adds one method: `set_credit_balance_threshold` (Metronome `credit_balance_threshold_configuration` auto-recharge).
- `apps/backend/core/billing/metronome_provisioning.py` — `ensure_metronome_customer(owner_id, stripe_customer_id)`. The backfill (Task 12) calls this so the Metronome customer exists before granting credits.
- `apps/backend/core/services/credit_ledger.py` + `apps/backend/core/billing/ddb_ledger_backend.py` — **the two modules being deleted.** Read their `top_up` / `set_auto_reload` / `deduct` signatures so the migrations preserve behavior.

The **authoritative Metronome contract** is `docs/superpowers/research/2026-06-07-metronome-ingest-contract.md`; the **migration spec** is `docs/superpowers/specs/2026-06-07-bedrock-metering-enforcement-metronome-design.md` §3.5 (verbatim: *"Existing DDB `credit_ledger` → deprecate → delete once the Metronome adapter is live. Backfill existing balances into Metronome prepaid credits (idempotency-keyed per owner; do not grandfather two money systems)."*). Key facts (grounded):

- **Auto-recharge** (research §2): Metronome `credit_balance_threshold_configuration` carries `threshold_amount` (recharge trigger), `recharge_to_amount` (target balance), `is_enabled`. Minimums: threshold ≥ $5, recharge_to ≥ threshold + $10. Metronome runs recharge actions asynchronously based on usage. This is the migration target for the legacy `set_auto_reload`.
- **Fund / top-up** (research §2): `POST /v1/contracts/edit` with `add_commits`, each `payment_gate_config:{payment_gate_type:"STRIPE"}` — already implemented in Phase-2 `fund_top_up`.
- **Credit grant / backfill** (research §2): a prepaid commit (`add_commits`). For the one-time balance backfill we grant a **free/promo-style commit WITHOUT a payment gate** (the owner already paid for that balance in the old system; re-gating on a new Stripe payment would double-charge). The backfill keys on a deterministic `transaction_id`-style commit metadata tag so re-running does NOT double-grant.
- **Money unit:** microcents (int), 1 cent = 10_000 microcents; 1 dollar = 1_000_000 microcents (the single `_MICROCENTS_PER_DOLLAR = 1_000_000` constant Phase 2 uses).

**The four migrations (verified with grep — `grep -rn "credit_ledger" apps/backend --include="*.py"`):**
1. `routers/billing.py:562` — `credit_ledger.get_balance(...)` → `get_ledger().get_balance(...)` (the interface).
2. `routers/billing.py:621` — `credit_ledger.set_auto_reload(...)` → `metronome_funding.set_auto_recharge(...)` (new helper).
3. `core/services/stripe_webhook_handlers.py:315` — `credit_ledger.top_up(...)` removed; only the existing `metronome_funding.fund_top_up(...)` block remains (no longer gated on `LEDGER_BACKEND=="metronome"` — Metronome is now the only backend).
4. `core/gateway/connection_pool.py:1258` — `credit_ledger.deduct(...)` inside `_maybe_deduct_credits`, called from the `if not settings.METER_IS_LIVE:` block at line 1134 — **the whole block + the `_maybe_deduct_credits` method deleted** (the meter at `routers/internal_usage.py:151` is the sole biller).

**Stale prose-only mentions (update wording, NOT calls):**
- `core/billing/bedrock_pricing.py:155` docstring — "Markup is applied separately by `credit_ledger.deduct`." → reword to reference the meter / ledger interface.
- `scripts/reconcile_bedrock_vs_ledger.py:79` comment — "applied only later in `credit_ledger.deduct`." → reword.
- `core/services/usage_service.py:58` docstring — "credit ledger (core/services/credit_ledger.py) handles authoritative billing" → reword to reference `get_ledger()` / Metronome.

**`METER_IS_LIVE` interplay (load-bearing — do NOT skip):** the meter endpoint `routers/internal_usage.py:146` only depletes when `settings.METER_IS_LIVE` is True; otherwise it is shadow-only. The connection_pool legacy deduct fires only when `METER_IS_LIVE` is False. Today both default False (legacy deduct is the live biller). Phase 3 **deletes the legacy deduct** — so to avoid billing going dark, Phase 3 **flips `METER_IS_LIVE` default to True** in the same pass (the meter becomes the sole biller). Both changes land together (Task 7) so the suite + real billing stay coherent.

**Sequencing principle (the suite stays GREEN at every commit):** migrate the callers FIRST (Tasks 1-7, each leaving the suite green by mocking Metronome), THEN flip the factory default + delete the modules + delete their tests (Tasks 8-10), THEN remove the CDK tables (Task 11), THEN add the one-time backfill script (Task 12), THEN full-suite + grep verification (Task 13). Deleting a module before its callers are migrated would break import-time and is forbidden.

**Run all commands from `apps/backend/` unless noted. Use `uv run` for Python.** Branch `phase-3-metronome-cutover` is already checked out — do NOT push, do NOT switch branches. The pre-commit hook runs `ruff format`/`check` on staged backend files; run `uv run ruff format` + `uv run ruff check --fix` before every commit. Shell: NO `rg` (use `grep --include` or the Read tool).

---

## File Structure

| File | Responsibility |
|---|---|
| `apps/backend/core/billing/metronome_client.py` (Modify) | Add `set_credit_balance_threshold(...)` — the Metronome auto-recharge HTTP shape (`POST /v1/contracts/edit` with `credit_balance_threshold_configuration`). |
| `apps/backend/core/billing/metronome_funding.py` (Modify) | Add `set_auto_recharge(owner_id, *, enabled, threshold_cents, amount_cents)` — resolves the Metronome customer + contract, calls `set_credit_balance_threshold`. The `top_up` migration target already exists (`fund_top_up`). |
| `apps/backend/routers/billing.py` (Modify) | Migrate `credits/balance` → `get_ledger().get_balance`; migrate `credits/auto_reload` → `metronome_funding.set_auto_recharge`; drop the `credit_ledger` import. |
| `apps/backend/core/services/stripe_webhook_handlers.py` (Modify) | Drop the `credit_ledger.top_up` shadow-write + the `credit_ledger` import; keep `metronome_funding.fund_top_up` as the sole funding call (un-gate it — Metronome is the only backend now). |
| `apps/backend/core/gateway/connection_pool.py` (Modify) | Delete the `if not settings.METER_IS_LIVE: _maybe_deduct_credits(...)` block AND the `_maybe_deduct_credits` method (the meter `deplete()` is the sole biller). |
| `apps/backend/core/config.py` (Modify) | Flip `LEDGER_BACKEND` default to `"metronome"`; flip `METER_IS_LIVE` default to `True`. Remove the now-orphan `CREDITS_TABLE` / `CREDIT_TRANSACTIONS_TABLE` settings. |
| `apps/backend/core/billing/ledger_interface.py` (Modify) | Flip the factory default to `"metronome"`; delete the `"ddb"` branch + the `DdbLedgerBackend` lazy import; update the docstring. |
| `apps/backend/core/billing/bedrock_pricing.py` (Modify) | Reword the `credit_ledger.deduct` docstring mention. |
| `apps/backend/core/services/usage_service.py` (Modify) | Reword the `credit_ledger` docstring mention. |
| `apps/backend/scripts/reconcile_bedrock_vs_ledger.py` (Modify) | Reword the `credit_ledger.deduct` comment. |
| `apps/backend/scripts/backfill_balances_to_metronome.py` (Create) | One-time idempotent migration: read every owner balance from the `credits` DDB table, ensure a Metronome customer, grant an equal prepaid credit (no payment gate), keyed so re-running does not double-grant. Dry-run by default; `--apply` to execute. |
| `apps/infra/lib/stacks/database-stack.ts` (Modify) | Remove `creditsTable` + `creditTransactionsTable` (definitions, `public readonly` fields, CfnOutputs). |
| `apps/infra/lib/stacks/service-stack.ts` (Modify) | Remove the `creditsTable`/`creditTransactionsTable` props, grants, and `CREDITS_TABLE`/`CREDIT_TRANSACTIONS_TABLE` env. |
| `apps/infra/lib/isol8-stage.ts` + `apps/infra/lib/local-stage.ts` (Modify) | Remove the `creditsTable`/`creditTransactionsTable` wiring passed into ServiceStack. |
| `apps/backend/tests/unit/billing/test_metronome_client.py` (Modify) | Add a test for `set_credit_balance_threshold` request shape. |
| `apps/backend/tests/unit/billing/test_metronome_funding.py` (Modify) | Add tests for `set_auto_recharge` (request shape + minimums + disable path). |
| `apps/backend/tests/unit/routers/test_billing_credits.py` (Modify) | Re-point the balance + auto-reload tests off `credit_ledger` onto `get_ledger` / `metronome_funding`. |
| `apps/backend/tests/unit/services/test_stripe_webhook_metronome_funding.py` (Modify) | Drop the `credit_ledger.top_up` patches; assert funding is unconditional (no `LEDGER_BACKEND` gate). |
| `apps/backend/tests/unit/gateway/test_connection_pool_credits.py` (Modify) | Delete the Task-6 `METER_IS_LIVE`-gated legacy-deduct tests (lines 265-347); keep the `gate_chat` read tests (they read `get_ledger().get_balance`, still valid). |
| `apps/backend/tests/unit/billing/test_ledger_interface.py` (Modify) | Re-point the default/`ddb`-branch tests to the new metronome default; delete the `DdbLedgerBackend` tests. |
| `apps/backend/tests/unit/routers/test_internal_usage.py` (Modify) | Migrate the four live-deplete `TestDeplete` tests off the deleted DDB backend — patch `get_ledger` with a stub returning `DepleteResult` (Task 8 Step 5). |
| `apps/backend/tests/unit/billing/test_metronome_settings.py` (Modify) | Flip the `LEDGER_BACKEND == "ddb"` default assertion to `"metronome"` (Task 7). |
| `apps/backend/tests/unit/test_meter_is_live_setting.py` (Modify) | Flip the default-False assertion to default-True. |
| `apps/backend/tests/unit/scripts/test_backfill_balances_to_metronome.py` (Create) | moto DDB + mocked Metronome HTTP; idempotent re-run asserts no double-grant. (Dir already exists with `__init__.py` — no package scaffold needed.) |

### DELETIONS

| File | Why |
|---|---|
| `apps/backend/core/services/credit_ledger.py` (DELETE) | The legacy hand-rolled DDB ledger. No caller imports it after Tasks 1-7. |
| `apps/backend/core/billing/ddb_ledger_backend.py` (DELETE) | The Phase-1 interim `LedgerBackend`. The factory's `"ddb"` branch is removed in Task 8. |
| `apps/backend/tests/unit/services/test_credit_ledger.py` (DELETE) | Tests the deleted module (11 tests). |
| `apps/backend/tests/unit/billing/test_ddb_ledger_backend.py` (DELETE) | Tests the deleted backend (9 tests). |
| `apps/backend/tests/unit/gateway/test_connection_pool_credits.py` — Task-6 block ONLY (lines 265-347) | The `_maybe_deduct_credits` / `METER_IS_LIVE`-gated legacy deduct is deleted; its tests go. The file itself survives (its `gate_chat` read tests stay). |
| CDK `creditsTable` + `creditTransactionsTable` (definitions ~215/228, CfnOutputs ~352-358, `public readonly` ~34-35) | The DDB ledger tables. Removed in Task 11 after the backfill drains them. |

---

### Task 1: `metronome_client.set_credit_balance_threshold` — the auto-recharge HTTP shape

**Files:**
- Modify: `apps/backend/core/billing/metronome_client.py`
- Modify: `apps/backend/tests/unit/billing/test_metronome_client.py`

**Design (read before coding):**
Metronome auto-recharge is configured via `credit_balance_threshold_configuration` on the customer's contract (research §2): `threshold_amount` (recharge trigger, dollars), `recharge_to_amount` (target balance, dollars), `is_enabled`. It is set through the same `POST /v1/contracts/edit` endpoint as commits. We add one thin client method that owns the request shape — no business logic, mirroring the existing `edit_contract_add_commits`. The dollar amounts and the threshold-minimum validation are the funding helper's job (Task 2); the client just marshals.

- [ ] **Step 1: Write the failing test**

Append to `apps/backend/tests/unit/billing/test_metronome_client.py`:

```python
@pytest.mark.asyncio
async def test_set_credit_balance_threshold_shape():
    client, fake = _client_with(_resp(200, {}))
    await client.set_credit_balance_threshold(
        metronome_customer_id="cust-uuid-1",
        contract_id="contract-1",
        is_enabled=True,
        threshold_amount=5.0,
        recharge_to_amount=25.0,
    )

    args, kwargs = fake.post.call_args
    assert args[0] == "/contracts/edit"
    assert kwargs["json"] == {
        "customer_id": "cust-uuid-1",
        "contract_id": "contract-1",
        "credit_balance_threshold_configuration": {
            "is_enabled": True,
            "threshold_amount": 5.0,
            "recharge_to_amount": 25.0,
        },
    }


@pytest.mark.asyncio
async def test_set_credit_balance_threshold_disable_omits_amounts():
    client, fake = _client_with(_resp(200, {}))
    await client.set_credit_balance_threshold(
        metronome_customer_id="cust-uuid-1",
        contract_id="contract-1",
        is_enabled=False,
    )
    args, kwargs = fake.post.call_args
    assert args[0] == "/contracts/edit"
    assert kwargs["json"] == {
        "customer_id": "cust-uuid-1",
        "contract_id": "contract-1",
        "credit_balance_threshold_configuration": {"is_enabled": False},
    }
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/unit/billing/test_metronome_client.py -k threshold -v`
Expected: FAIL — `AttributeError: 'MetronomeClient' object has no attribute 'set_credit_balance_threshold'`

- [ ] **Step 3: Add the method**

In `apps/backend/core/billing/metronome_client.py`, immediately after the `edit_contract_add_commits` method, add:

```python
    async def set_credit_balance_threshold(
        self,
        *,
        metronome_customer_id: str,
        contract_id: str,
        is_enabled: bool,
        threshold_amount: float | None = None,
        recharge_to_amount: float | None = None,
    ) -> None:
        """POST /v1/contracts/edit — configure auto-recharge via
        credit_balance_threshold_configuration (research §2). When enabling,
        Metronome auto-funds a commit up to recharge_to_amount once the balance
        crosses below threshold_amount (it runs the recharge asynchronously
        based on usage). When disabling, the amounts are omitted."""
        config: dict[str, object] = {"is_enabled": is_enabled}
        if is_enabled:
            config["threshold_amount"] = threshold_amount
            config["recharge_to_amount"] = recharge_to_amount
        await self._post(
            "/contracts/edit",
            {
                "customer_id": metronome_customer_id,
                "contract_id": contract_id,
                "credit_balance_threshold_configuration": config,
            },
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/billing/test_metronome_client.py -v`
Expected: PASS (the existing 6 client tests + the 2 new threshold tests = 8 passed).

- [ ] **Step 5: Commit**

```bash
uv run ruff format core/billing/metronome_client.py tests/unit/billing/test_metronome_client.py
uv run ruff check --fix core/billing/metronome_client.py tests/unit/billing/test_metronome_client.py
git add apps/backend/core/billing/metronome_client.py apps/backend/tests/unit/billing/test_metronome_client.py
git commit -m "feat(billing): add Metronome set_credit_balance_threshold (auto-recharge HTTP shape)"
```

---

### Task 2: `metronome_funding.set_auto_recharge` — the auto-reload migration target

**Files:**
- Modify: `apps/backend/core/billing/metronome_funding.py`
- Modify: `apps/backend/tests/unit/billing/test_metronome_funding.py`

**Design (read before coding):**
`set_auto_recharge(owner_id, *, enabled, threshold_cents, amount_cents)` is the Metronome replacement for the legacy `credit_ledger.set_auto_reload`. It resolves the Metronome customer id from the fast-gate row (same `_customer_id_for` helper Phase-2 funding uses) and the contract id from `settings.METRONOME_CREDITS_CONTRACT_ID`, converts cents → dollars, enforces Metronome's documented minimums (research §2: threshold ≥ $5, recharge_to ≥ threshold + $10), and calls `MetronomeClient.set_credit_balance_threshold`. **Mapping note:** the legacy `set_auto_reload` took `threshold_cents` + `amount_cents` (the *recharge increment*). Metronome's `recharge_to_amount` is the *target balance*, not an increment — so `recharge_to_amount = threshold + amount` (top the balance back up to threshold + the requested increment). When disabling, only `is_enabled=False` is sent. Errors raise `FundingError` (reuse the Phase-2 exception) so the router can surface a 400; unlike `fund_top_up`'s best-effort re-arm, auto-recharge config is a user-initiated settings write where a failure SHOULD surface.

- [ ] **Step 1: Write the failing tests**

Append to `apps/backend/tests/unit/billing/test_metronome_funding.py`:

```python
from core.billing.metronome_funding import set_auto_recharge  # noqa: E402


def _fake_client_recharge():
    fake = MagicMock()
    fake.set_credit_balance_threshold = AsyncMock()
    return fake


@pytest.mark.asyncio
async def test_set_auto_recharge_enable_maps_cents_to_target_balance(provisioned_table):
    fake = _fake_client_recharge()
    await set_auto_recharge(
        owner_id="org_abc",
        enabled=True,
        threshold_cents=500,  # $5.00 threshold
        amount_cents=2000,  # $20.00 recharge increment
        contract_id="contract-1",
        client=fake,
    )
    fake.set_credit_balance_threshold.assert_awaited_once()
    _, kwargs = fake.set_credit_balance_threshold.call_args
    assert kwargs["metronome_customer_id"] == "cust-uuid-1"
    assert kwargs["contract_id"] == "contract-1"
    assert kwargs["is_enabled"] is True
    assert kwargs["threshold_amount"] == 5.0
    # recharge_to = threshold ($5) + increment ($20) = $25 target balance.
    assert kwargs["recharge_to_amount"] == 25.0


@pytest.mark.asyncio
async def test_set_auto_recharge_disable_sends_only_is_enabled(provisioned_table):
    fake = _fake_client_recharge()
    await set_auto_recharge(owner_id="org_abc", enabled=False, contract_id="contract-1", client=fake)
    _, kwargs = fake.set_credit_balance_threshold.call_args
    assert kwargs["is_enabled"] is False
    assert kwargs.get("threshold_amount") is None
    assert kwargs.get("recharge_to_amount") is None


@pytest.mark.asyncio
async def test_set_auto_recharge_enable_requires_amounts(provisioned_table):
    fake = _fake_client_recharge()
    with pytest.raises(FundingError, match="required when enabling"):
        await set_auto_recharge(owner_id="org_abc", enabled=True, contract_id="contract-1", client=fake)
    fake.set_credit_balance_threshold.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_auto_recharge_enforces_threshold_minimum(provisioned_table):
    fake = _fake_client_recharge()
    with pytest.raises(FundingError, match="at least"):
        await set_auto_recharge(
            owner_id="org_abc",
            enabled=True,
            threshold_cents=100,  # $1 — below the $5 minimum
            amount_cents=2000,
            contract_id="contract-1",
            client=fake,
        )
    fake.set_credit_balance_threshold.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_auto_recharge_raises_when_customer_not_provisioned(provisioned_table):
    fake = _fake_client_recharge()
    with pytest.raises(FundingError, match="not provisioned"):
        await set_auto_recharge(
            owner_id="org_unprovisioned",
            enabled=True,
            threshold_cents=500,
            amount_cents=2000,
            contract_id="contract-1",
            client=fake,
        )
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/unit/billing/test_metronome_funding.py -k auto_recharge -v`
Expected: FAIL — `ImportError: cannot import name 'set_auto_recharge' from 'core.billing.metronome_funding'`

- [ ] **Step 3: Add the helper**

In `apps/backend/core/billing/metronome_funding.py`, add (after `fund_top_up`):

```python
# Metronome documented auto-recharge minimums (research §2): the trigger
# threshold must be at least $5, and the recharge-to target must exceed the
# threshold by at least $10.
_MIN_THRESHOLD_CENTS = 500
_MIN_RECHARGE_OVER_THRESHOLD_CENTS = 1000


async def set_auto_recharge(
    *,
    owner_id: str,
    enabled: bool,
    threshold_cents: int | None = None,
    amount_cents: int | None = None,
    contract_id: str,
    client: MetronomeClient | None = None,
) -> None:
    """Configure Metronome auto-recharge for an owner (replaces the legacy
    credit_ledger.set_auto_reload).

    Maps the legacy (threshold_cents, amount_cents-as-increment) contract onto
    Metronome's credit_balance_threshold_configuration, where recharge_to_amount
    is a TARGET balance, not an increment: recharge_to = threshold + increment.
    Enforces Metronome's documented minimums (research §2). Unlike fund_top_up's
    best-effort re-arm, this is a user-initiated settings write, so failures
    raise FundingError for the router to surface.
    """
    customer_id = _customer_id_for(owner_id)

    if not enabled:
        await _set_threshold(customer_id, contract_id, is_enabled=False, client=client)
        return

    if threshold_cents is None or amount_cents is None:
        raise FundingError("threshold_cents and amount_cents are required when enabling auto-recharge")
    if threshold_cents < _MIN_THRESHOLD_CENTS:
        raise FundingError(f"threshold must be at least ${_MIN_THRESHOLD_CENTS / 100:.0f}")
    if amount_cents < _MIN_RECHARGE_OVER_THRESHOLD_CENTS:
        raise FundingError(
            f"recharge increment must be at least ${_MIN_RECHARGE_OVER_THRESHOLD_CENTS / 100:.0f} "
            "(Metronome requires recharge_to >= threshold + $10)"
        )

    threshold_dollars = threshold_cents / 100
    recharge_to_dollars = (threshold_cents + amount_cents) / 100
    await _set_threshold(
        customer_id,
        contract_id,
        is_enabled=True,
        threshold_amount=threshold_dollars,
        recharge_to_amount=recharge_to_dollars,
        client=client,
    )


async def _set_threshold(
    customer_id: str,
    contract_id: str,
    *,
    is_enabled: bool,
    threshold_amount: float | None = None,
    recharge_to_amount: float | None = None,
    client: MetronomeClient | None = None,
) -> None:
    if client is not None:
        await client.set_credit_balance_threshold(
            metronome_customer_id=customer_id,
            contract_id=contract_id,
            is_enabled=is_enabled,
            threshold_amount=threshold_amount,
            recharge_to_amount=recharge_to_amount,
        )
        return
    async with httpx.AsyncClient(base_url=settings.METRONOME_BASE_URL) as http:
        await metronome_client.for_settings(http).set_credit_balance_threshold(
            metronome_customer_id=customer_id,
            contract_id=contract_id,
            is_enabled=is_enabled,
            threshold_amount=threshold_amount,
            recharge_to_amount=recharge_to_amount,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/billing/test_metronome_funding.py -v`
Expected: PASS (the existing 3 funding tests + the 5 new auto-recharge tests = 8 passed).

- [ ] **Step 5: Commit**

```bash
uv run ruff format core/billing/metronome_funding.py tests/unit/billing/test_metronome_funding.py
uv run ruff check --fix core/billing/metronome_funding.py tests/unit/billing/test_metronome_funding.py
git add apps/backend/core/billing/metronome_funding.py apps/backend/tests/unit/billing/test_metronome_funding.py
git commit -m "feat(billing): add metronome_funding.set_auto_recharge (auto-reload migration target)"
```

---

### Task 3: Migrate `routers/billing.py` balance read → the ledger interface

**Files:**
- Modify: `apps/backend/routers/billing.py`
- Modify: `apps/backend/tests/unit/routers/test_billing_credits.py`

**Design (read before coding):**
`get_credits_balance` (line 562) calls `credit_ledger.get_balance`. Migrate it to `get_ledger().get_balance` — the `LedgerBackend` interface. With the Metronome backend (the new default after Task 7), `get_balance` reads the local fast-gate cache (`metronome_ledger_backend.py:83-100`), so the synchronous endpoint stays off Metronome's no-SLA balance API. The legacy `credit_ledger` import in `routers/billing.py:16` is partially migrated here (auto-reload in Task 4 removes the last use, then the import is dropped in Task 4). The tests patch `routers.billing.credit_ledger.get_balance` — re-point them to patch the interface.

- [ ] **Step 1: Update the balance tests to patch the interface**

In `apps/backend/tests/unit/routers/test_billing_credits.py`, replace the three balance tests' patch targets. Change every `patch("routers.billing.credit_ledger.get_balance", ...)` to patch the ledger factory. The cleanest seam is to patch `get_ledger` to return a stub whose `get_balance` is the fake:

```python
def _ledger_stub(get_balance):
    stub = MagicMock()
    stub.get_balance = get_balance
    return stub
```

Add `from unittest.mock import MagicMock` to the imports if not present. Then:

```python
@pytest.mark.asyncio
async def test_get_balance_returns_microcents(async_client, credit_ledger_tables):
    with patch(
        "routers.billing.get_ledger",
        return_value=_ledger_stub(AsyncMock(return_value=12_345_678)),
    ):
        resp = await async_client.get("/api/v1/billing/credits/balance")
    assert resp.status_code == 200
    assert resp.json() == {
        "balance_microcents": 12_345_678,
        "balance_dollars": "12.35",
    }


@pytest.mark.asyncio
async def test_get_balance_org_member_reads_owner_id(async_client_org_member, credit_ledger_tables):
    captured: dict = {}

    async def fake_get_balance(key, *, consistent: bool = False):
        captured["key"] = key
        return 9_876_543

    with patch("routers.billing.get_ledger", return_value=_ledger_stub(fake_get_balance)):
        resp = await async_client_org_member.get("/api/v1/billing/credits/balance")
    assert resp.status_code == 200
    assert resp.json() == {"balance_microcents": 9_876_543, "balance_dollars": "9.88"}
    assert captured["key"] == "org_test_456"


@pytest.mark.asyncio
async def test_get_balance_personal_uses_user_id(async_client, credit_ledger_tables):
    captured: dict = {}

    async def fake_get_balance(key, *, consistent: bool = False):
        captured["key"] = key
        return 1_000_000

    with patch("routers.billing.get_ledger", return_value=_ledger_stub(fake_get_balance)):
        resp = await async_client.get("/api/v1/billing/credits/balance")
    assert resp.status_code == 200
    assert captured["key"] == "user_test_123"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/routers/test_billing_credits.py -k get_balance -v`
Expected: FAIL — `AttributeError: <module 'routers.billing'> does not have the attribute 'get_ledger'` (the router does not import `get_ledger` yet).

- [ ] **Step 3: Migrate the endpoint**

In `apps/backend/routers/billing.py`, add the import near the other `core.billing` imports (line 25 area, `from core.billing.bedrock_pricing import get_all_rates`):

```python
from core.billing.ledger_interface import get_ledger
```

Then change `get_credits_balance` (line 562):

```python
    balance_uc = await get_ledger().get_balance(resolve_owner_id(ctx))
```

(Was `await credit_ledger.get_balance(resolve_owner_id(ctx))`.)

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/unit/routers/test_billing_credits.py -k get_balance -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
uv run ruff format routers/billing.py tests/unit/routers/test_billing_credits.py
uv run ruff check --fix routers/billing.py tests/unit/routers/test_billing_credits.py
git add apps/backend/routers/billing.py apps/backend/tests/unit/routers/test_billing_credits.py
git commit -m "refactor(billing): read credit balance via the ledger interface, not credit_ledger"
```

---

### Task 4: Migrate `routers/billing.py` auto-reload → Metronome auto-recharge; drop the `credit_ledger` import

**Files:**
- Modify: `apps/backend/routers/billing.py`
- Modify: `apps/backend/tests/unit/routers/test_billing_credits.py`

**Design (read before coding):**
`set_auto_reload` (line 621) calls `credit_ledger.set_auto_reload`. Migrate it to `metronome_funding.set_auto_recharge(owner_id, enabled, threshold_cents, amount_cents, contract_id=settings.METRONOME_CREDITS_CONTRACT_ID)`. The helper raises `FundingError` on bad input / unprovisioned customer; map it to a 400 (mirroring the existing `BillingServiceError` → 400 pattern in `top_up_credits`). This removes the LAST `credit_ledger` use in `routers/billing.py`, so drop the import (`from core.services import clerk_admin, credit_ledger` → `from core.services import clerk_admin`).

- [ ] **Step 1: Update the auto-reload tests**

In `apps/backend/tests/unit/routers/test_billing_credits.py`, replace the two auto-reload tests' patch targets (`patch("routers.billing.credit_ledger.set_auto_reload", ...)`) with `metronome_funding.set_auto_recharge`:

```python
@pytest.mark.asyncio
async def test_set_auto_reload_persists(async_client, credit_ledger_tables):
    with patch(
        "routers.billing.metronome_funding.set_auto_recharge", new=AsyncMock()
    ) as mock_set:
        resp = await async_client.put(
            "/api/v1/billing/credits/auto_reload",
            json={"enabled": True, "threshold_cents": 1000, "amount_cents": 2000},
        )
    assert resp.status_code == 200
    mock_set.assert_awaited_once()
    _, kwargs = mock_set.call_args
    assert kwargs["enabled"] is True
    assert kwargs["threshold_cents"] == 1000
    assert kwargs["amount_cents"] == 2000


@pytest.mark.asyncio
async def test_set_auto_reload_org_uses_owner_id(async_client_org_admin, credit_ledger_tables):
    captured: dict = {}

    async def fake_set(*, owner_id, **kwargs):
        captured["owner_id"] = owner_id

    with patch("routers.billing.metronome_funding.set_auto_recharge", new=fake_set):
        resp = await async_client_org_admin.put(
            "/api/v1/billing/credits/auto_reload",
            json={"enabled": True, "threshold_cents": 1000, "amount_cents": 2000},
        )
    assert resp.status_code == 200
    assert captured["owner_id"] == "org_test_456"
```

(If the existing `test_set_auto_reload_org_uses_owner_id` asserted a different owner id or positional-arg shape, match the surviving fixture's owner id and the keyword-arg signature `set_auto_recharge(*, owner_id=...)`.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/routers/test_billing_credits.py -k auto_reload -v`
Expected: FAIL — `AttributeError: <module 'routers.billing'> does not have the attribute 'metronome_funding'`.

- [ ] **Step 3: Migrate the endpoint + drop the import**

In `apps/backend/routers/billing.py`:

1. Change the import line 16 from `from core.services import clerk_admin, credit_ledger` to `from core.services import clerk_admin`.
2. Add (near the other `core.billing` imports): `from core.billing import metronome_funding` and `from core.billing.metronome_funding import FundingError`.
3. Replace the `set_auto_reload` body (the `await credit_ledger.set_auto_reload(...)` call at line 621):

```python
    try:
        await metronome_funding.set_auto_recharge(
            owner_id=resolve_owner_id(ctx),
            enabled=body.enabled,
            threshold_cents=body.threshold_cents,
            amount_cents=body.amount_cents,
            contract_id=settings.METRONOME_CREDITS_CONTRACT_ID,
        )
    except FundingError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Return JSON (not 204) — the frontend `useApi` helper unconditionally
    # parses successful responses with .json(), so an empty body is read as
    # a parse failure and surfaces as an error in CreditsPanel.
    return {"status": "ok"}
```

- [ ] **Step 4: Run to verify they pass + confirm no `credit_ledger` left in billing.py**

Run: `uv run pytest tests/unit/routers/test_billing_credits.py -v`
Expected: PASS (all balance + auto-reload + top-up + webhook tests; the webhook tests patch `routers.billing` indirectly via the Stripe handler, untouched here).

Run: `grep -n "credit_ledger" apps/backend/routers/billing.py || echo "NO credit_ledger in billing.py"`
Expected: prints `NO credit_ledger in billing.py`.

- [ ] **Step 5: Commit**

```bash
uv run ruff format routers/billing.py tests/unit/routers/test_billing_credits.py
uv run ruff check --fix routers/billing.py tests/unit/routers/test_billing_credits.py
git add apps/backend/routers/billing.py apps/backend/tests/unit/routers/test_billing_credits.py
git commit -m "refactor(billing): route auto-reload to Metronome auto-recharge; drop credit_ledger import"
```

---

### Task 5: Migrate the Stripe top-up webhook off `credit_ledger.top_up`

**Files:**
- Modify: `apps/backend/core/services/stripe_webhook_handlers.py`
- Modify: `apps/backend/tests/unit/services/test_stripe_webhook_metronome_funding.py`

**Design (read before coding):**
`handle_checkout_session` (line 315) calls `credit_ledger.top_up` then, gated on `LEDGER_BACKEND=="metronome"`, calls `metronome_funding.fund_top_up`. Phase 3 removes the DDB `top_up` shadow-write AND the `LEDGER_BACKEND` gate — Metronome is the only backend, so a paid top-up funds a Metronome commit unconditionally. Drop the `from core.services import credit_ledger` import (line 30). Keep the best-effort try/except around `fund_top_up` (a Metronome outage must not 500 the webhook back to Stripe). The existing tests patch `core.services.credit_ledger.top_up` — drop those patches; assert `fund_top_up` is awaited regardless of `LEDGER_BACKEND`.

- [ ] **Step 1: Update the funding tests**

In `apps/backend/tests/unit/services/test_stripe_webhook_metronome_funding.py`, remove every `patch("core.services.credit_ledger.top_up", ...)` and the `LEDGER_BACKEND` monkeypatches, and assert funding fires on a paid session unconditionally:

```python
@pytest.mark.asyncio
async def test_funds_metronome_on_paid_top_up(monkeypatch):
    monkeypatch.setattr("core.config.settings.METRONOME_CREDITS_CONTRACT_ID", "contract-1")
    monkeypatch.setattr("core.config.settings.METRONOME_CREDITS_PRODUCT_ID", "prod-credits")
    with patch("core.billing.metronome_funding.fund_top_up", new=AsyncMock()) as mock_fund:
        await handle_checkout_session(_session_event(), BillingService())
    mock_fund.assert_awaited_once()
    _, kwargs = mock_fund.call_args
    assert kwargs["owner_id"] == "org_abc"
    assert kwargs["amount_microcents"] == 25_000_000  # 2500 cents * 10_000
    assert kwargs["contract_id"] == "contract-1"
    assert kwargs["product_id"] == "prod-credits"
    assert kwargs["stripe_session_id"] == "cs_test_123"


@pytest.mark.asyncio
async def test_metronome_funding_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr("core.config.settings.METRONOME_CREDITS_CONTRACT_ID", "contract-1")
    monkeypatch.setattr("core.config.settings.METRONOME_CREDITS_PRODUCT_ID", "prod-credits")
    with patch(
        "core.billing.metronome_funding.fund_top_up",
        new=AsyncMock(side_effect=RuntimeError("metronome down")),
    ):
        # Best-effort: a Metronome outage must not 500 the webhook back to Stripe.
        result = await handle_checkout_session(_session_event(), BillingService())
    assert result is None


@pytest.mark.asyncio
async def test_unpaid_session_funds_nothing(monkeypatch):
    with patch("core.billing.metronome_funding.fund_top_up", new=AsyncMock()) as mock_fund:
        await handle_checkout_session(_session_event(payment_status="unpaid"), BillingService())
    mock_fund.assert_not_awaited()
```

(Delete the old `test_funds_metronome_when_backend_metronome` / `test_does_not_fund_metronome_when_backend_ddb` pair — the backend gate no longer exists.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/services/test_stripe_webhook_metronome_funding.py -v`
Expected: FAIL — the handler still calls `credit_ledger.top_up` (now unpatched → it will try the real DDB path and error, or the `LEDGER_BACKEND` gate skips `fund_top_up`). Either way the new assertions fail.

- [ ] **Step 3: Migrate the handler**

In `apps/backend/core/services/stripe_webhook_handlers.py`:

1. Remove the import line 30: `from core.services import credit_ledger`.
2. Delete the `await credit_ledger.top_up(...)` block (lines 315-323).
3. Replace the gated metronome-funding block (lines 325-351) with an UNCONDITIONAL best-effort funding call (Metronome is the only backend now):

```python
    # Fund a Stripe-payment-gated Metronome prepaid commit from this paid
    # top-up. Metronome is the sole ledger (Phase 3), so this always runs on a
    # paid session. Best-effort — a Metronome outage must not 500 the webhook
    # back to Stripe (which would trigger redelivery storms). The next top-up
    # or a manual reconcile recovers a missed commit.
    from core.billing import metronome_funding
    from core.config import settings

    try:
        await metronome_funding.fund_top_up(
            owner_id=owner_id,
            amount_microcents=amount_microcents,
            contract_id=settings.METRONOME_CREDITS_CONTRACT_ID,
            product_id=settings.METRONOME_CREDITS_PRODUCT_ID,
            stripe_session_id=session.get("id"),
        )
    except Exception:
        put_metric("metronome.fund.error", dimensions={"source": "stripe_checkout"})
        logger.exception(
            "Metronome funding failed for owner %s session %s",
            owner_id,
            session.get("id"),
        )
```

(**LOAD-BEARING import detail:** `stripe_webhook_handlers.py` has NO module-level `settings` import — the deleted gated block had its own LOCAL `from core.config import settings as _settings` and referenced `_settings.…`. The replacement above therefore carries its OWN `from core.config import settings` (function-local, alongside the `metronome_funding` import) so the bare `settings.METRONOME_CREDITS_CONTRACT_ID` resolves — without it the handler raises `NameError: name 'settings' is not defined`. `put_metric` + `logger` ARE module-level (lines 28/33), so they need no new import. Equivalent alternative: keep the existing `from core.config import settings as _settings` line and reference `_settings.…` — pick one, just don't reference an unimported bare `settings`.)

- [ ] **Step 4: Run to verify they pass + the broader webhook suite**

Run: `uv run pytest tests/unit/services/test_stripe_webhook_metronome_funding.py -v`
Expected: PASS (3 passed).

Run: `uv run pytest tests/unit/routers/test_billing_credits.py -k "webhook or checkout or completed or payment" -v`
Expected: PASS — the existing webhook tests no longer assert a DDB `top_up` (those that patched `routers.billing.credit_ledger.top_up` must be updated; see Step 5).

- [ ] **Step 5: Reconcile the webhook tests in `test_billing_credits.py`**

`test_billing_credits.py` has several tests that patch `routers.billing.credit_ledger.top_up` to assert the DDB ledger is credited (e.g. `test_checkout_session_completed_credits_ledger` ~line 182, `test_async_payment_succeeded_credits_after_ach_settles` ~line 331). The DDB `top_up` is gone — re-point each to assert `metronome_funding.fund_top_up` is awaited instead. For a paid `checkout.session.completed` / `async_payment_succeeded`:

```python
    with patch(
        "core.billing.metronome_funding.fund_top_up", new=AsyncMock()
    ) as mock_fund:
        # ... existing event-construction + POST to /webhooks/stripe ...
    mock_fund.assert_awaited_once()
    _, kwargs = mock_fund.call_args
    assert kwargs["owner_id"] == "<owner from the test's metadata>"
    assert kwargs["amount_microcents"] == <expected cents> * 10_000
```

For the negative tests (`test_async_payment_failed_does_not_credit`, `test_checkout_session_completed_ignores_non_credit_purposes`, `test_webhook_metadata_without_owner_id_no_ops`), assert `fund_top_up.assert_not_awaited()`. Run after editing:

Run: `uv run pytest tests/unit/routers/test_billing_credits.py -v`
Expected: PASS (all tests; no test references `credit_ledger`).

Run: `grep -n "credit_ledger" apps/backend/tests/unit/routers/test_billing_credits.py || echo "NO credit_ledger refs"`
Expected: prints `NO credit_ledger refs` (the fixture name `credit_ledger_tables` may remain as a fixture *name* — that's fine, it provisions moto tables; only the `credit_ledger.<fn>` module references must be gone).

- [ ] **Step 6: Commit**

```bash
uv run ruff format core/services/stripe_webhook_handlers.py tests/unit/services/test_stripe_webhook_metronome_funding.py tests/unit/routers/test_billing_credits.py
uv run ruff check --fix core/services/stripe_webhook_handlers.py tests/unit/services/test_stripe_webhook_metronome_funding.py tests/unit/routers/test_billing_credits.py
git add apps/backend/core/services/stripe_webhook_handlers.py apps/backend/tests/unit/services/test_stripe_webhook_metronome_funding.py apps/backend/tests/unit/routers/test_billing_credits.py
git commit -m "refactor(billing): Stripe top-up funds Metronome only; drop credit_ledger.top_up shadow-write"
```

---

### Task 6: Delete the connection_pool legacy deduct path

**Files:**
- Modify: `apps/backend/core/gateway/connection_pool.py`
- Modify: `apps/backend/tests/unit/gateway/test_connection_pool_credits.py`

**Design (read before coding):**
`connection_pool.py` has TWO credit touch-points: the **gate read** (`gate_chat` → `get_ledger().get_balance`, line 1946 — KEEP, it's already on the interface) and the **legacy deduct** (the `if not settings.METER_IS_LIVE: await self._maybe_deduct_credits(...)` block at lines 1134-1150, plus the `_maybe_deduct_credits` method at lines 1156-1272 which imports + calls `credit_ledger.deduct`). The meter endpoint (`routers/internal_usage.py:151`) is the sole biller once `METER_IS_LIVE` is True (flipped to default in Task 7). DELETE the gated block + the entire `_maybe_deduct_credits` method. The Task-6 tests in `test_connection_pool_credits.py` (lines 265-347) assert the legacy deduct fires/skips on the flag and that the method is retained — delete them. The `gate_chat` read tests (card3 zero balance blocked, etc.) stay.

- [ ] **Step 1: Delete the legacy-deduct tests**

In `apps/backend/tests/unit/gateway/test_connection_pool_credits.py`, delete the entire Task-6 section starting at the comment `# --------- Task 6: METER_IS_LIVE gates the legacy deduct (cutover seam) ---------` (line 265) through the end of `test_maybe_deduct_credits_method_retained_as_flag_gated_safety_net` (the `assert hasattr(GatewayConnection, "_maybe_deduct_credits")` test, ~line 347). These three tests (`test_meter_live_skips_legacy_deduct_keeps_analytics`, `test_meter_off_runs_legacy_deduct_keeps_analytics`, `test_maybe_deduct_credits_method_retained_as_flag_gated_safety_net`) assert behavior of the deleted path.

- [ ] **Step 2: Delete the deduct call site + the method in connection_pool.py**

In `apps/backend/core/gateway/connection_pool.py`:

1. Delete the entire `# Single-deduct cutover seam (METER_IS_LIVE...)` comment block + the `if not settings.METER_IS_LIVE:` try/except (lines 1113-1150). The surrounding `try:` for `_fetch_and_record_usage` (the analytics path) and its `except Exception: put_metric("chat.session_usage.fetch.error")` at line 1152 must be preserved — only the deduct block inside it goes. After deletion, the analytics `record_usage` write (lines 1090-1111) is the last thing in that `try`.
2. Delete the entire `_maybe_deduct_credits` method (from `async def _maybe_deduct_credits(` at line 1156 through its final `logger.info("Deducted credits ...")` at line 1272). This removes the only `from core.services import credit_ledger` import in the file (line 1196, which is local to the method).

Replace the deleted comment block with a one-line note so the analytics path's intent stays clear:

```python
            # Billing happens out-of-band: the AWS Bedrock invocation-log meter
            # (routers/internal_usage.py) is the sole biller — every run,
            # cache-correct, exactly-once on requestId — via get_ledger().deplete().
            # This container path only records analytics counters.
```

- [ ] **Step 3: Run the gate tests (they must still pass) + confirm no credit_ledger left**

Run: `uv run pytest tests/unit/gateway/test_connection_pool_credits.py -v`
Expected: PASS — the `gate_chat` read tests (card3/card1/card2/subscription gates) pass; no Task-6 deduct tests remain.

Run: `grep -n "credit_ledger\|_maybe_deduct_credits" apps/backend/core/gateway/connection_pool.py || echo "NO legacy deduct in connection_pool"`
Expected: prints `NO legacy deduct in connection_pool`.

- [ ] **Step 4: Confirm the app still imports**

Run: `uv run python -c "import core.gateway.connection_pool; print('connection_pool import ok')"`
Expected: prints `connection_pool import ok`.

- [ ] **Step 5: Commit**

```bash
uv run ruff format core/gateway/connection_pool.py tests/unit/gateway/test_connection_pool_credits.py
uv run ruff check --fix core/gateway/connection_pool.py tests/unit/gateway/test_connection_pool_credits.py
git add apps/backend/core/gateway/connection_pool.py apps/backend/tests/unit/gateway/test_connection_pool_credits.py
git commit -m "refactor(billing): delete connection_pool legacy credit deduct (meter is sole biller)"
```

---

### Task 7: Flip the defaults — `LEDGER_BACKEND=metronome` + `METER_IS_LIVE=True`

**Files:**
- Modify: `apps/backend/core/config.py`
- Modify: `apps/backend/tests/unit/test_meter_is_live_setting.py`
- Modify: `apps/backend/tests/unit/billing/test_metronome_settings.py` (its `LEDGER_BACKEND == "ddb"` assertion is now wrong)

**Design (read before coding):**
This is the cutover toggle. Flip `LEDGER_BACKEND` default `"ddb"` → `"metronome"` (line 103) and `METER_IS_LIVE` default `False` → `True` (line 104). Together: the meter `deplete()` is the sole biller AND it bills against Metronome. `test_meter_is_live_setting.py` asserts default-False — flip it to default-True; `test_metronome_settings.py:16` asserts `LEDGER_BACKEND == "ddb"` — flip it to `"metronome"`. **This task is what the merge gate guards** — after it, an unconfigured-Metronome deploy is broken. Keep the `CREDITS_TABLE`/`CREDIT_TRANSACTIONS_TABLE` settings for now (the backfill in Task 12 reads `CREDITS_TABLE`); they are removed in Task 11 alongside the CDK tables. Update the `METER_IS_LIVE` inline comment to note the legacy deduct is gone.

- [ ] **Step 1: Flip the meter-live setting test**

In `apps/backend/tests/unit/test_meter_is_live_setting.py`, change `test_meter_is_live_defaults_false` to assert default True and rename it:

```python
def test_meter_is_live_defaults_true():
    settings = Settings()
    assert hasattr(settings, "METER_IS_LIVE")
    # Phase 3 cutover: the AWS invocation-log meter is the sole biller, so the
    # meter depletes by default. The legacy connection_pool deduct is deleted.
    assert settings.METER_IS_LIVE is True
```

Update the module docstring's "default False so the legacy deduct stays the sole live biller" to "default True — the AWS meter is the sole biller (the legacy connection_pool deduct was deleted in Phase 3)". Keep `test_meter_is_live_reads_env` (env override still works).

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_meter_is_live_setting.py -v`
Expected: FAIL — `assert False is True` (default is still False in config.py).

- [ ] **Step 3: Flip the config defaults**

In `apps/backend/core/config.py`:

Change line 103:
```python
    LEDGER_BACKEND: str = "metronome"  # ledger system-of-record. Phase 3 cutover: Metronome is the SOLE ledger; the DDB backend + credit_ledger.py were deleted. "ddb" is no longer a valid value.
```

Change line 104:
```python
    METER_IS_LIVE: bool = True  # Phase 3 cutover: the AWS Bedrock invocation-log meter's get_ledger().deplete() is the SOLE biller (then enforce_after_deplete). The legacy connection_pool deduct was deleted. The env var can still force it off for a break-glass shadow run, but there is no longer a second biller to fall back to.
```

- [ ] **Step 4: Fix the `test_metronome_settings.py` default-backend assertion, then verify the flip**

`apps/backend/tests/unit/billing/test_metronome_settings.py:16` asserts `settings.LEDGER_BACKEND == "ddb"` with a stale "cutover is Phase 3" comment. The flip just made that wrong, so update it NOW (unconditional — verified it lives in this file):

```python
    # Phase 3 cutover: Metronome is the sole ledger; "ddb" is no longer valid.
    assert settings.LEDGER_BACKEND == "metronome"
```

Then run: `uv run pytest tests/unit/test_meter_is_live_setting.py tests/unit/billing/test_metronome_settings.py -v`
Expected: PASS — `METER_IS_LIVE` defaults True and `LEDGER_BACKEND` defaults `"metronome"`.

Run: `uv run python -c "from core.config import settings; print(settings.LEDGER_BACKEND, settings.METER_IS_LIVE)"`
Expected: prints `metronome True`.

- [ ] **Step 5: Commit**

```bash
uv run ruff format core/config.py tests/unit/test_meter_is_live_setting.py tests/unit/billing/test_metronome_settings.py
uv run ruff check --fix core/config.py tests/unit/test_meter_is_live_setting.py tests/unit/billing/test_metronome_settings.py
git add apps/backend/core/config.py apps/backend/tests/unit/test_meter_is_live_setting.py apps/backend/tests/unit/billing/test_metronome_settings.py
git commit -m "feat(billing): cutover defaults — LEDGER_BACKEND=metronome + METER_IS_LIVE=true"
```

---

### Task 8: Remove the `ddb` branch from `get_ledger()` + delete `ddb_ledger_backend.py`

**Files:**
- Modify: `apps/backend/core/billing/ledger_interface.py`
- Modify: `apps/backend/tests/unit/billing/test_ledger_interface.py`
- Modify: `apps/backend/tests/unit/routers/test_internal_usage.py` (the meter endpoint exercises `get_ledger().deplete` through the soon-deleted DDB backend — re-point onto a stubbed ledger)
- DELETE: `apps/backend/core/billing/ddb_ledger_backend.py`
- DELETE: `apps/backend/tests/unit/billing/test_ddb_ledger_backend.py`

**Design (read before coding):**
With the default flipped to metronome (Task 7) and no caller selecting `"ddb"`, remove the `"ddb"` branch + the `DdbLedgerBackend` lazy import from `get_ledger()`. Then delete `ddb_ledger_backend.py` + its test. `test_ledger_interface.py` references `DdbLedgerBackend` (import line 7, the default + `honours_ddb` tests, the `ledger_tables` fixture seam test) — re-point them to metronome.

**LOAD-BEARING (suite-green gate — do NOT skip Step 5):** `tests/unit/routers/test_internal_usage.py` has a `ledger_tables` fixture (lines 101-131) that sets `LEDGER_BACKEND=ddb` and creates moto `test-credits`/`test-credit-txns` tables, and four `TestDeplete` tests (`test_prices_attributes_and_deplets`, `test_replay_is_noop`, `test_error_status_still_deplets`, `test_identity_arn_fallback_attributes_when_aip_misses`) call the REAL meter endpoint with `meter_live` → the endpoint calls `get_ledger().deplete(event)` (`routers/internal_usage.py:151`) and asserts the DDB backend's real arithmetic (`balance_microcents == 86_000`). Once this task removes the `"ddb"` branch, `get_ledger()` RAISES `ValueError("Unknown LEDGER_BACKEND 'ddb'…")` for those tests, and the DDB backend they relied on is gone — so they MUST be migrated in this same task or the suite goes RED at this commit (violating the green-at-every-commit invariant). They are re-pointed to patch `routers.internal_usage.get_ledger` with a stub whose `deplete` returns a `DepleteResult`, mirroring the existing `test_meter_live_calls_deplete_and_enforce` (line 233) which already patches `get_ledger`. (`test_meter_shadow_logs_and_does_not_deplete` sets `METER_IS_LIVE=False` so it never calls `get_ledger` — it survives untouched; the real-deplete arithmetic coverage now lives in `test_metronome_ledger_backend.py`.)

Order within this task (all land in ONE commit at Step 8, so the green-at-every-commit invariant is about the committed state, not the intermediate scoped runs): rewrite `test_ledger_interface.py` (Step 1) → remove the `"ddb"` branch (Step 3) → scoped-run `test_ledger_interface.py` (Step 4) → migrate the `test_internal_usage.py` deplete tests (Step 5) → only THEN delete `ddb_ledger_backend.py` + its standalone test (Step 6). Do NOT commit between Steps 3 and 5 — between them `test_internal_usage.py`'s deplete tests are transiently red (branch gone, tests not yet migrated); the Step-4 run is deliberately scoped to `test_ledger_interface.py` only, and the Step-8 commit is taken after Step 5 makes the whole surface green again.

- [ ] **Step 1: Rewrite the interface tests to be metronome-only**

Replace `apps/backend/tests/unit/billing/test_ledger_interface.py` entirely:

```python
"""Unit tests for the ledger interface: DepleteResult + get_ledger() factory.

Phase 3 cutover: Metronome is the SOLE backend. The DDB backend + the "ddb"
factory branch were deleted, so the factory returns MetronomeLedgerBackend by
default and rejects every non-metronome value.
"""

import pytest

from core.billing.ledger_interface import DepleteResult, get_ledger
from core.billing.metronome_ledger_backend import MetronomeLedgerBackend


def test_deplete_result_fields():
    r = DepleteResult(balance_microcents=500, applied=True)
    assert r.balance_microcents == 500
    assert r.applied is True


def test_get_ledger_returns_metronome_by_default(monkeypatch):
    monkeypatch.delenv("LEDGER_BACKEND", raising=False)
    monkeypatch.setattr("core.config.settings.LEDGER_BACKEND", "metronome")
    assert isinstance(get_ledger(), MetronomeLedgerBackend)


def test_get_ledger_honours_metronome_setting(monkeypatch):
    monkeypatch.setenv("LEDGER_BACKEND", "metronome")
    assert isinstance(get_ledger(), MetronomeLedgerBackend)


def test_get_ledger_rejects_ddb_now(monkeypatch):
    # "ddb" is no longer a valid backend — the DDB ledger was deleted in Phase 3.
    monkeypatch.setenv("LEDGER_BACKEND", "ddb")
    with pytest.raises(ValueError, match="ddb"):
        get_ledger()


def test_get_ledger_rejects_unknown_backend(monkeypatch):
    monkeypatch.setenv("LEDGER_BACKEND", "bogus")
    with pytest.raises(ValueError, match="bogus"):
        get_ledger()
```

(The old `ledger_tables` moto fixture + `test_get_ledger_deplete_then_balance_through_interface` exercised the DDB backend end-to-end through the interface; the equivalent metronome end-to-end is covered by `test_metronome_ledger_backend.py`, so it is not re-created here.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/billing/test_ledger_interface.py -v`
Expected: FAIL — `test_get_ledger_rejects_ddb_now` fails because the factory still has the `"ddb"` branch (returns `DdbLedgerBackend`, no `ValueError`).

- [ ] **Step 3: Remove the `ddb` branch**

In `apps/backend/core/billing/ledger_interface.py`, replace the `get_ledger()` body:

```python
def get_ledger() -> LedgerBackend:
    """Return the configured ledger backend.

    Phase 3 cutover: Metronome is the SOLE ledger system-of-record. The interim
    DDB backend (ddb_ledger_backend.py) and the hand-rolled credit_ledger.py
    were deleted. The only valid LEDGER_BACKEND value is "metronome".
    """
    backend = os.environ.get("LEDGER_BACKEND") or settings.LEDGER_BACKEND
    if backend == "metronome":
        from core.billing.metronome_ledger_backend import MetronomeLedgerBackend

        return MetronomeLedgerBackend()
    raise ValueError(f"Unknown LEDGER_BACKEND {backend!r} (expected 'metronome'; the DDB ledger was deleted in Phase 3)")
```

Update the module docstring (lines 1-8) to drop the "ddb (interim) now ... Metronome adapter in Phase 3" framing — it's metronome-only now:

```python
"""Thin ledger interface — the per-event seam between the meter and the money store.

`deplete(event)` applies one usage event to the owner's balance, exactly once.
`get_balance(owner_id)` reads it. The backend is Metronome (the sole ledger
system-of-record since the Phase-3 cutover) — callers depend only on this
interface, never on the backend.
"""
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/unit/billing/test_ledger_interface.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Migrate the meter-deplete tests in `test_internal_usage.py` off the deleted DDB backend**

The four `TestDeplete` tests that go live (`test_prices_attributes_and_deplets`, `test_replay_is_noop`, `test_error_status_still_deplets`, `test_identity_arn_fallback_attributes_when_aip_misses`) currently rely on the DDB backend through `ledger_tables` (`LEDGER_BACKEND=ddb`). The DDB branch is now gone, so re-point them to patch `routers.internal_usage.get_ledger` with a stub whose `deplete` returns a `DepleteResult`, exactly like the already-present `test_meter_live_calls_deplete_and_enforce` (line 233). Concretely:

1. Add a small helper near the top of `test_internal_usage.py` (after the imports), mirroring the `_ledger_stub` pattern used in `test_billing_credits.py`:

```python
from core.billing.ledger_interface import DepleteResult  # noqa: E402


def _ledger_stub(deplete_result):
    stub = MagicMock()
    stub.deplete = AsyncMock(return_value=deplete_result)
    return stub
```

2. Rewrite each of the four tests to patch `get_ledger` instead of depending on `ledger_tables`'s DDB arithmetic. They no longer need the `ledger_tables` fixture (the stub returns the result directly); keep `meter_live`. Pattern (shown for `test_prices_attributes_and_deplets`; apply the same to the other three, preserving each test's `request_id` / `run_status` / resolver-patch specifics):

```python
def test_prices_attributes_and_deplets(self, client, meter_live):
    with (
        patch(
            "routers.internal_usage.resolve_owner_from_aip_arn",
            AsyncMock(return_value="org_abc"),
        ),
        patch(
            "routers.internal_usage.price_from_aip_arn",
            AsyncMock(return_value=10_000),
        ),
        patch(
            "routers.internal_usage.get_ledger",
            return_value=_ledger_stub(DepleteResult(balance_microcents=86_000, applied=True)),
        ),
        patch("core.services.bedrock_enforcement.enforce_after_deplete", AsyncMock()),
    ):
        r = self._post(client, request_id="req-A")
    assert r.status_code == 200
    data = r.json()
    assert data["applied"] is True
    assert data["balance_microcents"] == 86_000
```

For `test_replay_is_noop`, the stub's `deplete` returns `applied=True` on the first call and `applied=False` on the second (same balance) — use `AsyncMock(side_effect=[DepleteResult(86_000, True), DepleteResult(86_000, False)])`. For `test_error_status_still_deplets` keep `run_status="error"` and `applied=True`. For `test_identity_arn_fallback_attributes_when_aip_misses` keep the `resolve_owner_from_aip_arn → None` + `resolve_owner_from_identity_arn → "org_abc"` patches and the same `get_ledger` stub.

3. Leave `test_meter_shadow_logs_and_does_not_deplete` and `test_meter_live_calls_deplete_and_enforce` untouched at the patch level — both already patch `get_ledger` (the shadow one also sets `METER_IS_LIVE=False`, so the endpoint never calls `get_ledger` anyway). They still take the `ledger_tables` fixture (see #4).

4. **Strip the now-invalid env from the `ledger_tables` fixture (lines ~101-131) — REQUIRED, not optional.** The fixture still creates the two moto tables (`test-credits`, `test-credit-txns`) used by the three tests that assert `ledger_tables.scan(TableName="test-credit-txns")["Count"] == 0` (`test_meter_shadow_logs_and_does_not_deplete`, `TestEdgeCases.test_unattributed_owner_skips_without_deplete`, `test_unknown_model_skips_without_deplete` — keep these moto-table creations so those scan assertions still resolve). BUT delete its three setenv lines:
   - `monkeypatch.setenv("CREDITS_TABLE", "test-credits")`
   - `monkeypatch.setenv("CREDIT_TRANSACTIONS_TABLE", "test-credit-txns")`
   - `monkeypatch.setenv("LEDGER_BACKEND", "ddb")`
   The `LEDGER_BACKEND=ddb` setenv now points the factory at a deleted branch (the migrated tests stub `get_ledger`, so they don't need it; the scan-only tests never call `get_ledger`'s real path), and the `CREDITS_TABLE`/`CREDIT_TRANSACTIONS_TABLE` setenvs name settings Task 11 removes from `config.py` — leaving them would also trip Task 13 Step 4's deletion-invariant grep (which scans for `CREDITS_TABLE`/`CREDIT_TRANSACTIONS_TABLE`). Do NOT add a `LEDGER_BACKEND=metronome` setenv that would construct the real Metronome backend — every deplete is stubbed.

Run: `uv run pytest tests/unit/routers/test_internal_usage.py -v`
Expected: PASS — the four migrated deplete tests + the two untouched ones + the 401/422 guards.

- [ ] **Step 6: Delete the DDB backend + its test**

```bash
git rm apps/backend/core/billing/ddb_ledger_backend.py
git rm apps/backend/tests/unit/billing/test_ddb_ledger_backend.py
```

- [ ] **Step 7: Confirm nothing imports the deleted backend**

Run: `grep -rn "ddb_ledger_backend\|DdbLedgerBackend" apps/backend --include="*.py" || echo "NO DdbLedgerBackend refs"`
Expected: prints `NO DdbLedgerBackend refs`.

Run: `uv run python -c "from core.billing.ledger_interface import get_ledger; print('ledger_interface import ok')"`
Expected: prints `ledger_interface import ok`.

- [ ] **Step 8: Commit**

```bash
uv run ruff format core/billing/ledger_interface.py tests/unit/billing/test_ledger_interface.py tests/unit/routers/test_internal_usage.py
uv run ruff check --fix core/billing/ledger_interface.py tests/unit/billing/test_ledger_interface.py tests/unit/routers/test_internal_usage.py
git add apps/backend/core/billing/ledger_interface.py apps/backend/tests/unit/billing/test_ledger_interface.py apps/backend/tests/unit/routers/test_internal_usage.py
git commit -m "refactor(billing): metronome-only get_ledger(); migrate meter tests; delete DdbLedgerBackend"
```

---

### Task 9: Delete `credit_ledger.py` + its test + reword the stale prose mentions

**Files:**
- DELETE: `apps/backend/core/services/credit_ledger.py`
- DELETE: `apps/backend/tests/unit/services/test_credit_ledger.py`
- Modify: `apps/backend/core/billing/bedrock_pricing.py`
- Modify: `apps/backend/core/services/usage_service.py`
- Modify: `apps/backend/scripts/reconcile_bedrock_vs_ledger.py`

**Design (read before coding):**
After Tasks 1-6 no code *imports or calls* `credit_ledger`. The only remaining
references are FOUR prose-only mentions (the `usage_service.py` MODULE docstring
on line 1, the `usage_service.py` `record_usage` docstring on line 58,
`bedrock_pricing.py:155`, and `scripts/reconcile_bedrock_vs_ledger.py:79`).
**Reword the prose FIRST (Step 1), THEN run the caller-verification grep (Step 2),
THEN delete (Step 3)** — if you grep for the bare token `credit_ledger` before
the prose is reworded, the four prose mentions match and the gate falsely fires.
The verification grep therefore targets import/call shapes, not the bare token.

- [ ] **Step 1: Reword the four stale prose mentions FIRST**

`apps/backend/core/services/usage_service.py` line 1 — the MODULE docstring reads:
```python
"""Usage tracking — counter writes for analytics. Billing flows through credit_ledger."""
```
change to:
```python
"""Usage tracking — counter writes for analytics. Billing flows through the ledger interface (get_ledger, backed by Metronome)."""
```

`apps/backend/core/billing/bedrock_pricing.py` line 155 — change:
```python
    Markup is applied separately by ``credit_ledger.deduct``. Cache tokens
```
to:
```python
    Markup is applied separately at metering time (the AWS invocation-log
    meter builds the billed amount from this raw cost). Cache tokens
```

`apps/backend/core/services/usage_service.py` line 58 — change the docstring lines that read:
```python
    Writes per-period + lifetime + per-member counters to DynamoDB. The
    credit ledger (core/services/credit_ledger.py) handles authoritative
    billing for card-3 (bedrock_claude) users; this is a counter-only
```
to:
```python
    Writes per-period + lifetime + per-member counters to DynamoDB. The
    ledger interface (core/billing/ledger_interface.get_ledger, backed by
    Metronome) handles authoritative billing; this is a counter-only
```

`apps/backend/scripts/reconcile_bedrock_vs_ledger.py` line 79 — change:
```python
  table). The 1.4× customer markup is applied only later in
  ``credit_ledger.deduct``. The AWS bill is itself un-marked-up, so the
```
to:
```python
  table). The 1.4× customer markup is applied only later at metering time
  (the AWS invocation-log meter). The AWS bill is itself un-marked-up, so the
```

- [ ] **Step 2: Verify no remaining `credit_ledger` import or call (the merge gate's central check)**

With the four prose mentions reworded (Step 1), the only `credit_ledger`
references left are import/call shapes — there must be NONE. Grep for the
import + dotted-call shapes specifically (NOT the bare token, which would
also match the `credit_ledger_tables` fixture name and the about-to-be-deleted
files):
```bash
grep -rnE "import credit_ledger|from core\.services\.credit_ledger|credit_ledger\.[a-z_]+\(" apps/backend --include="*.py" \
  | grep -vE "test_credit_ledger\.py|core/services/credit_ledger\.py" \
  || echo "NO credit_ledger callers remain"
```
Expected: prints `NO credit_ledger callers remain` (the only excluded hits are the test file + the module itself, both about to be deleted). If any other hit appears, STOP — a migration was missed; go back to Tasks 3-6.

- [ ] **Step 3: Delete the module + its test**

```bash
git rm apps/backend/core/services/credit_ledger.py
git rm apps/backend/tests/unit/services/test_credit_ledger.py
```

- [ ] **Step 4: Confirm the module is gone and nothing references it**

Run: `grep -rn "core.services.credit_ledger\|services import credit_ledger\|credit_ledger\." apps/backend --include="*.py" | grep -vE "credit_ledger_tables" || echo "CLEAN — no credit_ledger module references"`
Expected: prints `CLEAN — no credit_ledger module references` (the `credit_ledger_tables` moto fixture *name* is the only allowed residual — it is a fixture identifier, not a reference to the deleted module).

Run: `uv run python -c "import core.services.usage_service, core.billing.bedrock_pricing; print('imports ok')"`
Expected: prints `imports ok`.

- [ ] **Step 5: Commit**

```bash
uv run ruff format core/billing/bedrock_pricing.py core/services/usage_service.py scripts/reconcile_bedrock_vs_ledger.py
uv run ruff check --fix core/billing/bedrock_pricing.py core/services/usage_service.py scripts/reconcile_bedrock_vs_ledger.py
git add apps/backend/core/billing/bedrock_pricing.py apps/backend/core/services/usage_service.py apps/backend/scripts/reconcile_bedrock_vs_ledger.py
git commit -m "refactor(billing): delete credit_ledger.py; reword stale ledger prose"
```

---

### Task 10: Full-suite green checkpoint (post-deletion, pre-CDK)

**Files:**
- (none — verification only)

**Design (read before coding):**
Before touching CDK, confirm the deletions left the suite green. This is the "callers migrated, modules deleted, suite green" milestone the sequencing guarantees.

- [ ] **Step 1: Run the billing + gateway + webhook suites**

Run: `uv run pytest tests/unit/billing/ tests/unit/routers/test_billing_credits.py tests/unit/services/test_stripe_webhook_metronome_funding.py tests/unit/gateway/test_connection_pool_credits.py tests/unit/routers/test_metronome_webhook.py tests/unit/routers/test_internal_usage.py tests/unit/services/test_provision_gate_ledger.py tests/unit/test_meter_is_live_setting.py -v`
Expected: PASS — every migrated/retained test green; no test imports `credit_ledger` or `ddb_ledger_backend`.

- [ ] **Step 2: Run the broader unit suite for collateral regressions**

Run: `uv run pytest tests/unit/ -q`
Expected: PASS — the only changes from baseline are the deleted test files (fewer tests) and the migrated assertions. Any NEW failure must be in a file this plan touched — investigate immediately, do not proceed.

- [ ] **Step 3: Confirm both deleted modules + the legacy deduct are fully gone**

Run:
```bash
grep -rn "credit_ledger\|ddb_ledger_backend\|DdbLedgerBackend\|_maybe_deduct_credits" apps/backend --include="*.py" \
  | grep -v "credit_ledger_tables" \
  || echo "FULLY REMOVED"
```
Expected: prints `FULLY REMOVED` (the fixture name `credit_ledger_tables` is the only allowed residual; if it bothers, rename it to `credits_moto_tables` in a follow-up — out of scope here).

- [ ] **Step 4: Lint the full touched surface**

Run: `uv run ruff check core/ routers/ scripts/reconcile_bedrock_vs_ledger.py tests/unit/billing/ tests/unit/routers/test_billing_credits.py tests/unit/services/test_stripe_webhook_metronome_funding.py tests/unit/gateway/test_connection_pool_credits.py tests/unit/test_meter_is_live_setting.py`
Expected: no errors.

- [ ] **Step 5: Commit any lint fixups (if any)**

```bash
uv run ruff format core/ routers/ scripts/ tests/unit/
uv run ruff check --fix core/ routers/ scripts/ tests/unit/
git add -A apps/backend
git commit -m "chore(billing): Phase-3 post-deletion lint + green checkpoint" || echo "nothing to commit"
```

---

### Task 11: CDK — drop the `credits` + `credit-transactions` tables (and their settings)

**Files:**
- Modify: `apps/infra/lib/stacks/database-stack.ts`
- Modify: `apps/infra/lib/stacks/service-stack.ts`
- Modify: `apps/infra/lib/isol8-stage.ts`
- Modify: `apps/infra/lib/local-stage.ts`
- Modify: `apps/backend/core/config.py`

**Design (read before coding):**
The DDB ledger tables are no longer read by the backend (the backfill in Task 12 reads them via an explicit env-name, run as a one-off BEFORE this CDK change deploys to prod). Remove the two tables, their grants, env wiring, and `public readonly` fields. **Cross-stack export safety (verified):** `grep -rnE "credits-table|credit-transactions-table|Fn.importValue" apps/infra/lib --include="*.ts"` shows the two `CfnOutput` export lines (`${this.stackName}-credits-table`, `-credit-transactions-table`) but **NO `Fn.importValue` consumer anywhere** — so removing the exports does not break another stack's synth (an exported value can only be deleted safely when nothing imports it; nothing does). Remove the `CfnOutput`s in the same change. The `removalPolicy` on the tables honors the per-env `config.removalPolicy` (RETAIN in prod) — so removing the CDK construct orphans (not destroys) a prod table with RETAIN; a follow-up manual delete + retention window finishes the job. Also drop the now-orphan `CREDITS_TABLE` / `CREDIT_TRANSACTIONS_TABLE` settings from `config.py` (the backfill reads the table name from an explicit `--credits-table` arg / env, not these settings — see Task 12).

- [ ] **Step 1: Remove the table definitions + public fields + outputs in database-stack.ts**

In `apps/infra/lib/stacks/database-stack.ts`:

1. Delete the `public readonly creditsTable: dynamodb.Table;` and `public readonly creditTransactionsTable: dynamodb.Table;` field declarations (lines 34-35).
2. Delete the `this.creditsTable = new dynamodb.Table(this, "CreditsTable", {...});` block (lines 213-223, including its leading comment).
3. Delete the `this.creditTransactionsTable = new dynamodb.Table(this, "CreditTransactionsTable", {...});` block (lines 225-237, including its leading comment).
4. Delete the two `CfnOutput`s: `CreditsTableName` (lines 352-355) and `CreditTransactionsTableName` (lines 356-359).
5. The `MetronomeFastGateTable` leading comment (line ~245) ends with `PK owner_id; mirrors creditsTable.` — that now dangles at a deleted construct. Reword the tail to `PK owner_id.` (drop the `mirrors creditsTable` clause) so no comment references a removed table.

Leave the `MetronomeFastGateTable` table construct itself (lines 239-254) intact — it is the live fast-gate.

- [ ] **Step 2: Remove the props, grants, and env in service-stack.ts**

In `apps/infra/lib/stacks/service-stack.ts`:

1. In the `props.database` interface block, delete `creditsTable: dynamodb.Table;` (line 86) and `creditTransactionsTable: dynamodb.Table;` (line 87).
2. Delete the two grants (lines 485-486): `props.database.creditsTable.grantReadWriteData(this.taskRole);` and `props.database.creditTransactionsTable.grantReadWriteData(this.taskRole);`.
3. Delete the two env entries (lines 1007-1009): `CREDITS_TABLE: props.database.creditsTable.tableName,` and the `CREDIT_TRANSACTIONS_TABLE: props.database.creditTransactionsTable.tableName,` (note it spans two lines).

- [ ] **Step 3: Remove the wiring in both stages**

In `apps/infra/lib/isol8-stage.ts`, delete the two lines (~92-93) passing `creditsTable: database.creditsTable,` and `creditTransactionsTable: database.creditTransactionsTable,` into ServiceStack.

In `apps/infra/lib/local-stage.ts`, delete the same two lines (~96-97).

- [ ] **Step 4: Drop the orphan settings in config.py**

In `apps/backend/core/config.py`, delete the two lines (101-102):
```python
    CREDITS_TABLE: str = ""
    CREDIT_TRANSACTIONS_TABLE: str = ""
```

The leading comment on line 100 reads `# Credit ledger + OAuth tables (set by CDK)` — `OAUTH_TOKENS_TABLE` (line 109) survives, so reword it to `# OAuth tables (set by CDK)` rather than deleting it.

(The backfill script in Task 12 takes the credits table name from a `--credits-table` CLI arg, defaulting to `isol8-{env}-credits`, so it does not depend on these settings.)

- [ ] **Step 5: TypeScript compile + (offline) synth check**

Run (from repo root): `pnpm exec tsc --noEmit -p apps/infra/tsconfig.json`
Expected: no type errors referencing `creditsTable` / `creditTransactionsTable` (every reference removed).

If AWS creds are available, also run (from `apps/infra/`): `pnpm exec cdk synth isol8-dev-database isol8-dev-service --quiet 2>&1 | head -40`
Expected: synth succeeds; the `credits` / `credit-transactions` tables + their outputs no longer appear in the template.

- [ ] **Step 6: Confirm the backend still imports without the settings**

Run (from `apps/backend/`): `uv run python -c "from core.config import settings; assert not hasattr(settings, 'CREDITS_TABLE'); print('config clean')"`
Expected: prints `config clean`.

Run: `uv run pytest tests/unit/ -q`
Expected: PASS — no test references `CREDITS_TABLE` via `settings` (the moto-based tests that set `CREDITS_TABLE` via `monkeypatch.setenv` are for the deleted backend's tests, already removed; any surviving fixture that sets the env is inert).

- [ ] **Step 7: Commit**

```bash
git add apps/infra/lib/stacks/database-stack.ts apps/infra/lib/stacks/service-stack.ts apps/infra/lib/isol8-stage.ts apps/infra/lib/local-stage.ts apps/backend/core/config.py
git commit -m "feat(infra): drop credits + credit-transactions DDB tables (DDB ledger deleted)"
```

---

### Task 12: One-time balance-backfill script (DDB → Metronome prepaid credits)

**Files:**
- Create: `apps/backend/scripts/backfill_balances_to_metronome.py`
- Create: `apps/backend/tests/unit/scripts/test_backfill_balances_to_metronome.py`

**Design (read before coding):**
A one-shot migration: scan the `credits` DDB table (`user_id` PK holds the owner_id; `balance_microcents` is the balance), and for every owner with a positive balance, (1) ensure a Metronome customer exists (`metronome_provisioning.ensure_metronome_customer` — needs the owner's `stripe_customer_id` from `billing_repo`), then (2) grant an equal prepaid credit as a **non-payment-gated** commit (the owner already paid for that balance in the old system; re-gating on Stripe would double-charge). **Idempotency (spec §3.5 "idempotency-keyed per owner; do not grandfather two money systems"):** stamp each backfill commit with `metadata.backfill_key = f"ddb-balance-backfill:{owner_id}"` and, before granting, record a marker on the fast-gate row (`backfill_done_at`); re-running skips any owner whose marker is set. This makes the script safe to re-run (a partial run resumes without double-granting). Dry-run by default (`--apply` to execute), mirroring `backfill_paperclip_for_legacy_users.py`. Unit-tested with moto (the `credits` + fast-gate tables) + mocked Metronome HTTP.

The script grants via a dedicated `metronome_funding.fund_backfill_credit(owner_id, amount_microcents, ...)` helper to keep the non-payment-gated commit shape distinct from `fund_top_up`. We add that helper here.

- [ ] **Step 1: Add the `fund_backfill_credit` helper + test**

In `apps/backend/core/billing/metronome_funding.py`, add (after `set_auto_recharge`):

```python
async def fund_backfill_credit(
    *,
    owner_id: str,
    amount_microcents: int,
    contract_id: str,
    product_id: str,
    client: MetronomeClient | None = None,
) -> None:
    """Grant a one-time, NON-payment-gated prepaid credit during the DDB->Metronome
    balance migration. The owner already paid for this balance in the old ledger,
    so we must NOT re-gate it on a new Stripe payment (that would double-charge).
    Stamped with a deterministic backfill_key in metadata so a re-run is a
    server-side-visible no-op partner to the script's own idempotency marker.
    """
    if amount_microcents <= 0:
        raise FundingError(f"amount_microcents must be positive, got {amount_microcents}")
    customer_id = _customer_id_for(owner_id)
    commit = {
        "product_id": product_id,
        "amount": amount_microcents / _MICROCENTS_PER_DOLLAR,
        "metadata": {"backfill_key": f"ddb-balance-backfill:{owner_id}", "owner_id": owner_id},
    }
    if client is not None:
        await client.edit_contract_add_commits(
            metronome_customer_id=customer_id, contract_id=contract_id, add_commits=[commit]
        )
        return
    async with httpx.AsyncClient(base_url=settings.METRONOME_BASE_URL) as http:
        await metronome_client.for_settings(http).edit_contract_add_commits(
            metronome_customer_id=customer_id, contract_id=contract_id, add_commits=[commit]
        )
```

Append to `apps/backend/tests/unit/billing/test_metronome_funding.py`:

```python
from core.billing.metronome_funding import fund_backfill_credit  # noqa: E402


@pytest.mark.asyncio
async def test_fund_backfill_credit_is_not_payment_gated(provisioned_table):
    fake = _fake_client()
    await fund_backfill_credit(
        owner_id="org_abc",
        amount_microcents=12_000_000,  # $12.00
        contract_id="contract-1",
        product_id="prod-credits",
        client=fake,
    )
    _, kwargs = fake.edit_contract_add_commits.call_args
    commit = kwargs["add_commits"][0]
    assert commit["amount"] == 12.0
    # NON-payment-gated: the owner already paid in the old system.
    assert "payment_gate_config" not in commit
    assert commit["metadata"]["backfill_key"] == "ddb-balance-backfill:org_abc"


@pytest.mark.asyncio
async def test_fund_backfill_credit_rejects_non_positive(provisioned_table):
    fake = _fake_client()
    with pytest.raises(FundingError, match="positive"):
        await fund_backfill_credit(
            owner_id="org_abc",
            amount_microcents=0,
            contract_id="contract-1",
            product_id="prod-credits",
            client=fake,
        )
```

Run: `uv run pytest tests/unit/billing/test_metronome_funding.py -k backfill -v`
Expected: FAIL → after adding the helper, PASS.

- [ ] **Step 2: Write the failing backfill-script test**

Create `apps/backend/tests/unit/scripts/test_backfill_balances_to_metronome.py`:

```python
"""Unit tests for the one-time DDB-balance -> Metronome-credit backfill.

moto-mocks the credits + fast-gate tables; mocks the Metronome funding +
provisioning HTTP so no live call is made. Asserts idempotency (a second run
grants nothing more).
"""

from unittest.mock import AsyncMock, patch

import boto3
import pytest
from moto import mock_aws

from scripts.backfill_balances_to_metronome import backfill_owner, run_backfill


@pytest.fixture
def tables(monkeypatch):
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="isol8-test-credits",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="isol8-test-metronome-fast-gate",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        # One funded owner, one zero-balance owner (skipped).
        client.put_item(
            TableName="isol8-test-credits",
            Item={"user_id": {"S": "org_funded"}, "balance_microcents": {"N": "12000000"}},
        )
        client.put_item(
            TableName="isol8-test-credits",
            Item={"user_id": {"S": "org_zero"}, "balance_microcents": {"N": "0"}},
        )
        # The funded owner is already Metronome-provisioned.
        client.put_item(
            TableName="isol8-test-metronome-fast-gate",
            Item={"owner_id": {"S": "org_funded"}, "metronome_customer_id": {"S": "cust-1"}},
        )
        monkeypatch.setenv("METRONOME_FAST_GATE_TABLE", "isol8-test-metronome-fast-gate")
        monkeypatch.setattr("core.config.settings.METRONOME_CREDITS_CONTRACT_ID", "contract-1")
        monkeypatch.setattr("core.config.settings.METRONOME_CREDITS_PRODUCT_ID", "prod-credits")
        yield client


@pytest.mark.asyncio
async def test_backfill_owner_grants_equal_credit_and_marks_done(tables):
    with (
        patch("scripts.backfill_balances_to_metronome.ensure_metronome_customer", new=AsyncMock(return_value="cust-1")),
        patch("scripts.backfill_balances_to_metronome.fund_backfill_credit", new=AsyncMock()) as mock_grant,
        patch(
            "scripts.backfill_balances_to_metronome.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"stripe_customer_id": "cus_1"}),
        ),
    ):
        granted = await backfill_owner("org_funded", 12_000_000, apply=True, credits_table="isol8-test-credits")
    assert granted is True
    mock_grant.assert_awaited_once()
    _, kwargs = mock_grant.call_args
    assert kwargs["owner_id"] == "org_funded"
    assert kwargs["amount_microcents"] == 12_000_000
    # Idempotency marker persisted on the fast-gate row.
    item = tables.get_item(
        TableName="isol8-test-metronome-fast-gate", Key={"owner_id": {"S": "org_funded"}}
    )["Item"]
    assert "backfill_done_at" in item


@pytest.mark.asyncio
async def test_backfill_owner_idempotent_skips_when_marker_set(tables):
    tables.update_item(
        TableName="isol8-test-metronome-fast-gate",
        Key={"owner_id": {"S": "org_funded"}},
        UpdateExpression="SET backfill_done_at = :t",
        ExpressionAttributeValues={":t": {"S": "2026-06-08T00:00:00Z"}},
    )
    with patch("scripts.backfill_balances_to_metronome.fund_backfill_credit", new=AsyncMock()) as mock_grant:
        granted = await backfill_owner("org_funded", 12_000_000, apply=True, credits_table="isol8-test-credits")
    assert granted is False
    mock_grant.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_backfill_dry_run_grants_nothing(tables):
    with patch("scripts.backfill_balances_to_metronome.fund_backfill_credit", new=AsyncMock()) as mock_grant:
        summary = await run_backfill(env="test", apply=False, credits_table="isol8-test-credits")
    mock_grant.assert_not_awaited()
    # Dry-run still reports the funded owner as a candidate, skips the zero one.
    assert summary["candidates"] == 1
    assert summary["granted"] == 0


@pytest.mark.asyncio
async def test_run_backfill_apply_grants_only_positive_balances(tables):
    with (
        patch("scripts.backfill_balances_to_metronome.ensure_metronome_customer", new=AsyncMock(return_value="cust-1")),
        patch("scripts.backfill_balances_to_metronome.fund_backfill_credit", new=AsyncMock()) as mock_grant,
        patch(
            "scripts.backfill_balances_to_metronome.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"stripe_customer_id": "cus_1"}),
        ),
    ):
        summary = await run_backfill(env="test", apply=True, credits_table="isol8-test-credits")
    # Only org_funded granted; org_zero skipped.
    mock_grant.assert_awaited_once()
    assert summary["granted"] == 1
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest tests/unit/scripts/test_backfill_balances_to_metronome.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.backfill_balances_to_metronome'`.

- [ ] **Step 4: Write the backfill script**

Create `apps/backend/scripts/backfill_balances_to_metronome.py`:

```python
"""One-time DDB-balance -> Metronome-credit migration (Phase 3 cutover).

Reads every owner's balance from the legacy ``credits`` DDB table and grants an
equal Metronome prepaid credit (NON-payment-gated — the owner already paid for
that balance in the old ledger; re-gating on Stripe would double-charge). Run
ONCE per environment BEFORE the credits/credit-transactions tables are dropped
(spec §3.5: "Backfill existing balances into Metronome prepaid credits;
idempotency-keyed per owner; do not grandfather two money systems").

Idempotent: each granted owner gets a ``backfill_done_at`` marker on the
fast-gate row; re-running skips marked owners, so a partial run resumes without
double-granting. The granted commit also carries a deterministic
``metadata.backfill_key`` so the grant is traceable in Metronome.

Run::

    cd apps/backend
    uv run python -m scripts.backfill_balances_to_metronome --env prod
    uv run python -m scripts.backfill_balances_to_metronome --env prod --apply

Defaults to dry-run; pass ``--apply`` to actually grant. The credits table name
defaults to ``isol8-{env}-credits`` (override with ``--credits-table``).

DO NOT run this after the credits table is dropped — the source data is gone.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time

import boto3

from core.billing.metronome_funding import fund_backfill_credit
from core.billing.metronome_provisioning import ensure_metronome_customer
from core.config import settings
from core.repositories import billing_repo

logger = logging.getLogger("backfill_balances_to_metronome")


def _fast_gate_table():
    # Env-first (matches metronome_funding/_fast_gate_table_name +
    # metronome_provisioning): the pydantic `settings` singleton freezes env at
    # import time, so a test/runtime `METRONOME_FAST_GATE_TABLE` env override
    # would be invisible if we read `settings.` only. Reading os.environ first
    # makes the override (and the unit test's monkeypatch.setenv) take effect.
    name = os.environ.get("METRONOME_FAST_GATE_TABLE") or settings.METRONOME_FAST_GATE_TABLE
    if not name:
        raise RuntimeError("METRONOME_FAST_GATE_TABLE is empty — backfill is misconfigured.")
    return boto3.resource("dynamodb", region_name=settings.AWS_REGION).Table(name)


def _credits_table(name: str):
    return boto3.resource("dynamodb", region_name=settings.AWS_REGION).Table(name)


def _already_backfilled(owner_id: str) -> bool:
    resp = _fast_gate_table().get_item(Key={"owner_id": owner_id}, ConsistentRead=True)
    return bool((resp.get("Item") or {}).get("backfill_done_at"))


def _mark_backfilled(owner_id: str) -> None:
    _fast_gate_table().update_item(
        Key={"owner_id": owner_id},
        UpdateExpression="SET backfill_done_at = :t",
        ExpressionAttributeValues={":t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
    )


async def backfill_owner(owner_id: str, balance_microcents: int, *, apply: bool, credits_table: str) -> bool:
    """Grant one owner's balance as a Metronome credit. Returns True if a grant
    was made (or would be, in dry-run), False if skipped (already done / no
    Stripe customer)."""
    if balance_microcents <= 0:
        return False
    if _already_backfilled(owner_id):
        logger.info("SKIP %s — already backfilled", owner_id)
        return False

    if not apply:
        logger.info("DRY-RUN would grant %s: %d microcents", owner_id, balance_microcents)
        return True

    account = await billing_repo.get_by_owner_id(owner_id)
    stripe_customer_id = (account or {}).get("stripe_customer_id")
    if not stripe_customer_id:
        logger.warning("SKIP %s — no stripe_customer_id; provision manually", owner_id)
        return False

    await ensure_metronome_customer(owner_id=owner_id, stripe_customer_id=stripe_customer_id)
    await fund_backfill_credit(
        owner_id=owner_id,
        amount_microcents=balance_microcents,
        contract_id=settings.METRONOME_CREDITS_CONTRACT_ID,
        product_id=settings.METRONOME_CREDITS_PRODUCT_ID,
    )
    _mark_backfilled(owner_id)
    logger.info("GRANTED %s: %d microcents", owner_id, balance_microcents)
    return True


async def run_backfill(*, env: str, apply: bool, credits_table: str) -> dict:
    table = _credits_table(credits_table)
    candidates = 0
    granted = 0
    scan_kwargs: dict = {}
    while True:
        resp = table.scan(**scan_kwargs)
        for item in resp.get("Items", []):
            owner_id = item.get("user_id")
            balance = int(item.get("balance_microcents", 0))
            if balance <= 0:
                continue
            candidates += 1
            if await backfill_owner(owner_id, balance, apply=apply, credits_table=credits_table):
                if apply:
                    granted += 1
        if "LastEvaluatedKey" not in resp:
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    summary = {"candidates": candidates, "granted": granted, "apply": apply}
    logger.info("backfill summary: %s", summary)
    return summary


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Backfill DDB credit balances into Metronome prepaid credits.")
    parser.add_argument("--env", required=True, help="Environment name (dev/prod) — used to default the table name.")
    parser.add_argument("--apply", action="store_true", help="Actually grant credits (default: dry-run).")
    parser.add_argument("--credits-table", default=None, help="Override the credits table name.")
    args = parser.parse_args()
    credits_table = args.credits_table or f"isol8-{args.env}-credits"
    asyncio.run(run_backfill(env=args.env, apply=args.apply, credits_table=credits_table))


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run to verify they pass**

Run: `uv run pytest tests/unit/scripts/test_backfill_balances_to_metronome.py tests/unit/billing/test_metronome_funding.py -v`
Expected: PASS (the 4 backfill-script tests + the funding tests incl. the 2 new `fund_backfill_credit` tests).

- [ ] **Step 6: Confirm the script imports + `--help` runs (no live calls)**

Run: `uv run python -m scripts.backfill_balances_to_metronome --help`
Expected: prints the argparse help with `--env`, `--apply`, `--credits-table` (no traceback, no AWS/Metronome call).

- [ ] **Step 7: Commit**

```bash
uv run ruff format core/billing/metronome_funding.py scripts/backfill_balances_to_metronome.py tests/unit/scripts/test_backfill_balances_to_metronome.py tests/unit/billing/test_metronome_funding.py
uv run ruff check --fix core/billing/metronome_funding.py scripts/backfill_balances_to_metronome.py tests/unit/scripts/test_backfill_balances_to_metronome.py tests/unit/billing/test_metronome_funding.py
git add apps/backend/core/billing/metronome_funding.py apps/backend/scripts/backfill_balances_to_metronome.py apps/backend/tests/unit/scripts/test_backfill_balances_to_metronome.py apps/backend/tests/unit/billing/test_metronome_funding.py
git commit -m "feat(billing): one-time idempotent DDB-balance -> Metronome-credit backfill script"
```

---

### Task 13: Full-suite verification + cutover invariants

**Files:**
- (none — verification only)

- [ ] **Step 1: Run the full billing/metering surface**

Run: `uv run pytest tests/unit/billing/ tests/unit/scripts/ tests/unit/routers/test_billing_credits.py tests/unit/routers/test_metronome_webhook.py tests/unit/routers/test_internal_usage.py tests/unit/services/test_stripe_webhook_metronome_funding.py tests/unit/services/test_provision_gate_ledger.py tests/unit/gateway/test_connection_pool_credits.py tests/unit/test_meter_is_live_setting.py -v`
Expected: PASS — every cutover test green.

- [ ] **Step 2: Run the full unit suite for collateral regressions**

Run: `uv run pytest tests/unit/ -q`
Expected: PASS (baseline minus the two deleted test files + the deleted Task-6 deduct tests).

- [ ] **Step 3: Cutover invariant — Metronome is the only ledger**

Run: `uv run python -c "from core.config import settings; assert settings.LEDGER_BACKEND == 'metronome' and settings.METER_IS_LIVE is True; print('cutover defaults OK')"`
Expected: prints `cutover defaults OK`.

- [ ] **Step 4: Deletion invariant — no orphan import of the deleted modules**

Run:
```bash
grep -rn "credit_ledger\|ddb_ledger_backend\|DdbLedgerBackend\|_maybe_deduct_credits\|CREDITS_TABLE\|CREDIT_TRANSACTIONS_TABLE" apps/backend --include="*.py" \
  | grep -vE "credit_ledger_tables|backfill_balances_to_metronome|test_backfill" \
  || echo "NO orphan references"
```
Expected: prints `NO orphan references`. (Allowed residuals: the `credit_ledger_tables` moto fixture *name*, and the backfill script/test which legitimately read the `credits` table by an explicit arg — neither imports the deleted modules. If any other hit appears, a deletion was incomplete.)

- [ ] **Step 5: CDK type-check (tables gone)**

Run (from repo root): `pnpm exec tsc --noEmit -p apps/infra/tsconfig.json`
Expected: no type errors; no `creditsTable` / `creditTransactionsTable` references remain.

- [ ] **Step 6: Lint the full Phase-3 surface**

Run: `uv run ruff check core/ routers/ scripts/ tests/unit/`
Expected: no errors.

- [ ] **Step 7: Final commit (lint fixups, if any)**

```bash
uv run ruff format core/ routers/ scripts/ tests/unit/
uv run ruff check --fix core/ routers/ scripts/ tests/unit/
git add -A apps/backend
git commit -m "chore(billing): Phase-3 cutover final verification" || echo "nothing to commit"
```

---

## Self-Review

**1. Spec coverage (Phase 3 = cutover + DDB-ledger deletion, spec §3.5):**
- §3.5 "Existing DDB `credit_ledger` → deprecate → delete once the Metronome adapter is live" → Task 9 deletes `credit_ledger.py`; Task 8 deletes `DdbLedgerBackend` + the `"ddb"` factory branch. ✅
- §3.5 "switch the prod ledger backend to the Metronome adapter" → Task 7 flips `LEDGER_BACKEND` default to `"metronome"`; Task 8 removes the only alternative. ✅
- §3.5 "Backfill existing balances into Metronome prepaid credits (idempotency-keyed per owner; do not grandfather two money systems)" → Task 12: scans `credits`, grants an equal NON-payment-gated credit, idempotency marker (`backfill_done_at`) + deterministic `backfill_key`; dry-run default. ✅
- §3.5 Phase-3 ordering ("backfill prod balances ... then deprecate → delete the DDB ledger") → the CRITICAL MERGE GATE encodes: run backfill on prod BEFORE merge/table-drop; Task 11 drops the tables only after Task 12 ships the backfill. ✅
- "the meter as sole biller" (spec §2a/§3.2/§3.3 — billing decoupled from web transport, every-run) → Task 6 deletes the connection_pool legacy deduct; Task 7 flips `METER_IS_LIVE` default True so `routers/internal_usage.py`'s `get_ledger().deplete()` is the single biller. ✅

**2. The four caller migrations (grounded in `grep -rn "credit_ledger" apps/backend`):**
- `routers/billing.py:562` `get_balance` → `get_ledger().get_balance` (Task 3). The metronome backend's `get_balance` reads the fast-gate cache (`metronome_ledger_backend.py:83-100`), so the synchronous endpoint stays off Metronome's no-SLA balance API. ✅
- `routers/billing.py:621` `set_auto_reload` → `metronome_funding.set_auto_recharge` via Metronome `credit_balance_threshold_configuration` (Tasks 1-2 build it, Task 4 wires it). The `credit_ledger` import is dropped in Task 4. ✅
- `stripe_webhook_handlers.py:315` `top_up` → removed; `metronome_funding.fund_top_up` is the sole funding call, un-gated (Task 5). The `credit_ledger` import is dropped. ✅
- `connection_pool.py:1258` `deduct` (inside the `if not settings.METER_IS_LIVE:` block at 1134) → the block + the `_maybe_deduct_credits` method deleted (Task 6). The gate READ path (`connection_pool.py:1946` `get_ledger().get_balance`) is untouched — it's already on the interface. ✅
- Stale prose: FOUR mentions reworded (Task 9) — `usage_service.py:1` (module docstring), `usage_service.py:58` (record_usage docstring), `bedrock_pricing.py:155`, `reconcile_bedrock_vs_ledger.py:79`. (The `usage_service.py:1` module docstring was the one the original draft missed; it would have tripped the `credit_ledger\.` verification grep.) ✅
- Test fallout of the deletions (suite-green): the meter endpoint's four live-deplete tests in `test_internal_usage.py` exercised `get_ledger().deplete` through the DDB backend with `LEDGER_BACKEND=ddb`; Task 8 Step 5 re-points them to a stubbed `get_ledger` so they survive the `"ddb"`-branch removal. `test_metronome_settings.py:16`'s `LEDGER_BACKEND == "ddb"` assertion is flipped to `"metronome"` in Task 7. ✅

**3. Interface decision (documented at top + here):** the `LedgerBackend` Protocol stays `deplete` + `get_balance` (the per-event seam). `top_up`/`set_auto_reload` route directly to Metronome funding/auto-recharge helpers (NOT added to the Protocol — they are Metronome-specific money-in operations). `adjustment` + `should_auto_reload` + `InsufficientBalanceError` have ZERO external callers (verified by grep) so they die with the module, no migration target. After Phase 3 no caller imports `credit_ledger` (Task 9 Step 2 import/call-shape grep + Task 13 Step 4 grep-verify). ✅

**4. Sequencing keeps the suite green at each commit:** migrate callers FIRST (Tasks 1-7, each mocking Metronome so the suite stays green), THEN flip the factory + delete modules + migrate/delete their tests (Tasks 8-9 — Task 8 migrates BOTH `test_ledger_interface.py` and the `test_internal_usage.py` deplete tests before removing the `"ddb"` branch; Task 9 reword-prose-first then import/call-shape grep gates the `credit_ledger.py` deletion), checkpoint (Task 10), THEN drop CDK tables (Task 11), THEN add the backfill (Task 12), THEN final verify (Task 13). No module is deleted before its callers are migrated; no `"ddb"`-relying test outlives the `"ddb"` branch. ✅

**5. Cross-stack export safety (Task 11):** verified `grep -rnE "credits-table|credit-transactions-table|Fn.importValue" apps/infra/lib` shows the two `CfnOutput` export lines but NO `Fn.importValue` consumer — so removing the exports does not break another stack's synth. The tables' `removalPolicy` honors `config.removalPolicy` (RETAIN in prod), so the CDK construct removal orphans-not-destroys a prod table; a follow-up manual delete after a retention window finishes it. ✅

**6. `METER_IS_LIVE` interplay (the subtle one):** deleting the connection_pool legacy deduct (gated on `not METER_IS_LIVE`) WITHOUT flipping `METER_IS_LIVE` to True would make BOTH billers inert (the meter only depletes when `METER_IS_LIVE` is True). Task 7 flips both defaults together so exactly one biller (the meter) is live. The env var still allows a break-glass shadow run, but with no second biller it would mean "no billing" — documented in the new config comment. ✅

**7. Placeholder scan:** no TODO/TBD/FIXME/"handle edge cases"/"implement later". Every code block is complete and runnable. Empty-string Metronome contract/product defaults are intentional non-configured-context defaults (matching the Phase-2 convention), not placeholders. ✅

**8. Test-only / mocked-Metronome caveat (the merge-gate's whole point):** every test mocks httpx + moto-mocks DDB, so the suite passes with NO live Metronome. A real prod deploy without a live Metronome account + secrets + contract/product ids + the backfill having been run would break billing and gating. The CRITICAL MERGE GATE at the top encodes the three operator pre-conditions (prod Metronome provisioned; dev sandbox validated; backfill run on prod) and the "ready, DO NOT MERGE" status. ✅

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-08-phase3-metronome-cutover.md`. Recommended order is Task 1 → 13 in sequence, committing each. The dependency chain: Task 2's `set_auto_recharge` uses Task 1's client method; Task 4 wires Task 2; Tasks 8-9 (deletions) run only after Tasks 3-6 migrate every caller (Task 9 Step 1 grep-gates the deletion); Task 11 (CDK table drop) runs after Task 12's backfill ships; Task 13 verifies the cutover + deletion invariants.

**DO NOT push. DO NOT MERGE until the CRITICAL MERGE GATE at the top is satisfied** — a prod Metronome account provisioned (sales-gated), the dev sandbox cutover validated, and the balance backfill (Task 12) run against prod BEFORE the tables drop. This PR is "ready, DO NOT MERGE until prod Metronome live + backfill run". Merging makes Metronome the only ledger with no DDB fallback.
