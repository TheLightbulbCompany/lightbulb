# Marketplace: agent requirements transparency + accurate deploy/hire — Design

**Date:** 2026-06-17
**Status:** Design (awaiting review → implementation plan)
**Area:** marketplace publish/export + deploy/hire (`apps/backend/core/services/`,
`apps/backend/routers/marketplace_*`, `apps/frontend/src/components/marketplace/`)
**Goal:** Make the marketplace deploy/hire flows accurately reflect what each agent
ships and needs — on both the publisher (seller) and buyer sides.

---

## 1. Problem

A marketplace agent can look fine on its listing and be **hollow on deploy** — it
ships pipelines that call tools, skills, and external APIs that the buyer's
container has no way to satisfy, with nothing in the UI disclosing that.

Verified live against prod (`org_3DBS8G83oPrMm49yN2b70EjoFpN`, the only meaningful
tenant; pre-GA, no external hirers yet — this is pre-onboarding hardening). The two
published agents are **Tally** (bookkeeping) and **Vera** (support/ops); both are
entirely `.lobster`-pipeline driven. Their published slices declare
`mcp.servers: {}` and `skills: {}` — yet their pipelines call:

- **Tally:** `--tool stripe-api` (skill), `--tool gog` (Google Sheets/Drive),
  `--tool xero`, plus raw `curl` to Plaid, QuickBooks, and Perplexity — referencing
  `$STRIPE_API_KEY`, `$PLAID_*`, `$QBO_*`, `$PERPLEXITY_API_KEY`.
- **Vera:** raw `curl` to Stripe (`/v1/refunds`, `$STRIPE_API_KEY`) and Intercom
  (`$INTERCOM_ACCESS_TOKEN`), plus `--tool telcall-twilio` (`$TWILIO_*`),
  `--tool local-rag-qdrant`, `--tool taskr`, and `$ZENDESK_*`.

None of this is visible to a buyer, and most of it is unsatisfiable on the platform.

### Root gaps

1. **The export never sees the dependencies.** It builds the slice from declared
   config only (`catalog_slice`), and even that reads the **global**
   `openclaw.json` (`agent_export.py:435`), not the agent's per-workspace effective
   config — so the slice is empty. It never parses `.lobster` pipelines or scripts,
   where the real dependencies live as `--tool X`, `$ENV`, and `curl <host>`.
2. **Nothing discloses requirements.** Neither the seller wizard nor the buyer
   listing/hire surface shows what an agent needs. The buyer learns only when the
   agent 401s/skips in production.
3. **The platform covers little of what these agents need,** and the one big
   overlap (Stripe) is wired incompatibly: the integrations tab wires the Stripe
   **MCP** (`@stripe/mcp`, `STRIPE_SECRET_KEY`), while the agents call a custom
   `stripe-api` skill / raw API with `STRIPE_API_KEY`.
4. **`gog` (Google) is gated.** It is `internal: true` + `requires_bedrock: true`
   (Google Limited-Use policy), so buyers can't connect it — every `--tool gog`
   step fails for them.

---

## 2. Verified model (facts that drive the design)

Established by reading the fork we run (`~/Desktop/agent`), prod EFS/logs, the
codebase, and two research passes (build-vs-buy + OpenClaw declared-requirements).

### What already exists in-house (reuse, don't rebuild)

- **The hire gate is ~80% built (PR #774, "M8").**
  `catalog_service.assert_credentials_satisfied(owner_id, manifest, slice_)` blocks
  deploy with `MissingCredentialsError` (409) listing which providers to connect.
  It reads required providers from `manifest.requires.credentials[]`
  (`{provider, optional?}`), **also derives** a requirement from a registry MCP
  server in the slice (`mcp.servers.stripe` → needs Stripe), treats `optional: true`
  as a hint, and checks "connected" against `integrations_repo`. It is not
  under-built — it is **under-fed**: nothing populates `requires.credentials` today.
- **Args-secret scrub shipped (PR #893).** `catalog_slice._scrub_arg_secrets` +
  `marketplace_safety` `arg-secret-suspected` gate already blank/​block secrets
  passed as positional CLI args (`--api-key <KEY>`).
- **The publish wizard already has a Bundle step.** `wizardSteps.BundleStep` lets
  the seller toggle which MCPs + skills ride along (`selected_mcps`/
  `selected_skills` → `extract_agent_slice`). The artifact preview
  (`enumerate_for_preview` → `useArtifactPreview`) already returns
  `available_mcps`/`available_skills`/`safety_flags`.

### What is borrowable (data + standards, not code)

- **Gitleaks ruleset (MIT)** — a host/keyword/key-prefix → service-name table for
  ~160 providers (`api.plaid.com`→Plaid, `sk_live`→Stripe…). Vendor as **data**.
  Gap-fill names from TruffleHog's 800+ detectors but **reference only**
  (TruffleHog is AGPL — do not embed/link its code).
- **OpenClaw plugin manifests** declare requirements machine-readably:
  `openclaw.plugin.json` `setup.providers[].envVars` + `contracts.tools` (61/72
  bundled manifests), plus the aggregate `scripts/lib/official-external-*-catalog.json`
  (`envVars`, `credentialPath`, `requiresCredential`, `signupUrl`). **Read** these.
- **MCP `server.json` registry format** declares `environmentVariables[]`
  (`{name, isRequired, isSecret}`). Optional consume path for MCP servers.

### What must be built (irreducibly ours)

- Parsing `.lobster` + `openclaw.invoke --tool` + `$ENV` + `curl <host>` — bespoke
  to the platform; no third-party tool understands it.
- Classifying detected items against **our** integration rail (supported vs BYO vs
  gated vs bundled).
- The two-mode Requirements Ledger UI (author + consumer).

### Declared-vs-parse split (from the OpenClaw audit)

- **Readable (declared):** plugin env requirements + tool names; MCP server
  `env`/`headers`/`oauth`; configured-skill `skills.entries[*].env`; skill
  `requires` frontmatter (when authors fill it in).
- **Parse-only (undeclared):** anything a `.lobster` pipeline or a SKILL.md shell
  snippet actually invokes (free-form command string; the runner injects the whole
  `process.env`), core bundled tools like `fast-io` (no per-tool requirements
  registry), and the curl-to-external-API case.

### Runtime caveats to respect

- **MCP is global-only; per-workspace `openclaw.json` MCP/skills are inert**
  (memory `project_openclaw_mcp_global_only_nested_inert`). Bundling ships *wiring*;
  whether it runs depends on the global merge — captured by the disposition, not
  assumed.
- **Secrets are always blanked on export** (env, headers, and `--flag` args).
  Bundling never ships a key.

---

## 3. Scope

**In scope**

1. `requirements_scan()` — a backend scanner producing a structured requirements
   list from an agent's **effective** config + workspace (declared-read ∪
   body-parse), classified by disposition.
2. A classification registry (bundled set + `providers.yaml` + channels + gog-gated
   + Gitleaks-seeded service-ID table).
3. Manifest schema: populate the existing `manifest.requires.credentials` (gate
   fuel) + a richer frozen `manifest.requirements[]` (the full ledger).
4. **Publish gate** (seller) — block/force-acknowledge on "won't run as published".
5. **Hire gate** (buyer) — reuse `assert_credentials_satisfied`, fed by the scanned
   `requires.credentials`; honest warn (not block) for BYO/gated.
6. The **Requirements Ledger** UI in two modes: author (seller wizard step +
   listing detail tab) and consumer (buyer listing section + hire dialog),
   personalized to the buyer's connected integrations.
7. Backfill: re-scan the existing published listings (Tally/Vera) so their
   requirements populate without a re-export.
8. Unit/integration tests (TDD).

**Dependency (sequence first or fold in)**

- **Publish-from-effective-config** (the pending Fix 2 of
  `docs/superpowers/specs/2026-06-15-marketplace-args-leak-and-effective-config-design.md`).
  Until the export reads the per-workspace config, the *declared* layer stays
  empty. The scanner's body-parse layer works regardless, but the declared layer
  depends on this. We land that fix first (or fold its effective-config resolver
  into the scanner's input).

**Out of scope** (flagged, not built)

- **Re-authoring Tally/Vera** to actually run (drop phantom skills, drop gog, wire
  creds). The scanner/gate makes their state *honest*; fixing the agents is separate
  agent-content work the user explicitly deferred.
- **Buyer key-provisioning UX** for BYO/unsupported providers (Plaid, Perplexity,
  …). The platform cannot inject those env vars yet; the hire gate is honest that
  "these steps will skip." Its own project.
- **Ungating `gog`** / a native non-Google output surface. `gog` is classified as a
  gated requirement per the product decision; the Google-dependency redesign is a
  separate workstream.

---

## 4. Design — the scanner (`requirements_scan`)

New module `core/services/marketplace/requirements_scan.py`. Pure function over an
agent's effective config + workspace dir → `list[Requirement]`. No I/O beyond
reading the already-resolved files; fully unit-testable with fixtures (Tally/Vera
become golden fixtures).

### 4a. Inputs

```
requirements_scan(
    effective_config: dict,     # per-workspace openclaw.json if present, else global
    agent_dir: Path,            # workspaces/<agent_id> — pipelines/, scripts/, *.md
    bundled_mcps: set[str],     # what the seller chose to bundle (Bundle step)
    bundled_skills: set[str],
) -> list[Requirement]
```

### 4b. Detectors (declared-read ∪ body-parse)

- **Declared (read):**
  - `effective_config.mcp.servers[*]` → server key + `env`/`headers` names.
  - `effective_config.skills.entries[*]` → skill name + `env`/`apiKey` names.
  - plugin manifests / official catalog → plugin tool names + declared `envVars`.
- **Body-parse (regex over `pipelines/*.lobster`, `scripts/*`, `*.md` shell
  snippets):**
  - tool invocations: `openclaw.invoke --tool <name>` and `<name>.<action>` pipe
    heads; `--tool` flags.
  - env refs: `$NAME` / `${NAME}` (uppercase, len≥3).
  - external hosts: `https?://<host>` from `curl`/`https.get`/`fetch`.
  - provenance is recorded per hit: `pipeline:<file>:<step-id>`, `script:<file>`,
    `env:<NAME>`, `host:<host>`.

### 4c. Requirement record

```
Requirement {
  key: str            # canonical provider/tool key: "stripe", "plaid", "gog", "llm-task"
  display_name: str   # "Stripe", "Plaid", "Google Sheets/Drive"
  kind: "tool"|"skill"|"mcp"|"cli"|"channel"|"external_api"|"bundled"
  disposition: "included" | "connect" | "byo" | "unavailable"
  env: list[str]      # referenced env-var names (e.g. ["STRIPE_API_KEY"])
  detected_via: list[str]   # provenance hits (deduped)
  bundled: bool       # did the seller bundle wiring for it
  required: bool      # seller annotation; default true
  note: str | None    # seller "how to get a key" note
  reason: str | None  # why unavailable / mismatch ("no implementation",
                      # "internal + Bedrock-only", "wiring mismatch: tab wires
                      # Stripe MCP/STRIPE_SECRET_KEY, agent uses STRIPE_API_KEY")
}
```

### 4d. Classification → disposition

Detected items collapse to canonical providers (via the service-ID table for
hosts/env, and direct name match for tools/skills/MCPs), then:

| Condition | disposition |
|---|---|
| bundled-in-image tool/skill, or keyless | **included** |
| needs key; key/provider in `providers.yaml` (registry) **and** wiring matches | **connect** |
| channel tool (`slack`/`discord`/`telegram`/`whatsapp`) | **connect** (via Channels) |
| `gog`/Google (internal + Bedrock-only) | **unavailable** (gated) |
| tool/skill referenced but no implementation (not bundled, not in slice, not in registry) | **unavailable** (phantom) |
| needs key; registry provider **but wiring mismatch** | **byo** + mismatch `reason` |
| needs key; not in registry/channels | **byo** |

The **Stripe mismatch** (Tally's `stripe-api` skill / `STRIPE_API_KEY` vs the tab's
MCP/`STRIPE_SECRET_KEY`) is detected here and emitted as a `byo` row with a mismatch
`reason` (or `unavailable` when the `stripe-api` skill has no implementation).

### 4e. Classification sources (one place each)

- **`BUNDLED_TOOLS` / `BUNDLED_SKILLS`** — maintained constants for core tools
  (`fast-io`, `llm-task`, `summarize`, …) + bundled plugins (`lobster`,
  `memory-core`) + Atlas-seeded skills (`capability-evolver`). (Core tools have no
  upstream registry — we own this list; documented as such.)
- **`providers.yaml`** — supported integration keys, their tool key, env names,
  actor (mcp/cli/skill), and gating flags (`internal`, `requires_bedrock`).
- **`CHANNELS`** — `{slack, discord, telegram, whatsapp}`.
- **service-ID table** — vendored from Gitleaks (MIT) as `known_services.yaml`
  (host/env-prefix → display name) for the BYO long tail.

---

## 5. Design — population + freeze

Three stages: **scan (auto) → seller confirms (wizard) → freeze into manifest →
buyer reads + personalizes.**

- **Wizard (live):** the artifact-preview endpoint runs `requirements_scan()` and
  returns `requirements[]` alongside the existing preview fields. The seller's
  "Requirements" step renders it (author mode) and lets them edit
  `required`/`note`, dismiss a false positive, and `acknowledge` "won't run" rows.
- **Artifact build (freeze):** on submit, `agent_export` runs the same scanner once
  more and writes the seller-confirmed result into:
  - `manifest.requires.credentials[]` — `{provider, optional}` for every `connect`
    + `byo` registry-resolvable row (fuels the existing hire gate).
  - `manifest.requirements[]` — the full frozen ledger (rows, dispositions,
    provenance, notes). Immutable + versioned with the tarball, like `safety_flags`.
  - a denormalized DDB summary (counts + provider keys) for fast listing render +
    the publish gate.
- **Buyer views (read):** read the frozen manifest; never re-scan. Personalize only
  the `connect` rows against this buyer's `integrations_repo` connected set.

---

## 6. Design — the two gates (the seller/buyer asymmetry)

**Publish gate (seller), in `marketplace_listings` submit/publish:**
block (or require explicit `acknowledge`) when the frozen requirements contain any
`unavailable` ("won't run as published") row — phantom skills, gated gog. `byo`/
`connect` rows never block publish; they warn. This stops hollow agents going live.

**Hire gate (buyer), reusing `assert_credentials_satisfied`:**
hard-block only on `connect` rows that are **required and unconnected** (registry
providers the buyer can actually connect, e.g. Stripe-via-tab). It **cannot** block
on `byo`/`unavailable` — the platform can't verify or inject those keys yet — so
those surface as honest warnings ("these steps will skip until you set them").
`optional: true` rows never block.

> Principle: **block what we can fix; warn honestly about what we can't.**

---

## 7. Design — the Requirements Ledger UI

One component, two modes. Matches the existing marketplace editorial system
(extracted, not invented): `font-editorial-serif`; forest-green `#06402b`
(primary), terracotta `#8a3a2a`/`#fbe9e7`/`#f0b3a8` (reserved for genuine "won't
run"), warm parchment `#f3eee5`/`#e0dbd0` (chips, rules), ink `#1a1a1a`, secondary
`#5b5b5b`, muted-italic `#8a8578`; full-round pills; uppercase tracked micro-labels.
Rows group by disposition; each row shows item · provenance · disposition mark.
Four disposition marks: **Included** (green) · **Connect in Settings** (ink + green
link) · **Bring your own** (parchment) · **Won't run** (terracotta, rare).

### Author mode (seller)

- **New wizard step "Requirements"** after Bundle (Pick → Details → Bundle →
  **Requirements** → Pricing → Review). Editable: `required`/`optional`, add note,
  dismiss false positive. A terracotta banner + **Acknowledge** control gates
  publish when "won't run" rows exist. Generic POV ("any buyer").
- **Listing detail tab:** the existing "Bundle" tab becomes **"Bundle &
  Requirements"** — same ledger, read-only, for auditing a live listing.

### Consumer mode (buyer)

- **Listing page "What you'll need to run this"** section above Hire — read-only,
  **personalized**: each `connect` row resolves to ✓connected / ○needed against the
  buyer's integrations; top-line verdict ("You can run this now" / "You'll need to
  connect N things" / "Parts won't run on your plan"). Seller BYO notes inline.
- **Hire dialog (the gate):** when the hire gate would block, render the ledger
  **filtered to the buyer's unmet rows** — `connect` blockers with **Connect**
  deep-links (Settings/Channels); `byo`/`unavailable` as non-blocking "these steps
  will skip" warnings; then retry hire.

### Bundle ↔ requirements relationship

The seller chooses what to **bundle** (per agent, existing Bundle step); the scanner
detects what's **needed**. Bundling shifts a row's disposition — it never removes a
real need:

| Agent needs X, seller… | buyer sees |
|---|---|
| bundles X, keyless | **Included** |
| bundles X, key is a tab provider | **Connect in Settings** |
| bundles X, key not in tab | **Bring your own** (wiring ships, key blanked) |
| doesn't bundle X, pipeline calls it | **Bring your own** (no wiring ships) |
| X phantom / gated | **Won't run** |

`requirements = detected − bundled, classified.`

---

## 8. Components touched

| File | Change |
|---|---|
| `core/services/marketplace/requirements_scan.py` | **New.** Scanner + classifier + `Requirement`. |
| `core/services/marketplace/known_services.yaml` | **New.** Gitleaks-seeded host/env→service table (MIT-derived data). |
| `core/services/agent_export.py` | Run scanner at artifact build; write `requires.credentials` + `requirements` to manifest; resolve effective config (folds in / depends on the 2026-06-15 Fix 2). |
| `core/services/catalog_slice.py` | Expose effective-config + bundled sets to the scanner (no behavior change to scrub). |
| `core/services/marketplace_service.py` | Persist requirements summary on the DDB row; `enumerate_for_preview` returns `requirements`. |
| `routers/marketplace_listings.py` | Publish gate (acknowledge "won't run"); preview returns requirements. |
| `core/services/catalog_service.py` | Hire gate already exists; ensure it consumes the scanned `requires.credentials` + surfaces warn rows. |
| `apps/frontend/.../marketplace/RequirementsLedger.tsx` | **New.** Two-mode ledger component. |
| `apps/frontend/.../marketplace/drawer/wizardSteps.tsx` | New "Requirements" step (author mode). |
| `apps/frontend/.../marketplace/ListingDetailView.tsx` | "Bundle" → "Bundle & Requirements" tab. |
| `apps/frontend/.../marketplace/storefront/Listing/*` | Buyer "What you'll need" section (consumer mode, personalized). |
| `apps/frontend/.../marketplace/drawer/useHire.ts` | Surface gate result as the filtered ledger, not a raw error. |
| `apps/backend/scripts/backfill_marketplace_requirements.py` | **New.** Re-scan published listings' S3 tarballs → update manifest. Idempotent, dry-run default. |

---

## 9. Error handling / edge cases

- Scanner must **never block export on its own exception** — degrade to
  `requirements: []` + a `scan_incomplete` flag rather than failing publish.
- Over-reporting is preferred to under-reporting (a false "needs X" is honest noise;
  a missed dep is the current bug). Seller can dismiss false positives.
- Malformed/missing per-workspace config → fall back to global (per the
  effective-config resolver); never raise.
- `manifest.requires` null/non-dict → reads as "no requirements" (already handled by
  `assert_credentials_satisfied`; keep that contract).
- Personalization must not leak another tenant's connection state — resolve against
  the requesting buyer's owner_id only.
- Secret values never appear in `requirements` (only env-var *names*); the existing
  scrub + args-scrub + slice-leak gate remain authoritative.

---

## 10. Backfill (Tally/Vera)

`backfill_marketplace_requirements.py`: for each published listing, fetch the S3
`workspace.tar.gz` + slice, run `requirements_scan()` over the extracted tree,
write `requirements` + `requires.credentials` into the manifest (new immutable
key under the same version prefix or a sidecar `requirements.json`), and update the
DDB summary. Dry-run by default; `--apply` to write. Makes the two live listings
honest without a seller re-export.

---

## 11. Testing plan (TDD)

Targeted unit tests during development; full suite at final verification (project
policy — no full `pnpm test`; backend pytest targeted).

- `tests/unit/services/marketplace/test_requirements_scan.py` — golden fixtures from
  Tally + Vera: detect every `--tool`, `$ENV`, `curl host`; classify each into the
  right disposition; Stripe mismatch flagged; phantom skills → unavailable; bundled
  → included; scanner never raises on malformed input.
- `tests/unit/services/test_agent_export.py` — manifest carries `requirements` +
  `requires.credentials`; bundling shifts disposition; effective-config sourcing.
- `tests/unit/routers/test_marketplace_listings.py` — publish gate blocks/ack on
  "won't run"; preview returns requirements.
- `tests/credentials/test_marketplace_requires_credentials.py` — extend: hire gate
  blocks on scanned `connect` provider; BYO/gated never block.
- Frontend: `RequirementsLedger` renders both modes; buyer personalization
  (connected vs needed); hire dialog shows filtered blockers.

---

## 12. Open questions / deferred

- **Buyer key-provisioning UX** for BYO/unsupported providers — deferred; the hire
  gate is honest ("steps will skip") until it lands.
- **gog disposition** — classified gated (decided). Native-output redesign deferred.
- **Re-authoring Tally/Vera** — out of scope; the gate will flag them honestly.
- **LLM-assisted extraction** for free-form scripts the static parser can't reach —
  deferred; static deep-scan first, measure miss rate against the golden fixtures.
