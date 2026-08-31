# P0c — Data plane + ledger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The owner-day rollup (revenue, visitors, signups, open threads, agent spend), refreshed by Connect webhooks and a scheduled pull, plus the retained PostHog token, work-product register-on-create, and the per-capability policy row + revoke-all — everything P1's Home/Money tabs render from.

**Architecture:** No new tables — rollup rows ride the existing `usage-counters` table under a `day:`-prefixed sort key (the `member:` prefix set the precedent). Refresh is event-driven (Connect webhooks mark an owner-day dirty and recompute inline) plus an hourly pass in the existing in-process scheduled worker (`update_service.run_scheduled_worker`, the only scheduler that exists). Provider truth stays provider-native: Stripe reads via the platform key + `Stripe-Account`, PostHog reads via a newly retained OAuth token; we persist only the six-counter cache row. Policy is a map on the billing row; revoke-all composes primitives that already exist.

**Tech Stack:** FastAPI + DynamoDB (plain dicts), stripe-python `StripeClient`, httpx (PostHog HogQL), existing repos/services cited per task.

**Spec:** `docs/superpowers/specs/2026-08-28-owner-data-plane-design.md` §4 (data layer), §4.5 (policy/ledger). UX preamble: the controller's tab-UX notes (Home renders the six sparklines from these rows; Spend reads what already exists; every number must be explainable by a provider link — "a page never renders from data only we hold").

## Global Constraints

- Table names come from `core.dynamodb.get_table("<suffix>")` — never literal full names (prefix migration in flight).
- `usage-counters` attributes are `Decimal`; money is **microcents** everywhere in that table (existing misnomer `total_spend_microdollars` noted in `usage_service.py:60`); NEW rollup attributes use honest names with `_microcents` suffixes.
- The scheduled worker runs transiently ×2 during deploys (`maxHealthyPercent: 200`) — every new pass must be idempotent or claim-guarded; copy the `_PURGE_PASS_INTERVAL_SECONDS` monotonic idiom (`update_service.py:989-995`).
- Stripe calls on the owner's account: platform key + `stripe_account=` (Connect, v1 reads). NEVER store amounts from webhooks without re-reading from the API (webhook payloads are unauthenticated hints post-verification, and replay-dedup already exists).
- Webhook additions ride the EXISTING dual-secret endpoint (`marketplace_purchases.py` — Connect-scoped registration); every new branch discriminates company-vs-seller via `billing_repo.get_by_company_stripe_account_id` first, exactly like `_handle_account_updated` (`marketplace_purchases.py:422-439`).
- Secrets: names-only in DynamoDB; values only in Secrets Manager under `lightbulb/{env}/integrations/{owner_id}/{provider}` (`integration_secret_store`).
- No new IAM: `table/*-{env}-*` wildcard already covers `usage-counters`; the integrations secret scope already covers the posthog retention.
- Every service function gets its unit test in the same task; run scoped pytest per task, full suite rides CI.

---

### Task 1: Rollup repo — day rows on `usage-counters`

**Files:**
- Create: `apps/backend/core/repositories/rollup_repo.py`
- Test: `apps/backend/tests/unit/repositories/test_rollup_repo.py`

**Interfaces:**
- Produces: `async def put_day(owner_id: str, day: str, values: dict[str, int]) -> None` (day = `"YYYY-MM-DD"`; values keys exactly: `revenue_microcents, visitors, signups, followers, open_threads, agent_spend_microcents`; whole-row `SET` overwrite — recompute-and-replace, not increment), `async def get_days(owner_id: str, start_day: str, end_day: str) -> list[dict]` (Query `owner_id` + `BETWEEN` on `period` with `day:` prefix, returns rows sorted ascending with `day` popped out of the `day:` SK), `async def delete_all_for_owner(owner_id: str) -> int` (mirror of `usage_repo.delete_all_for_owner:84` but only `day:`-prefixed rows).
- Consumes: `core.dynamodb.get_table("usage-counters")`; SK format `f"day:{day}"` (namespaced beside the existing `"%Y-%m"`, `"lifetime"`, `"member:*"` values).

- [ ] **Step 1: failing tests** (moto, same harness as `tests/unit/repositories/test_usage_repo.py` if present — else the existing usage-repo test's fixture style): `put_day` then `get_days` round-trips all six values as ints; `get_days` range excludes a row outside the window and ignores a `"2026-08"` month row; `put_day` twice overwrites (not adds); `delete_all_for_owner` removes only `day:` rows and returns the count.
- [ ] **Step 2: run** `uv run pytest tests/unit/repositories/test_rollup_repo.py -q` → FAIL (module missing).
- [ ] **Step 3: implement** — module docstring states the SK namespace decision and that values are whole-row replacement because every writer recomputes from provider truth.
- [ ] **Step 4: run** → PASS. **Step 5: commit** `feat(rollup): day rows on usage-counters under the day: SK namespace`.

### Task 2: Retained PostHog token

**Files:**
- Modify: `apps/backend/routers/connections.py:48-56` (`_retains_secret`), `apps/backend/core/services/credentials/registry.py` (add `retain_secret: bool = False` to `Provider`), `apps/backend/core/services/credentials/providers.yaml` (posthog block ~`:1241`: add `retain_secret: true`)
- Test: extend `apps/backend/tests/unit/routers/test_connections.py` (or the module that covers `_retains_secret` today — find with `grep -rn "_retains_secret" tests/`)

**Interfaces:**
- Produces: `_retains_secret(p) == (p.refresh_owner != "actor") or p.retain_secret` → the OAuth callback now writes the posthog credential dict to `integration_secret_store.put(owner_id, "posthog", credential)` alongside the container store; disconnect path already deletes via the same store (verify by reading the disconnect handler — if it filters on `_retains_secret` the delete comes free; state what you found in the report).
- Constraint: the retained copy is for BACKEND HogQL reads (P0c Task 4). Container behavior unchanged.

- [ ] Steps: failing test (posthog provider ⇒ `_retains_secret` True; an `actor`-refresh provider without the flag stays False) → implement (flag plumb: yaml → registry dataclass → predicate) → scoped tests green → commit `feat(connections): retain the posthog token for backend reads`.

### Task 3: Owner-Stripe day reader

**Files:**
- Create: `apps/backend/core/services/company_stripe_reads.py`
- Test: `apps/backend/tests/unit/services/test_company_stripe_reads.py`

**Interfaces:**
- Consumes: `billing_repo.get_by_owner_id(owner_id)` → `company_stripe_account_id`; `stripe.StripeClient(settings.STRIPE_SECRET_KEY)` with `stripe_account=` per call (v1 Connect reads — NOT the Provisioning client; do not touch `stripe_provisioning.py`).
- Produces: `async def day_money(owner_id: str, day: str) -> dict | None` returning `{"revenue_microcents": int, "signups": int}` — `None` when no company account is bound. Revenue = sum of `balance_transactions.list(created={gte,lt day bounds}, type="charge")` `net` (Stripe amounts are cents → ×10_000 to microcents); signups = `customers.list(created bounds)` count (paginate with `auto_paging_iter` capped at 1000/day, `# ponytail:` comment naming the cap). All SDK calls via `asyncio.to_thread`. Errors: log + return `None` — a rollup miss is a stale tile, never a 500.

- [ ] Steps: failing tests with a fake StripeClient injected via `patch.object` on a module-level `_client()` helper (copy the seam pattern from `stripe_provisioning._raw`) covering: happy path sums nets and counts customers with cents→microcents conversion; unbound owner → `None`; SDK raise → `None` — then implement → green → commit.

### Task 4: PostHog day reader

**Files:**
- Create: `apps/backend/core/services/owner_posthog.py`
- Test: `apps/backend/tests/unit/services/test_owner_posthog.py`

**Interfaces:**
- Token source (doc-verified 2026-08-31, PostHog `settings/web.py:1294-1300`: access tokens live 1h; refresh tokens live 30d and ROTATE on every use with a 120s grace window): PRIMARY = the container's EFS OAuth store — reuse `mcp_oauth._store_path(owner_id, ...)`/`_store_key16` to locate `mcp-oauth/posthog-*.json` and read `tokens.access_token`. If expired (store carries no expiry timestamp — attempt the query and refresh on 401): POST `token_url` grant_type=refresh_token with the store's `refresh_token` + `clientInformation.client_id` (public client, PKCE, no secret), then `atomic_write` the rotated pair back to the SAME store file (the 120s grace tolerates a race with a running container's own refresh). FALLBACK when the EFS read fails = `integration_secret_store.get(owner_id, "posthog")` (Task 2). A refresh failure with invalid_grant → `None` (stale tile; owner reconnect heals it — a 30d-idle container outlives its refresh token by design).
- Consumes: the above, plus PostHog API `POST https://us.posthog.com/api/projects/@current/query` with `{"query": {"kind": "HogQLQuery", "query": ...}}`, Bearer auth, httpx, 10s timeout. (The org/project routing rides the token; if `@current` 404s in dev verification, the fallback documented in the module is reading the project id from the credential's `provider_user_id` — implement `@current` only, note the fallback in a comment.)
- Produces: `async def day_traffic(owner_id: str, day: str) -> dict | None` → `{"visitors": int}` via `SELECT count(DISTINCT person_id) FROM events WHERE event = '$pageview' AND toDate(timestamp) = toDate('{day}')` (day interpolated after `re.fullmatch(r"\d{4}-\d{2}-\d{2}", day)` — reject anything else, injection fence). No token / HTTP error / non-200 → `None`, log once.

- [ ] Steps: failing tests (respx or patched httpx client): happy path parses `results[0][0]`; no retained token → `None` without an HTTP call; 429 → `None`; malformed day raises `ValueError` — implement → green → commit.

### Task 5: Rollup service + the scheduled pass

**Files:**
- Create: `apps/backend/core/services/rollup_service.py`
- Modify: `apps/backend/core/services/update_service.py` (new pass in `run_scheduled_worker`, constants near `:51`)
- Test: `apps/backend/tests/unit/services/test_rollup_service.py`

**Interfaces:**
- Produces: `async def refresh_owner_day(owner_id: str, day: str) -> dict | None` — composes Task 3 `day_money`, Task 4 `day_traffic` — and on a None source CARRIES FORWARD the existing row's values for that source's keys (read `rollup_repo.get_days(owner, day, day)` once on the outage path; zeros only when no prior row exists): stale beats zero for a dashboard, and this also stops the month-boundary pass from zeroing the prior month's final agent-spend stamp — `open_threads` = count of open support/agent-mail threads (use the existing agent-mail inbox repo's list/count for the owner — cite the exact function you find in `core/repositories/` in your report; 0 when absent), `agent_spend_microcents` = `usage_repo.get_period_usage(owner_id, day-month)["total_spend_microdollars"]` apportioned as the month-to-date value ONLY on the current month's rows (a `# ponytail:` comment: per-day inference split needs per-day usage rows — not worth it until Home ships); `followers` = 0 (bundle.social is P3 — honest zero, the tile hides when every value in range is 0). Writes via `rollup_repo.put_day`; returns the row or `None` if every source was `None`.
- Produces: `async def _rollup_pass()` in `update_service.py`: hourly (`_ROLLUP_PASS_INTERVAL_SECONDS = 60*60`, monotonic idiom copied from `:989-995`); iterates owners via `billing_repo` scan filtered on `attribute_exists(company_stripe_account_id)` (add `billing_repo.list_company_stripe_owners() -> list[str]` — a paginated scan, `# ponytail: scan is fine at fleet size; GSI when >1k owners`); refreshes TODAY and YESTERDAY (UTC) per owner; per-owner try/except so one owner's failure never starves the rest (the worker's own pass idiom).
- Registered in the worker loop body after `_paperclip_purge_pass`, try/except-wrapped identically.

- [ ] Steps: failing tests (all three sources patched): full compose writes the six keys; partial sources write what exists; all-None writes nothing; the pass calls refresh for today+yesterday per listed owner and swallows one owner's exception — implement → green → commit.

### Task 6: Connect money-webhooks → rollup refresh

**Files:**
- Modify: `apps/backend/routers/marketplace_purchases.py:174-195` (extend the chain), `apps/backend/core/config.py` (nothing new — secrets exist)
- Test: extend `apps/backend/tests/unit/routers/test_marketplace_purchases.py` (run the WHOLE file — prior lesson: this dispatcher's tests hit DynamoDB when new lookups are added; patch `billing_repo` at module scope like the existing tests do)

**Interfaces:**
- New branches for `payment_intent.succeeded`, `charge.succeeded`, `invoice.paid`, `customer.created`, `payout.paid`, `payout.failed`: resolve `event.get("account")` via `get_by_company_stripe_account_id`; if it's a company row → `asyncio.create_task(rollup_service.refresh_owner_day(owner_id, today_utc))` (fire-and-forget; the webhook 200s regardless — the hourly pass is the backstop) and return `{"status":"ok"}`; not a company account → fall through to existing behavior/ack (seller events unaffected).
- NOTE for the dev-verification checklist (do NOT automate): the sandbox webhook `we_1UADoQEcGJXHk5N7yfvAea4P` (Connect) must gain these six events — a Dashboard/API step the controller performs.
- `invoice.paid` collision: the platform endpoint (a) also handles `invoice.paid` — different endpoint + source-prefixed dedup key (`stripe-marketplace:` at `:167`) already isolates them; state this in a comment at the branch.

- [ ] Steps: failing tests (company event → refresh scheduled; seller event → untouched path; unknown account → ack) → implement → whole-file green → commit.

### Task 7: Work products — register-on-create

**Files:**
- Create: `apps/backend/core/services/work_products.py`
- Modify: `apps/backend/core/services/provisioning_workflow.py` (call at `_bind`, after `land_credentials` succeeds ~`:961`)
- Test: `apps/backend/tests/unit/services/test_work_products.py` + extend the bind-path test in `tests/unit/services/test_provisioning_workflow.py`

**Interfaces:**
- Produces: `async def register(owner_id: str, *, type: str, provider: str, title: str, url: str | None = None, external_id: str | None = None, metadata: dict | None = None) -> str | None` — resolves the owner's company via the same `PaperclipRepo(table_name="paperclip-companies")` + bootstrap-admin session pattern as `board_cards.file_board_card` (`board_cards.py:63-64`); creates via `paperclip_admin_client.create_issue_work_product`… **CHECK**: creation is issue-scoped upstream (`POST /issues/{id}/work-products`). Resolve the company's provisioning issue if the workflow row carries `issue_id`; when there is none, SKIP with a debug log and return `None` — never invent an issue. Body: `{"type": "artifact", "provider": provider, "title": title, "url": url, "externalId": external_id, "reviewState": "none", "metadata": metadata}` per `CreateWorkProductBody` aliases. Never raises (best-effort receipt; failures must not fail the rail).
- Consumes in `_bind`: `await work_products.register(owner_id, type="artifact", provider=row["provider_name"], title=f"Provisioned {row['service_ref']}", external_id=row["resource_id"], metadata={"workflow_id": row["workflow_id"], "level": "company"})` guarded on the row's `issue_id`.

- [ ] Steps: failing tests (mocked admin client: with issue_id → create called with aliased body; without → returns None, no call; client raise → None) → implement → green → commit.

### Task 8: Policy row + revoke-all (the P0d slice)

**Files:**
- Modify: `apps/backend/core/repositories/billing_repo.py` (policy map accessors), `apps/backend/routers/company_stripe.py` or new `apps/backend/routers/company_policy.py` (mount under `/api/v1/company` in `main.py` beside the provisioning router)
- Test: `apps/backend/tests/unit/routers/test_company_policy.py`

**Interfaces:**
- Row shape: `capability_policy: dict[str, bool]` on billing-accounts — keys exactly `money_moves, outreach, publishing, provisioning`; absent key ⇒ `True` (require approval — fail-closed default). Accessors: `get_capability_policy(owner_id) -> dict[str,bool]` (fills defaults), `set_capability_policy(owner_id, updates: dict[str,bool]) -> dict` (validates keys, `SET capability_policy = :p` merge).
- Routes (owner-authed via existing `AuthContext`, org-admin-gated with the same admin check the approve route uses — read `routers/company_provisioning.py`'s owner-route guard and reuse it): `GET /company/policy` → the filled map; `PATCH /company/policy` body `{capability: bool, ...}` → updated map. Rejects unknown capability names 422.
- `POST /company/revoke-all` (org-admin, plus a `confirm: "revoke"` body field): composes EXISTING primitives in this order — cancel open provisioning workflows (`account_wipe`'s cancel helper — reuse, don't reimplement), `integration_secret_store.delete` for every `projects/*` key on the owner's integrations rows + retained `posthog`, Stripe Connect deauthorize (the disconnect path `company_stripe.py:239` already does it — call its service function, not the route), and returns `{revoked: [...]}` naming what it did. NEVER touches provider resources themselves (spec §3.6: teardown never deprovisions).
- Enforcement wiring of the policy map into the rails is P1 — this task ships the row + surface only (state that in the router docstring).

- [ ] Steps: failing tests (defaults fill; patch merges + validates; revoke-all calls each primitive once, order asserted, confirm-guard 422 without the word) → implement → green → commit.

### Task 9 (final): whole-branch review + full backend suite

- [ ] `uv run pytest tests/ -q` full, `ruff check`, infra untouched (verify `git diff --stat origin/main -- apps/infra` is empty).
- [ ] Controller dispatches the final code reviewer per the SDD skill, fixes one wave, ledgers residuals.
- [ ] PR body: what P1 renders from each new piece + the dev-verification checklist (webhook event additions in the sandbox, one owner's rollup row inspected after an hour, posthog retention visible in SM).

## Deliberately out of P0c
- Lightbulb MCP server (own plan — it's a product surface, not plumbing).
- Per-day inference split, `followers` (bundle.social P3), policy ENFORCEMENT in rails (P1), support-thread `kind` extension (P2 Support task owns it).
- Any frontend. P1's tabs consume this.
