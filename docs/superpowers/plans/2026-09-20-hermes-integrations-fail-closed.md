# Hermes integrations fail-closed implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent a new integration from reporting connected when its Hermes adapter cannot deliver a usable credential.

**Architecture:** Put one runtime-support decision beside the shared connection dispatcher. Use it in the Connections catalog and admission path; let profile-projection errors reach the existing route rollback. This is an interim safety slice before the approved runtime credential broker, not an alternate credential store.

**Tech Stack:** FastAPI, Pydantic registry, pytest, Hermes profiles in S3.

**Spec:** `docs/superpowers/specs/2026-09-20-hermes-integration-credentials-design.md`

## Global Constraints

- The pinned runtime is Hermes Agent 0.21.3 (`v2026.9.14`); do not add OpenClaw/EFS credential writes.
- Do not add any third-party secret to an S3 profile to make a failing connection appear successful.
- Existing connections must remain visible and disconnectable even if new connections are unavailable.
- All backend changes go through source control and CI/CD; never mutate a production runtime directly.
- This plan is disposable after it ships; delete it in the shipping PR.

---

### Task 1: Reject providers without a working Hermes credential path

**Files:**
- Modify: `apps/backend/core/services/credentials/apply_connection.py`
- Test: `apps/backend/tests/credentials/test_apply_connection.py`

**Interfaces:**
- Produces: `runtime_supported(p: registry.Provider) -> bool` in `apply_connection.py`.
- Produces: `RuntimeUnsupportedError` from `apply_connection` when the provider has no projection or an actor-owned MCP grant would be reconstructed from an absent backend secret.

- [x] **Step 1: Write failing tests** in `test_apply_connection.py`. Replace `test_apply_agentcore_materializer_with_no_projection_is_still_skipped` with an assertion that the existing synthetic `toml_cli` provider raises `RuntimeUnsupportedError`. Add a parameterized test using real registry providers `stripe`, `notion`, `sentry`, `linear`, `attio`, `intercom`, `apollo`, `cloudflare`, and `quickbooks`: each must raise before `reconcile_integrations` is called. Assert `runtime_supported` is true for `google`, `github`, `posthog`, `meta_ads`, and `parallel`.
- [x] **Step 2: Run** the focused test file and observe failures because unsupported providers returned success.
- [x] **Step 3: Implement the smallest shared guard.** `runtime_supported` returns false when a pure materializer is absent from `hermes_cli.supports`, when an actor-owned MCP provider does not retain its credential, or when an `openclaw_paths` provider has no Hermes mapping. At the start of `_apply_connection_agentcore`, raise `RuntimeUnsupportedError` when false. Keep existing supported paths unchanged.
- [x] **Step 4: Run** the same test file and confirm the guard tests pass.

### Task 2: Propagate a connect-time profile failure

**Files:**
- Modify: `apps/backend/core/services/credentials/apply_connection.py`
- Test: `apps/backend/tests/credentials/test_apply_connection.py`

**Interfaces:**
- Consumes: `RuntimeUnsupportedError` and `runtime_supported` from Task 1.
- Produces: a raised exception on failed `reconcile_integrations`, allowing the existing paste/OAuth rollback paths to keep the status row disconnected.

- [x] **Step 1: Change** `test_apply_agentcore_never_raises_when_refresh_fails` to expect the original `FileNotFoundError`; retain its real `apply_connection` call.
- [x] **Step 2: Run** the focused test and observe failure because the dispatcher swallowed the error.
- [x] **Step 3: Remove** the `try/except` around `reconcile_integrations` in `_apply_connection_agentcore`; leave the `pending=provider_key` call intact.
- [x] **Step 4: Run** the targeted backend suites; supported connections still succeed and route rollbacks keep failed ones unconnected.

### Task 3: Make the catalog and OAuth start truthful

**Files:**
- Modify: `apps/backend/routers/connections.py`
- Modify: `apps/backend/routers/platform_info.py`
- Modify: `apps/frontend/src/components/settings/ConnectButton.tsx`
- Test: `apps/backend/tests/unit/routers/test_connections.py`
- Test: `apps/backend/tests/unit/routers/test_platform_info.py`
- Test: `apps/frontend/src/components/settings/__tests__/ConnectButton.requestable.test.tsx`

**Interfaces:**
- Consumes: `apply_connection.runtime_supported(p)` from Task 1.
- Produces: `unavailable` Connections status and no connect action for a disconnected unsupported provider, while retaining a connected provider's row and disconnect path. The public agent catalog omits unsupported providers. New OAuth starts fail before sending the owner to the provider.

- [x] **Step 1: Add tests** for disconnected `stripe` and `vercel_cli` rows being `unavailable`, an existing connected row remaining `connected`, `_start_oauth` refusing unsupported `stripe`, the public catalog omitting it, and the unavailable row offering no Connect/Request action. Use the real provider registry and mock only external dependencies.
- [x] **Step 2: Run** the focused backend and frontend tests. The new assertions failed on current catalog/start/button behavior.
- [x] **Step 3: Update** `_connection_row` to show `unavailable` without `requestable` for runtime failures; `_start_oauth` raises 503 before the OAuth dance; `platform_info` omits unusable rails; `ConnectButton` renders no action for unavailable, non-requestable rows. The existing `apply_connection` guard remains the final defense for paste and callback paths.
- [x] **Step 4: Run** the targeted backend suites, frontend test, Ruff and ESLint. Local backend test execution used `.venv/bin/python -m pytest -o addopts= -p no:cov` because `uv run` selected a dependency-empty environment; its default coverage plugin also slowed startup.

### Task 4: Validate the safety slice and hand off to Identity decision

**Files:**
- Modify: `apps/infra/hermes/skills-src/lightbulb/references/integrations.md` only if its advertised capability list contradicts the catalog after Task 3.
- Modify: `apps/infra/hermes/skills-overlay/lightbulb/references/integrations.md` when the source reference changes; the overlay is what this runtime image loads.
- Test: the targeted backend tests from Task 3.

**Interfaces:**
- Produces: a reviewed diff demonstrating that no new long-lived secret storage path was added.

- [x] **Step 1: Review** `git diff` for unintended credential writes, status paths, and existing-connection teardown visibility.
- [x] **Step 2: Re-run** targeted tests and lint after correcting the skill reference. Result: 180 backend tests, 4 frontend tests, Ruff and ESLint passed.
- [ ] **Step 3: Commit** the source and tests. Do not push or promote a runtime from this safety slice without the normal CI and dev acceptance flow.
- [ ] **Step 4: Start** the separate AgentCore Identity decision spike defined in the approved spec. It must prove individual grant deletion and workspace-bound retrieval before Identity becomes an authority for customer grants.
