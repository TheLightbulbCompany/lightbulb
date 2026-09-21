# Terra Goal Prompt — Production OpenRouter, Free, Base, Credits, and Bedrock Retirement

You are implementing a production-critical inference and billing migration in the Lightbulb monorepo. Work autonomously until the defined outcome is complete. Do not stop after proposing a plan, implementing only a gateway, or making tests pass in isolation. Preserve customer data and billing semantics. Read the repository instructions and the linked design documents before editing.

## Objective

Ship the complete customer-facing OpenRouter system:

- a 30-day no-card Free tier using only `nvidia/nemotron-3-ultra-550b-a55b:free`;
- a $20/month Base tier using the same single free model initially;
- the existing $50 Workspace and $150 Ultra tiers unchanged in price and benefits;
- one Lightbulb-owned OpenRouter completion key per workspace, invisible to customers;
- paid managed inference settled through the existing Lightbulb credit system, Stripe, and Metronome;
- coherent web, mobile, desktop, Teams, listening-mode, scheduled-work, and agent-facing behavior;
- production-safe logs, metrics, alarms, reconciliation, rollback, and eventual Bedrock AIP cleanup.

The work is complete only after local checks, dev deployment, end-to-end dev acceptance, staged production rollout, and the separately gated Bedrock cleanup criteria described below. Do not claim completion because unit tests pass.

## Repository and branch

- Repository: `/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync`
- Isolated worktree: `/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/.claude/worktrees/openrouter-credits`
- Branch: `codex/openrouter-credits`
- Read `CLAUDE.md`; it overrides `AGENTS.md` if they disagree.
- Never modify production servers directly. All application, Lambda, configuration, and infrastructure changes go through source control and CI/CD.
- Dev deploys on merge; production requires an explicit `deploy.yml` workflow dispatch.
- Watch every CI run after pushing.
- Do not merge or enable production traffic until the acceptance gates pass.

## Required source material

Read these fully before editing:

1. `docs/superpowers/specs/2026-09-20-managed-openrouter-billing-design.md`
2. `docs/superpowers/plans/2026-09-20-managed-openrouter-switch.md`
3. `docs/runbooks/openrouter-managed-inference.md`
4. `docs/runbooks/workspace-stripe-metronome-configuration.md`
5. `apps/infra/lib/stacks/database-stack.ts`
6. `apps/infra/lib/stacks/openrouter-gate-stack.ts`
7. `apps/infra/lambda/openrouter-gate/`
8. `apps/infra/lambda/openrouter-settlement/`
9. `apps/backend/core/services/openrouter_keys.py`
10. `apps/backend/core/services/hermes_profiles.py`
11. `apps/backend/core/services/hermes_provisioning.py`
12. `apps/backend/core/services/container_provision.py`
13. `apps/backend/core/services/provision_gate.py`
14. `apps/backend/core/services/billing_service.py`
15. `apps/backend/core/repositories/billing_repo.py`

The plan and spec are working documents. Update them when the implementation changes. Delete them when the feature ships, as required by repository policy; Git history is the durable record.

## Product contract — do not reinterpret

### Free

- 30 days from canonical workspace readiness, not account creation and not Stripe checkout.
- No credit card and no automatic charge.
- Full workspace and listening/scheduled-agent behavior.
- Exactly one model: `nvidia/nemotron-3-ultra-550b-a55b:free`.
- No model picker, provider picker, BYOK prompt, OpenRouter account prompt, Lightbulb credit requirement, or per-owner request cap initially.
- A global environment kill switch is required.
- At day 30, inference and workspace execution block on upgrade. Preserve the workspace and all S3 profile data.
- A separately purchased Lightbulb credit balance does not extend the 30-day entitlement.

### Base

- $20/month through a separate dev/prod Stripe recurring Price.
- The same single Nemotron Ultra free-model policy initially.
- No synthetic included credit grant while the model is zero-cost.
- A successful authenticated Stripe invoice activates or renews Base.
- Invoice failure, cancellation, or expiration must remove Base entitlement without deleting workspace data.
- Base does not expose BYOK, ChatGPT OAuth, an OpenRouter key, the public model catalog, or paid managed models.

### Workspace and Ultra

- Existing $50 Workspace and $150 Ultra Stripe prices stay unchanged.
- Preserve direct OpenAI/Anthropic BYOK and ChatGPT OAuth behavior currently available on Workspace.
- Preserve the existing Lightbulb credit purchase UX.
- Paid OpenRouter inference uses Metronome as customer-money truth and Stripe as payment processor.
- Purchased credits survive Free→Base, Base→Workspace, upgrades, downgrades, and provider switches.
- Do not rename the idempotent Metronome request description strings beginning with `isol8 top-up`.

## Security and ownership invariants

- Lightbulb owns the OpenRouter account, management key, workspace, and completion keys.
- Customers never need or receive an OpenRouter account or key.
- Management key secret: `lightbulb/{env}/openrouter/management`.
- Completion-key plaintext exists only in Secrets Manager under the owner-key prefix.
- Store only hash/state/policy/secret reference in DynamoDB.
- Never put an `sk-or-` value in a Hermes S3 profile, AgentCore runtime environment, browser response, log, metric, exception, test snapshot, or frontend bundle.
- Hermes sends primary and every auxiliary task through the Lightbulb gate or fails closed. No OpenRouter/Hermes auto-chain fallback.
- Agent-edited profiles cannot choose an upstream URL, arbitrary model, fallback model, provider policy, router plugin, server tool, or unpriced multimodal mode.
- Provider switches freeze turn admission, wait for idle, rewrite every roster profile, update the runtime spec, stop the warm session, and thaw only the same serialized generation.
- One pending provider switch per workspace. A same-choice retry re-dispatches the current generation; a different choice receives a conflict.

## Current verified external state

- AWS account: `877352799272`, CLI profile `isol8-admin`, region `us-east-1`.
- Dev management secret exists: `lightbulb/dev/openrouter/management`.
- OpenRouter dev workspace exists:
  - name: `Lightbulb Dev`
  - slug: `lightbulb-dev`
  - ID: `5db3e7d3-4447-43b8-ac39-b1e35e3f54a9`
- Live management-key proof passed: create 201; get/disable/delete 200; plaintext returned only at creation.
- Workspace-specific disposable-key proof passed and returned the exact dev workspace ID.
- Nemotron Ultra free sentinel passed live: completion 200, `LIGHTBULB_ULTRA_OK`, reported cost 0, disposable key deleted. An earlier call returned no completed choice, so transient empty-choice behavior must be tested and handled.
- OpenRouter account currently reports zero purchased credits and zero usage. Do not require a purchase for Free/Base work.
- Metronome nanodollar proof passed exactly: `12,345,678` nanodollars debited `$0.012345678`; duplicate transaction ID produced no second debit.
- Dev Stripe–Metronome mapping is wrong. The runbook contains the exact IDs and repair order. Do not claim paid end-to-end acceptance until repaired.
- GitHub CLI authentication is currently invalid.
- OrbStack is running but the Docker socket is absent, so Docker-based CDK asset synthesis currently fails.

## Current branch state

Important commits:

- `922dee794` — disabled managed OpenRouter implementation
- `718ab4ebe` — live dev key/workspace proof record
- `cac298bc6` — free-model smoke record
- `1ecda2dbb` — Nemotron Ultra smoke record
- `d4b451233` — 30-day free trial entitlement and copy
- `906b45d21` — audited production-readiness spec and plan

There are currently uncommitted changes in:

- `apps/infra/lambda/openrouter-gate/core.mjs`
- `apps/infra/lambda/openrouter-gate/core.test.mjs`
- `apps/infra/lambda/openrouter-gate/ledger.mjs`

Those changes add zero-price request admission and durable outbox creation without a Metronome reservation. The focused gate/ledger suite passes. Review and finish them; do not discard them.

## Existing storage decisions

Keep exactly the two new OpenRouter tables already implemented:

1. `openrouter-keys`: durable owner key claim/lifecycle state.
2. `openrouter-requests`: durable request/generation/outbox and reconciliation state with the pending-state index.

Do not add another billing ledger or quota table. Metronome remains customer-money truth. The existing `usage-counters` table is available if a future owner quota becomes necessary, but this release explicitly has no per-owner Free/Base request cap.

## Work package 1 — finish zero-cost gate and settlement

Write or complete tests before implementation.

Required behavior:

- A policy is free only when both prompt and completion price ceilings are exactly zero and the requested model equals the configured Free/Base model.
- A zero-cost request still validates bearer, owner, entitlement, model, request size, streaming mode, text-only content, tools, and owner key.
- Free requests do not read or mutate Metronome fast-gate balance and do not require a Metronome customer/contract.
- Persist the request outbox before forwarding and persist generation ID before emitting its first bytes.
- Definitive pre-generation denial marks the free outbox released without a money mutation.
- Disconnect/ambiguous failure remains pending for reconciliation.
- Settlement fetches generation metadata. If actual cost is exactly zero, mark settled, store tokens/model/provider/cancellation metadata, claim the generation idempotently, and do not ingest Metronome or touch the fast gate.
- If a supposedly free generation reports any positive cost, fail closed, alarm, preserve the pending record, and stop widening rollout. Never silently treat it as free.
- Paid requests retain the existing atomic reservation, Metronome event, nanodollar precision, replay protection, and fast-gate correction unchanged.
- Daily activity reconciliation includes free keys and treats an unexpected positive-cost gap as a production blocker.

Required checks:

```bash
cd apps/infra
node --test lambda/openrouter-gate/*.test.mjs
uv run pytest lambda/openrouter-settlement/test_index.py -q
```

## Work package 2 — route trial and Base through Ultra

Add an internal provider/config mode for Free/Base rather than storing a customer-selectable provider choice on `trial_local`.

Required behavior:

- `provider_choice_from_row(trial_local)` resolves to an internal OpenRouter free mode, not `bedrock_trial`.
- Base resolves to that same internal mode from its plan/entitlement field.
- The internal mode produces a Hermes custom-provider block pointing to the Lightbulb gate and `${LIGHTBULB_GATE_BEARER}`.
- Primary and all entries in `AUXILIARY_TASKS` use the pinned Ultra model and empty fallback chains.
- Context and output ceilings are explicit and verified against the pinned Hermes image.
- Provisioning calls `ensure_owner_key` before writing profiles or activating runtime ingress.
- Free/Base owner-key policy is zero-cost Ultra only. Paid Workspace owner keys remain compatible with later paid policies.
- Trial/Base runtime roles have no direct Bedrock inference permission.
- Existing workspaces change through the serialized provider-switch convergence path; no warm session continues using Bedrock after the row resolves to OpenRouter.
- `models.list` returns only Ultra for Free/Base.
- `lightbulb-facts.json` reports the customer plan and model without exposing internal provider details or credentials.

Test primary, auxiliary, delegation, cron, Teams/Paperclip, profile-edit bypass, warm-session convergence, and key-provision failure.

## Work package 3 — complete the $20 Base billing lifecycle

Add separate `STRIPE_BASE_PRICE_ID_DEV` and `STRIPE_BASE_PRICE_ID_PROD` configuration and a server-owned `$20` display price. Never derive authorization from a client price string.

Required behavior:

- Checkout creates one subscription item for Base.
- No card is requested for Free signup.
- Free does not auto-convert or auto-charge at day 30.
- An authenticated successful Base invoice activates or renews Base.
- Failed/incomplete/unpaid/canceled Base blocks execution and routes to billing recovery.
- Duplicate and out-of-order webhooks cannot activate twice, revert a newer state, or create duplicate subscriptions.
- Free→Base preserves owner, AgentCore runtime, S3 profiles, Paperclip company, messages, marketplace state, purchased credits, and OpenRouter owner key.
- Base→Workspace and Workspace→Base change the entitlement/model/provider controls without replacing the workspace.
- Billing portal and cancellation return to the correct Lightbulb surface.
- Apple historical reconciliation remains correct; do not invent a Base IAP product unless explicitly authorized.

## Work package 4 — unify every customer surface

Create or reuse one server-owned plan catalog. All surfaces must render the same matrix:

| Plan | Price | Model/provider behavior |
| --- | ---: | --- |
| Free | $0 for 30 days | Nemotron Ultra only; no card; no provider/model picker |
| Base | $20/month | Nemotron Ultra only; no provider/model picker |
| Workspace | existing $50/month | Lightbulb managed credits, BYOK, ChatGPT OAuth |
| Ultra | existing $150/month | Existing Ultra benefits |

Cover web, mobile, desktop, signup, pricing, settings, provider controls, chat error cards, trial countdown, expired state, billing portal, upgrade flows, and agent-facing skills/facts. Free/Base never show OpenRouter branding or keys. Update the mobile AI-data recipient disclosure and version consent before any mobile traffic reaches OpenRouter.

## Work package 5 — repair and prove credits

Use Stripe test mode and the Metronome dev sandbox. No real-money purchase is required.

- Repair the environment mismatch exactly as documented in the runbook.
- Preserve existing customers/contracts/balances; do not null IDs or recreate legitimate rows.
- Add the OpenRouter nanodollar metric/rate additively to the existing dev rate card so Bedrock rollback rates remain active.
- Read every affected contract's effective rate schedule; a rate-card read alone is insufficient.
- Prove one purchase UUID yields one Stripe test collection and one Metronome grant under retries.
- Prove one paid OpenRouter generation yields one debit with exact decimal arithmetic and duplicate replay yields none.
- Prove depleted balance blocks paid inference, top-up restores it, and Free/Base zero-cost inference remains independent of balance.
- Preserve `isol8 top-up …` idempotency descriptions.

## Work package 6 — observability and privacy

Logs/metrics may contain owner ID, key hash, request ID, generation ID, model/provider, status, token counts, cost, policy revision, latency, retry count, and settlement age. They must never contain prompt/completion content, bearer tokens, plaintext completion keys, management keys, or full Secrets Manager payloads.

Add dashboards/alarms for:

- OpenRouter 401/402/429/5xx and empty choices;
- key provision/rotation/retirement failures;
- missing generation IDs;
- pending settlement age and activity gaps;
- free generation reporting positive cost;
- gate admission/denial by plan/reason;
- cohort error rate and latency;
- pooled OpenRouter balance for paid traffic.

Support diagnostics must resolve owner → key hash → gate request → generation → Metronome event without reading content or credentials.

## Work package 7 — infrastructure, CI, and dev deployment

- Restore a working local Docker engine or prove the exact CI bundling path another way. Do not rewrite Lambda packaging merely to avoid a broken local daemon.
- Run CDK synth twice if workflow asset-job discovery changes generated assets.
- Run workflow transport and production-dispatch guard tests.
- Restore `gh` authentication, push the feature branch, and open a reviewable PR.
- Watch every CI run.
- Configure dev variables only after code is committed:
  - `OPENROUTER_WORKSPACE_ID_DEV=5db3e7d3-4447-43b8-ac39-b1e35e3f54a9`
  - pinned Ultra model ID
  - reviewed zero-cost model policy
  - safe dev owner-key ceiling/backstop
- Keep every customer flag and schedule disabled through infrastructure deployment, then enable only the dedicated dev cohort.

## Work package 8 — full dev acceptance

Use fresh dev accounts and the production provisioning funnel. Validate:

1. New signup → no card → six-agent starter roster → trial clock arms once at readiness +30 days.
2. All six agents can chat and call tools through Ultra.
3. Listening mode, scheduled work, Teams/Paperclip, delegation, compression, title/review, mobile, desktop, and AgentCore Browser use the same gate.
4. No upstream key exists in profile S3, runtime env, UI, logs, or browser responses.
5. Empty choice, 429, disconnect, runtime restart, key rotation, and gate retry recover safely.
6. Day-30 expiration blocks execution and preserves workspace state.
7. Free→Base succeeds only after Stripe test invoice success and keeps all state.
8. Base renewal failure/cancellation blocks correctly.
9. Base→Workspace exposes paid controls and credits without losing state.
10. Paid credit purchase, debit, replay, exhaustion, recovery, and Bedrock rollback all pass.

Record exact evidence in the runbook. Do not call a mocked unit test an end-to-end acceptance test.

## Work package 9 — staged production rollout

- Obtain written OpenRouter permission for Lightbulb-funded customer-facing managed inference and confirm selected provider/model terms.
- Verify production secrets/workspace/Stripe Prices/Metronome rate contract through readback without exposing values.
- Deploy through CI/CD with all flags off.
- Enable one internal production owner.
- Observe multiple daily reconciliation windows, costs, errors, latency, and key activity.
- Expand by explicit cohorts with an immediate kill switch and documented Bedrock rollback.
- Do not auto-migrate every owner at once.

## Work package 10 — Bedrock AIP retirement

This is a separate final PR after OpenRouter production acceptance.

Before mutation, produce a dry-run inventory proving:

- zero active owners resolve to Bedrock;
- zero Hermes profiles/runtime specs reference Bedrock AIPs or gate URLs;
- zero unsettled Bedrock usage remains;
- no rollback cohort still requires Bedrock;
- every owner has a healthy OpenRouter key or intentionally uses BYOK/ChatGPT.

Then idempotently remove only retired inference assets:

- owner AIPs and AIP reverse-index rows;
- Bedrock-only invoke permissions and owner policy statements;
- Bedrock gate/meter schedules, resources, alarms, and obsolete configuration;
- spent migration code and one-shot scripts after the migration runs.

Preserve AgentCore runtimes, Hermes S3 profiles, Paperclip companies/agents, messages, integrations, subscriptions, credits, marketplace data, and OpenRouter keys. A rerun must be safe. Keep a documented rollback point until the cleanup PR is accepted.

## Required local verification

Run focused tests after each task, then the appropriate broad suites once at the end:

```bash
cd apps/backend && uv run ruff check .
cd apps/backend && uv run pytest tests/ -q
cd apps/infra && node --test lambda/openrouter-gate/*.test.mjs lambda/bedrock-gate/core.test.mjs
cd apps/infra && uv run pytest lambda/openrouter-settlement/test_index.py -q
cd apps/infra/hermes && ../../backend/.venv/bin/python -m pytest tests/ -q
cd apps/infra && ./node_modules/.bin/vitest run
cd apps/frontend && ./node_modules/.bin/tsc --noEmit
cd apps/frontend && ./node_modules/.bin/vitest run
cd apps/mobile && pnpm test
cd apps/infra && ./node_modules/.bin/cdk synth --quiet
git diff --check
```

If an existing unrelated suite cannot run, identify the exact environmental or pre-existing cause. Do not hide a failure, weaken assertions, or call the feature complete without equivalent evidence.

## Engineering rules

- Use TDD for money, authorization, concurrency, lifecycle, and provider-routing behavior.
- Fix shared root causes after tracing every caller; do not patch only one route.
- Prefer deletion and existing primitives. Do not add a third OpenRouter table, second billing ledger, speculative provider abstraction, or duplicate plan catalog.
- Preserve exact decimal arithmetic for money; never use binary floats for settlement.
- Every external write is idempotent or durably claimed.
- All teardown is deferred until pending requests drain.
- Default every new route/schedule/capability to off.
- Never make a production mutation outside CI/CD.
- Never log or print secrets, even in dev probes.
- Do not use `sleep` or `tail` in shell commands.
- Do not change unrelated branding residue or protected identifiers documented in `CLAUDE.md`.

## Definition of done

Do not mark the goal complete until all of these are true:

- Free, Base, Workspace, and Ultra behave exactly as the product matrix specifies on every client.
- Free signup and 30-day use require no card; expiry never charges automatically.
- Base billing is live at $20 with correct invoice lifecycle.
- Every Lightbulb-funded inference request goes through the gate with a workspace key that customers cannot see.
- Free requests settle at zero without Metronome; any positive cost fails closed.
- Paid requests debit Metronome exactly once and reconcile to OpenRouter activity.
- Stripe test-mode purchase/top-up/replay passes end to end.
- Logs, dashboards, alarms, support diagnostics, and privacy consent are complete.
- Dev acceptance passes all four customer journeys.
- Production cohort rollout has healthy observed daily reconciliation windows.
- Bedrock cleanup dry-run is zero-surprise, final cleanup passes, and preserved data is verified.
- CI is green except for repository-documented accepted reviewer outages.
- The PR description accurately states final behavior, validation, rollout, and residual risk.

When blocked on external credentials, commercial approval, payment-provider UI, or production cohort observation, complete every independent code/test/documentation task first. Report the exact blocker and the concrete artifact ready for review; do not substitute a mock for the missing live proof.
