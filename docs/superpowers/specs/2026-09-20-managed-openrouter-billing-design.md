# Production OpenRouter inference, free trial, and Base tier

## Product boundaries

Lightbulb owns the OpenRouter account, management key, and every per-workspace inference key. Customers continue to see Lightbulb subscriptions, Lightbulb credits, and the existing model/provider settings; they never need an OpenRouter account or key. The existing $50 Workspace and $150 Ultra prices, direct OpenAI/Anthropic BYOK, and ChatGPT OAuth remain available. The no-card `trial_local` lasts 30 days from canonical readiness and uses only `nvidia/nemotron-3-ultra-550b-a55b:free`. There is no Lightbulb per-owner request cap in the initial release; OpenRouter availability and a global kill switch are the operational bounds. Base is $20/month and retains the same single-model policy until measured cost and reliability justify a change.

OpenRouter keys are spending controls against Lightbulb's pooled upstream account, not customer wallets. For Phase 1, Metronome remains the customer prepaid balance and payment-gated top-up ledger; Stripe remains the subscription and payment processor. Existing credit purchase UX is unchanged. No production cutover occurs without written confirmation that customer-facing managed inference is permitted under OpenRouter and chosen model-provider terms.

## Phase 1 inference architecture

The backend creates one OpenRouter completion key for each Lightbulb owner using a server-only management key. Initially use one OpenRouter workspace per Lightbulb environment and capped per-owner keys; Standard's five-workspace ceiling makes one OpenRouter workspace per customer inappropriate. Set a restrictive workspace default guardrail for the allowed model/provider/privacy ceiling, while the Lightbulb gate applies the per-tier policy. Add per-key guardrails only if a tested policy gap requires them. Store the completion key's plaintext only in AWS Secrets Manager and its OpenRouter hash/policy state in DynamoDB. Key creation has no documented idempotency token, so a durable local claim and orphan-reconciliation path are required. Key rotation creates and verifies a replacement before the old key is disabled.

Hermes on AgentCore sends OpenAI-compatible Chat Completions to a Lightbulb streaming inference gate using an owner-scoped gate bearer. The gate alone reads the OpenRouter completion key. It verifies the live subscription and managed-credit provider choice, resolves the owner model policy, checks the Metronome fast-gate projection, reserves an estimate, clamps request features that could cause unbounded spend, and forwards to OpenRouter. Reject a caller-supplied upstream URL, arbitrary model, fallback, provider policy, or router plugin. OpenRouter's key limit and assigned guardrail are independent backstops. The gate must remain the only usable path for Lightbulb-funded inference; do not store an upstream key in any Hermes profile or runtime environment.

The model policy is server-owned and versioned. Trial and Base expose exactly `nvidia/nemotron-3-ultra-550b-a55b:free`; customers do not see the public OpenRouter catalog or choose a free model. Workspace and Ultra may later expose broader approved policies, but `openrouter/auto` remains disabled until separately benchmarked and bounded. Record the actual selected model and provider. Jev is a structured-decision service, not a standard chat model; integration with it is separate from this primary-inference migration.

## Hermes contract

The backend's per-agent profile writer must set the main model provider, gateway base URL, context bound, and explicit auxiliary routing for every task in `AUXILIARY_TASKS`. The bridge's fill-missing Bedrock pin must not populate Bedrock routes for an OpenRouter-managed profile. Delegated agents, cron/background work, Teams/Paperclip runs, vision, title generation, memory and compression must all use the owner gate or explicitly fail closed; no Hermes auto-chain may reach an unmetered provider. The per-owner AgentCore role remains for S3, browser, integrations, and runtime trust even after Bedrock inference moves.

Provider changes rewrite the S3 profiles and stop the warm AgentCore session before retiring the prior bearer. The model picker is derived from Lightbulb's approved model policy, not the whole public OpenRouter catalog. An agent-edited profile cannot bypass the gate's model and credit checks. The pinned Hermes image, rather than current upstream docs alone, is the acceptance target.

## Metering contract

The gate durably records owner, key hash, gate request ID, model policy revision, OpenRouter generation ID when known, reservation, and settlement state. OpenRouter's generation metadata supplies actual `total_cost`, token/cache counts, provider, final model, and cancellation state. Emit one idempotent `openrouter_inference` event per generation to Metronome with string `provider_cost_nanodollars` (one dollar = one billion units), rated under the approved Lightbulb markup. The isolated Metronome sandbox proved that 12,345,678 units debit exactly $0.012345678 and transaction replay does not debit twice. The local fast-gate projection remains microcent-denominated; round its provisional debit upward to a whole microcent and correct it from Metronome's authoritative balance refresh.

Streaming disconnects and gateway crashes remain pending until reconciliation. A worker retries generation lookups and compares per-key OpenRouter usage/activity with settled owner events. A key-level usage gap blocks widening rollout and alarms; it is never counted as free. OpenRouter Broadcast may assist observability but is not assumed to be a billing-grade delivery feed. Continue to use Metronome's balance as customer money truth; OpenRouter's per-key dollar limit is an upstream safety stop.

## Phase 2 product behavior

The no-card `trial_local` clock lasts 30 days from canonical readiness. Keep full listening and no automatic charge. Trial inference uses the one server-owned free model and therefore does not require a Metronome balance or emit a customer debit. A trial owner may explicitly buy Lightbulb credits without starting Base; purchase does not extend the day-30 platform deadline. On day 30, Base subscription is required, and any purchased credits remain on that owner's ledger.

Base is $20/month for the same workspace experience and the same single free-model policy. It does not receive a synthetic monthly credit grant while that model remains zero-cost. Trial and Base may buy Lightbulb credits, but those credits become useful only on a paid model policy offered by a higher tier or a later Base policy revision. The $50 Workspace plan preserves BYOK/ChatGPT choices, Lightbulb managed credits, and purchased credits; Ultra retains its existing benefits. A successful authenticated Stripe invoice is the only event that activates or renews Base. Existing trial deadlines and subscription records are not reset by migration.

## Production operations and customer experience

Every web, mobile, desktop, and agent-facing surface must use one server-owned plan catalog: 30-day Free, $20 Base, existing $50 Workspace, and existing $150 Ultra. Checkout, invoices, billing portal, provider controls, model list, exhausted/expired states, upgrade copy, and support diagnostics must agree on the same entitlement. Free and Base never show an OpenRouter key, credit prompt, BYOK control, or model picker.

Structured logs and metrics cover gate admission/denial, OpenRouter status and latency, selected model/provider, key hash, request/generation correlation, settlement state, retry age, balance projection, and policy revision. They never contain prompts, completions, bearer tokens, plaintext OpenRouter keys, or management credentials. Alarms cover upstream 401/402/429/5xx, missing generation IDs, settlement lag/gaps, key-provision failures, and cohort error rate.

Bedrock AIPs are rollback assets during rollout. Cleanup is a separate, measured stage: prove no active owner/profile/runtime references an AIP, no unsettled Bedrock usage remains, and no rollback cohort needs Bedrock; then remove owner AIPs, reverse-index rows, permissions, gate/meter resources, and obsolete configuration through version-controlled jobs. Cleanup must be idempotent and must not delete Hermes S3 workspaces, subscriptions, credits, or OpenRouter keys.

## Rollout and rollback

Keep the Bedrock gate, AIPs, meter, and Metronome path intact for owners not yet moved and for rollback. Dev first: sandbox key and rate-card proofs, pinned-image Hermes smoke, full billing flow, streaming abort/retry and reconciliation. Then a small production owner cohort via CI/CD, with alarms and daily cost reconciliation. Only after all managed owners are on OpenRouter and there are no outstanding Bedrock dependencies may a separate cleanup remove AIPs or Bedrock-only infrastructure. The Muse/Zen draft PR is not part of either rollout and must not be merged as written.
