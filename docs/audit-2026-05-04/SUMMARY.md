# Lightbulb Codebase Audit — Synthesis

**Date:** 2026-05-04
**Method:** Three parallel agents, read-only sweep across backend, frontend, and infra/desktop/misc, scored against The Pragmatic Programmer's six relevant lenses (DRY, orthogonality, tracer bullets, design by contract, broken windows, reversibility).
**Source files:** [backend-findings.md](backend-findings.md), [frontend-findings.md](frontend-findings.md), [infra-desktop-misc-findings.md](infra-desktop-misc-findings.md). Companion deepening list: [DEEPENING-CANDIDATES.md](DEEPENING-CANDIDATES.md).

---

## Headline scores

| Area | Overall | Worst lens | Best lens |
|---|---|---|---|
| Backend | **6/10** | Orthogonality 5 (private cross-layer reaches, services with own boto3) | Broken windows 8 (only 2 TODOs, no commented-out code) |
| Frontend | **5.8/10** | Tracer bullets 4 (5 dark panels + 2 fully-orphan files) | Broken windows 7 / Reversibility 7 |
| CDK infra | 7/10 | DRY 6 (10 near-identical Table blocks; 17 IAM `*`) | Orthogonality 9 (cross-stack KMS posture is gold) |
| Desktop (Tauri) | 8/10 | Reversibility 7 (`tauri.conf.json` checked-in points at dev) | Orthogonality 9 (3 commands, all wired, clean module split) |
| Paperclip integration | 6/10 | Broken windows 5 (untracked 77 MB upstream clone) | Orthogonality 8 (clean stack composition) |
| Scripts | 8/10 | Broken windows 7 (one untracked destructive prod script) | Design by contract 9 (strict modes everywhere, dry-run defaults) |
| **Repo hygiene** | **4/10** | Broken windows 3 (8 untracked top-level entries, 19 untracked plans/specs) | — |

**The single worst score in the audit is repo hygiene.** It's also the meta-problem: it hides which work is real WIP, which scripts are load-bearing, and which plans are dead. Fixing it is one afternoon of triage and pays back across every future `git status`.

---

## Seven cross-cutting themes

These are patterns that appeared in **at least two** of the three audits.

### 1. Documentation rot is the highest-traffic broken window
- `CLAUDE.md` says 19 services (actual: 39), 130 test files (actual: 152), claims a `/node` router prefix that isn't mounted, and omits the entire `routers/teams/` subpackage (12 files), `core/billing/`, and all the paperclip_* services.
- 75+ plans/specs in `docs/superpowers/` with **zero** machine-readable status — figuring out "is this done?" requires reading each one.
- Frontend has stale `["free", "starter", "pro", "enterprise"]` tier dropdown and stale "free-tier scale-to-zero reaper" comments, three weeks after the flat-fee cutover deleted both.
- `next.config.ts` excludes `@huggingface/transformers` and `onnxruntime-node` from bundles — neither is in `package.json`.

The cost: every wrong line taxes onboarding (yours, mine, anyone else's). Stale comments are worse than no comments because they actively mislead.

### 2. Vendor SDKs leak past the wrapper that exists to contain them
| Vendor | Wrapper that exists | Number of bypass sites |
|---|---|---|
| Clerk REST | `core/services/clerk_admin.py` (its docstring even names the bypass sites) | 2 routers |
| Stripe | `core/services/billing_service.py` | 2 routers (`billing.py`, `webhooks.py`) call `stripe.*` directly |
| PostHog | `apps/frontend/src/lib/analytics.ts` (has a `capture()` wrapper) | 14 components import `usePostHog()` directly |
| Boto3 / DynamoDB | `core/dynamodb.get_table` | 5 services hold their own `boto3.client/resource` |

Each one is the same pattern: the abstraction was started, never finished, and now the inconsistency is the worst of both worlds — the wrapper exists (suggesting it should be used), and the bypass exists (proving it isn't).

### 3. "Sub-services" that fragmented past their useful point
- Backend: 4 `catalog_*` modules (one is 504 LOC; two others are ~100 LOC pure-function helpers with single callers) and 12 `paperclip_*` services. The `paperclip_*` count is feature-driven (Teams BFF added them in a short window) but the `catalog_*` split adds 2 import hops per change without buying testability.
- Backend: 5 services own their own boto3 clients/resources outside `core/dynamodb` (`connection_service.py`, `oauth_service.py`, `credit_ledger.py`, `webhook_dedup.py`, plus the dead `bedrock_client.py`). Each reinvents the same `_table()` factory.
- Frontend: 4 hooks (`useBilling`, `useContainerStatus`, `useProvisioningState`, `useTeamsApi`) bypass `useApi` and reimplement auth+fetch+error. They throw `Error("Failed to fetch")` while `useApi` throws `ApiError(status, body)` — losing the structured error info on the very paths that most need it (billing failures, provisioning failures).

The DRY violation is real, but the deeper problem is the next bug: when the auth flow changes or error handling needs to add retries, four (or five) places need to be updated and one will be missed.

### 4. Half-built features and zero-caller modules
- Backend: `core/services/bedrock_client.py` (32 LOC, **zero callers**). `routers/node_proxy.py` (272 LOC, **no `APIRouter()` instance** — it's a service file misnamed and mislocated; CLAUDE.md falsely claims it's mounted).
- Backend: `_PAPERCLIP_RETRY_KIND` private alias self-described as "kept until the next cleanup pass" — the next cleanup pass has not happened.
- Frontend: `ControlIframe.tsx` (zero imports), `ActionsPanel.tsx` (zero imports, not in router), `hooks/index.ts` barrel (zero importers), `useGateway.send` raw method (zero callers since `useActivityPing` was deleted in the flat-fee cutover).
- Frontend: 5 control panels (`Instances`, `Nodes`, `Config`, `Debug`, `Logs`) routed but never linked from `ControlSidebar` — reachable only via `?panel=debug` URL surgery, all last touched 2026-03-15.
- Infra: `apps/terraform/` directory with zero `.tf` files, only stale cache.

### 5. Layering violations (cross-layer back-edges)
- `core/services/paperclip_autoprovision.py:38` imports from `routers.webhooks` — services should never import from routers.
- `main.py:257` imports private `_admin` and `_resolve_user_email` from `routers/teams/agents.py` — startup wiring depends on private symbols in a router.
- `routers/webhooks.py:577` uses `from boto3.dynamodb.conditions import Key` — webhook router knows DDB query syntax that should be a repo method.

Each one would silently break in non-obvious ways during a refactor.

### 6. Duplicate predicates that should be one function
| Predicate | Sites | Suggested home |
|---|---|---|
| `is_subscription_provisioned(account)` | 4 (provision_gate, connection_pool, config router, billing router) | `core/services/provision_gate.py` already has the canonical helper |
| `_BLOCKED_REPEAT_STATUSES` (related rule) | 1 (billing router) | Same module as above |
| `isOrgAdmin(membership)` | 5 components (ControlSidebar, UsagePanel, AgentChatWindow, ProvisioningStepper, settings/page) | New `useIsOrgAdmin()` hook |
| `isBedrockTier(me)` (treats undefined as Bedrock) | 2 (ControlSidebar, ControlPanelRouter) | Same as above |
| `useSWR("/users/me")` + `UserMeResponse` type | 3 components, type re-declared 3x | New `useCurrentUser()` hook |

### 7. The "untracked everywhere" disease
Eight untracked top-level entries: `paperclip/` (77 MB upstream clone), `.tmp-paperclip-audit/`, `.superpowers/`, `.hypothesis/`, `.claude/`, `apps/desktop/src-tauri/.sidecar-tmp/` (24 MB), `scripts/purge_pre_cutover_users.py` (12 KB *destructive prod script*), plus 19 untracked plan/spec markdowns. Individually each is defensible; collectively they make `git status` unreadable and hide the fact that several genuinely should be committed (the audit doc, the prod purge script, the live plans).

---

## Top 15 wins, ranked by ROI across the whole repo

ROI = simplification + risk reduction per hour of work. Severity in brackets.

### Zero-risk deletes (do in one PR, ~1 hour)
1. **[HIGH]** Delete `core/services/bedrock_client.py` — zero callers, 32 LOC.
2. **[HIGH]** Delete `apps/frontend/src/components/control/ControlIframe.tsx` and `ActionsPanel.tsx` — zero imports each, ~370 LOC.
3. **[HIGH]** Delete `apps/frontend/src/hooks/index.ts` — barrel, zero importers.
4. **[HIGH]** Delete `useGateway.send` raw method + context type field — zero callers since the flat-fee cutover.
5. **[MED]** Delete `next.config.ts:63-80` ML-package exclusions — packages aren't installed.
6. **[MED]** Delete the `_PAPERCLIP_RETRY_KIND` shim — self-described temporary.
7. **[HIGH]** Delete `apps/terraform/` — graveyard, zero `.tf` files. CLAUDE.md already says it's dead.

### Repo-hygiene afternoon (~3 hours, mostly mechanical)
8. **[HIGH]** `.gitignore` `paperclip/` (and document it as the upstream-reference twin of `~/Desktop/openclaw`). Same treatment for `.sidecar-tmp/`, `.hypothesis/`, `.superpowers/` if not already.
9. **[HIGH]** Move `.tmp-paperclip-audit/route-audit.md` → `docs/audit-2026-05-02-paperclip-routes.md` and commit it. Delete the directory.
10. **[HIGH]** Commit `scripts/purge_pre_cutover_users.py` with a `# DEPRECATED: one-shot 2026-04-27 cutover. Do not re-run.` header. Destructive prod scripts must be in version control.
11. **[HIGH]** Triage the 19 untracked `docs/superpowers/{plans,specs}/*.md`. Commit live ones; delete `2026-04-13-free-tier-scale-to-zero.md` (superseded by the cutover); add a one-line `Status: {Draft|Approved|In progress|Done|Stale}` header to every plan and spec. Codify the convention in CLAUDE.md.

### Documentation correctness (~1 hour)
12. **[HIGH]** Rewrite the backend section of CLAUDE.md to match reality. Drop per-file annotations that go stale; replace with a `tree` snippet. Fix the false `/node` mount claim, the wrong service/test counts, and add the missing `routers/teams/`, `core/billing/`, `paperclip_*` services. (Per project memory `project_pragmatic_audit_2026_05_04.md`, this is the highest-traffic doc and is silently misleading every reader.)
13. **[HIGH]** Fix `ContainerActionsPanel.tsx:16` — the `TIERS = ["free", "starter", "pro", "enterprise"]` admin dropdown still ships post-flat-fee. Operators can click a tier that no longer maps to anything.

### Surgical fixes that prevent the next bug
14. **[HIGH]** Extract one subscription-active gate, kill four duplicates. Promote `core/services/provision_gate.is_subscription_provisioned` and have `connection_pool.py`, `routers/config.py`, `routers/billing.py` call it. ~30 LOC deleted; eliminates the next "we updated three of four sites" bug.
15. **[HIGH]** Move `routers/node_proxy.py` to `core/services/node_proxy.py`. Fix the two import sites in `websocket_chat.py`. Update CLAUDE.md.

---

## Recommended PR sequencing

The audit produced ~60 findings across the three files. Bundling matters — small risk-free PRs that each tell one story land easier than one mega-PR.

| PR | Scope | Estimated time | Risk |
|---|---|---|---|
| **PR 1: dead-code purge** | Wins #1–6 (all the zero-caller deletes) | 30 min | Zero — no behavior change |
| **PR 2: repo hygiene** | Wins #8–11 (gitignore, commit, triage, status headers) | 3 hours (most of the time is the docs triage) | Zero |
| **PR 3: terraform graveyard** | Win #7 | 5 min | Zero |
| **PR 4: doc correctness** | Wins #12–13 (CLAUDE.md rewrite + tier dropdown fix) | 1 hour | Low — UX surface |
| **PR 5: subscription predicate** | Win #14 + the related `_BLOCKED_REPEAT_STATUSES` consolidation | 1 hour | Medium — affects billing/gating logic, deserves test coverage |
| **PR 6: node_proxy relocation** | Win #15 | 30 min | Low |
| **PR 7: clerk_admin completion** | Route `routers/billing.py` and `routers/desktop_auth.py` through `clerk_admin` (the docstring already names the targets) | 1 hour | Low |
| **PR 8: stripe boundary** | Move `stripe.*` calls in `routers/billing.py` and `routers/webhooks.py` into `BillingService` | 2 hours | Medium |
| **PR 9: useApi consolidation** | Route the 4 hooks through `useApi` so `ApiError` is consistent | 2 hours | Medium — surfaces real errors that were being swallowed |
| **PR 10: dark-panel decision** | Either link `Instances`/`Nodes`/`Config`/`Debug`/`Logs` in `ControlSidebar.NAV_ITEMS` or delete them | depends | Low (delete) or product decision (keep) |

After PRs 1–4 (the "free wins"), backend health moves ~6 → 7.5, frontend ~5.8 → 7, repo hygiene 4 → 8. After PRs 5–10 (the surgical fixes), backend ~7.5 → 8.5 and frontend ~7 → 8.

---

## What this audit deliberately did NOT do

- **No rewrites proposed.** Surgical fixes only.
- **No abstractions proposed without evidence.** YAGNI: don't introduce a port or wrapper unless ≥2 adapters justify it.
- **No file-by-file rating of every test.** Test culture is strong (152 backend tests, near-zero skips, real coverage on the highest-risk hook `useAgentChat`); the issues are concentrated in 5 high-value components without tests (see frontend findings).
- **No security review.** Tangentially noted: `cdk-nag` not enabled (would catch IAM `*` automatically), and `withGlobalTauri: true` exposes the full Tauri JS API to the embedded WebView. Both worth a follow-up but out of scope here.
- **No feature-by-feature deep dive.** This was a triage; the deepening pass (next document) targets specific clusters of shallow modules for surgical refactor.
