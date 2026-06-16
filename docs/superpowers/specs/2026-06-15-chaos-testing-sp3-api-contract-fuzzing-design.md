# Chaos Testing — Sub-project 3: API Contract Fuzzing

**Date:** 2026-06-15
**Status:** Approved design, autonomous execution
**Goal:** Prove the FastAPI HTTP edge is resilient to malformed/hostile input, cannot be reached without auth, and never violates its declared response contract — hermetically, on every PR.
**Spec sibling:** SP1 (`2026-06-15-chaos-testing-sp1-logic-invariants-design.md`). Ships together with SP1 (both hermetic/CI). SP2 (FIS infra faults) + SP4 (load) come after.

## Scope honesty

SP3 hardens the **HTTP edge**: input validation, auth gating, response-contract conformance. It does **not** test infra/network fault resilience (container kills, DynamoDB throttling, partitions, load) — that is SP2 + SP4. "App + edge resilient" after SP1+SP3; "infra + network resilient" is the next phase.

## §1 — Two passes

**Pass 1 — Negative / auth (broad, all 190 operations).** schemathesis loads the app's OpenAPI schema (`/api/v1/openapi.json`) via `schemathesis.openapi.from_asgi(...)` and fires generated malformed/hostile requests at every operation **with no auth token**, in-process over ASGI (no live server). Runs under `moto` so any DynamoDB-backed public endpoint is mocked rather than hitting real AWS.

Checks (schemathesis built-ins via `case.call_and_validate()`):
- **`not_a_server_error`** — no `5xx` on any input (a 500 on garbage = unhandled-exception bug).
- **`response_schema_conformance`** — responses match the declared OpenAPI schema.
- **`status_code_conformance`** — status codes are among those declared.
- **Custom `no_auth_bypass`** — any operation whose route depends on `get_current_user` must return `401/403` unauthenticated; a `2xx` is a **critical** finding.

Hermetic because unauthenticated requests bounce at the auth/validation layer before any Stripe/ECS/Paperclip/gateway call.

**Pass 2 — Curated deep slice (narrow, authenticated).** For a small **allowlist** of pure-DDB read operations (no external deps), override `get_current_user` (authenticated test user) + `moto`, and fuzz with valid auth so handler logic is actually exercised. Same `not_a_server_error` + schema-conformance checks. The allowlist is explicit and starts small (e.g. usage summary, marketplace search) — grown as safe endpoints are identified.

## §2 — Architecture

```
apps/backend/tests/chaos/api/
  __init__.py
  conftest.py                  # schemathesis schema from the ASGI app; moto wiring; auth override helper
  test_contract_negative.py    # Pass 1 — unauth, whole surface, under moto
  test_contract_authed_slice.py# Pass 2 — authed, curated pure-DDB allowlist
```

- Schema built once from `main.app` via `schemathesis.openapi.from_asgi("/api/v1/openapi.json", app)`.
- `@schema.parametrize()` generates one test per operation; `case.call_and_validate()` runs the checks.
- Marked `@pytest.mark.chaos`; runs in the existing backend CI `pytest tests/` step. schemathesis is already a dependency (resolves to v4.x).

## §3 — Findings & exclusions

- All findings batch into `docs/superpowers/research/2026-06-15-chaos-sp1-findings.md` (shared SP1+SP3 artifact) under an SP3 section: 5xx (bug), auth-bypass (critical), schema drift (contract).
- A **documented** exclusion list for operations that 5xx purely from an unmocked external dependency even unauthenticated (e.g. a public webhook that verifies a provider signature against a live SDK). Each exclusion carries a written reason; **no silent skips**. Prefer wrapping in moto/a minimal stub over excluding where cheap.
- The exclusion list lives in `conftest.py` as a named, commented constant so it's auditable.

## §4 — Hermetic execution & determinism

- In-process ASGI (no network, no live server). `moto` active for the whole run so no real AWS.
- Bounded generation: a per-operation example cap (schemathesis `Config`/settings) to keep CI runtime sane, mirroring SP1's `ci` Hypothesis profile.
- Env defaults (`CLERK_ISSUER`, `ENCRYPTION_KEY`, AWS region/creds) set in the api conftest, same as the SP1 chaos conftest.

## Open risks

- **schemathesis v4 API drift:** pinned `>=3.28.0` resolved to v4.10.2; the loader is `schemathesis.openapi.from_asgi` (not the v3 `schemathesis.from_asgi`). Implementation verifies the exact `call_and_validate` + custom-check registration API against the installed version.
- **False 5xx from unmocked deps:** triaged into the exclusion list with reasons, not silently skipped. If the list grows large, that itself is a finding about how much of the surface can't be exercised hermetically.
- **OpenAPI schema fidelity:** response-schema-conformance is only as good as the declared response models; endpoints with loose/absent response schemas get weaker conformance checking (noted per-finding).
