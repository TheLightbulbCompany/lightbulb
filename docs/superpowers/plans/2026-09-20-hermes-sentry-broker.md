# Hermes Sentry credential broker implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore one OAuth-protected remote MCP integration through a workspace-owned credential authority without writing its refresh token or access token to S3 profiles.

**Architecture:** Sentry was the first code proof for a hosted MCP bearer header. Stripe is now the first active-provider lifecycle to verify. The backend stores and refreshes the workspace grant in Secrets Manager. The Hermes bridge asks for a short-lived token before constructing an agent, injects it into the turn's existing secret scope, and keeps only a `${VAR}` reference in profile configuration. Existing workspace ownership and per-owner runtime remain unchanged.

**Tech Stack:** FastAPI, existing `httpx`/`boto3`, existing owner lifecycle lease, Hermes 0.21.3 bridge, pytest.

**Spec:** `docs/superpowers/specs/2026-09-20-hermes-integration-credentials-design.md`

## Global Constraints

- No third-party credential value or token file in the S3 Hermes profile; `config.yaml` may contain only `${VAR}` references.
- Hook tokens identify a workspace, not a roster agent. Verify a supplied run against a backend record when one exists; otherwise log only a workspace-level issuance and treat the agent/run fields as hints. The active agent can still see its allowed access token.
- An already-issued access token can remain valid until vendor expiry after disconnect. The backend must stop new issuance immediately and close/rebuild warm MCP sessions.
- Preserve the existing admin consent flow and workspace-shared grant semantics.
- Add no new dependency or provider abstraction for one Sentry implementation.
- This plan is deleted when the Sentry slice ships; git history keeps the execution details.

---

### Task 1: Backend-owned Sentry refresh

**Files:**
- Modify: `apps/backend/core/services/credentials/oauth_dance.py`
- Modify: `apps/backend/core/repositories/integrations_repo.py` to record reconnect-required without replacing row metadata
- Create: `apps/backend/core/services/credentials/access_token.py`
- Test: `apps/backend/tests/credentials/test_access_token.py`
- Test: `apps/backend/tests/credentials/test_integrations_repo.py`

**Interfaces:**
- Produces: `access_token.get(owner_id: str, provider_key: str) -> str`. Raises a typed reconnect-required error when no connected, refreshable grant exists.
- Uses: `owner_lifecycle_guard`, `integrations_repo.get`, `integration_secret_store.get/put`, registry OAuth metadata.

- [x] **Step 1: Write tests** for disconnected, fresh, expired, rotated, rejected, and client-changed grants. Tests exercise the real refresh logic with DynamoDB/Secrets Manager/HTTP boundaries stubbed.
- [x] **Step 2: Run** `.venv/bin/python -m pytest -o addopts= -p no:cov tests/credentials/test_access_token.py -q`; observe the missing-module failure.
- [x] **Step 3: Implement** refresh using the registry's auth method/resource indicator and the existing owner lifecycle lease. Record reconnect-required without replacing row metadata.
- [x] **Step 4: Run** focused access-token, OAuth dance, and repository tests plus Ruff.

### Task 2: Authenticated runtime vending

**Files:**
- Modify: `apps/backend/routers/runtime_rpc.py` (already mounted at `/api/v1/runtime`)
- Test: `apps/backend/tests/unit/routers/test_runtime_credentials.py`

**Interfaces:**
- Consumes: `access_token.get` from Task 1.
- Produces: hook-token-authenticated `POST /api/v1/runtime/credentials/u/{owner_id}` returning only connected, supported remote-MCP access tokens for the specified roster agent. Response has `Cache-Control: no-store`.

- [x] **Step 1: Write tests** for wrong hook, outside-roster agent, disconnected grant, access-only response, refresh failure and dev-only gating.
- [x] **Step 2: Run** the focused router test and observe its initial 404.
- [x] **Step 3: Add** the route to the already-mounted `runtime_rpc` router. Reuse `_require_hook_token` and the roster loader; return only supported, connected access tokens with `Cache-Control: no-store`.
- [x] **Step 4: Run** focused route tests and Ruff.

### Task 3: Hermes per-turn delivery and Sentry configuration

**Files:**
- Modify: `apps/infra/hermes/bridge/turn.py`
- Modify: `apps/backend/core/services/credentials/registry.py`
- Modify: `apps/backend/core/services/credentials/providers.yaml`
- Modify: `apps/backend/core/services/credentials/mcp_server.py`
- Modify: `apps/backend/core/services/hermes_profiles.py`
- Modify: `apps/frontend/src/hooks/useConnections.ts`, `components/settings/ConnectionsList.tsx`, and `ConnectButton.tsx` to expose reconnect-required for migrated rows
- Test: `apps/infra/hermes/tests/test_turn_credentials.py`
- Test: `apps/backend/tests/credentials/test_mcp_server.py`
- Test: `apps/backend/tests/unit/services/test_hermes_profiles_integrations.py`

**Interfaces:**
- Consumes: Task 2's credential response.
- Produces: Sentry `mcp_servers.sentry.headers.Authorization` configured as `Bearer ${MCP_SENTRY_ACCESS_TOKEN}` with no `auth: oauth` and no S3 token file. The bridge passes `MCP_SENTRY_ACCESS_TOKEN` through `_secret_scope(agent_id, overlay)` for a turn and clears the overlay afterward.

- [x] **Step 1: Write failing tests** for Sentry's bearer config, no secret in S3, old token/client-file cleanup, bridge scope lifetime, and reconnect UX.
- [x] **Step 2: Run** each focused test and observe the expected failure before implementation.
- [x] **Step 3: Add** brokered Sentry in dev, use Hermes's existing secret-scope overlay, and remove old S3/warm token files. The runtime broker refuses production issuance while Sentry is dev-only.
- [x] **Step 4a: Run** the pinned-image MCP scope smoke in CI; it passes.
- [ ] **Step 4b: Run** a controlled dev OAuth, tool-read, refresh, cold-runtime, and disconnect lifecycle before enabling the provider beyond dev. Stripe takes priority over Sentry for this live check.

### Task 4: Dev acceptance and expansion decision

**Files:**
- Modify: `apps/infra/hermes/skills-src/lightbulb/references/integrations.md` and its overlay only for confirmed Sentry behavior.
- Modify: `docs/superpowers/specs/2026-09-20-hermes-integration-credentials-design.md` only to record a material finding from the pilot.

**Interfaces:**
- Produces: evidence for the shared lifecycle contract before enabling more remote MCP providers.

- [ ] **Step 1: On a controlled dev workspace,** connect Sentry, confirm a harmless `mcp__sentry__*` read works, expire/refresh its token, roll the AgentCore runtime, repeat the read, disconnect, and prove a warm and cold run cannot use it. Verify that S3 contains no Sentry credential object; inspect only names/metadata, never print token values.
- [ ] **Step 2: Review** the diff, tests, runtime behavior, and credential placement. Enable a second provider only after this slice passes.
- [ ] **Step 3: Commit** source and tests, delete this disposable plan in the shipping PR, push through CI/CD, and watch the run. Production promotion remains on demand after dev acceptance.
