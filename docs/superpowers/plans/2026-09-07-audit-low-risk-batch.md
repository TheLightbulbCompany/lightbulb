# Audit Low-Risk Batch — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the low-risk findings from the 2026-09-07 whole-codebase audit — delete dead code, remove one live trap, close the cross-boundary drift class with lockstep tests, and correct comments that lie about behavior elsewhere.

**Architecture:** Ten independent tasks, each a deletion, a test, or a comment correction. No task changes runtime behavior of a live path except Task 1 (removes unreachable methods) and Task 8 (prod-gates two account-level CDK resources). Nothing here touches live prod IAM or the checkout path — those are deliberately deferred.

**Tech Stack:** Python 3.12 / FastAPI / pytest / ruff (backend), Node + vitest (infra), AWS CDK TypeScript.

**Spec:** `docs/superpowers/plans/2026-09-07-audit-report.md` — the audit report. §6 ("Deliberately NOT recommended") is binding: it names load-bearing scar tissue that must NOT be touched.

## Global Constraints

- **§6 of the spec is a hard boundary.** Do NOT touch: `trial_cap.py` (any of it), `connection_pool.py` vetoes 7-9, the seven `provider_choice` switches in `containers/config.py`, `_size_for_owner`'s agent-count branch, `discover_ip`'s no-cache rule, `_revoke_owner_iam`, the bedrock-gate-tokens liveness predicate, `metronome_fast_gate.set_balance`, `auth-stack.ts`'s `make()` helper, `efs_file_lock`/`.isol8-guard`/`.isol8-protocol`. If a task seems to require touching one of these, STOP and report.
- **Do NOT consolidate the cross-boundary copies** (rate card ×4, `CAP_MICROCENTS` ×3, `TIER_TO_FM`). §6: consolidation is forced-impossible by process boundaries. Add lockstep TESTS only.
- **Do NOT narrow `table-names.ts`'s leading `*` wildcard.** That is deferred to a separate plan (live prod IAM, medium risk).
- **Do NOT change `core/services/billing_service.py` checkout paths.** Deferred to a separate plan.
- Backend tests: `cd apps/backend && uv run pytest <paths> -v`. Lint: `uv run ruff check . && uv run ruff format --check .`
- Infra tests: `cd apps/infra && npm test`
- Any change touching `apps/infra/lib/` MUST run `cd apps/infra && npx cdk synth` and commit the regenerated `.github/workflows/deploy.yml` in the SAME commit if it changed. `infra-ci.yml` enforces this.
- Commit after each task. Conventional commit prefixes (`fix:`, `chore:`, `test:`, `refactor:`).
- Never run `git stash` (multi-worktree repo). Never merge, push to main, or dispatch a deploy.

---

### Task 1: Delete the MetronomeFastGate blocked-ness trap

The audit's #2 item. `MetronomeFastGate.is_blocked` returns `remaining <= 0`. The LIVE predicate, `metronome_gate._row_blocked`, returns `remaining <= RESERVE_ESTIMATE_MICROCENTS`. Any future caller reaching for the obvious-looking method admits an owner the bedrock-gate will then 429. Verified: zero production callers.

**Files:**
- Modify: `apps/backend/core/billing/metronome_fast_gate.py` (delete `is_blocked` and `get_remaining`; rewrite the class + module docstring)
- Test: `apps/backend/tests/unit/billing/test_metronome_fast_gate.py` (remove any tests of the deleted methods)

**Interfaces:**
- Consumes: nothing
- Produces: `MetronomeFastGate` keeps `get_row`, `set_balance`, and every other member unchanged. `set_balance`'s UPDATE-not-`put_item` behavior is load-bearing (§6) — do not touch it.

- [ ] **Step 1: Prove zero production callers**

```bash
cd apps/backend
grep -rn "\.is_blocked(\|\.get_remaining(" --include="*.py" . | grep -v tests/
```
Expected: no output. If ANY line appears, STOP and report — the premise is wrong.

- [ ] **Step 2: Delete both methods**

Remove `async def is_blocked` and `async def get_remaining` from the `MetronomeFastGate` class in `apps/backend/core/billing/metronome_fast_gate.py`. Leave `get_row`, `set_balance` and all other members exactly as they are.

- [ ] **Step 3: Correct the docstrings**

The class docstring says "The synchronous chat gate's view of Metronome balance." Replace with a docstring that states plainly: this class is the DDB projection's storage layer only; the blocked-ness predicate lives in `core.services.metronome_gate._row_blocked` and is `remaining <= RESERVE_ESTIMATE_MICROCENTS`, NOT `remaining <= 0`. Update the module docstring the same way if it repeats the old claim.

- [ ] **Step 4: Remove now-dead tests**

```bash
cd apps/backend && grep -n "is_blocked\|get_remaining" tests/unit/billing/test_metronome_fast_gate.py
```
Delete only tests that exercise the two removed methods. Keep every `set_balance` / `get_row` test.

- [ ] **Step 5: Run tests**

```bash
cd apps/backend && uv run pytest tests/unit/billing/ tests/unit/services/test_metronome_gate.py -v
```
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/billing/metronome_fast_gate.py apps/backend/tests/unit/billing/test_metronome_fast_gate.py
git commit -m "fix(billing): delete the MetronomeFastGate blocked-ness trap"
```

---

### Task 2: Lockstep tests for the cross-boundary money constants

The audit's #3 and #4, and lying-comment #1 and #2. `CAP_MICROCENTS` lives in three places and `RESERVE_ESTIMATE_MICROCENTS` in two, protected only by a "keep in lockstep" comment. The Bedrock rate card has FOUR copies and the 4th (`bedrock-gate/rates.mjs`) is the one on the settle path and the only unguarded one. `rates.mjs`'s own comment claims `rates.test.mjs` asserts byte-equality — **that file has never existed.**

**Files:**
- Modify: `apps/infra/lambda/bedrock-usage-meter/test_index.py` (add the lockstep assertions)
- Modify: `.github/workflows/backend.yml` (add the two paths so the test runs on the PRs that could break it)
- Modify: `apps/infra/lambda/bedrock-gate/rates.mjs` (correct the comment that names a nonexistent test)
- Modify: `apps/infra/test/bedrock-gate-stack.test.ts` (delete the stale `2_600_000` comment at ~line 75)

**Interfaces:**
- Consumes: `core.services.trial_cap.CAP_MICROCENTS`, `RESERVE_ESTIMATE_MICROCENTS`; `core.billing.bedrock_pricing._RATES`; `core.billing.bedrock_aip.TIER_TO_FM`
- Produces: nothing importable

- [ ] **Step 1: Confirm current values agree before locking them**

```bash
cd apps/backend && uv run python -c "
from core.services import trial_cap
print('backend CAP    ', trial_cap.CAP_MICROCENTS)
print('backend RESERVE', trial_cap.RESERVE_ESTIMATE_MICROCENTS)"
grep -n "CAP_MICROCENTS\|RESERVE_ESTIMATE_MICROCENTS" apps/infra/lambda/bedrock-usage-meter/index.py apps/infra/lib/stacks/bedrock-gate-stack.ts
```
Record the values. If they already disagree, STOP and report — that is a live bug, not a test gap.

- [ ] **Step 2: Write the failing lockstep test**

In `apps/infra/lambda/bedrock-usage-meter/test_index.py` (which already hard-imports the backend), add tests that assert:
1. `index.CAP_MICROCENTS == trial_cap.CAP_MICROCENTS`
2. The `CAP_MICROCENTS: "<digits>"` and `RESERVE_ESTIMATE_MICROCENTS: "<digits>"` literals regexed out of `apps/infra/lib/stacks/bedrock-gate-stack.ts` equal the backend values.
3. The `RATES` table parsed out of `apps/infra/lambda/bedrock-gate/rates.mjs` equals `bedrock_pricing._RATES`.
4. The `TIER_TO_FM` map parsed out of `rates.mjs` equals `bedrock_aip.TIER_TO_FM`.

For (3) and (4), `bedrock_pricing._RATES` has inline `#` comments — strip them before comparing, or shell out to `node -e` to evaluate `rates.mjs` and emit JSON. Prefer the `node -e` route: it cannot drift from how the Lambda actually parses the file.

**Do NOT put these in vitest.** `infra-ci.yml` does not run on backend PRs, so a vitest assertion fires only after the drift has merged and dev auto-deployed.

- [ ] **Step 3: Prove the tests actually catch drift**

Temporarily change `CAP_MICROCENTS` in `apps/infra/lambda/bedrock-usage-meter/index.py` to a different number, run the test, confirm FAIL, then revert. Do the same for one rate in `rates.mjs`. Paste both failures in the report — a lockstep test that cannot fail is worse than none.

- [ ] **Step 4: Add the paths to backend.yml**

Add `apps/infra/lambda/bedrock-gate/**` and `apps/infra/lib/stacks/bedrock-gate-stack.ts` to the `paths:` filter in `.github/workflows/backend.yml`, so a PR editing either runs this test.

- [ ] **Step 5: Correct rates.mjs's comment**

`apps/infra/lambda/bedrock-gate/rates.mjs` lines ~4-6 claim `rates.test.mjs` asserts byte-equality. It does not exist. Replace with a comment naming the real guard: the lockstep test in `apps/infra/lambda/bedrock-usage-meter/test_index.py`, which runs on backend PRs.

- [ ] **Step 6: Delete the stale comment in the infra test**

`apps/infra/test/bedrock-gate-stack.test.ts` ~line 75 carries a stale `2_600_000` reference. Delete it.

- [ ] **Step 7: Run tests**

```bash
cd apps/infra/lambda/bedrock-usage-meter && python -m pytest test_index.py -v
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/apps/infra && npm test
```
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add apps/infra/lambda/bedrock-usage-meter/test_index.py .github/workflows/backend.yml apps/infra/lambda/bedrock-gate/rates.mjs apps/infra/test/bedrock-gate-stack.test.ts
git commit -m "test(billing): lock the cross-boundary money constants in step"
```

---

### Task 3: Delete the orphaned billing ledger code

The audit's #6, plus lying-comment #3. `UsageEvent`'s docstring cites `LedgerBackend`, which does not exist in this repo — orphaned by PR #846 in June. `add_trial_daily_spend` has zero production callers: the meter reimplements it in raw boto3 at `index.py:596` and the gate writes it from JS.

**Files:**
- Delete: `apps/backend/core/billing/usage_event.py`, `apps/backend/tests/unit/billing/test_usage_event.py`
- Modify: `apps/backend/core/billing/bedrock_pricing.py` (delete `price_from_aip_arn`)
- Modify: `apps/backend/core/billing/bedrock_aip.py` (delete the three `resolve_*_from_aip_arn` functions)
- Modify: `apps/backend/core/repositories/billing_repo.py` (delete `add_trial_daily_spend` at ~line 1240; correct the "sole writer" comment at ~1245-1250)
- Modify: `apps/backend/core/gateway/connection_pool.py` (~1714-1719 mirrors the same false "sole writer" claim)
- Modify: tests that patch `add_trial_daily_spend`

**Interfaces:**
- Consumes: nothing
- Produces: nothing. **KEEP** `bedrock_aip.persist_owner_aips` and `_put_reverse_row` (the meter reads those rows) and **KEEP** `bedrock_pricing.price_from_tier` (Task 2's lockstep oracle depends on it).

- [ ] **Step 1: Prove each deletion target is unreferenced**

```bash
cd apps/backend
for S in "UsageEvent" "price_from_aip_arn" "resolve_owner_from_aip_arn" "resolve_tier_from_aip_arn" "resolve_plan_from_aip_arn" "add_trial_daily_spend"; do
  echo "--- $S ---"; grep -rn "$S" --include="*.py" . | grep -v "^./tests/" | grep -v "def $S"
done
```
Any hit outside tests and the definition itself means STOP and report. Note the exact `resolve_*_from_aip_arn` names may differ — list them from `bedrock_aip.py` first and check each.

- [ ] **Step 2: Delete the code**

Delete the two files, and the named functions from `bedrock_pricing.py`, `bedrock_aip.py` and `billing_repo.py`.

- [ ] **Step 3: Correct the false "sole writer" comments**

`billing_repo.py` ~1245-1250 claims "The Bedrock invocation-log meter Lambda is the **sole writer** of this counter… two writers would double-count." That is false. Rewrite it to name BOTH writers (the bedrock-gate settles first from JS; the meter counts only gateway misses) and the shared `requestId` dedup table that makes exactly-once hold. Apply the same correction to the mirrored claim in `connection_pool.py` ~1714-1719.

- [ ] **Step 4: Update the tests that patched the deleted function**

`tests/unit/routers/test_websocket_agent_chat.py:941` patches `add_trial_daily_spend` to assert it is never called. With the function gone that guard becomes structural — delete the patch and the assertion, and leave a one-line comment saying the guard is now structural. Delete the `add_trial_daily_spend` tests in `tests/unit/repositories/test_billing_repo.py`.

- [ ] **Step 5: Run tests**

```bash
cd apps/backend && uv run pytest tests/unit/billing/ tests/unit/repositories/test_billing_repo.py tests/unit/routers/test_websocket_agent_chat.py tests/unit/gateway/ -v
uv run ruff check . && uv run ruff format --check .
```
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add -A apps/backend
git commit -m "chore(billing): delete the orphaned ledger code and correct the sole-writer claim"
```

---

### Task 4: Delete the bedrock-gate spike scaffolding

The audit's #7. Five tracked files of throwaway spike code, ~950 lines.

**Files:**
- Delete: `apps/infra/lambda/bedrock-gate/spike/server.mjs`, `client.mjs`, `invoke-client.mjs`, `package.json`, `package-lock.json`
- Keep: `apps/infra/lambda/bedrock-gate/spike/SPIKE-RESULTS.md` (cited by `proxy.mjs:3-6`) and `spike/.gitignore`

- [ ] **Step 1: Confirm SPIKE-RESULTS.md is cited and the rest are not**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync
grep -rn "SPIKE-RESULTS" apps/ | head
grep -rn "spike/server\|spike/client\|spike/invoke-client" apps/ | grep -v "^apps/infra/lambda/bedrock-gate/spike/" | head
```
Expected: SPIKE-RESULTS.md IS cited; the others are not referenced outside the spike dir.

- [ ] **Step 2: Delete**

```bash
git rm apps/infra/lambda/bedrock-gate/spike/server.mjs apps/infra/lambda/bedrock-gate/spike/client.mjs apps/infra/lambda/bedrock-gate/spike/invoke-client.mjs apps/infra/lambda/bedrock-gate/spike/package.json apps/infra/lambda/bedrock-gate/spike/package-lock.json
```
If `package-lock.json` is not tracked, skip it — do not fail the task.

- [ ] **Step 3: Verify the gate still synths**

```bash
cd apps/infra && npx cdk synth >/dev/null && echo SYNTH_OK && npm test
```
Expected: SYNTH_OK and tests pass. `git status --short .github/workflows/deploy.yml` — if it changed, commit it too.

- [ ] **Step 4: Commit**

```bash
git add -A apps/infra
git commit -m "chore(bedrock-gate): delete the spike scaffolding, keep SPIKE-RESULTS.md"
```

---

### Task 5: Delete the dead paperclip_proxy surface

The audit's #8. `POST /__provision__` is a LIVE route with zero consumers that ignores `resolve_owner_id` and creates a personal company keyed on `auth.user_id` for an org caller — the exact org-scoping bug commit `ea66ac447` deleted `_autoprovision` for. It was missed by that sweep.

**Files:**
- Modify: `apps/backend/routers/paperclip_proxy.py` (delete `_provisioning_response` ~266-342, `_bootstrap_html` ~800-916, `_decode_clerk_publishable_key` ~1126-1194, and the `POST /__provision__` route)
- Modify: the two tests covering `_decode_clerk_publishable_key`

- [ ] **Step 1: Prove zero consumers**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync
grep -rn "__provision__" apps/ packages/ --include="*.py" --include="*.ts" --include="*.tsx" | grep -v "routers/paperclip_proxy.py"
grep -rn "_provisioning_response\|_bootstrap_html\|_decode_clerk_publishable_key" apps/backend --include="*.py" | grep -v "routers/paperclip_proxy.py"
```
Expected: only test references. Any frontend/mobile caller of `__provision__` means STOP and report.

- [ ] **Step 2: Read the route before deleting it**

Read the `POST /__provision__` handler in full and confirm it calls `provision_org` without single-flight and keys on `auth.user_id` rather than `resolve_owner_id`. State this in the report. Note `docs/audit-2026-05-02-paperclip-routes.md` documents this file's route-filter security model — read it before editing.

- [ ] **Step 3: Delete the four items and their tests**

- [ ] **Step 4: Run tests**

```bash
cd apps/backend && uv run pytest tests/unit/routers/ -k "paperclip" -v && uv run ruff check .
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A apps/backend
git commit -m "fix(paperclip): delete the ungated __provision__ route and its dead HTML stubs"
```

---

### Task 6: Hoist the onboarding-state defaults to one owner

The audit's #12. `readiness_state.py:141-163` hand-copies `paperclip_repo`'s 25-key defaults dict and is **already missing 4 keys** (`first_contact_state`, `first_contact_attempts`, `first_contact_reason`, `friday_agent_id`). Commit `6b42f427e` (#1820) already paid this lockstep tax once.

**Files:**
- Modify: `apps/backend/core/repositories/paperclip_repo.py` (add module-level `ONBOARDING_STATE_DEFAULTS`)
- Modify: `apps/backend/core/services/readiness_state.py:141-163` (import and use it)
- Test: `apps/backend/tests/unit/services/test_readiness_state.py`

**Interfaces:**
- Produces: `paperclip_repo.ONBOARDING_STATE_DEFAULTS: dict` — the single defaults dict, consumed by `readiness_state`.

- [ ] **Step 1: Diff the two dicts and record the delta**

```bash
cd apps/backend && sed -n '135,170p' core/services/readiness_state.py
grep -n "first_contact_state\|friday_agent_id\|onboarding" core/repositories/paperclip_repo.py | head -30
```
Write the exact missing-key list into the report before changing anything.

- [ ] **Step 2: Write the failing test**

Add a test to `tests/unit/services/test_readiness_state.py` asserting the two key sets are identical:

```python
def test_readiness_onboarding_keys_match_repo_defaults():
    from core.repositories import paperclip_repo
    from core.services import readiness_state
    assert set(readiness_state._onboarding_defaults().keys()) == set(
        paperclip_repo.ONBOARDING_STATE_DEFAULTS.keys()
    )
```

Adapt the accessor name to whatever `readiness_state` actually exposes — read lines 135-170 first and use the real symbol. Run it, confirm it FAILS listing the 4 missing keys, and paste the failure into the report.

- [ ] **Step 3: Extract the constant and use it**

Define `ONBOARDING_STATE_DEFAULTS` at module level in `paperclip_repo.py` (sourced from the existing in-function dict), have `paperclip_repo` use it, and replace the hand-copy in `readiness_state.py` with an import.

- [ ] **Step 4: Verify the extra keys are inert**

Confirm the 4 newly-present keys do not change any consumer's behavior — the audit states they are provably inert on the wrapper's consumers, but verify it and say so. Note the row-*present* return at `paperclip_repo.py:660` is a THIRD copy this task does not unify; leave it and note it in the report.

- [ ] **Step 5: Run tests**

```bash
cd apps/backend && uv run pytest tests/unit/services/test_readiness_state.py tests/unit/repositories/ tests/unit/routers/test_container_status.py -v
```
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add -A apps/backend
git commit -m "fix(onboarding): one owner for the onboarding-state defaults"
```

---

### Task 7: Delete the admin-metrics middleware

The audit's cost section. 206 CloudWatch metric identities, **read by nothing**. 357 lines across the middleware, its registration and its test.

**Files:**
- Delete: `apps/backend/core/middleware/admin_metrics.py`, `apps/backend/tests/unit/middleware/test_admin_metrics.py`
- Modify: `apps/backend/main.py:741` (remove the import and the `add_middleware` registration)

- [ ] **Step 1: Prove nothing reads the metrics**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync
grep -rn "admin_api\." apps/infra/lib/stacks/observability-stack.ts | head
grep -rn "AdminMetricsMiddleware\|admin_metrics" apps/ --include="*.py" --include="*.ts" | grep -v "core/middleware/admin_metrics.py\|tests/unit/middleware/test_admin_metrics.py"
```
If any dashboard or alarm in `observability-stack.ts` references an `admin_api.*` metric, STOP and report — deleting the emitter would blank a live dashboard.

- [ ] **Step 2: Delete the two files and the registration in main.py**

- [ ] **Step 3: Run tests**

```bash
cd apps/backend && uv run pytest tests/unit/ -x -q 2>&1 | tail -20 && uv run ruff check .
```
Expected: PASS. Confirm the app still imports: `uv run python -c "import main"`.

- [ ] **Step 4: Commit**

```bash
git add -A apps/backend
git commit -m "chore(observability): delete the admin-metrics middleware nothing reads"
```

---

### Task 8: Prod-gate the account-level observability resources

The audit's #11 and pattern P5. Dev and prod share ONE AWS account, so a `$1,500` account-wide Budget with no CostFilters and the GuardDuty + Access Analyzer EventBridge rules are each created twice and mail the same `admin@` inbox twice. The file's own comments three lines away establish the ENV-BLIND → PROD-GATED rule.

**Files:**
- Modify: `apps/infra/lib/stacks/observability-stack.ts` (~2243-2283 `MonthlyBudget`; ~3040-3075 `createAccountHardening`)
- Modify: `apps/infra/test/observability-stack.test.ts` (~283 `Budgets::Budget` count; ~279 `Events::Rule` count 5 → 3 for dev, plus a new prod assertion)

- [ ] **Step 1: Read the neighbouring ENV-BLIND comment and follow its pattern exactly**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync && grep -n "ENV-BLIND\|envName === \"prod\"" apps/infra/lib/stacks/observability-stack.ts | head -20
```
Use the same guard idiom already in the file. Do not invent a new one.

- [ ] **Step 2: Gate both resources**

Wrap `MonthlyBudget` in `if (this.envName === "prod")`. Early-return from `createAccountHardening` unless prod.

- [ ] **Step 3: Update the test counts**

The `Events::Rule` enumeration at ~279 is a **deliberate guard, not a stale count** — update it to 3 for dev and ADD a prod assertion rather than deleting it.

- [ ] **Step 4: Synth and test**

```bash
cd apps/infra && npx cdk synth >/dev/null && echo SYNTH_OK && npm test
git status --short .github/workflows/deploy.yml
```
Expected: SYNTH_OK, tests pass. If deploy.yml changed, `git add` it.

- [ ] **Step 5: Commit**

```bash
git add -A apps/infra .github/workflows/deploy.yml
git commit -m "fix(observability): prod-gate the account-wide budget and hardening rules"
```

---

### Task 9: Delete the subsumed IAM statements and the dead table-name helper

The audit's #9 part (a) ONLY. Two DynamoDB IAM statements are exactly subsumed by a wildcard added in August (`DynamoDbOwnedTables`, wider actions, same role); #2103 deleted the statements it subsumed and left these two only because tests assert on the sid. `legacyTablePrefix` has zero call sites but is imported by nine files.

**Files:**
- Modify: `apps/infra/lib/stacks/service-stack.ts` (~806-849 `DynamoDbIntegrations`, ~1154-1174 `DynamoDbConnections`)
- Modify: `apps/infra/lib/table-names.ts:24` (delete `legacyTablePrefix`) and its nine importers
- Modify: `apps/infra/test/integrations-resources.test.ts:168,211`; `apps/infra/test/credential-export-decouple.test.ts:176`; `apps/infra/test/service-stack.test.ts:265`

**Interfaces:**
- Produces: nothing. **DO NOT** touch `allTableArns` / `bothTableArns` or the leading `*` wildcard — deferred to a separate plan.

- [ ] **Step 1: Prove subsumption before deleting**

Read `DynamoDbOwnedTables` and both statements being deleted. Confirm in writing, in the report, that `DynamoDbOwnedTables` covers every resource ARN AND every action of both, on the SAME role. If it does not strictly subsume them, STOP and report.

- [ ] **Step 2: Prove `legacyTablePrefix` is unused**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync && grep -rn "legacyTablePrefix" apps/ | grep -v "table-names.ts"
```
Expected: only imports, never a call. Remove the symbol and every now-unused import.

- [ ] **Step 3: Delete, then update the four test assertions**

- [ ] **Step 4: Synth, diff, test**

```bash
cd apps/infra && npx cdk synth >/dev/null && echo SYNTH_OK && npm test
npx cdk diff 2>&1 | grep -iE "IAM|Policy" | head -30
```
Confirm the diff shows ONLY the two statements being removed and no change to any other policy. Paste that diff excerpt into the report. If `deploy.yml` changed, commit it.

- [ ] **Step 5: Commit**

```bash
git add -A apps/infra .github/workflows/deploy.yml
git commit -m "chore(iam): delete the two subsumed DynamoDB statements and the dead legacyTablePrefix"
```

---

### Task 10: Correct the remaining lying comments

The audit's #5 and lying-comment #4. A comment in this repo is load-bearing documentation; a wrong one is worse than none.

**Files:**
- Modify: `CLAUDE.md` (lines ~481, ~770, ~1036 and the diagram at ~425-433)
- Modify: `apps/backend/core/gateway/connection_pool.py` (~3686-3712 veto rationale)

- [ ] **Step 1: Verify the real header name**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync
grep -rn "Isol8-Public-Host\|Lightbulb-Public-Host" apps/ packages/ | head
```
Expected: the live header is `X-Isol8-Public-Host` (`api-stack.ts:207`, `main.py:148`, `paperclip_proxy.py:13`) and `X-Lightbulb-Public-Host` appears in NO source file. Confirm before editing.

- [ ] **Step 2: Fix CLAUDE.md**

Correct all four occurrences to `X-Isol8-Public-Host`, and add it to the "deliberate residue" list in the Rebrand section so nobody renames it casually.

- [ ] **Step 3: Correct the connection_pool veto rationale**

The comment at ~3686-3712 says direct callers "deliberately stop despite a live connection, fenced by the `has_active_runs`/`is_parked` vetoes." Commit `9213918ad` (#1670) added an unconditional locked re-check at ~3742 that fires for EVERY trigger on a superset condition, making that rationale false and vetoes 4-5 outcome-redundant. Rewrite the comment to describe what the code actually does now. **Do NOT change the code** — §6 says vetoes 7-9 are load-bearing TOCTOU re-evaluations. Comment only.

- [ ] **Step 4: Verify no behavior changed**

```bash
cd apps/backend && git diff --stat && uv run pytest tests/unit/gateway/test_connection_pool_scale_to_zero.py -v
```
Expected: only comment/doc lines in the diff for `connection_pool.py`; tests PASS.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md apps/backend/core/gateway/connection_pool.py
git commit -m "docs: correct the public-host header name and the stale veto rationale"
```
