# Chaos SP1 — Findings & Test-Quality Audit

Audit artifact for the SP1 chaos suite (`apps/backend/tests/chaos/`). Two parts:
- **Part A — proactive audit** of existing tests for the five invariant areas.
- **Part B — empirical findings** surfaced by chaos machine failures.

**Headline:** across all five invariant areas, the chaos machines **pass** — no
product invariant breaks were found at the `ci` profile (50 examples each). Every
failure encountered during implementation was a *test-harness* bug (wrong fixture
kwargs / wrong invariant probe), corrected in the same task. That is a reassuring
pre-launch result for the logic that was in scope; see the deferred slices below
for what was NOT exercised.

---

## Part A — Existing test audit

Rubric: **over-mock** (data layer mocked so the invariant is never exercised) ·
**vacuous** (assertions that cannot fail) · **masking** (`xfail`/`skip`/loosened
assert hiding a regression) · **coverage-shape** (happy-path-only where chaos
explores failure/ordering space).

| Test file | tests | Real DB? | Rubric flag | Note | Action |
|-----------|-------|----------|-------------|------|--------|
| `tests/unit/containers/test_config.py` | 47 | n/a (pure fn) | none | Asserts on the pure builder output; no vacuous asserts (0 `.called`). Strong. | none |
| `tests/test_paperclip_provisioning.py` | 33 | moto | none | Uses real moto repo. Good fidelity. | none |
| `tests/unit/services/test_webhook_dedup.py` | 4 | moto | coverage-shape | Real conditional-write dedup, but happy-path only — no duplicate/out-of-order stream. | complemented by `billing_machine` |
| `tests/unit/services/test_stripe_webhook_handlers.py` | 7 | mocked | over-mock (mild) | `billing_service` mocked; exercises handler branching, not DDB state. | complemented by chaos (dedup); handler-lifecycle still a follow-up |
| `tests/unit/routers/test_marketplace_purchases.py` | 13 | mocked | over-mock (mild) | Stripe + repo mocked (21 `return_value`/`called`). | marketplace state transitions now moto-backed in `marketplace_machine` |

**No masking constructs** (`xfail`/`skip`/`pytestmark`-skip) found in any of the
five areas — a clean result; nothing to remove. The two mild over-mock flags are
not broken tests: they're unit tests doing legitimate handler/branch coverage. The
chaos suite adds the missing real-data-layer + ordering coverage rather than
replacing them. **No existing test was changed** (nothing met the "cheap fix"
bar; no vacuous assertion or stale xfail was found).

---

## Part B — Chaos findings

### Product invariants: all holding (no bugs found)

| Machine | Invariant | Result | Existing test that already covered it |
|---------|-----------|--------|----------------------------------------|
| `invariants/test_config_schema.py` | `build_openclaw_config_dict` never raises across provider/owner combos | PASS (50 ex.) | `test_config.py` (example-based) |
| `invariants/test_usage_counters.py` | owner rollup == recorded volume == Σ member rows; `request_count` exact | PASS (50 ex.) | partial (`test_usage*`) |
| `machines/marketplace_machine.py` | real transitions == reference SM; ≤1 published row; illegal acts raise documented exc | PASS (50 ex.) | `test_marketplace_purchases.py` (happy path) |
| `machines/provisioning_machine.py` | `provision_org` idempotent short-circuit; N calls→1 row; zero HTTP | PASS (50 ex.) | `test_paperclip_provisioning.py` |
| `machines/billing_machine.py` | webhook dedup exactly-once, order-independent | PASS (50 ex.) | `test_webhook_dedup.py` (happy path) |

No code fixes or regression tests were required, because no invariant broke.

### Test-harness bugs fixed during implementation (not product bugs)

These are recorded for transparency — each was a mistake in the chaos test itself,
caught by its own run, fixed in the same task:
- **conftest table schemas:** usage SK is `period` (not `sk`); dedup table is
  `webhook-event-dedup`; three different table-name mechanisms (prefix / env var /
  settings) had to be wired. (Task 2)
- **config strategy:** real `provider_choice` values are `bedrock_claude` /
  `chatgpt_oauth` / `byo_key`; `bedrock_claude` requires `aip_arns` with
  **opus/sonnet/haiku** (three keys). (Tasks 3, 5)
- **usage accumulation:** `@given` reuses the function-scoped moto fixture and
  Hypothesis shrinks nonces into collisions → reset per example via
  `delete_all_for_owner`. (Task 6)
- **marketplace probe:** `get_published_by_id` falls back to the highest version
  when nothing is published, so it's the wrong probe for "≤1 published" — switched
  to a raw count over all versions. (Task 7)

### Deferred slices (follow-ups — NOT exercised by SP1)

These are honest coverage gaps the SP1 base does not close. They are the most
likely places a real bug could still hide:

1. **Config-brick zod validation is INERT until the schema bundle is vendored.**
   `test_emitted_config_passes_upstream_schema` auto-skips because
   `OpenClawSchema`'s import graph pulls ~30 `@openclaw/*` workspace packages that
   only resolve after a full `pnpm install && pnpm build` of the fork. **Action for
   the user:** run `apps/backend/scripts/vendor_openclaw_schema.sh` on a machine
   with the fork built, commit the generated `openclaw-config-schema.mjs`, and the
   flagship config-brick test goes live automatically (staleness guard keeps it
   pinned). Until then only the no-crash slice runs.
2. **Provisioning full-chain-from-empty** (`sign_up → create_company → invite →
   create_agent`) is not exercised — only the idempotency short-circuit is. Faking
   it faithfully needs ~7 admin methods + the session-cookie challenge and would be
   mock-dominated. The member-join orphan-422 path lives here.
3. **Billing handler lifecycle** (subscription `trialing→active→past_due→canceled`
   legality, marketplace-purchase entitlement-exactly-once, Metronome credit
   lifecycle) is not driven end-to-end — only the dedup gate beneath them is.
   Needs `respx`-stubbed Stripe + `BillingService` construction.
4. **Marketplace multi-version atomicity** (`publish_new_version` TransactWriteItems
   flipping v_new→published + v_prev→retired) is not tested — only single-version
   transitions. Leans on moto's TransactWriteItems fidelity.
