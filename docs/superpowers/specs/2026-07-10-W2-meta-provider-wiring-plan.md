# W2 — Meta (Muse Spark) provider wiring + reconciliation — Implementation Plan

**Date:** 2026-07-10
**Parent design:** `2026-07-09-no-card-trial-design.md`
**Status:** Draft plan (depends on W1 delivering a `meta`-capable dev image)

## Goal

Wire free-trial containers to run **`meta/muse-spark-1.1`** via OpenClaw's native
`meta` provider, and **verify token/spend reconciliation works** so the $3 cap
(W3) can trust real usage. This is the linchpin workstream: the reconciliation
check here de-risks the entire cap before W3 is built.

## Verified facts this builds on (workflows ws9pabvsr, wlwycbdhe)

- Provider id **`meta`**, model **`meta/muse-spark-1.1`**, transport
  **`openai-responses`**, key env **`MODEL_API_KEY`**, base built into the provider.
- Pricing **$1.25/M input · $4.25/M output**; reasoning tokens billed as output.
- `provider_choice` today is a closed set of three (`billing_repo.py:228`); Meta is
  net-new — no `meta`/`muse` string exists in `apps/backend`.
- The generator: `build_provider_block()` (`config.py:311-482`) →
  `build_openclaw_config_dict()` (`:841`) → `write_openclaw_config()` (`:1421`) →
  `write_user_configs` → `provision_user_container`. Provider choice is read by
  `resolve_provider_choice()` (`container_provision.py:19-30`), default
  `bedrock_claude`.
- **Non-Bedrock models record spend 0** today (`usage_service.py:99-118`) — the
  `estimatedCostUsd` override is Bedrock-gated (`:87-88`). So Meta spend is invisible
  to Metronome by construction (good) **but also uncomputed** (must add).
- Config-schema round-trips clean at the target (W1); protocol unchanged.

## Dependency

W1 must deliver a **dev extended image that has the `meta` provider** (Path C: bake
`@openclaw/meta-provider` on the 6.10 base; or Path A: 2026.7.1 base). W2 config
wiring is identical either way. W2 can be developed and unit-tested before the image
lands, but the **reconciliation verification (Step 0) requires the dev image + a
Meta API key**.

---

## Step 0 — Reconciliation verification (DO FIRST; it's the load-bearing gate)

**Why first:** the $3 cap = reserve-worst-case → reconcile-to-actual. If Meta's
per-turn `usage` does not flow into Isol8's accounting, the cap falls back to
worst-case-only (~$0.386/turn → $3 ≈ 8 turns instead of 60–150). Everything
downstream depends on this being true.

1. Obtain a Meta Model API key ($20 credits; US-only preview — may be waitlisted).
2. On a dev container running the `meta`-capable image, set `provider_choice=meta`
   and run several Muse Spark turns (small + large context, with reasoning).
3. Inspect exactly what the usage path records vs. what Meta reports:
   - What `sessions.list` returns for a `meta/…` session (`connection_pool.py:935`,
     `_fetch_and_record_usage`) — does it carry `prompt_tokens`/`completion_tokens`
     (and cached-token detail) from Meta's `openai-responses` `usage`?
   - Does OpenClaw surface an `estimatedCostUsd`-equivalent for non-Bedrock, or only
     raw tokens?
4. **Decision gate:**
   - **If actual tokens flow through** → proceed; compute $ = tokens × pricing in
     `record_usage` for `meta/` (Step 4). Cap is trustworthy.
   - **If they don't** → either (a) add a container-side hook to report Meta `usage`
     back over the gateway, or (b) fall back to reserving a realistic per-turn
     estimate (not the 200K worst case) + the config per-turn clamp as the hard
     ceiling. Escalate the finding before building W3.

**Output:** a short written verdict + the exact fields available for reconciliation.

---

## Step 1 — Shared Meta key as a task-def secret (CDK)

- Add `META_MODEL_API_KEY` to Secrets Manager via `auth-stack.ts` (secret *name*
  passed to service/container stacks per the existing no-ISecret convention).
- Inject it into the **free-tier** container task def **only** — conditional on the
  provider being `meta`, so paid containers never receive it. Wire in
  `ecs_manager` task-def construction alongside how per-owner secrets attach today.
- Never write the key into `openclaw.json` (EFS plaintext). OpenClaw reads
  `MODEL_API_KEY` from the environment; the generated config only names the provider
  + model.

## Step 2 — `build_provider_block` Meta branch (`config.py`)

- Add a `provider_choice == "meta"` branch to `build_provider_block()`
  (`config.py:311-482`) returning `(providers_config, default_model, plugin_entries)`:
  - `default_model = {primary: "meta/muse-spark-1.1", fallbacks: ["meta/muse-spark-1.1"]}`.
  - **Path C (plugin):** ensure the `meta` plugin entry is enabled in
    `plugins.entries` (the bundled/baked plugin registers provider `meta`); no
    explicit `models.providers` block needed.
  - **Path A (built-in):** provider `meta` is built-in; just set the default model.
  - Set free-tier **reasoning effort = `medium`** (Muse Spark defaults `high`;
    thinking billed as output — medium stretches the $3 and cuts latency).
  - Set per-turn clamps: `contextTokens: 200000`, `maxTokens: 32000` (the hard
    single-turn blast-radius ceiling, and the worst-case reservation input).
  - No AIP ARNs (Bedrock-only), no Metronome provisioning for Meta owners.
- **Do NOT add `meta` to `_VALID_PROVIDER_CHOICES`** (`billing_repo.py:228`) — Meta
  is a free-tier *mode*, never a selectable paid provider (W3 abuse boundary).
  Represent it as a distinct free-tier flag read by the config path, not a
  user-settable `provider_choice`.

## Step 3 — Thread the free-tier/meta selection through the call chain

- `resolve_provider_choice()` (`container_provision.py:19-30`) → resolve to `meta`
  when the owner is a free-tier trial (read the free-trial claim/flag, not the paid
  `billing_accounts.provider_choice`).
- Thread through `background_provision` → `provision_user_container` →
  `write_user_configs` → `write_openclaw_config` → `build_openclaw_config_dict` →
  `build_provider_block`. Mirror how `provider_choice` flows today.

## Step 4 — Meta spend computation (feeds W3's $3 cap)

- In `record_usage` (`usage_service.py:45-155`), add a `meta/` cost path:
  `spend = input_tokens × $1.25/M + output_tokens × $4.25/M` from the reconciled
  actual usage (Step 0). Keep it **out of Metronome/Stripe** — write it to the
  free-trial spend counter (W3's `free-trial-claims.spent_microcents`), not the
  Bedrock/Metronome path.
- Pricing constants live next to the provider config (single source).

## Step 5 — Tests & validation

- **Unit:** `build_provider_block("meta", …)` emits the expected block; free-tier
  resolution picks `meta`; paid never receives `META_MODEL_API_KEY`; `change_provider`
  rejects target `meta`.
- **Config round-trip:** the generated Meta config validates against the target
  image's boot schema (`.strict()` parse) — no dropped/unknown keys.
- **Integration (dev):** provision a free-tier dev container → confirm it boots on
  `meta`, streams a Muse Spark reply, and Step-0 reconciliation records real spend.
- Follow repo test conventions (targeted per task; run suites at end).

## Risks / open

- **Reconciliation flow-through (Step 0)** — the one that can invalidate the cap
  design. Gated first.
- **Meta API availability** — US-only preview, undisclosed rate limits; the shared
  key is a single point (W3 adds the global kill-switch).
- **`openai-responses` reasoning replay** — confirm multi-turn reasoning context
  behaves under `effort: medium` without cost blow-ups.
- **Terms gate** (parent spec) — human ToS review before any real free-user data.
