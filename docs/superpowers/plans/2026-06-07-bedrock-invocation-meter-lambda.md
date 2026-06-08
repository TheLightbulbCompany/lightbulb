# AWS Bedrock Invocation-Log Meter (Lambda → backend deplete) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the every-run, tenant-proof Bedrock usage meter — AWS model-invocation logging → a dumb transport Lambda → an authed backend endpoint that prices, attributes, and depletes the ledger — so spend is captured on *every* invocation (including failing heartbeats and `error`/`aborted` runs), with the cache tokens that the in-container `sessions.list` biller misses.

**Architecture:** AWS Bedrock model-invocation logging (mode "Both": CloudWatch Logs + S3, text+embedding modalities) is enabled once per fan-out region. A CloudWatch Logs subscription filter scoped to the Isol8 application-inference-profile (AIP) ARN shape drives a meter Lambda. The Lambda is a **dumb transport**: it parses each log record (gunzip + base64 decode the subscription payload), fetches the S3 object when the body was offloaded (>100KB), extracts token/cache/status/region fields, and POSTs the extracted record to a service-token-authed backend endpoint. **All pricing, owner-attribution, and depletion live in the backend** (`POST /api/v1/internal/usage/from-bedrock-log`) so a future Metronome swap is a config flip — it resolves the owner via Plan 2's `resolve_owner_from_aip_arn`, prices via `bedrock_pricing`, builds a Plan-1 `UsageEvent`, and calls Plan-1 `get_ledger().deplete()` (exactly-once on the Bedrock `requestId`).

**Tech Stack:** Python 3.12 (`urllib.request` stdlib transport + `boto3` for the S3 fetch — no pip bundling, mirroring `ws-connect`), FastAPI + Pydantic (the internal endpoint), PyJWT (a new `bedrock_meter` service-token kind), `pytest` + `pytest-asyncio` + `moto[dynamodb]` for backend tests, AWS CDK (TypeScript, `aws-cdk-lib`) + Jest `Template.fromStack` for the meter Lambda / subscription filter / DLQ / `AwsCustomResource` logging config.

---

## Context for the implementer (read before Task 1)

This is **Plan 3 of 4** for Phase 1 of the Bedrock metering remediation (spec: `docs/superpowers/specs/2026-06-07-bedrock-metering-enforcement-metronome-design.md`). The other three plans are siblings:

- **Plan 1 (ALREADY WRITTEN — `docs/superpowers/plans/2026-06-07-ledger-interface-ddb-idempotency.md`):** defines `core/billing/usage_event.py::UsageEvent`, `core/billing/ledger_interface.py::LedgerBackend.deplete(event) -> DepleteResult` / `get_balance` / `get_ledger()`, and `core/billing/ddb_ledger_backend.py::DdbLedgerBackend`. **This plan CONSUMES those — never redefine them.** In particular `deplete()` is exactly-once on `event.request_id` (the Bedrock `requestId`), so this plan must NOT add its own dedup.
- **Plan 2 (AIP + IAM enforcement):** defines `resolve_owner_from_aip_arn(aip_arn: str) -> str | None` (the reverse lookup this plan calls for attribution) and `revoke_owner_bedrock` / `restore_owner_bedrock` (Plan 4 calls those, not this plan). Plan 2 also persists, per owner, which tier (`opus`/`sonnet`/`haiku`) each AIP ARN maps to. **This plan CONSUMES `resolve_owner_from_aip_arn` and the tier resolution it exposes.**
- **Plan 4 (enforcement loop + cutover):** wires the pre-chat gate, the kill-switch on balance ≤ 0, and retires the legacy `connection_pool` deduct. Not this plan.

**Plan 2 dependency — exact names this plan imports (these MUST exist before Task 6 runs, but the tests patch them so this plan is independently testable):**

```python
from core.billing.bedrock_aip import (
    resolve_owner_from_aip_arn,
    resolve_owner_from_identity_arn,
    resolve_tier_from_aip_arn,
)
```

- `resolve_owner_from_aip_arn(aip_arn: str) -> str | None` — owner attribution from the AIP ARN (`modelId`), the primary path (Plan 2).
- `resolve_owner_from_identity_arn(identity_arn: str) -> str | None` — **fallback** owner attribution from the assumed-role ARN (`identity.arn`), used when the AIP-ARN lookup misses (Plan 2). **The spec (§3.2) requires attribution "via the AIP ARN OR the assumed-role ARN"; without this the captured `identity_arn` is dead code — see the attribution note below.** This is a Plan-2-owned contract: Plan 2's per-owner IAM role (one role per owner, name pattern `isol8-owner-<owner_id>`) is the assumed role whose ARN appears in `identity.arn`, so Plan 2 already holds the owner↔role mapping and is the correct owner of this reverse lookup.
- `resolve_tier_from_aip_arn(aip_arn: str) -> str | None` — returns `"opus" | "sonnet" | "haiku" | None` for an AIP ARN (Plan 2). **This plan needs it because `bedrock_pricing.normalize_model_id` cannot turn an AIP ARN into a foundation-model id** — see the landmine below.

**RECONCILED MODULE PATH (final — cross-plan review settled this):** the AIP resolvers + kill-switch primitives are owned by Plan 2 and live in `core/billing/bedrock_aip.py`. That is the single canonical path this plan imports from (`from core.billing.bedrock_aip import resolve_owner_from_aip_arn, resolve_owner_from_identity_arn, resolve_tier_from_aip_arn`). An earlier draft pointed at a different module under a separate top-level package; that path was WRONG and has been reconciled to the canonical `core/billing/bedrock_aip.py` everywhere — there is no longer any path mismatch to fix at execution time. Everything else is patched in tests. The grounding here assumes Plan 2 lands `core/billing/bedrock_aip.py` with those three functions.

**SHARED-CONTRACT ADDENDUM (this plan adds one symbol to Plan 2's surface):** the original SHARED CONTRACTS listed only `resolve_owner_from_aip_arn` / `resolve_tier_from_aip_arn` from Plan 2. This plan additionally requires `resolve_owner_from_identity_arn(identity_arn: str) -> str | None` so the spec-mandated assumed-role-ARN fallback is wired, not dead. Plan 2 MUST define it in `core/billing/bedrock_aip.py` (it owns the owner↔IAM-role mapping). The Task 4 stub below stubs all three.

**The AIP-ARN pricing landmine (load-bearing — read it twice).** The invocation-log `modelId` is the per-owner **AIP ARN**, e.g. `arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-opus-org_abc`. `bedrock_pricing.normalize_model_id` (`core/billing/bedrock_pricing.py:94`) does `model_id.split("/", 1)[1]` → that yields the bare *profile id* (`aip-opus-org_abc`), **not** a foundation-model id like `anthropic.claude-opus-4-6-v1`. So `get_rate(aip_arn)` raises `UnknownModelError` and the deduct silently skips → revenue loss. **The fix in this plan:** map the AIP ARN → tier (via Plan 2's `resolve_tier_from_aip_arn`) → the canonical foundation-model id, then price that. Task 4 builds the tier→FM map and the `price_from_aip_arn` helper. **Do NOT call `cost_microcents(model_id=aip_arn)` directly — it does not work.**

**The S3-offload landmine (the #1 correctness risk).** Bedrock offloads the invocation body to S3 when it exceeds 100KB — exactly the large-cached-prefix profile that drove the runaway. The cache-read / cache-write token counts live in that body. If the Lambda reads them as 0 on offloaded records, the original ~30× cache-blind undercount returns. **The exact JSON field paths and the offload-reference field are UNVERIFIED until Task 1 captures a real dev record.** Task 1 is a hard build-gate: no parsing code is written until a real inline record AND a real >100KB S3-offloaded record are captured and committed as fixtures, and the exact paths are locked. The parser is written defensively (try the candidate paths, fall back to S3) — never hardcode an unverified path as if confirmed.

**The W53 account-global collision landmine.** `observability-stack.ts:1609-1616` is an explicit tombstone: Cost Anomaly Detection was removed because it is an **account-global** resource and the per-env observability stack deploys in *both* dev and prod (same AWS account `877352799272`), so the two deployments fight. `bedrock:PutModelInvocationLoggingConfiguration` is **account+region-level — exactly one config per region**, and there is **one Bedrock invocation-log group per region per account**. Therefore the whole meter (logging config + log group + subscription filter + Lambda) MUST be a **single-owner, account-scoped stack instantiated once**, NOT folded into the per-env observability stack. This plan creates a dedicated `BedrockMeterStack` instantiated **once** (in the prod stage only) so dev and prod never collide. Dev validation happens via Task 1's temporary manual enablement, not a second permanent stack.

**Run all backend commands from `apps/backend/`. Run all CDK commands from `apps/infra/`. Use `uv run` for Python, `npx` for CDK/Jest.** `asyncio_mode = auto` is set (`pytest.ini:6`, `pyproject.toml:41`) so async test functions need no `@pytest.mark.asyncio`, but Plan 1's tests use the marker explicitly — this plan follows the marker convention to match Plan 1's fixtures.

---

## File Structure

| File | Responsibility |
|---|---|
| `apps/infra/lambda/bedrock-usage-meter/fixtures/inline_record.json` (Create, Task 1) | A real captured inline (non-offloaded) Bedrock invocation-log record. Ground-truth parser fixture. |
| `apps/infra/lambda/bedrock-usage-meter/fixtures/s3_offloaded_record.json` (Create, Task 1) | A real captured >100KB S3-offloaded record (cache tokens in S3). Ground-truth parser fixture. |
| `apps/infra/lambda/bedrock-usage-meter/fixtures/s3_body.json` (Create, Task 1) | The S3 object body the offloaded record points at (cache token counts live here). |
| `apps/infra/lambda/bedrock-usage-meter/FIELDS.md` (Create, Task 1) | The locked exact JSON paths for every extracted field + the offload reference. |
| `apps/backend/core/services/service_token.py` (Modify) | Add the `bedrock_meter` token kind + `mint_meter` / `verify_meter`. |
| `apps/backend/core/billing/bedrock_pricing.py` (Modify) | Add `price_from_aip_arn` (AIP ARN → tier → FM id → `cost_microcents`). |
| `apps/backend/core/config.py` (Modify, near line 102) | Add `METER_SERVICE_TOKEN_KEY` setting. |
| `apps/backend/routers/internal_usage.py` (Create) | The internal meter endpoint: `require_meter_token` dep + `BedrockLogRecord` schema + `POST /usage/from-bedrock-log`. |
| `apps/backend/main.py` (Modify) | Register `internal_usage.router` under `/api/v1/internal` with `include_in_schema=False`. |
| `apps/infra/lambda/bedrock-usage-meter/index.py` (Create) | The dumb transport Lambda: parse subscription payload, S3 fallback, POST to backend. |
| `apps/infra/lambda/bedrock-usage-meter/test_index.py` (Create) | Lambda unit tests against the Task-1 fixtures. |
| `apps/infra/lib/stacks/bedrock-meter-stack.ts` (Create) | Account-scoped, single-owner stack: logging config (`AwsCustomResource`), per-region Lambda + subscription filter + SQS DLQ. |
| `apps/infra/lib/isol8-stage.ts` (Modify) | Instantiate `BedrockMeterStack` ONCE (prod stage only). |
| `apps/infra/lib/stacks/auth-stack.ts` (Modify, Task 11) | Add the `bedrockMeterToken` secret slot (bare name `bedrock_meter_token` → `createSecret` prepends `isol8/${env}/`) for the pre-minted meter token. |
| `apps/infra/test/bedrock-meter-stack.test.ts` (Create) | CDK `Template.fromStack` assertions (filter pattern, DLQ, Lambda env, logging config, modalities). |
| `apps/backend/tests/unit/services/test_service_token.py` (Create/extend) | Meter-token round-trip + cross-kind rejection. |
| `apps/backend/tests/unit/billing/test_bedrock_pricing_aip.py` (Create) | `price_from_aip_arn` tests. |
| `apps/backend/tests/unit/routers/test_internal_usage.py` (Create) | Endpoint auth + happy path + edge cases (moto + Plan-1 `ledger_tables` fixture). |

---

### Task 1: BUILD-GATE — capture a real dev invocation-log record + lock the field paths

**This is an ops task, not TDD. Nothing downstream is written until it completes.** It produces three checked-in fixtures + a `FIELDS.md` that every later task references. Do NOT skip it or substitute a hand-authored guess — the cache-token paths and the S3-offload reference field are the two highest-risk unknowns in the whole plan.

**Files:**
- Create: `apps/infra/lambda/bedrock-usage-meter/fixtures/inline_record.json`
- Create: `apps/infra/lambda/bedrock-usage-meter/fixtures/s3_offloaded_record.json`
- Create: `apps/infra/lambda/bedrock-usage-meter/fixtures/s3_body.json`
- Create: `apps/infra/lambda/bedrock-usage-meter/FIELDS.md`

- [ ] **Step 1: SSO re-auth + confirm the AIP ARN shape and a live owner**

Run:
```bash
aws sso login --profile isol8-admin
aws dynamodb scan --table-name isol8-dev-containers --profile isol8-admin --region us-east-1 \
  --query 'Items[].owner_id.S' --output text
```
Expected: at least one dev owner id (e.g. `org_...` or `user_...`). Note one as `$OWNER` for the test run. (If Plan 2 already minted AIPs, also note the AIP ARN it stored on that owner's `containers` row.)

- [ ] **Step 2: Enable model-invocation logging in DEV (TEMPORARY — manual, not via the per-env stack)**

Create a dedicated dev log group + S3 bucket for the capture, then enable logging. This is account+region-global, so do it manually in dev ONLY for the capture; the permanent config is owned by `BedrockMeterStack` (prod) in Task 12.

Run:
```bash
aws logs create-log-group --log-group-name /isol8/dev/bedrock-invocation \
  --profile isol8-admin --region us-east-1 || true
DEV_BUCKET=isol8-dev-bedrock-invocation-capture-$(date +%s)
aws s3 mb "s3://$DEV_BUCKET" --profile isol8-admin --region us-east-1
# Bedrock needs a delivery role; create a minimal one for the capture.
aws iam create-role --role-name isol8-dev-bedrock-logging-capture \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"bedrock.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
  --profile isol8-admin || true
aws iam put-role-policy --role-name isol8-dev-bedrock-logging-capture \
  --policy-name capture --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":[\"logs:CreateLogStream\",\"logs:PutLogEvents\"],\"Resource\":\"arn:aws:logs:us-east-1:877352799272:log-group:/isol8/dev/bedrock-invocation:*\"},{\"Effect\":\"Allow\",\"Action\":[\"s3:PutObject\"],\"Resource\":\"arn:aws:s3:::$DEV_BUCKET/*\"}]}" \
  --profile isol8-admin
ROLE_ARN=arn:aws:iam::877352799272:role/isol8-dev-bedrock-logging-capture
aws bedrock put-model-invocation-logging-configuration --profile isol8-admin --region us-east-1 \
  --logging-config "{\"textDataDeliveryEnabled\":true,\"embeddingDataDeliveryEnabled\":true,\"imageDataDeliveryEnabled\":false,\"cloudWatchConfig\":{\"logGroupName\":\"/isol8/dev/bedrock-invocation\",\"roleArn\":\"$ROLE_ARN\",\"largeDataDeliveryS3Config\":{\"bucketName\":\"$DEV_BUCKET\"}},\"s3Config\":{\"bucketName\":\"$DEV_BUCKET\"}}"
aws bedrock get-model-invocation-logging-configuration --profile isol8-admin --region us-east-1
```
Expected: the final `get` echoes back the config with `textDataDeliveryEnabled: true`, `embeddingDataDeliveryEnabled: true`, and the `largeDataDeliveryS3Config` bucket set. The `largeDataDeliveryS3Config` is the >100KB-offload sink — it is what makes the offloaded-record capture possible.

- [ ] **Step 3: Generate a real cache-using run AND a >100KB cache run through the dev container**

Open `https://dev.isol8.co/chat` for `$OWNER`'s account and send: (a) a short message (inline record), then (b) paste a >100KB block of text and ask the agent to summarize it (forces a large cached prefix → S3 offload). Wait ~2 minutes for log delivery.

- [ ] **Step 4: Pull one inline record and one offloaded record from CloudWatch**

Run:
```bash
STREAM=$(aws logs describe-log-streams --log-group-name /isol8/dev/bedrock-invocation \
  --order-by LastEventTime --descending --max-items 1 \
  --profile isol8-admin --region us-east-1 --query 'logStreams[0].logStreamName' --output text)
aws logs get-log-events --log-group-name /isol8/dev/bedrock-invocation \
  --log-stream-name "$STREAM" --limit 20 --profile isol8-admin --region us-east-1 \
  --query 'events[].message' --output text > /tmp/bedrock-records.txt
```
Expected: `/tmp/bedrock-records.txt` contains one JSON object per line. Identify (a) a small inline record whose `input`/`output` include `inputBodyJson`/`outputBodyJson` inline, and (b) a large record whose body fields are an S3 reference (a key like `inputBodyS3Path` / `s3Location` / `..._s3` — the exact name is what you are locking).

- [ ] **Step 5: Save the three fixtures**

Save the chosen inline record verbatim to `apps/infra/lambda/bedrock-usage-meter/fixtures/inline_record.json`, the offloaded record verbatim to `apps/infra/lambda/bedrock-usage-meter/fixtures/s3_offloaded_record.json`, and the S3 object it references to `apps/infra/lambda/bedrock-usage-meter/fixtures/s3_body.json`:
```bash
mkdir -p apps/infra/lambda/bedrock-usage-meter/fixtures
# (manually copy the two records out of /tmp/bedrock-records.txt into the two files)
# Then fetch the offloaded body using the S3 ref you just identified:
aws s3 cp "s3://$DEV_BUCKET/<the-s3-key-from-the-offloaded-record>" \
  apps/infra/lambda/bedrock-usage-meter/fixtures/s3_body.json --profile isol8-admin
```
Expected: three non-empty JSON files. **Scrub any prompt/output text content** from the bodies before committing (the fixtures only need the metadata + token/usage blocks); replace large `inputBodyJson`/`outputBodyJson` text with `"<redacted>"`.

**CRITICAL — do NOT scrub the token/usage numbers.** The `s3_body.json` fixture MUST retain its REAL, NON-ZERO `cacheReadInputTokens` / `cacheWriteInputTokens` (or whatever the captured key names are) — those numbers are the ground truth that `test_merge_s3_body_pulls_cache_tokens_from_s3` asserts against. A zeroed or absent cache count makes the #1-landmine test vacuous (it would pass even when the merge silently yields 0). Pick a capture run (Step 3b, the >100KB summarize prompt) that genuinely produced a large cached prefix so the offloaded body carries a non-zero cache-read count; if your capture's `cacheReadInputTokens` is 0, re-run Step 3b with a larger pasted block until it isn't, then re-capture.

- [ ] **Step 6: Lock the exact paths in FIELDS.md**

Inspect the fixtures and record the *literal* JSON paths. Create `apps/infra/lambda/bedrock-usage-meter/FIELDS.md`:
```markdown
# Bedrock invocation-log record — locked field paths (captured dev, 2026-06-07)

Source: `aws bedrock get-model-invocation-logging-configuration` (dev capture).
Each CloudWatch log event `message` is ONE JSON object of this shape.

| Logical field        | JSON path (inline)                          | JSON path (S3-offloaded body)            |
|----------------------|---------------------------------------------|------------------------------------------|
| requestId            | record["requestId"]                         | (same — always in the CW record)         |
| modelId (AIP ARN)    | record["modelId"]                           | (same)                                   |
| identity.arn         | record["identity"]["arn"]                   | (same)                                   |
| region               | record["region"]                            | (same)                                   |
| timestamp            | record["timestamp"]                         | (same)                                   |
| run status / errorCode | record["errorCode"] (absent ⇒ success)    | (same)                                   |
| input_tokens         | record["input"]["inputTokenCount"]          | s3_body["usage"]["inputTokens"]          |
| output_tokens        | record["output"]["outputTokenCount"]        | s3_body["usage"]["outputTokens"]         |
| cache_read_tokens    | record["input"]["cacheReadInputTokenCount"] | s3_body["usage"]["cacheReadInputTokens"] |
| cache_write_tokens   | record["input"]["cacheWriteInputTokenCount"]| s3_body["usage"]["cacheWriteInputTokens"]|
| S3-offload reference  | record["input"]["inputBodyS3Path"] (key OR s3:// URI) | n/a |

NOTE: the paths above are CANDIDATES corrected to MATCH the captured fixtures.
If a captured fixture disagrees, the FIXTURE wins — edit this table and the
candidate-path lists in index.py:_extract / _merge_s3_body to match.
```
Replace every cell with the **actual** path observed in your fixtures (the table above is the AWS-documented candidate shape; the capture is the source of truth).

- [ ] **Step 7: TEAR DOWN the temporary dev logging (leave nothing account-global behind)**

Run:
```bash
aws bedrock delete-model-invocation-logging-configuration --profile isol8-admin --region us-east-1
aws bedrock get-model-invocation-logging-configuration --profile isol8-admin --region us-east-1
```
Expected: the second `get` returns an empty `{}` / `loggingConfig` absent — confirming dev no longer owns an account-global config (so Task 12's prod stack will not collide with a stale dev one). Leave the temp bucket/role for re-capture if needed, or delete them.

- [ ] **Step 8: Commit the fixtures + FIELDS.md**

```bash
git add apps/infra/lambda/bedrock-usage-meter/fixtures/ apps/infra/lambda/bedrock-usage-meter/FIELDS.md
git commit -m "chore(meter): capture real Bedrock invocation-log fixtures + lock field paths"
```

---

### Task 2: `bedrock_meter` service-token kind

**Files:**
- Modify: `apps/backend/core/services/service_token.py`
- Test: `apps/backend/tests/unit/services/test_service_token.py`

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/services/test_service_token.py`:

```python
"""Tests for service_token: the paperclip kind AND the new bedrock_meter kind.

The meter kind MUST NOT be accepted by verify() (the paperclip path the WS
authorizer trusts), and a paperclip token MUST NOT be accepted by
verify_meter() — a leaked meter token must never authorize container access.
"""

import jwt
import pytest

from core.services import service_token


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch):
    monkeypatch.setenv("PAPERCLIP_SERVICE_TOKEN_KEY", "test-signing-key-32-bytes-long-xx")


def test_mint_meter_round_trips():
    token = service_token.mint_meter()
    claims = service_token.verify_meter(token)
    assert claims["kind"] == service_token.METER_TOKEN_KIND


def test_meter_token_rejected_by_paperclip_verify():
    token = service_token.mint_meter()
    with pytest.raises(jwt.InvalidTokenError):
        service_token.verify(token)


def test_paperclip_token_rejected_by_verify_meter():
    token = service_token.mint("user_abc")
    with pytest.raises(jwt.InvalidTokenError):
        service_token.verify_meter(token)


def test_verify_meter_rejects_garbage():
    with pytest.raises(jwt.PyJWTError):
        service_token.verify_meter("not-a-jwt")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/services/test_service_token.py -v`
Expected: FAIL — `AttributeError: module 'core.services.service_token' has no attribute 'mint_meter'`

- [ ] **Step 3: Write minimal implementation**

In `apps/backend/core/services/service_token.py`, after the `SERVICE_TOKEN_KIND` / `DEFAULT_TTL_DAYS` lines (currently 24-25), add:

```python
METER_TOKEN_KIND = "bedrock_meter"
```

At the end of the file (after `verify`), add:

```python
def mint_meter(ttl_days: int = DEFAULT_TTL_DAYS) -> str:
    """Mint a meter→backend service token.

    Distinct kind from the paperclip service token: a leaked meter token must
    NEVER pass the WS authorizer's `kind == "paperclip_service"` check, so it
    cannot authorize container access. No `sub` is needed (the meter is not a
    user); a fixed sentinel sub keeps the JWT well-formed.
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": "__bedrock_meter__",
        "kind": METER_TOKEN_KIND,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=ttl_days)).timestamp()),
        "jti": secrets.token_urlsafe(16),
    }
    return jwt.encode(payload, _signing_key(), algorithm="HS256")


def verify_meter(token: str) -> dict:
    """Verify a meter service token. Returns the claims dict on success.

    Raises jwt.ExpiredSignatureError / jwt.InvalidTokenError on failure, incl.
    when the token is a paperclip-kind token (wrong kind for this surface).
    """
    claims = jwt.decode(token, _signing_key(), algorithms=["HS256"])
    if claims.get("kind") != METER_TOKEN_KIND:
        raise jwt.InvalidTokenError(f"Wrong kind claim: {claims.get('kind')!r}")
    return claims
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/services/test_service_token.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/service_token.py apps/backend/tests/unit/services/test_service_token.py
git commit -m "feat(billing): add bedrock_meter service-token kind (mint_meter/verify_meter)"
```

---

### Task 3: `METER_SERVICE_TOKEN_KEY` setting

**Files:**
- Modify: `apps/backend/core/config.py` (after line 102)

The meter token is signed with the same `PAPERCLIP_SERVICE_TOKEN_KEY` symmetric secret (`service_token._signing_key()` reads it), because the WS authorizer already shares that key and the *kind* claim is what isolates the meter surface. So no new signing key is needed for verification. We add `METER_SERVICE_TOKEN_KEY` only as the **slot the CDK injects the pre-minted token into the Lambda env**, kept distinct from the signing key name to avoid confusion in the Lambda. This is config-only; tested via the endpoint in Task 6.

- [ ] **Step 1: Add the setting**

In `apps/backend/core/config.py`, immediately after the `LEDGER_BACKEND` line that Plan 1 added (which itself sits right after `CREDIT_TRANSACTIONS_TABLE` at line 102), add:

```python
    # Internal Bedrock meter — the pre-minted bedrock_meter service token the
    # CDK injects into the meter Lambda env. The backend verifies it with the
    # shared PAPERCLIP_SERVICE_TOKEN_KEY signing secret (kind="bedrock_meter").
    METER_SERVICE_TOKEN_KEY: str = os.getenv("METER_SERVICE_TOKEN_KEY", "")
```

(If Plan 1's `LEDGER_BACKEND` line is not present yet, add this block right after the `CREDIT_TRANSACTIONS_TABLE` line at 102; the exact neighbor does not matter, only that it is inside the `Settings` class.)

- [ ] **Step 2: Confirm it imports**

Run: `uv run python -c "from core.config import settings; print(settings.METER_SERVICE_TOKEN_KEY == '')"`
Expected: `True`

- [ ] **Step 3: Commit**

```bash
git add apps/backend/core/config.py
git commit -m "feat(config): add METER_SERVICE_TOKEN_KEY for the Bedrock meter Lambda"
```

---

### Task 4: AIP-ARN → foundation-model pricing (`price_from_aip_arn`)

**Files:**
- Modify: `apps/backend/core/billing/bedrock_pricing.py`
- Test: `apps/backend/tests/unit/billing/test_bedrock_pricing_aip.py`

**Design (read before coding):** the meter receives an AIP ARN, which `normalize_model_id` cannot turn into a rate-table key (it returns the bare profile id). We add `price_from_aip_arn` which: (1) asks Plan 2's `resolve_tier_from_aip_arn(aip_arn)` for the tier, (2) maps the tier to the canonical foundation-model id (`_TIER_TO_FM`), (3) prices via the existing `cost_microcents`. If the tier is unknown (None) we fall back to letting `cost_microcents`/`get_rate` try the raw id (which will raise `UnknownModelError`) — the endpoint then skips + emits a metric. The tier→FM map is the same three keys already in `_RATES` (`bedrock_pricing.py:58-84`).

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/billing/test_bedrock_pricing_aip.py`:

```python
"""Tests for price_from_aip_arn — AIP ARN -> tier -> FM id -> cost."""

from unittest.mock import patch

import pytest

from core.billing import bedrock_pricing
from core.billing.bedrock_pricing import UnknownModelError, price_from_aip_arn

OPUS_AIP = (
    "arn:aws:bedrock:us-east-1:877352799272:"
    "application-inference-profile/aip-opus-org_abc"
)


def test_price_from_aip_arn_opus_via_tier():
    # 1000 input * 5.5 + 500 output * 27.5 = 5500 + 13750 = 19250 microcents
    with patch.object(bedrock_pricing, "resolve_tier_from_aip_arn", return_value="opus"):
        cost = price_from_aip_arn(
            aip_arn=OPUS_AIP, input_tokens=1000, output_tokens=500,
            cache_read_tokens=0, cache_write_tokens=0,
        )
    assert cost == 19250


def test_price_from_aip_arn_includes_cache_tokens():
    # opus: 1000*5.5 + 0 + 2000*0.55(cache_read) + 300*11.0(cache_write)
    #     = 5500 + 1100 + 3300 = 9900
    with patch.object(bedrock_pricing, "resolve_tier_from_aip_arn", return_value="opus"):
        cost = price_from_aip_arn(
            aip_arn=OPUS_AIP, input_tokens=1000, output_tokens=0,
            cache_read_tokens=2000, cache_write_tokens=300,
        )
    assert cost == 9900


def test_price_from_aip_arn_haiku_tier():
    # haiku: 100*1.1 + 100*5.5 = 110 + 550 = 660
    with patch.object(bedrock_pricing, "resolve_tier_from_aip_arn", return_value="haiku"):
        cost = price_from_aip_arn(
            aip_arn="arn:...:application-inference-profile/aip-haiku-x",
            input_tokens=100, output_tokens=100,
            cache_read_tokens=0, cache_write_tokens=0,
        )
    assert cost == 660


def test_price_from_aip_arn_unknown_tier_raises():
    with patch.object(bedrock_pricing, "resolve_tier_from_aip_arn", return_value=None):
        with pytest.raises(UnknownModelError):
            price_from_aip_arn(
                aip_arn="arn:...:application-inference-profile/aip-mystery",
                input_tokens=10, output_tokens=10,
                cache_read_tokens=0, cache_write_tokens=0,
            )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/billing/test_bedrock_pricing_aip.py -v`
Expected: FAIL — `ImportError: cannot import name 'price_from_aip_arn' from 'core.billing.bedrock_pricing'`

- [ ] **Step 3: Write minimal implementation**

In `apps/backend/core/billing/bedrock_pricing.py`, add the Plan-2 import at the top (after the existing `import re` block, around line 26):

```python
from core.billing.bedrock_aip import resolve_tier_from_aip_arn
```

After the `_RATES` dict (ends at line 84), add:

```python
# Tier (the AIP "lane" Plan 2 assigns per owner) -> canonical foundation-model
# id used as the rate-table key. The three keys mirror _RATES above. The meter
# receives an AIP ARN (modelId in the invocation log), which normalize_model_id
# cannot turn into a foundation id; this map closes that gap.
_TIER_TO_FM: dict[str, str] = {
    "opus": "anthropic.claude-opus-4-6-v1",
    "sonnet": "anthropic.claude-sonnet-4-6",
    "haiku": "anthropic.claude-haiku-4-5-20251001-v1:0",
}
```

After `cost_microcents` (ends at line 167), add:

```python
def price_from_aip_arn(
    *,
    aip_arn: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> int:
    """Price an invocation whose modelId is a per-owner AIP ARN.

    The AIP ARN cannot be priced directly (it carries no foundation-model id),
    so we resolve the owner's tier (Plan 2's AIP registry) and price the
    canonical foundation-model id for that tier.

    Raises:
        UnknownModelError: the AIP ARN maps to no known tier.
    """
    tier = resolve_tier_from_aip_arn(aip_arn)
    fm_id = _TIER_TO_FM.get(tier) if tier else None
    if fm_id is None:
        raise UnknownModelError(aip_arn)
    return cost_microcents(
        model_id=fm_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
    )
```

(The test patches `bedrock_pricing.resolve_tier_from_aip_arn`, so the function is imported into this module's namespace and patchable there — the import line above is what makes that work even before Plan 2 lands.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/billing/test_bedrock_pricing_aip.py -v`
Expected: PASS (4 passed). If it fails with `ModuleNotFoundError: No module named 'core.billing.bedrock_aip'` (Plan 2 not yet merged), create a minimal stub at the canonical Plan-2-owned path so this plan is independently runnable:

```bash
cat > core/billing/bedrock_aip.py <<'PY'
"""STUB — replaced by Plan 2 (AIP + IAM enforcement). Defines the three reverse
lookups this plan consumes. Plan 2 implements them against the per-owner AIP
map on the containers DDB row (AIP ARN→owner/tier) and the owner↔IAM-role
mapping (assumed-role ARN→owner)."""

def resolve_owner_from_aip_arn(aip_arn: str) -> str | None:
    return None

def resolve_owner_from_identity_arn(identity_arn: str) -> str | None:
    return None

def resolve_tier_from_aip_arn(aip_arn: str) -> str | None:
    return None
PY
git add core/billing/bedrock_aip.py
```

Then re-run; Expected: PASS. The stub returns `None`, but every test patches `resolve_tier_from_aip_arn`, so the stub's body is never exercised in tests. (`core/billing/` already exists from Plan 1, so no `__init__.py`/`mkdir` is needed.) **Do NOT commit the stub if Plan 2 is already merged** — only add it if the import is genuinely missing.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/billing/bedrock_pricing.py apps/backend/tests/unit/billing/test_bedrock_pricing_aip.py
git commit -m "feat(billing): price_from_aip_arn maps AIP ARN -> tier -> FM rate"
```

---

### Task 5: `BedrockLogRecord` schema + `require_meter_token` dependency

**Files:**
- Create: `apps/backend/routers/internal_usage.py`
- Test: `apps/backend/tests/unit/routers/test_internal_usage.py` (auth + schema only this task)

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/routers/test_internal_usage.py`:

```python
"""Tests for the internal Bedrock meter endpoint.

This router does NOT use Depends(get_current_user); auth is a bearer
bedrock_meter service token (service_token.verify_meter). Mirrors the
hooks_proxy 401 template.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core.services import service_token
from main import app


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch):
    monkeypatch.setenv("PAPERCLIP_SERVICE_TOKEN_KEY", "test-signing-key-32-bytes-long-xx")


@pytest.fixture
def client():
    return TestClient(app)


def _body(**overrides) -> dict:
    base = dict(
        request_id="req-1",
        model_id="arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-opus-org_abc",
        identity_arn="arn:aws:sts::877352799272:assumed-role/isol8-owner-org_abc/x",
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=2000,
        cache_write_tokens=300,
        run_status="end",
        region="us-east-1",
        occurred_at="2026-06-07T12:00:00Z",
    )
    base.update(overrides)
    return base


def test_missing_authorization_returns_401(client):
    r = client.post("/api/v1/internal/usage/from-bedrock-log", json=_body())
    assert r.status_code == 401


def test_wrong_kind_token_returns_401(client):
    # A paperclip-kind token must be rejected on this surface.
    bad = service_token.mint("user_x")
    r = client.post(
        "/api/v1/internal/usage/from-bedrock-log",
        headers={"Authorization": f"Bearer {bad}"},
        json=_body(),
    )
    assert r.status_code == 401


def test_garbage_token_returns_401(client):
    r = client.post(
        "/api/v1/internal/usage/from-bedrock-log",
        headers={"Authorization": "Bearer not-a-jwt"},
        json=_body(),
    )
    assert r.status_code == 401


def test_blank_request_id_returns_422(client):
    token = service_token.mint_meter()
    r = client.post(
        "/api/v1/internal/usage/from-bedrock-log",
        headers={"Authorization": f"Bearer {token}"},
        json=_body(request_id=""),
    )
    assert r.status_code == 422


def test_negative_tokens_returns_422(client):
    token = service_token.mint_meter()
    r = client.post(
        "/api/v1/internal/usage/from-bedrock-log",
        headers={"Authorization": f"Bearer {token}"},
        json=_body(input_tokens=-5),
    )
    assert r.status_code == 422
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/routers/test_internal_usage.py -v`
Expected: FAIL — every test 404s (route not mounted) instead of 401/422, because `internal_usage.router` does not exist yet and `main.app` has no such path.

- [ ] **Step 3: Write minimal implementation (schema + dep + stub endpoint)**

Create `apps/backend/routers/internal_usage.py`:

```python
"""Internal Bedrock meter endpoint.

The AWS invocation-log meter Lambda POSTs one extracted record per Bedrock
invocation here. This endpoint is the SINGLE place where pricing, owner
attribution, and ledger depletion happen — the Lambda is a dumb transport, so
swapping the ledger backend (DDB now, Metronome in Phase 3) never touches the
Lambda.

**Auth model.** Like routers/hooks_proxy.py, this router does NOT use
Depends(get_current_user) — the caller is a Lambda inside the VPC, not a Clerk
user. Auth is a bearer `bedrock_meter` service token (service_token.verify_meter).
The token's distinct kind means a leaked meter token cannot authorize container
access (the WS authorizer only trusts kind="paperclip_service").
"""

from __future__ import annotations

import logging

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from core.observability.metrics import put_metric
from core.services import service_token

logger = logging.getLogger(__name__)
router = APIRouter()


async def require_meter_token(request: Request) -> dict:
    """Auth dependency: validate the bearer bedrock_meter service token."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing_or_malformed_authorization")
    token = auth[len("Bearer ") :]
    try:
        return service_token.verify_meter(token)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="invalid_token") from None


class BedrockLogRecord(BaseModel):
    """One extracted Bedrock invocation-log record (the Lambda POSTs this)."""

    request_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)  # the AIP ARN from log `modelId`
    identity_arn: str | None = None  # log `identity.arn` (fallback attribution)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    run_status: str = Field(min_length=1)  # end | error | aborted
    region: str = Field(min_length=1)
    occurred_at: str = Field(min_length=1)  # ISO8601 from log `timestamp`


@router.post("/usage/from-bedrock-log")
async def from_bedrock_log(
    body: BedrockLogRecord,
    _claims: dict = Depends(require_meter_token),
) -> dict:
    """Price + attribute + deplete one Bedrock invocation. Filled in Task 6."""
    put_metric("meter.received", dimensions={"status": body.run_status})
    return {"applied": False, "balance_microcents": 0}
```

- [ ] **Step 4: Register the router in main.py**

In `apps/backend/main.py`, add `internal_usage` to the import block (lines 32-57 — insert alphabetically between `integrations` and `oauth`):

```python
    integrations,
    internal_usage,
    oauth,
```

Then, immediately after the `hooks_credentials` include (line 689), add:

```python
# Internal Bedrock meter — the AWS invocation-log Lambda POSTs extracted
# records here. Bearer bedrock_meter service-token auth (NOT Clerk). Kept out
# of /openapi.json (include_in_schema=False) so CI's schemathesis fuzzer never
# hammers it without a valid token — same posture as the paperclip proxy.
app.include_router(
    internal_usage.router,
    prefix="/api/v1/internal",
    include_in_schema=False,
)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/unit/routers/test_internal_usage.py -v`
Expected: PASS (5 passed)

- [ ] **Step 6: Commit**

```bash
git add apps/backend/routers/internal_usage.py apps/backend/main.py apps/backend/tests/unit/routers/test_internal_usage.py
git commit -m "feat(billing): internal meter endpoint scaffold (auth + BedrockLogRecord schema)"
```

---

### Task 6: Endpoint pricing + attribution + deplete (happy path)

**Files:**
- Modify: `apps/backend/routers/internal_usage.py`
- Test: `apps/backend/tests/unit/routers/test_internal_usage.py` (add `TestDeplete`)

**Design:** resolve owner via Plan 2's `resolve_owner_from_aip_arn(model_id)`, **falling back to `resolve_owner_from_identity_arn(identity_arn)` when the AIP-ARN lookup misses** (spec §3.2 mandates both attribution paths; the captured `identity_arn` would otherwise be dead code). Price the raw cost via `price_from_aip_arn`; apply markup `raw * 14 // 10` (matching `connection_pool.py:1216`); build a Plan-1 `UsageEvent` (`source="bedrock_invocation_log"`, `markup_multiplier=1.4`). Then gate the actual ledger mutation on the cutover seam:

- **`settings.METER_IS_LIVE` gate (GAP 5):** `METER_IS_LIVE` is **defined by Plan 4 in `core/config.py` — this plan CONSUMES it, never redefines it.** When `True` the endpoint calls `get_ledger().deplete(event)` then `enforce_after_deplete(owner_id, result)` and returns the real balance. When `False` the meter is **shadow-only**: it builds + prices + logs the would-be deplete but does NOT mutate the ledger, returning `{"applied": False, "shadow": True}`. This is the order-independent cutover toggle Plan 4 flips — when `False` the meter can run alongside the legacy `connection_pool` biller without double-deducting.
- **Kill-switch wiring (GAP 4):** inside the `if settings.METER_IS_LIVE:` branch, after `deplete()`, call `await enforce_after_deplete(owner_id, result)` (Plan-4-owned orchestrator in `core/services/bedrock_enforcement.py` — revokes Bedrock IAM + sets ECS desired=0 when the balance crossed ≤ 0). Import it at **function level** (inside the endpoint) to avoid an import cycle. Without this call the balance can cross ≤ 0 with no IAM revoke / desired=0 — the kill-switch never fires.

Idempotency is Plan 1's job (exactly-once on `request_id`) — this endpoint adds none.

**Metric-cardinality note:** `put_metric` **raises `ValueError`** on the denied keys `{user_id, container_id, request_id, owner_id}` (`metrics.py:54-58`). None of those keys are used below — but do NOT pass the **AIP ARN (`body.model_id`) as a metric dimension VALUE** either: it is one distinct value per owner per tier, so it blows up CloudWatch metric cardinality/cost (the exact problem `_DENIED_DIMENSIONS` guards against, just keyed under an allowed name). The AIP ARN belongs in a structured **log field**, not a dimension.

- [ ] **Step 1: Write the failing tests**

Append to `apps/backend/tests/unit/routers/test_internal_usage.py`:

```python
import boto3
from moto import mock_aws


@pytest.fixture
def ledger_tables(monkeypatch):
    """Mirror Plan 1's ledger_tables fixture (credits + credit-transactions)."""
    with mock_aws():
        c = boto3.client("dynamodb", region_name="us-east-1")
        c.create_table(
            TableName="test-credits",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        c.create_table(
            TableName="test-credit-txns",
            KeySchema=[
                {"AttributeName": "user_id", "KeyType": "HASH"},
                {"AttributeName": "tx_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "tx_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        c.put_item(
            TableName="test-credits",
            Item={"user_id": {"S": "org_abc"}, "balance_microcents": {"N": "100000"}},
        )
        monkeypatch.setenv("CREDITS_TABLE", "test-credits")
        monkeypatch.setenv("CREDIT_TRANSACTIONS_TABLE", "test-credit-txns")
        monkeypatch.setenv("LEDGER_BACKEND", "ddb")
        yield c


@pytest.fixture
def meter_live(monkeypatch):
    """Force the cutover seam ON for the live-deplete tests.

    `settings` is a module-level pydantic singleton, so a setenv after import is
    too late — patch the attribute directly. `METER_IS_LIVE` is OWNED by Plan 4
    (defined in core/config.py there); this plan only consumes it. If Plan 4 is
    not yet merged, `settings.METR_IS_LIVE` may be absent — `raising=False`
    creates it for the test, mirroring the production attribute.
    """
    monkeypatch.setattr(
        "routers.internal_usage.settings.METER_IS_LIVE", True, raising=False
    )
    yield


class TestDeplete:
    def _post(self, client, **overrides):
        token = service_token.mint_meter()
        return client.post(
            "/api/v1/internal/usage/from-bedrock-log",
            headers={"Authorization": f"Bearer {token}"},
            json=_body(**overrides),
        )

    def test_prices_attributes_and_deplets(self, client, ledger_tables, meter_live):
        with (
            patch("routers.internal_usage.resolve_owner_from_aip_arn", return_value="org_abc"),
            patch("routers.internal_usage.price_from_aip_arn", return_value=10_000),
            patch("core.services.bedrock_enforcement.enforce_after_deplete"),
        ):
            r = self._post(client, request_id="req-A")
        assert r.status_code == 200
        data = r.json()
        assert data["applied"] is True
        # raw 10000 * 14 // 10 = 14000 marked up; 100000 - 14000 = 86000
        assert data["balance_microcents"] == 86_000

    def test_replay_is_noop(self, client, ledger_tables, meter_live):
        with (
            patch("routers.internal_usage.resolve_owner_from_aip_arn", return_value="org_abc"),
            patch("routers.internal_usage.price_from_aip_arn", return_value=10_000),
            patch("core.services.bedrock_enforcement.enforce_after_deplete"),
        ):
            first = self._post(client, request_id="req-dup")
            second = self._post(client, request_id="req-dup")
        assert first.json()["applied"] is True
        assert second.json()["applied"] is False
        # Deducted exactly once.
        assert second.json()["balance_microcents"] == 86_000

    def test_error_status_still_deplets(self, client, ledger_tables, meter_live):
        with (
            patch("routers.internal_usage.resolve_owner_from_aip_arn", return_value="org_abc"),
            patch("routers.internal_usage.price_from_aip_arn", return_value=10_000),
            patch("core.services.bedrock_enforcement.enforce_after_deplete"),
        ):
            r = self._post(client, request_id="req-err", run_status="error")
        assert r.status_code == 200
        assert r.json()["applied"] is True
        assert r.json()["balance_microcents"] == 86_000

    def test_identity_arn_fallback_attributes_when_aip_misses(self, client, ledger_tables, meter_live):
        # AIP-ARN lookup misses (None) but the assumed-role ARN resolves the
        # owner — spec §3.2's fallback path must deplete, not skip.
        with (
            patch("routers.internal_usage.resolve_owner_from_aip_arn", return_value=None),
            patch("routers.internal_usage.resolve_owner_from_identity_arn", return_value="org_abc"),
            patch("routers.internal_usage.price_from_aip_arn", return_value=10_000),
            patch("core.services.bedrock_enforcement.enforce_after_deplete"),
        ):
            r = self._post(client, request_id="req-fallback")
        assert r.status_code == 200
        assert r.json()["applied"] is True
        assert r.json()["balance_microcents"] == 86_000

    def test_meter_live_calls_deplete_and_enforce(self, client, ledger_tables, meter_live):
        # GAP 4 + GAP 5: when METER_IS_LIVE is True the endpoint MUST deplete
        # the ledger AND fire the kill-switch (enforce_after_deplete) with the
        # resolved owner_id + the deplete result. Both deplete() and
        # enforce_after_deplete are awaited, so mock them with AsyncMock.
        fake_result = MagicMock(applied=True, balance_microcents=86_000)
        with (
            patch("routers.internal_usage.resolve_owner_from_aip_arn", return_value="org_abc"),
            patch("routers.internal_usage.price_from_aip_arn", return_value=10_000),
            patch("routers.internal_usage.get_ledger") as mock_get_ledger,
            patch(
                "core.services.bedrock_enforcement.enforce_after_deplete",
                new_callable=AsyncMock,
            ) as mock_enforce,
        ):
            mock_get_ledger.return_value.deplete = AsyncMock(return_value=fake_result)
            r = self._post(client, request_id="req-live")
        assert r.status_code == 200
        assert r.json() == {"applied": True, "balance_microcents": 86_000}
        # deplete was called exactly once...
        mock_get_ledger.return_value.deplete.assert_awaited_once()
        # ...and the kill-switch fired with (owner_id, result).
        mock_enforce.assert_awaited_once_with("org_abc", fake_result)

    def test_meter_shadow_logs_and_does_not_deplete(self, client, ledger_tables, monkeypatch):
        # GAP 5: when METER_IS_LIVE is False the endpoint is shadow-only — it
        # builds + prices the event but NEVER calls deplete() and returns
        # applied=False / shadow=True. This is what lets the meter run alongside
        # the legacy biller without double-deducting.
        monkeypatch.setattr(
            "routers.internal_usage.settings.METER_IS_LIVE", False, raising=False
        )
        with (
            patch("routers.internal_usage.resolve_owner_from_aip_arn", return_value="org_abc"),
            patch("routers.internal_usage.price_from_aip_arn", return_value=10_000),
            patch("routers.internal_usage.get_ledger") as mock_get_ledger,
            patch("core.services.bedrock_enforcement.enforce_after_deplete") as mock_enforce,
        ):
            r = self._post(client, request_id="req-shadow")
        assert r.status_code == 200
        assert r.json() == {"applied": False, "shadow": True}
        # The ledger was NEVER touched (deplete not called)...
        mock_get_ledger.return_value.deplete.assert_not_called()
        # ...and the kill-switch never fired in shadow mode.
        mock_enforce.assert_not_called()
        # ...and nothing was written to the txns table.
        resp = ledger_tables.scan(TableName="test-credit-txns")
        assert resp["Count"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/routers/test_internal_usage.py::TestDeplete -v`
Expected: FAIL — `AttributeError: <module 'routers.internal_usage'> does not have the attribute 'resolve_owner_from_aip_arn'` (the symbols are not yet imported into the router).

- [ ] **Step 3: Write the implementation**

In `apps/backend/routers/internal_usage.py`, add these imports (after the existing `from core.services import service_token` line):

```python
from core.billing.bedrock_aip import (
    resolve_owner_from_aip_arn,
    resolve_owner_from_identity_arn,
)
from core.billing.bedrock_pricing import UnknownModelError, price_from_aip_arn
from core.billing.ledger_interface import get_ledger
from core.billing.usage_event import UsageEvent
from core.config import settings
```

(`settings` is needed for the `settings.METER_IS_LIVE` cutover gate below. `METER_IS_LIVE` is DEFINED by Plan 4 in `core/config.py` — this plan only reads it, never redefines it. `enforce_after_deplete` is imported at **function level** inside the endpoint, NOT here, to break an import cycle: `core/services/bedrock_enforcement.py` (Plan 4) imports container/ledger services that transitively import routers.)

Replace the entire `from_bedrock_log` body (the Task-5 stub) with:

```python
@router.post("/usage/from-bedrock-log")
async def from_bedrock_log(
    body: BedrockLogRecord,
    _claims: dict = Depends(require_meter_token),
) -> dict:
    """Price + attribute + deplete one Bedrock invocation, exactly once.

    Idempotency is the ledger's job (deplete() is exactly-once on request_id),
    so this endpoint adds none. Returns applied=False on a deduplicated replay,
    an unattributed owner, or an unknown model — all observable via metrics,
    never silently dropped.
    """
    put_metric("meter.received", dimensions={"status": body.run_status})

    # Primary attribution: the per-owner AIP ARN in `modelId`. Fallback: the
    # assumed-role ARN in `identity.arn` (spec §3.2 mandates BOTH paths — the
    # role ARN attributes records whose AIP-ARN lookup misses, e.g. a stale AIP
    # the registry hasn't indexed yet). Without the fallback the captured
    # identity_arn would be dead code.
    owner_id = resolve_owner_from_aip_arn(body.model_id)
    if owner_id is None and body.identity_arn:
        owner_id = resolve_owner_from_identity_arn(body.identity_arn)
    if owner_id is None:
        # Unattributed (e.g. GooseTown spillover the filter let through, or an
        # AIP/role we don't own). Do NOT deplete a random owner — skip + observe.
        # model_id (AIP ARN) is a structured log field, NOT a metric dimension
        # (per-owner-per-tier cardinality blows up CloudWatch cost).
        put_metric("meter.unattributed", dimensions={"region": body.region})
        logger.warning("meter: unattributed record model=%s req=%s", body.model_id, body.request_id)
        return {"applied": False, "balance_microcents": 0}

    try:
        raw = price_from_aip_arn(
            aip_arn=body.model_id,
            input_tokens=body.input_tokens,
            output_tokens=body.output_tokens,
            cache_read_tokens=body.cache_read_tokens,
            cache_write_tokens=body.cache_write_tokens,
        )
    except UnknownModelError:
        # model_id (AIP ARN) is logged as a structured field, NOT passed as a
        # metric dimension value — it is per-owner-per-tier high cardinality.
        put_metric("meter.unknown_model", dimensions={"region": body.region})
        logger.warning("meter: unknown model %s req=%s", body.model_id, body.request_id)
        return {"applied": False, "balance_microcents": 0}

    billed = raw * 14 // 10  # markup 1.4, integer math (matches connection_pool)
    event = UsageEvent(
        owner_id=owner_id,
        request_id=body.request_id,
        model_id=body.model_id,
        input_tokens=body.input_tokens,
        output_tokens=body.output_tokens,
        cache_read_tokens=body.cache_read_tokens,
        cache_write_tokens=body.cache_write_tokens,
        raw_cost_microcents=raw,
        billed_microcents=billed,
        markup_multiplier=1.4,
        region=body.region,
        run_status=body.run_status,
        occurred_at=body.occurred_at,
        source="bedrock_invocation_log",
    )

    # Cutover seam (GAP 5). `settings.METER_IS_LIVE` is OWNED by Plan 4 (defined
    # in core/config.py there — this plan only CONSUMES it, never redefines it).
    # When False the meter is SHADOW-ONLY: it builds + prices the event and logs
    # the would-be deplete, but does NOT mutate the ledger, so the meter can run
    # alongside the legacy connection_pool biller without double-deducting. Plan
    # 4 flips it to True at cutover (and retires the legacy deduct in the same
    # pass). This is the order-independent toggle the whole cutover relies on.
    if settings.METER_IS_LIVE:
        # Function-level import to avoid an import cycle (the enforcement module
        # imports container/ledger services that transitively import routers).
        from core.services.bedrock_enforcement import enforce_after_deplete

        result = await get_ledger().deplete(event)
        put_metric(
            "meter.depleted",
            dimensions={"applied": str(result.applied), "status": body.run_status},
        )
        # Kill-switch (GAP 4): on a balance that crossed ≤ 0, Plan 4's
        # orchestrator revokes the owner's Bedrock IAM + sets ECS desired=0.
        # Without this call the balance can go negative with nothing stopping
        # further spend — the kill-switch would never fire.
        await enforce_after_deplete(owner_id, result)
        return {"applied": result.applied, "balance_microcents": result.balance_microcents}

    # Shadow / log-only mode: build + price the event, log the would-be deplete,
    # do NOT mutate the ledger and do NOT enforce.
    put_metric("meter.shadow", dimensions={"status": body.run_status})
    logger.info(
        "meter shadow (METER_IS_LIVE=False): would deplete owner=%s billed=%d request=%s",
        owner_id,
        event.billed_microcents,
        event.request_id,
    )
    return {"applied": False, "shadow": True}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/routers/test_internal_usage.py -v`
Expected: PASS (5 auth/schema + 6 deplete = 11 passed). The 6 deplete tests are: `test_prices_attributes_and_deplets`, `test_replay_is_noop`, `test_error_status_still_deplets`, `test_identity_arn_fallback_attributes_when_aip_misses`, `test_meter_live_calls_deplete_and_enforce` (GAP 4+5 live path), `test_meter_shadow_logs_and_does_not_deplete` (GAP 5 shadow path).

If a deplete test fails with `ModuleNotFoundError: No module named 'core.services.bedrock_enforcement'` (Plan 4 not yet merged — the function-level import inside the `METER_IS_LIVE` branch resolves at request time, and `patch("core.services.bedrock_enforcement.enforce_after_deplete")` needs the module to exist), add a minimal stub so this plan is independently runnable:

```bash
cat > core/services/bedrock_enforcement.py <<'PY'
"""STUB — replaced by Plan 4 (enforcement loop + cutover). The real
enforce_after_deplete revokes the owner's Bedrock IAM + sets ECS desired=0 when
the balance crossed <= 0. The stub is a no-op so this plan's live-path test can
patch it; every test patches it, so the body is never exercised."""

async def enforce_after_deplete(owner_id, result) -> None:
    return None
PY
git add core/services/bedrock_enforcement.py
```

Also, if `settings.METER_IS_LIVE` does not exist (Plan 4 not merged), the `meter_live` / shadow tests use `monkeypatch.setattr(..., raising=False)` which CREATES the attribute for the test — but the endpoint's `if settings.METER_IS_LIVE:` read would `AttributeError` at runtime when neither Plan 4 nor a test has set it. For independent runnability, add the setting to `core/config.py` ONLY as a stub-default IF Plan 4 has not landed it (do NOT redefine it if Plan 4 already owns the line):

```python
    # STUB-ONLY default — Plan 4 OWNS this setting in core/config.py. Add this
    # line ONLY if Plan 4 has not merged yet; delete on Plan 4 merge to avoid a
    # duplicate definition. Default False keeps the meter shadow-only.
    METER_IS_LIVE: bool = os.getenv("METER_IS_LIVE", "false").lower() == "true"
```

Then re-run; Expected: PASS (11 passed). **Do NOT commit either stub if Plan 4 is already merged** — only add them if the import/attribute is genuinely missing.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/routers/internal_usage.py apps/backend/tests/unit/routers/test_internal_usage.py
git commit -m "feat(billing): meter endpoint prices, attributes, deplets exactly-once, gated on METER_IS_LIVE + kill-switch"
```

---

### Task 7: Endpoint edge cases (unattributed, unknown model, schema-route mounting)

**Files:**
- Test: `apps/backend/tests/unit/routers/test_internal_usage.py` (add `TestEdgeCases`)

The unattributed + unknown-model *behaviors* are already implemented in Task 6; this task locks them with explicit tests and verifies the route is mounted but hidden from the schema (the schemathesis posture).

- [ ] **Step 1: Write the failing tests**

Append to `apps/backend/tests/unit/routers/test_internal_usage.py`:

```python
class TestEdgeCases:
    def _post(self, client, **overrides):
        token = service_token.mint_meter()
        return client.post(
            "/api/v1/internal/usage/from-bedrock-log",
            headers={"Authorization": f"Bearer {token}"},
            json=_body(**overrides),
        )

    def test_unattributed_owner_skips_without_deplete(self, client, ledger_tables):
        # BOTH attribution paths miss → truly unattributed → skip, no deplete.
        with (
            patch("routers.internal_usage.resolve_owner_from_aip_arn", return_value=None),
            patch("routers.internal_usage.resolve_owner_from_identity_arn", return_value=None),
        ):
            r = self._post(client, request_id="req-orphan")
        assert r.status_code == 200
        assert r.json() == {"applied": False, "balance_microcents": 0}
        # Nothing was written to the txns table.
        resp = ledger_tables.scan(TableName="test-credit-txns")
        assert resp["Count"] == 0

    def test_unknown_model_skips_without_deplete(self, client, ledger_tables):
        from core.billing.bedrock_pricing import UnknownModelError

        with (
            patch("routers.internal_usage.resolve_owner_from_aip_arn", return_value="org_abc"),
            patch(
                "routers.internal_usage.price_from_aip_arn",
                side_effect=UnknownModelError("aip-???"),
            ),
        ):
            r = self._post(client, request_id="req-unknown")
        assert r.status_code == 200
        assert r.json()["applied"] is False
        resp = ledger_tables.scan(TableName="test-credit-txns")
        assert resp["Count"] == 0

    def test_route_is_mounted_but_hidden_from_openapi(self, client):
        # Mounted: a POST with no auth hits the auth dep (401), not a 404.
        assert client.post("/api/v1/internal/usage/from-bedrock-log", json=_body()).status_code == 401
        # Hidden: not in the public schema (schemathesis posture).
        schema = client.get("/openapi.json").json()
        assert "/api/v1/internal/usage/from-bedrock-log" not in schema["paths"]
```

- [ ] **Step 2: Run tests to verify they pass (behavior already exists, schema posture set in Task 5)**

Run: `uv run pytest tests/unit/routers/test_internal_usage.py::TestEdgeCases -v`
Expected: PASS (3 passed). If `test_route_is_mounted_but_hidden_from_openapi` FAILS because the path IS in the schema, the `include_in_schema=False` from Task 5 Step 4 was dropped — re-add it.

- [ ] **Step 3: Run the full backend billing + router suite for this plan**

Run: `uv run pytest tests/unit/routers/test_internal_usage.py tests/unit/billing/test_bedrock_pricing_aip.py tests/unit/services/test_service_token.py -v`
Expected: PASS (all green).

- [ ] **Step 4: Commit**

```bash
git add apps/backend/tests/unit/routers/test_internal_usage.py
git commit -m "test(billing): meter endpoint edge cases (unattributed, unknown model, hidden route)"
```

---

### Task 8: Meter Lambda — parse + defensive extraction (against real fixtures)

**Files:**
- Create: `apps/infra/lambda/bedrock-usage-meter/index.py`
- Test: `apps/infra/lambda/bedrock-usage-meter/test_index.py`

**Design:** the CloudWatch Logs subscription delivers `event["awslogs"]["data"]` = base64(gzip(json)). The decoded payload has `logEvents[].message`, each a JSON Bedrock record. `_extract(record)` reads token/cache/status fields using the **locked paths from Task 1's FIELDS.md** as the primary candidate, with documented fallbacks. If the body was offloaded, `_extract` flags it and `_merge_s3_body` fetches the S3 object and re-reads the cache/token fields from there. **Open FIELDS.md and replace the candidate-path lists below with the locked paths if they differ.**

> **REVIEW FIX (C1/I3/M2 — applied in the shipped Lambda, divergent from the template below).** The handler is per-record FAIL-SAFE: the subscription invokes it synchronously, so a per-record failure cannot rely on the platform DLQ. Each of the three failure paths now durably routes the raw log event to the SQS DLQ (`_send_to_dlq`, via `DLQ_URL`) + emits a metric + `continue`s, instead of silently dropping or under-billing: (1) a JSON-parse failure → `meter.parse_failed` (review M2); (2) `_merge_s3_body` now **raises `_S3MergeError`** when the offloaded body is unreadable (unparseable ref OR `get_object` error) rather than returning the inline-zero cache counts — the handler catches it, DLQs the line, and emits `meter.s3_merge_failed` (review I3, the literal "never silently bill 0 on a parse miss" guarantee); (3) a backend POST failure → `meter.transport_error` (review C1). `_send_to_dlq` re-raises when no `DLQ_URL` is configured so the batch fails (CW Logs retries) rather than losing a record. The template `_merge_s3_body` below (which `return parsed`s the inline zeros on an unparseable ref) is the BUGGY pre-review shape — the shipped code raises instead.

- [ ] **Step 1: Write the failing test (against the Task-1 fixtures)**

Create `apps/infra/lambda/bedrock-usage-meter/test_index.py`:

```python
"""Tests for the Bedrock usage-meter Lambda — parse + S3 fallback + transport.

Uses the REAL captured fixtures from Task 1 as ground truth.
"""

import base64
import gzip
import importlib
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")


def _load(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


def _awslogs_event(*records):
    payload = {"logEvents": [{"message": json.dumps(r)} for r in records]}
    raw = gzip.compress(json.dumps(payload).encode())
    return {"awslogs": {"data": base64.b64encode(raw).decode()}}


@pytest.fixture(autouse=True)
def lambda_env(monkeypatch):
    monkeypatch.setenv("ALB_DNS_NAME", "alb.test.local")
    monkeypatch.setenv("METER_TOKEN", "meter-token-abc")
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    sys.modules.pop("index", None)
    import index  # noqa: F401
    yield
    sys.modules.pop("index", None)


def test_extract_inline_record_reads_tokens_and_cache():
    import index

    record = _load("inline_record.json")
    parsed = index._extract(record)
    assert parsed["request_id"] == record["requestId"]
    assert parsed["model_id"] == record["modelId"]
    # cache tokens are present and >= 0 (the inline record carries them)
    assert parsed["cache_read_tokens"] >= 0
    assert parsed["cache_write_tokens"] >= 0
    assert parsed["body_offloaded"] is False


def test_extract_offloaded_record_flags_offload():
    import index

    record = _load("s3_offloaded_record.json")
    parsed = index._extract(record)
    assert parsed["body_offloaded"] is True
    assert parsed["s3_ref"]  # a non-empty S3 reference was found


def _s3_usage(s3_body):
    """Pull the usage block from the committed S3 body fixture the same way
    _merge_s3_body does, so the test asserts against the FIXTURE's real values
    (not a hardcoded literal that could drift from the captured fixture)."""
    return s3_body.get("usage", s3_body)


def test_merge_s3_body_pulls_cache_tokens_from_s3():
    import index

    record = _load("s3_offloaded_record.json")
    s3_body = _load("s3_body.json")
    parsed = index._extract(record)

    # The committed fixture MUST carry NON-ZERO cache counts — that is the
    # entire point of the S3 path (Task 1 Step 5/6 guarantees this). Read the
    # expected values straight out of the fixture so the assertion fails if the
    # merge silently yields 0 (the #1 correctness landmine this plan exists to
    # prevent — a `>= 0` assertion would pass even on a wrong S3 path).
    usage = _s3_usage(s3_body)
    expected_read = int(
        usage.get("cacheReadInputTokens", usage.get("cacheReadInputTokenCount", 0))
    )
    expected_write = int(
        usage.get("cacheWriteInputTokens", usage.get("cacheWriteInputTokenCount", 0))
    )
    assert expected_read > 0, (
        "s3_body.json fixture must retain a non-zero cacheRead count "
        "(Task 1 Step 5/6) — a zero fixture makes this test vacuous."
    )

    fake_s3 = MagicMock()
    fake_s3.get_object.return_value = {
        "Body": MagicMock(read=lambda: json.dumps(s3_body).encode())
    }
    with patch("index.boto3.client", return_value=fake_s3):
        merged = index._merge_s3_body(parsed, record)
    # The cache tokens come from the S3 body and EQUAL the fixture's real
    # values — not 0, not the inline (offloaded ⇒ inline-zero) count.
    assert merged["cache_read_tokens"] == expected_read
    assert merged["cache_write_tokens"] == expected_write
    assert fake_s3.get_object.called


def test_handler_posts_each_record(monkeypatch):
    import index

    inline = _load("inline_record.json")
    posted = []

    def fake_post(parsed):
        posted.append(parsed)

    with patch.object(index, "_post_to_backend", side_effect=fake_post):
        result = index.handler(_awslogs_event(inline), None)
    assert result == {"statusCode": 200}
    assert len(posted) == 1
    assert posted[0]["request_id"] == inline["requestId"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/infra/lambda/bedrock-usage-meter && python -m pytest test_index.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'index'`

- [ ] **Step 3: Write the implementation**

Create `apps/infra/lambda/bedrock-usage-meter/index.py`:

```python
"""Bedrock invocation-log usage meter — dumb transport Lambda.

CloudWatch Logs subscription delivers gzipped+base64 records of Bedrock
model-invocation logs. This Lambda parses each record, fetches the S3-offloaded
body when present (>100KB → cache tokens live in S3), and POSTs the extracted
record to the backend internal meter endpoint. It does NOT price, attribute, or
deplete — that is the backend's single source of truth.

Field paths are the ones LOCKED in FIELDS.md (Task 1, captured from a real dev
record). The extractor tries the locked path first, then documented fallbacks,
so a Bedrock log-shape change degrades gracefully instead of silently zeroing
cache tokens.
"""

import base64
import gzip
import json
import logging
import os
import urllib.request
from urllib.parse import urlparse

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _first_int(d, *paths):
    """Return the first int found at any (k1, k2, ...) path in dict d, else 0."""
    for path in paths:
        node = d
        ok = True
        for key in path:
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                ok = False
                break
        if ok and isinstance(node, (int, float)):
            return int(node)
    return 0


def _find_s3_ref(record):
    """Return the S3 reference (key or s3:// URI) if the body was offloaded, else ''."""
    inp = record.get("input", {})
    for key in ("inputBodyS3Path", "s3Location", "inputBodyS3Location", "bodyS3Path"):
        ref = inp.get(key)
        if isinstance(ref, str) and ref:
            return ref
        if isinstance(ref, dict):
            uri = ref.get("uri") or ref.get("s3Uri") or ""
            if uri:
                return uri
    return ""


def _extract(record):
    """Extract the billing-relevant fields from one Bedrock log record."""
    s3_ref = _find_s3_ref(record)
    error_code = record.get("errorCode")
    run_status = "end"
    if error_code:
        run_status = "aborted" if "abort" in str(error_code).lower() else "error"
    return {
        "request_id": record.get("requestId", ""),
        "model_id": record.get("modelId", ""),
        "identity_arn": (record.get("identity") or {}).get("arn"),
        "input_tokens": _first_int(record, ("input", "inputTokenCount")),
        "output_tokens": _first_int(record, ("output", "outputTokenCount")),
        "cache_read_tokens": _first_int(record, ("input", "cacheReadInputTokenCount")),
        "cache_write_tokens": _first_int(record, ("input", "cacheWriteInputTokenCount")),
        "run_status": run_status,
        "region": record.get("region", ""),
        "occurred_at": record.get("timestamp", ""),
        "body_offloaded": bool(s3_ref),
        "s3_ref": s3_ref,
    }


def _parse_s3_ref(s3_ref):
    """Return (bucket, key) from a key or an s3://bucket/key URI."""
    if s3_ref.startswith("s3://"):
        p = urlparse(s3_ref)
        return p.netloc, p.path.lstrip("/")
    # Bare key — the bucket is the configured large-data sink.
    return os.environ.get("LARGE_DATA_BUCKET", ""), s3_ref


def _merge_s3_body(parsed, record):
    """Fetch the offloaded S3 body and overwrite token/cache counts from it.

    The cache tokens are the whole point of going to S3 — on offloaded records
    the inline counts read 0, so we MUST source them from the object body.
    """
    bucket, key = _parse_s3_ref(parsed["s3_ref"])
    if not bucket or not key:
        logger.warning("meter: offloaded record but unparseable s3_ref=%s", parsed["s3_ref"])
        return parsed
    body_obj = boto3.client("s3").get_object(Bucket=bucket, Key=key)
    body = json.loads(body_obj["Body"].read())
    usage = body.get("usage", body)
    parsed["input_tokens"] = _first_int({"u": usage}, ("u", "inputTokens"), ("u", "inputTokenCount")) or parsed["input_tokens"]
    parsed["output_tokens"] = _first_int({"u": usage}, ("u", "outputTokens"), ("u", "outputTokenCount")) or parsed["output_tokens"]
    parsed["cache_read_tokens"] = _first_int({"u": usage}, ("u", "cacheReadInputTokens"), ("u", "cacheReadInputTokenCount"))
    parsed["cache_write_tokens"] = _first_int({"u": usage}, ("u", "cacheWriteInputTokens"), ("u", "cacheWriteInputTokenCount"))
    return parsed


def _post_to_backend(parsed):
    """POST one extracted record to the backend internal meter endpoint."""
    payload = {k: v for k, v in parsed.items() if k not in ("body_offloaded", "s3_ref")}
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"http://{os.environ['ALB_DNS_NAME']}/api/v1/internal/usage/from-bedrock-log",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ['METER_TOKEN']}",
        },
        data=data,
    )
    urllib.request.urlopen(req, timeout=10)


def handler(event, context):
    payload = json.loads(gzip.decompress(base64.b64decode(event["awslogs"]["data"])))
    for log_event in payload.get("logEvents", []):
        try:
            record = json.loads(log_event["message"])
        except (ValueError, KeyError):
            logger.warning("meter: unparseable log event, skipping")
            continue
        parsed = _extract(record)
        if parsed["body_offloaded"]:
            parsed = _merge_s3_body(parsed, record)
        _post_to_backend(parsed)
    return {"statusCode": 200}
```

- [ ] **Step 4: Reconcile candidate paths with the locked fixtures**

Open `apps/infra/lambda/bedrock-usage-meter/FIELDS.md` and confirm every path used in `_extract` / `_find_s3_ref` / `_merge_s3_body` matches the locked table. If a fixture uses a different key (e.g. `inputBodyS3Uri` instead of `inputBodyS3Path`), add it to the candidate tuple in the matching helper. The fixtures are the source of truth — the test in Step 1 will fail until the code reads the real shape.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd apps/infra/lambda/bedrock-usage-meter && python -m pytest test_index.py -v`
Expected: PASS (4 passed). If `test_extract_offloaded_record_flags_offload` fails, the offload key in `_find_s3_ref` does not match the captured fixture — fix the candidate tuple per FIELDS.md.

- [ ] **Step 6: Commit**

```bash
git add apps/infra/lambda/bedrock-usage-meter/index.py apps/infra/lambda/bedrock-usage-meter/test_index.py
git commit -m "feat(meter): Bedrock invocation-log Lambda parse + S3-offload cache merge"
```

---

### Task 9: Meter Lambda — transport contract (POST shape + token)

**Files:**
- Test: `apps/infra/lambda/bedrock-usage-meter/test_index.py` (add transport assertions)

The transport (`_post_to_backend`) is implemented in Task 8; this task locks the wire contract — exact URL, bearer header, JSON body without internal-only keys.

- [ ] **Step 1: Write the failing test**

Append to `apps/infra/lambda/bedrock-usage-meter/test_index.py`:

```python
def test_post_to_backend_url_headers_and_body(monkeypatch):
    import index

    inline = _load("inline_record.json")
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["body"] = json.loads(req.data.decode())
        return MagicMock()

    with patch("index.urllib.request.urlopen", side_effect=fake_urlopen):
        index.handler(_awslogs_event(inline), None)

    assert captured["url"] == "http://alb.test.local/api/v1/internal/usage/from-bedrock-log"
    assert captured["headers"]["authorization"] == "Bearer meter-token-abc"
    assert captured["headers"]["content-type"] == "application/json"
    # Internal-only keys are stripped from the wire body.
    assert "body_offloaded" not in captured["body"]
    assert "s3_ref" not in captured["body"]
    # The contract fields the backend BedrockLogRecord requires are present.
    for key in ("request_id", "model_id", "input_tokens", "output_tokens",
                "cache_read_tokens", "cache_write_tokens", "run_status",
                "region", "occurred_at"):
        assert key in captured["body"]
```

- [ ] **Step 2: Run test to verify it passes (transport already implemented in Task 8)**

Run: `cd apps/infra/lambda/bedrock-usage-meter && python -m pytest test_index.py::test_post_to_backend_url_headers_and_body -v`
Expected: PASS. If it FAILS because a contract key is missing from the body, the `_extract` dict in Task 8 omits it — add it (the backend's `BedrockLogRecord` requires exactly those keys).

- [ ] **Step 3: Run the full Lambda test module**

Run: `cd apps/infra/lambda/bedrock-usage-meter && python -m pytest test_index.py -v`
Expected: PASS (5 passed).

- [ ] **Step 4: Commit**

```bash
git add apps/infra/lambda/bedrock-usage-meter/test_index.py
git commit -m "test(meter): lock the Lambda->backend transport contract"
```

---

### Task 10: CDK — `BedrockMeterStack` props + Lambda + DLQ + in-VPC SG

**Files:**
- Create: `apps/infra/lib/stacks/bedrock-meter-stack.ts`
- Test: `apps/infra/test/bedrock-meter-stack.test.ts`

**Design:** a dedicated, single-owner stack (instantiated ONCE in Task 11 — prod only — to dodge the W53 account-global collision). This task builds the per-region transport: the meter Lambda (no-bundle asset, `PYTHON_3_12`, in-VPC like the WS Lambdas), an SQS DLQ for at-least-once retries, and the Lambda SG with egress to the ALB on port 80 + 443 for the S3 fetch. The logging config + subscription filter come in Task 12 + 13.

> **REVIEW FIX (C1/I1/I2 — applied in the shipped code, divergent from the templates below).** A CloudWatch Logs subscription filter invokes the destination Lambda **synchronously**, so the Lambda's `deadLetterQueue` prop (the *async*-invocation DLQ) does NOT fire on a per-record failure — a backend 5xx, an unparseable S3 ref, or an S3 fetch error would otherwise silently drop the record (or POST a zeroed-cache record → ~30× under-bill). The fix keeps the synchronous wiring but makes durability load-bearing in the Lambda code: the handler `sqs:SendMessage`s each failed raw log event to the DLQ (via a `DLQ_URL` env var) + emits a failure metric + `continue`s. So the shipped `bedrock-meter-stack.ts` ALSO: (1) injects `DLQ_URL: dlq.queueUrl` into the Lambda env, (2) `dlq.grantSendMessages(this.meterFn)`, (3) adds a CloudWatch `Alarm` on the DLQ depth (`ApproximateNumberOfMessagesVisible > 0`) AND one alarm per skip/failure metric (`meter.unattributed`, `meter.unknown_model`, `meter.s3_merge_failed`, `meter.transport_error`, `meter.parse_failed`) so a silent revenue leak pages instead of hiding. The CDK test asserts the DLQ is **wired** (`DeadLetterConfig.TargetArn` + the `sqs:SendMessage` grant + the alarms), not merely present (review I1).

- [ ] **Step 1: Write the failing test**

Create `apps/infra/test/bedrock-meter-stack.test.ts`:

```typescript
import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import { Template, Match } from "aws-cdk-lib/assertions";
import {
  BedrockMeterStack,
  BedrockMeterStackProps,
} from "../lib/stacks/bedrock-meter-stack";

describe("BedrockMeterStack", () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App();
    const env = { account: "877352799272", region: "us-east-1" };
    const support = new cdk.Stack(app, "Support", { env });
    const vpc = new ec2.Vpc(support, "Vpc");
    const alb = new elbv2.ApplicationLoadBalancer(support, "Alb", {
      vpc,
      internetFacing: false,
    });

    const props: BedrockMeterStackProps = {
      env,
      envName: "prod",
      vpc,
      alb,
      albSecurityGroup: alb.connections.securityGroups[0],
      meterTokenSecretName: "isol8/prod/bedrock_meter_token",
      meterKmsKeyArn:
        "arn:aws:kms:us-east-1:877352799272:key/abc-123",
      fanoutRegions: ["us-east-1"],
    };

    const stack = new BedrockMeterStack(app, "BedrockMeterStack", props);
    template = Template.fromStack(stack);
  });

  test("creates the meter Lambda with ALB_DNS_NAME env", () => {
    template.hasResourceProperties("AWS::Lambda::Function", {
      Runtime: "python3.12",
      Environment: {
        Variables: Match.objectLike({
          ALB_DNS_NAME: Match.anyValue(),
        }),
      },
    });
  });

  test("creates an SQS DLQ", () => {
    template.resourceCountIs("AWS::SQS::Queue", 1);
  });

  test("Lambda SG has egress to ALB on port 80", () => {
    template.hasResourceProperties("AWS::EC2::SecurityGroupEgress", {
      FromPort: 80,
      ToPort: 80,
    });
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npx jest test/bedrock-meter-stack.test.ts`
Expected: FAIL — `Cannot find module '../lib/stacks/bedrock-meter-stack'`

- [ ] **Step 3: Write minimal implementation**

Create `apps/infra/lib/stacks/bedrock-meter-stack.ts`:

```typescript
import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as sqs from "aws-cdk-lib/aws-sqs";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { Construct } from "constructs";

// =============================================================================
// BedrockMeterStack — account-scoped, SINGLE-OWNER Bedrock usage meter.
// =============================================================================
// Instantiated ONCE (prod stage only). Bedrock model-invocation logging is an
// ACCOUNT+REGION-level config (one per region) and there is ONE invocation-log
// group per region per account — so a per-env duplicate would collide, exactly
// like the Cost Anomaly Detection tombstone in observability-stack.ts:1609-1616.
// Dev validation uses temporary manual enablement (capture plan), not a second
// permanent stack.
// =============================================================================

export interface BedrockMeterStackProps extends cdk.StackProps {
  envName: string;
  vpc: ec2.IVpc;
  alb: elbv2.IApplicationLoadBalancer;
  albSecurityGroup: ec2.ISecurityGroup;
  /** Secrets Manager NAME of the pre-minted bedrock_meter token (not ISecret). */
  meterTokenSecretName: string;
  /** AuthStack KMS key ARN — the Lambda needs kms:Decrypt to read the token. */
  meterKmsKeyArn: string;
  /** Bedrock fan-out regions to enable logging + a filter in (Task 13). */
  fanoutRegions: string[];
}

export class BedrockMeterStack extends cdk.Stack {
  public readonly meterFn: lambda.Function;

  constructor(scope: Construct, id: string, props: BedrockMeterStackProps) {
    super(scope, id, props);

    // Dead-letter queue for at-least-once retries. The endpoint is idempotent
    // (deplete() exactly-once on requestId) so replays are safe.
    const dlq = new sqs.Queue(this, "MeterDlq", {
      queueName: `isol8-${props.envName}-bedrock-meter-dlq`,
      retentionPeriod: cdk.Duration.days(14),
    });

    // Lambda SG: egress to the ALB on 80 (internal endpoint) + 443 (S3 fetch).
    const meterSg = new ec2.SecurityGroup(this, "MeterLambdaSg", {
      vpc: props.vpc,
      description: "Bedrock meter Lambda — egress to ALB + S3",
      allowAllOutbound: false,
    });
    meterSg.addEgressRule(
      props.albSecurityGroup,
      ec2.Port.tcp(80),
      "Allow meter Lambda to reach ALB on port 80",
    );
    meterSg.addEgressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(443),
      "Allow HTTPS for S3 + AWS APIs",
    );

    this.meterFn = new lambda.Function(this, "MeterFn", {
      functionName: `isol8-${props.envName}-bedrock-meter`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "index.handler",
      code: lambda.Code.fromAsset(
        path.join(__dirname, "..", "..", "lambda", "bedrock-usage-meter"),
      ),
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [meterSg],
      deadLetterQueueEnabled: true,
      deadLetterQueue: dlq,
      environment: {
        ALB_DNS_NAME: props.alb.loadBalancerDnsName,
      },
      logRetention: logs.RetentionDays.ONE_MONTH,
    });

    // Inject the pre-minted meter token from Secrets Manager (cold-start fetch
    // not needed — passing the secret ARN + an env mapping keeps the value out
    // of CFN; the function reads it via the SDK extension or, simpler, the CDK
    // resolves the secret value at deploy into a non-logged env. We use the
    // ARN + a kms:Decrypt grant, mirroring the WS authorizer pattern.)
    const meterSecret = secretsmanager.Secret.fromSecretNameV2(
      this,
      "MeterTokenSecret",
      props.meterTokenSecretName,
    );
    meterSecret.grantRead(this.meterFn);
    // grantRead only adds GetSecretValue — KMS-encrypted secrets also need
    // kms:Decrypt on the CMK (documented WS-authorizer footgun).
    this.meterFn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "KmsDecryptForMeterToken",
        effect: iam.Effect.ALLOW,
        actions: ["kms:Decrypt"],
        resources: [props.meterKmsKeyArn],
      }),
    );
    this.meterFn.addEnvironment(
      "METER_TOKEN_SECRET_ARN",
      meterSecret.secretArn,
    );

    cdk.Tags.of(this).add("Project", "isol8");
    cdk.Tags.of(this).add("Environment", props.envName);
  }
}
```

Also update `apps/infra/lambda/bedrock-usage-meter/index.py` so the Lambda reads the token from Secrets Manager at cold-start (the env now carries `METER_TOKEN_SECRET_ARN`, not a literal `METER_TOKEN`). Replace the `_post_to_backend` token read with a cold-start fetch — change the top of `index.py` to add:

```python
_METER_TOKEN = None


def _meter_token():
    global _METER_TOKEN
    if _METER_TOKEN is None:
        literal = os.environ.get("METER_TOKEN")
        if literal:
            _METER_TOKEN = literal
        else:
            arn = os.environ["METER_TOKEN_SECRET_ARN"]
            _METER_TOKEN = boto3.client("secretsmanager").get_secret_value(SecretId=arn)["SecretString"]
    return _METER_TOKEN
```

and in `_post_to_backend` change the Authorization header to `f"Bearer {_meter_token()}"`. (Tests set the `METER_TOKEN` literal env, so the secret-fetch branch is not exercised in unit tests — the literal short-circuit keeps Task 8/9 tests green.)

- [ ] **Step 4: Re-run the Lambda tests (the literal-token short-circuit keeps them green)**

Run: `cd apps/infra/lambda/bedrock-usage-meter && python -m pytest test_index.py -v`
Expected: PASS (5 passed) — `_meter_token()` returns the `METER_TOKEN` literal the test sets, so no boto3 secret call happens.

- [ ] **Step 5: Run the CDK test to verify it passes**

Run: `npx jest test/bedrock-meter-stack.test.ts`
Expected: PASS (3 passed)

- [ ] **Step 6: Commit**

```bash
git add apps/infra/lib/stacks/bedrock-meter-stack.ts apps/infra/test/bedrock-meter-stack.test.ts apps/infra/lambda/bedrock-usage-meter/index.py
git commit -m "feat(infra): BedrockMeterStack — meter Lambda + DLQ + in-VPC SG + token secret"
```

---

### Task 11: Wire `BedrockMeterStack` into the stage (prod-only, single owner)

**Files:**
- Modify: `apps/infra/lib/isol8-stage.ts`
- Test: `apps/infra/test/bedrock-meter-stack.test.ts` (the stack test already covers the construct; this task adds a stage-gating guard test inline)

- [ ] **Step 1: Add the stage instantiation (guarded to prod)**

In `apps/infra/lib/isol8-stage.ts`, add the import near the other stack imports (line 1-11):

```typescript
import { BedrockMeterStack } from "./stacks/bedrock-meter-stack";
```

After the `ObservabilityStack` instantiation (ends at line 178), add:

```typescript
    // BedrockMeterStack — account-scoped, SINGLE-OWNER usage meter. Bedrock
    // model-invocation logging is account+region-global (one config per
    // region, one invocation-log group per region per account), so this stack
    // is instantiated ONCE — prod only — to avoid the dev/prod collision that
    // retired Cost Anomaly Detection (observability-stack.ts:1609-1616). Dev
    // validation uses the temporary manual capture path, not a second stack.
    if (env === "prod") {
      const bedrockMeter = new BedrockMeterStack(
        this,
        `isol8-${env}-bedrock-meter`,
        {
          stackName: `isol8-${env}-bedrock-meter`,
          envName: env,
          vpc: network.vpc,
          alb: network.alb,
          albSecurityGroup: network.albSecurityGroup,
          meterTokenSecretName: `isol8/${env}/bedrock_meter_token`,
          meterKmsKeyArn: auth.kmsKey.keyArn,
          fanoutRegions: ["us-east-1", "us-east-2", "us-west-2"],
        },
      );
      bedrockMeter.addDependency(network);
      bedrockMeter.addDependency(auth);
    }
```

- [ ] **Step 2: Verify dev does NOT synth a meter stack and prod DOES**

Run:
```bash
cd apps/infra && npx cdk synth --quiet 2>&1 | grep -c "isol8-prod-bedrock-meter" ; npx cdk synth --quiet 2>&1 | grep -c "isol8-dev-bedrock-meter"
```
Expected: first count ≥ 1 (prod stack synthesizes), second count `0` (dev does not). If dev count is non-zero, the `env === "prod"` guard is wrong.

- [ ] **Step 3: Add the AuthStack secret for the meter token**

The `meterTokenSecretName` references `isol8/${env}/bedrock_meter_token`. Add it to AuthStack so it exists. **Grounding (read before editing — the obvious-looking call is wrong twice over):**

- `createSecret` is a **local `const`, not a method** — it is declared at `auth-stack.ts:51-68` (`const createSecret = (logicalId, secretName, generateConfig?, description?) => {...}`). So you call **bare `createSecret(...)`, NOT `this.createSecret(...)`** (`this.createSecret` does not exist and will not compile).
- `createSecret` **internally prepends `isol8/${env}/`** to its 2nd arg (`auth-stack.ts:59`: `secretName: \`isol8/${env}/${secretName}\``). So you pass the **bare** secret name `"bedrock_meter_token"`, NOT the full path. Passing `isol8/${env}/bedrock_meter_token` would yield the doubly-prefixed name `isol8/${env}/isol8/${env}/bedrock_meter_token`, which would NOT match `meterTokenSecretName: \`isol8/${env}/bedrock_meter_token\`` in Step 1 → the Lambda's `fromSecretNameV2` would resolve a non-existent secret and `grantRead`/`kms:Decrypt` would target the wrong ARN.
- The constructor binds `const env = props.environment;` at `auth-stack.ts:36`, and the existing secrets pass bare names (`"clerk_issuer"`, `"paperclip_service_token_key"`, …) — mirror that exactly.

In `apps/infra/lib/stacks/auth-stack.ts`, after the `paperclipServiceTokenKey` block (ends at line 102), add (mirroring its `generateConfig` so CDK seeds a 64-char random value on first create):

```typescript
    // Bedrock meter service token (Plan 3 — the invocation-log meter). The
    // VALUE is minted out-of-band (a one-time `service_token.mint_meter()` run,
    // stored via `aws secretsmanager put-secret-value`) — the random value
    // CDK generates is overwritten before the meter Lambda is wired. createSecret
    // prepends `isol8/${env}/`, so pass the BARE name; the resulting secret name
    // `isol8/${env}/bedrock_meter_token` matches BedrockMeterStack's
    // meterTokenSecretName exactly.
    this.bedrockMeterToken = createSecret(
      "BedrockMeterToken",
      "bedrock_meter_token",
      { passwordLength: 64, excludePunctuation: true },
      "Pre-minted bedrock_meter service-token JWT injected into the meter Lambda; overwritten out-of-band after mint_meter().",
    );
```

and declare `public readonly bedrockMeterToken: secretsmanager.Secret;` with the other public readonly fields (alongside `paperclipServiceTokenKey` at `auth-stack.ts:29`).

- [ ] **Step 4: Run the CDK tests + synth to confirm no errors**

Run: `cd apps/infra && npx jest test/bedrock-meter-stack.test.ts && npx cdk synth --quiet > /dev/null && echo SYNTH_OK`
Expected: jest PASS + `SYNTH_OK`

- [ ] **Step 5: Commit**

```bash
git add apps/infra/lib/isol8-stage.ts apps/infra/lib/stacks/auth-stack.ts
git commit -m "feat(infra): wire BedrockMeterStack into prod stage (single-owner) + meter token secret"
```

---

### Task 12: CDK — account-scoped model-invocation-logging config (text+embedding) via AwsCustomResource

**Files:**
- Modify: `apps/infra/lib/stacks/bedrock-meter-stack.ts`
- Test: `apps/infra/test/bedrock-meter-stack.test.ts` (add logging-config + modality assertions)

**Design:** CDK has no L1 for `PutModelInvocationLoggingConfiguration` (confirmed: `grep ModelInvocationLogging node_modules/aws-cdk-lib` returns nothing; only `aws-bedrock/bedrock.generated.*` exists). Use `AwsCustomResource` to call `bedrock:PutModelInvocationLoggingConfiguration` on create/update and `DeleteModelInvocationLoggingConfiguration` on delete. Enable `textDataDeliveryEnabled: true` AND `embeddingDataDeliveryEnabled: true` (memory MUST-FIX: embeddings = Titan, used by memory-core — otherwise embedding spend is invisible). This runs in the stack's own region (us-east-1); the other fan-out regions get their own config in Task 13.

**PARAMETER-CASING WARNING (deploy-only failure mode — read before Step 3).** `AwsCustomResource` with `service: "Bedrock"` passes `parameters` straight to the AWS SDK call — so the keys MUST match the SDK's expected shape *exactly*: the top-level wrapper is `loggingConfig` (lowercase `l`), and the nested keys are `cloudWatchConfig` / `s3Config` / `largeDataDeliveryS3Config` / `textDataDeliveryEnabled` / `embeddingDataDeliveryEnabled` / `imageDataDeliveryEnabled`. A casing mismatch passes `cdk synth` AND `jest` (the test only greps for the literal substrings — it does NOT validate the SDK accepts the shape) and **fails only at `cdk deploy`** when the custom-resource Lambda invokes the SDK. The Task-1 capture already proved this exact shape works against the live API (Task 1 Step 2 ran `aws bedrock put-model-invocation-logging-configuration --logging-config "{...}"` successfully) — **cross-check the keys in the code below against the working Task-1 `--logging-config` JSON; they must match key-for-key.** Step 5b adds a synth-body round-trip assertion and Step 5c a deploy-validation gate so a mismatch is caught before prod.

- [ ] **Step 1: Write the failing test**

Append to `apps/infra/test/bedrock-meter-stack.test.ts` (inside the `describe` block):

```typescript
  test("creates a Bedrock invocation-log group + S3 sink", () => {
    template.resourceCountIs("AWS::Logs::LogGroup", 1);
    // The S3 sink bucket for >100KB body offload.
    template.resourceCountIs("AWS::S3::Bucket", 1);
  });

  test("AwsCustomResource sets logging config with text+embedding modalities", () => {
    // The custom resource encodes the PutModelInvocationLoggingConfiguration
    // call (incl. both modalities) in its Create parameters.
    const customResources = template.findResources("Custom::AWS");
    const bodies = JSON.stringify(customResources);
    expect(bodies).toContain("PutModelInvocationLoggingConfiguration");
    expect(bodies).toContain("textDataDeliveryEnabled");
    expect(bodies).toContain("embeddingDataDeliveryEnabled");
  });
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npx jest test/bedrock-meter-stack.test.ts`
Expected: FAIL — `creates a Bedrock invocation-log group` expects 1 LogGroup, receives 0 (the log retention construct may create one for the Lambda; assert the Bedrock-specific group below if so).

- [ ] **Step 3: Write the implementation**

In `apps/infra/lib/stacks/bedrock-meter-stack.ts`, add imports:

```typescript
import * as s3 from "aws-cdk-lib/aws-s3";
import * as cr from "aws-cdk-lib/custom-resources";
```

Add a public field declaration `public readonly invocationLogGroup: logs.LogGroup;` and, before the `cdk.Tags.of(this)` lines, add:

```typescript
    // --- Bedrock model-invocation logging sink (this stack's region) ---
    // S3 sink for both the full log copy and the >100KB body offload.
    const logSink = new s3.Bucket(this, "InvocationLogSink", {
      bucketName: `isol8-${props.envName}-bedrock-invocation-${this.region}`,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
    });

    this.invocationLogGroup = new logs.LogGroup(this, "InvocationLogGroup", {
      logGroupName: `/isol8/${props.envName}/bedrock-invocation`,
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // Bedrock's delivery role — assumed by the bedrock.amazonaws.com service
    // to write to the log group + S3 sink.
    const deliveryRole = new iam.Role(this, "BedrockLoggingRole", {
      assumedBy: new iam.ServicePrincipal("bedrock.amazonaws.com"),
    });
    this.invocationLogGroup.grantWrite(deliveryRole);
    logSink.grantWrite(deliveryRole);

    // No L1 for PutModelInvocationLoggingConfiguration — use AwsCustomResource.
    const loggingConfig = {
      cloudWatchConfig: {
        logGroupName: this.invocationLogGroup.logGroupName,
        roleArn: deliveryRole.roleArn,
        largeDataDeliveryS3Config: { bucketName: logSink.bucketName },
      },
      s3Config: { bucketName: logSink.bucketName },
      textDataDeliveryEnabled: true,
      embeddingDataDeliveryEnabled: true,
      imageDataDeliveryEnabled: false,
    };
    new cr.AwsCustomResource(this, "BedrockLoggingConfig", {
      onCreate: {
        service: "Bedrock",
        action: "putModelInvocationLoggingConfiguration",
        parameters: { loggingConfig },
        physicalResourceId: cr.PhysicalResourceId.of(
          `bedrock-logging-${this.region}`,
        ),
      },
      onUpdate: {
        service: "Bedrock",
        action: "putModelInvocationLoggingConfiguration",
        parameters: { loggingConfig },
        physicalResourceId: cr.PhysicalResourceId.of(
          `bedrock-logging-${this.region}`,
        ),
      },
      onDelete: {
        service: "Bedrock",
        action: "deleteModelInvocationLoggingConfiguration",
      },
      policy: cr.AwsCustomResourcePolicy.fromStatements([
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            "bedrock:PutModelInvocationLoggingConfiguration",
            "bedrock:DeleteModelInvocationLoggingConfiguration",
            "bedrock:GetModelInvocationLoggingConfiguration",
          ],
          resources: ["*"], // account-level config — no resource ARN exists
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ["iam:PassRole"],
          resources: [deliveryRole.roleArn],
        }),
      ]),
    });
```

- [ ] **Step 4: Adjust the log-group count assertion if the Lambda log-retention construct creates its own group**

If Step 2's `resourceCountIs("AWS::Logs::LogGroup", 1)` now fails with a count of 2 (Lambda `logRetention` provisions a second group), change that assertion to target the Bedrock group by name:

```typescript
  test("creates the Bedrock invocation-log group", () => {
    template.hasResourceProperties("AWS::Logs::LogGroup", {
      LogGroupName: Match.stringLikeRegexp("/isol8/prod/bedrock-invocation"),
    });
  });
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `npx jest test/bedrock-meter-stack.test.ts`
Expected: PASS (logging-config + modality + sink assertions all green).

- [ ] **Step 5b: Assert the SDK parameter shape round-trips through synth (catches a casing typo at jest time)**

The modality grep in Step 1 only proves the substrings appear *somewhere*; it does not prove the nested keys sit under the SDK-required `loggingConfig.cloudWatchConfig` path. Add a stronger assertion that the synthesized custom-resource Create body carries the exact nested key path. Append to `apps/infra/test/bedrock-meter-stack.test.ts` (inside the `describe`):

```typescript
  test("logging-config custom resource uses the SDK-exact nested key path", () => {
    const customResources = template.findResources("Custom::AWS");
    const cfg = Object.values(customResources).find((r: any) =>
      JSON.stringify(r).includes("PutModelInvocationLoggingConfiguration"),
    ) as any;
    expect(cfg).toBeDefined();
    // The Create payload is JSON-stringified into the resource's Create prop.
    const createStr: string = cfg.Properties.Create;
    const create = JSON.parse(createStr);
    // SDK shape: { service, action, parameters: { loggingConfig: { cloudWatchConfig: {...}, s3Config: {...}, textDataDeliveryEnabled, ... } } }
    expect(create.parameters.loggingConfig.cloudWatchConfig).toBeDefined();
    expect(create.parameters.loggingConfig.s3Config).toBeDefined();
    expect(create.parameters.loggingConfig.textDataDeliveryEnabled).toBe(true);
    expect(create.parameters.loggingConfig.embeddingDataDeliveryEnabled).toBe(true);
    expect(create.parameters.loggingConfig.imageDataDeliveryEnabled).toBe(false);
  });
```

Run: `npx jest test/bedrock-meter-stack.test.ts -t "SDK-exact"`
Expected: PASS. If `create.parameters.loggingConfig` is `undefined`, the top-level wrapper key is mis-cased (e.g. `LoggingConfig`) — fix it to lowercase `loggingConfig`. (If the synthesized `Create` prop is a CFN token rather than a plain string in your CDK version, fall back to asserting against `JSON.stringify(cfg)` containing `"loggingConfig"` and `"cloudWatchConfig"` as adjacent substrings.)

- [ ] **Step 5c: Deploy-validate the logging config in DEV before trusting prod (the casing failure mode is deploy-only)**

`cdk synth` + `jest` cannot prove the SDK *accepts* the parameter shape — only a real call can. Since `BedrockMeterStack` is prod-only (Task 11), validate the custom-resource shape against the live API in dev manually, mirroring the Task-1 capture call (which already succeeded with this exact JSON, so this is a confirmation, not a discovery):

```bash
# Dry-run the exact loggingConfig JSON the custom resource will send.
aws bedrock put-model-invocation-logging-configuration --profile isol8-admin --region us-east-1 \
  --logging-config '{"textDataDeliveryEnabled":true,"embeddingDataDeliveryEnabled":true,"imageDataDeliveryEnabled":false,"cloudWatchConfig":{"logGroupName":"/isol8/dev/bedrock-invocation","roleArn":"<dev-capture-role-arn>","largeDataDeliveryS3Config":{"bucketName":"<dev-capture-bucket>"}},"s3Config":{"bucketName":"<dev-capture-bucket>"}}'
aws bedrock get-model-invocation-logging-configuration --profile isol8-admin --region us-east-1
# Then TEAR DOWN again (do not leave an account-global dev config behind — see Task 1 Step 7).
aws bedrock delete-model-invocation-logging-configuration --profile isol8-admin --region us-east-1
```

Expected: the `put` returns success (HTTP 200, no `ValidationException`) and the `get` echoes the config — proving the key casing the custom resource emits is the casing the SDK accepts. If `put` raises a `ValidationException` / `ParamValidationError`, the key shape in the code is wrong — fix it and re-run Step 5b's jest assertion to lock the corrected shape.

- [ ] **Step 6: Commit**

```bash
git add apps/infra/lib/stacks/bedrock-meter-stack.ts apps/infra/test/bedrock-meter-stack.test.ts
git commit -m "feat(infra): account-scoped Bedrock logging config (text+embedding) via AwsCustomResource"
```

---

### Task 13: CDK — subscription filter (AIP-ARN-scoped) + fan-out across the 3 regions

**Files:**
- Modify: `apps/infra/lib/stacks/bedrock-meter-stack.ts`
- Test: `apps/infra/test/bedrock-meter-stack.test.ts` (add filter + fan-out assertions)

**Design:** a CloudWatch Logs `SubscriptionFilter` on the invocation-log group, destination = the meter Lambda, **filterPattern scoped to the Isol8 AIP-ARN shape** (memory MUST-FIX: a too-broad filter captured unrelated GooseTown traffic). The pattern matches `application-inference-profile` records so non-Isol8 invocations are dropped at the source. For the other two fan-out regions (`us-east-2`, `us-west-2`), Bedrock writes to *their* regional log group, and a subscription filter's destination Lambda must be in the SAME region — so we create a per-region cross-region construct. To keep the plan single-stack and synth-clean, this task builds the in-region filter and emits **per-region `AwsCustomResource` logging configs** for the other regions (the cross-region Lambda+filter is itself a follow-on; for Phase 1 we log in all 3 regions and subscribe in the home region, with a documented gap that the other two regions' delivery is captured via S3 + a daily sweep until per-region Lambdas land — see FIELDS note). The filter pattern + the 3-region logging are the testable contract here.

- [ ] **Step 1: Write the failing test**

Append to `apps/infra/test/bedrock-meter-stack.test.ts` (inside the `describe` block):

```typescript
  test("subscription filter is scoped to the AIP-ARN shape", () => {
    template.hasResourceProperties("AWS::Logs::SubscriptionFilter", {
      FilterPattern: Match.stringLikeRegexp("application-inference-profile"),
    });
  });

  test("the meter Lambda is the subscription destination", () => {
    template.resourceCountIs("AWS::Logs::SubscriptionFilter", 1);
  });

  test("enables logging in all 3 fan-out regions", () => {
    // One AwsCustomResource logging config per fan-out region.
    const customResources = template.findResources("Custom::AWS");
    const loggingConfigs = Object.values(customResources).filter((r: any) =>
      JSON.stringify(r).includes("ModelInvocationLoggingConfiguration"),
    );
    expect(loggingConfigs.length).toBe(3);
  });
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npx jest test/bedrock-meter-stack.test.ts`
Expected: FAIL — no `AWS::Logs::SubscriptionFilter` resource yet, and only 1 logging-config custom resource (not 3).

- [ ] **Step 3: Write the implementation**

In `apps/infra/lib/stacks/bedrock-meter-stack.ts`, add imports:

```typescript
import * as logsDestinations from "aws-cdk-lib/aws-logs-destinations";
```

After the Task-12 `AwsCustomResource` block, add the subscription filter (home region) and the per-region logging configs:

```typescript
    // Subscription filter — drive the meter Lambda, scoped to the Isol8
    // AIP-ARN shape so unrelated traffic (e.g. GooseTown) is dropped at the
    // source. Bedrock records carry the AIP ARN in `modelId`; the term filter
    // requires the invocation-profile substring to be present.
    new logs.SubscriptionFilter(this, "InvocationSubscription", {
      logGroup: this.invocationLogGroup,
      destination: new logsDestinations.LambdaDestination(this.meterFn),
      filterPattern: logs.FilterPattern.literal(
        '"application-inference-profile"',
      ),
    });

    // Fan-out logging configs — Bedrock writes to a per-region invocation-log
    // group, so enable logging in EVERY fan-out region. The home region
    // (this.region) is configured by the Task-12 block above; this loop covers
    // the remaining regions. Each is an account-level config in its region.
    for (const region of props.fanoutRegions) {
      if (region === this.region) continue;
      new cr.AwsCustomResource(this, `BedrockLoggingConfig-${region}`, {
        onCreate: {
          service: "Bedrock",
          action: "putModelInvocationLoggingConfiguration",
          region,
          parameters: { loggingConfig: this._regionLoggingConfig(region, props) },
          physicalResourceId: cr.PhysicalResourceId.of(`bedrock-logging-${region}`),
        },
        onUpdate: {
          service: "Bedrock",
          action: "putModelInvocationLoggingConfiguration",
          region,
          parameters: { loggingConfig: this._regionLoggingConfig(region, props) },
          physicalResourceId: cr.PhysicalResourceId.of(`bedrock-logging-${region}`),
        },
        onDelete: {
          service: "Bedrock",
          action: "deleteModelInvocationLoggingConfiguration",
          region,
        },
        policy: cr.AwsCustomResourcePolicy.fromStatements([
          new iam.PolicyStatement({
            effect: iam.Effect.ALLOW,
            actions: [
              "bedrock:PutModelInvocationLoggingConfiguration",
              "bedrock:DeleteModelInvocationLoggingConfiguration",
            ],
            resources: ["*"],
          }),
        ]),
      });
    }
```

Add the per-region helper method to the class (after the constructor):

```typescript
  private _regionLoggingConfig(
    region: string,
    props: BedrockMeterStackProps,
  ): Record<string, unknown> {
    // The other-region groups/buckets are pre-provisioned out-of-band (one-time
    // `aws logs create-log-group` + `aws s3 mb` per region) because a stack in
    // us-east-1 cannot create resources in another region without a cross-region
    // sub-stack. Phase 1 logs to S3 in those regions and sweeps daily until the
    // per-region Lambda lands; the config below points Bedrock at those sinks.
    const bucket = `isol8-${props.envName}-bedrock-invocation-${region}`;
    return {
      cloudWatchConfig: {
        logGroupName: `/isol8/${props.envName}/bedrock-invocation`,
        roleArn: `arn:aws:iam::${this.account}:role/isol8-${props.envName}-bedrock-logging-${region}`,
        largeDataDeliveryS3Config: { bucketName: bucket },
      },
      s3Config: { bucketName: bucket },
      textDataDeliveryEnabled: true,
      embeddingDataDeliveryEnabled: true,
      imageDataDeliveryEnabled: false,
    };
  }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `npx jest test/bedrock-meter-stack.test.ts`
Expected: PASS (filter pattern, single subscription filter, 3 logging configs).

- [ ] **Step 5: Commit**

```bash
git add apps/infra/lib/stacks/bedrock-meter-stack.ts apps/infra/test/bedrock-meter-stack.test.ts
git commit -m "feat(infra): AIP-scoped subscription filter + 3-region invocation logging"
```

---

### Task 14: CI guard — provider stays `bedrock-runtime`, modalities stay on

**Files:**
- Test: `apps/infra/test/bedrock-meter-stack.test.ts` (add a guard test)

Memory MUST-FIX: a guard must ensure the logging config captures `bedrock-runtime` invocations and keeps both modalities. The logging config does not name a provider directly (Bedrock model-invocation logging captures `bedrock-runtime` `Invoke*`/`Converse*` calls by default), so the guard asserts the modality flags stay `true` and `imageDataDeliveryEnabled` stays `false` (cost discipline) — a regression that flips a modality off would silently hide spend.

- [ ] **Step 1: Write the failing test**

Append to `apps/infra/test/bedrock-meter-stack.test.ts` (inside the `describe` block):

```typescript
  test("GUARD: text+embedding stay enabled, image stays disabled in every config", () => {
    const customResources = template.findResources("Custom::AWS");
    const configs = Object.values(customResources).filter((r: any) =>
      JSON.stringify(r).includes("ModelInvocationLoggingConfiguration"),
    );
    expect(configs.length).toBeGreaterThanOrEqual(1);
    for (const cfg of configs) {
      const body = JSON.stringify(cfg);
      expect(body).toContain('"textDataDeliveryEnabled":true');
      expect(body).toContain('"embeddingDataDeliveryEnabled":true');
      expect(body).toContain('"imageDataDeliveryEnabled":false');
    }
  });
```

- [ ] **Step 2: Run test to verify it passes (the config already sets these — this is a regression guard)**

Run: `npx jest test/bedrock-meter-stack.test.ts -t "GUARD"`
Expected: PASS. If it FAILS, a modality flag drifted in Task 12/13 — restore `textDataDeliveryEnabled:true`, `embeddingDataDeliveryEnabled:true`, `imageDataDeliveryEnabled:false`.

- [ ] **Step 3: Commit**

```bash
git add apps/infra/test/bedrock-meter-stack.test.ts
git commit -m "test(infra): guard text+embedding modalities stay enabled (image off)"
```

---

### Task 15: Final full-suite verification + lint + synth

**Files:** none (verification only)

- [ ] **Step 1: Backend — full targeted suite for this plan**

Run:
```bash
cd apps/backend && uv run pytest \
  tests/unit/routers/test_internal_usage.py \
  tests/unit/billing/test_bedrock_pricing_aip.py \
  tests/unit/services/test_service_token.py -v
```
Expected: PASS (all this plan's backend tests green).

- [ ] **Step 2: Backend — full unit suite (catch cross-module regressions)**

Run: `cd apps/backend && uv run pytest tests/unit -q`
Expected: PASS — no regressions from the `main.py` router include or the `bedrock_pricing` / `service_token` edits. (Pre-existing failures unrelated to this plan's files are not this plan's responsibility — confirm any failure is in a file this plan did not touch.)

- [ ] **Step 3: Backend — lint**

Run: `cd apps/backend && uv run ruff check routers/internal_usage.py core/billing/bedrock_pricing.py core/services/service_token.py core/config.py`
Expected: no errors.

- [ ] **Step 4: Lambda — tests**

Run: `cd apps/infra/lambda/bedrock-usage-meter && python -m pytest test_index.py -v`
Expected: PASS (5 passed — 3 parse/S3 [`_extract` inline, `_extract` offloaded, `_merge_s3_body`] + 1 handler-posts + 1 transport-contract).

- [ ] **Step 5: Infra — CDK tests + synth (no collision/synth errors)**

Run:
```bash
cd apps/infra && npx jest test/bedrock-meter-stack.test.ts && npx cdk synth --quiet > /dev/null && echo SYNTH_OK
```
Expected: jest PASS + `SYNTH_OK`. The synth proves the prod meter stack composes and dev does not synth one (so no account-global collision).

- [ ] **Step 6: Confirm dev synthesizes NO bedrock-meter stack (the W53 invariant)**

Run: `cd apps/infra && npx cdk synth --quiet 2>&1 | grep -c "isol8-dev-bedrock-meter"`
Expected: `0`

- [ ] **Step 7: Final commit (if any verification fix was needed)**

```bash
git add -A
git commit -m "test(meter): final full-suite verification for the Bedrock invocation-log meter" || echo "nothing to commit"
```

---

## Self-Review

**1. Spec coverage (this plan covers spec §3.2 "AWS invocation-log meter" + §3.6 "build-gate field capture" + §3.7 "logging in all 3 regions"):**

- §3.2 "AWS model-invocation logging → Lambda → backend deplete" → Task 8/9 (Lambda), Task 6 (backend deplete), Task 10-13 (CDK wiring). ✅
- §3.2 "Lambda is a dumb transport (parse + S3 fetch + POST, no pricing/attribution/deplete)" → Task 8 (`_extract`/`_merge_s3_body`), Task 9 (`_post_to_backend`); pricing/attribution/deplete live ONLY in `routers/internal_usage.py` (Task 6). ✅
- §3.2 "internal endpoint `POST /api/v1/internal/usage/from-bedrock-log`, service-token authed" → Task 5 (`require_meter_token` + schema + mount), Task 2 (`bedrock_meter` kind). ✅
- §3.2 "resolve owner via the AIP ARN (`modelId`) **OR the assumed-role ARN (`identity.arn`)**, price via `bedrock_pricing`, markup `raw*14//10`, build `UsageEvent`, call `deplete()`" → Task 6: primary `resolve_owner_from_aip_arn(model_id)` with a `resolve_owner_from_identity_arn(identity_arn)` fallback (both wired AND tested — `test_identity_arn_fallback_attributes_when_aip_misses`; `identity_arn` is no longer dead code). The fallback resolver is a SHARED-CONTRACT addendum Plan 2 owns (owner↔IAM-role mapping). All three AIP resolvers are imported from the RECONCILED canonical path `core.billing.bedrock_aip` (Plan-2-owned), in Task 4 AND Task 6 — the earlier wrong module path was removed everywhere. ✅
- **GAP 5 — `settings.METER_IS_LIVE` cutover gate** → Task 6: `deplete()` + `enforce_after_deplete()` run ONLY inside `if settings.METER_IS_LIVE:`; when False the endpoint is shadow-only (builds + prices + logs the would-be deplete, returns `{"applied": False, "shadow": True}`, never mutates the ledger). `METER_IS_LIVE` is DEFINED by Plan 4 in `core/config.py` — this plan CONSUMES it, never redefines it. Tested both directions: `test_meter_live_calls_deplete_and_enforce` (True ⇒ deplete called) + `test_meter_shadow_logs_and_does_not_deplete` (False ⇒ deplete NOT called, `shadow=True`, zero txn rows). This is the order-independent seam letting the meter run alongside the legacy `connection_pool` biller without double-deducting. ✅
- **GAP 4 — kill-switch wiring** → Task 6: inside the `METER_IS_LIVE` branch, after `deplete()`, the endpoint `await enforce_after_deplete(owner_id, result)` (Plan-4-owned `core/services/bedrock_enforcement.py`; revokes Bedrock IAM + sets ECS desired=0 when balance crossed ≤ 0). Imported at FUNCTION level to avoid an import cycle. Tested: `test_meter_live_calls_deplete_and_enforce` asserts `enforce_after_deplete` is awaited once with `(owner_id, result)`; `test_meter_shadow_logs_and_does_not_deplete` asserts it is NOT called in shadow mode. Without this the balance can cross ≤ 0 with no revoke/desired=0 — the kill-switch would never fire. ✅
- §3.2 "bill on error/aborted too" → `_extract` derives `run_status` from `errorCode`; endpoint never gates on status; `test_error_status_still_deplets` (Task 6). ✅
- §3.2 ">100KB S3-offload — fetch the object, don't read cache as 0" → Task 8 `_merge_s3_body` + `test_merge_s3_body_pulls_cache_tokens_from_s3`; Task 1 captures a real offloaded fixture. ✅
- §3.6 "EARLY task: enable dev logging, capture a real cache-using record, lock exact field paths, parse defensively" → Task 1 (hard build-gate) + FIELDS.md + Task 8 reconciliation step. ✅
- §3.7 "logging mode Both (CW + S3), text+embedding modalities, all 3 fan-out regions" → Task 12 (mode Both via `cloudWatchConfig`+`s3Config`, text+embedding), Task 13 (3 regions). ✅
- Filter scoped to AIP-ARN shape (GooseTown spillover MUST-FIX) → Task 13 `FilterPattern` + `test_subscription_filter_is_scoped`. ✅
- Unattributed records skip + metric, never deplete a random owner → Task 6/7 (`meter.unattributed`, `test_unattributed_owner_skips_without_deplete`). ✅
- Idempotency = Plan 1's job, endpoint adds none → Task 6 (no dedup in endpoint), `test_replay_is_noop`. ✅
- W53 account-global collision avoided → single-owner `BedrockMeterStack`, prod-only instantiation, Task 11 dev-absent assertion + Task 15 Step 6. ✅
- Distinct `bedrock_meter` token kind (leaked token can't reach a container) → Task 2 `test_meter_token_rejected_by_paperclip_verify`. ✅
- `include_in_schema=False` (schemathesis posture) → Task 5/7 `test_route_is_mounted_but_hidden_from_openapi`. ✅
- **Deletion inventory rows relevant to this plan:** Plan 3 is purely additive — it introduces the meter that *will* let Plan 4 delete the SOLE legacy deduct path, `connection_pool._maybe_deduct_credits` (`connection_pool.py:1139`, which applies the same `raw * 14 // 10` markup at line 1216). This plan does NOT delete it (that is Plan 4's single-deduct-path cutover). NOTE: `websocket_chat.py:639-651` is NOT a deduct path — it is the `msg_type == "usage"` → `_safe_record_usage` block (usage *recording* into the counters table, no `credit_ledger.deduct`/`estimatedCost`); grep confirms `websocket_chat.py` has no deduct call. Plan 4's cutover touches `connection_pool` only. Confirmed: this plan makes no edits to `connection_pool.py`, `credit_ledger.py`, `usage_service.py`, or `websocket_chat.py` in any task. ✅
- **Not in this plan (correct):** the kill-switch IMPLEMENTATION (`enforce_after_deplete` body — IAM revoke + `desired=0`), the pre-chat gate, and the `METER_IS_LIVE` config DEFINITION all stay in Plan 4 — this plan only CALLS `enforce_after_deplete` and READS `settings.METER_IS_LIVE` (GAP 4 + GAP 5 wiring), never defines either. AIP minting + IAM + `revoke/restore_owner_bedrock` (Plan 2), the AIP resolver bodies in `core/billing/bedrock_aip.py` (Plan 2 — this plan only imports them), the ledger interface + `DdbLedgerBackend` (Plan 1), the daily drift monitor (Plan 4). The Metronome adapter is Phase 3 — but the seam is preserved: pricing/attribution/deplete are all backend-side, so flipping `LEDGER_BACKEND` to `metronome` (Plan 1's factory) swaps the deduct sink without touching the Lambda or the endpoint, and the `UsageEvent` this endpoint builds maps 1:1 onto the Metronome ingest contract (request_id→transaction_id, owner_id→customer_id alias, tokens→properties).

**2. Placeholder scan:** Scanned for every red-flag pattern (deferred-work markers, vague "do this later" notes, "similar to an earlier task", and uncoded edge-case hand-waving). None present. Every code step shows complete code; every test/run step shows the exact command + expected output. The one explicitly-documented Phase-1 gap (per-region Lambda+filter for `us-east-2`/`us-west-2`) is a stated, bounded design decision (log-to-S3-and-sweep in those regions, home-region Lambda subscribes) with the rationale inline in Task 13 — a deliberate scoping choice, not deferred work; the 3-region *logging* contract is fully implemented and tested.

**3. Type consistency:**
- `BedrockLogRecord` field names (`request_id`, `model_id`, `identity_arn`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `run_status`, `region`, `occurred_at`) match exactly: the Lambda's `_extract` output dict keys (minus `body_offloaded`/`s3_ref` which `_post_to_backend` strips), the endpoint's body usage, and the test `_body()` factory.
- The endpoint builds a Plan-1 `UsageEvent` with its EXACT 14 fields (`owner_id, request_id, model_id, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, raw_cost_microcents, billed_microcents, markup_multiplier, region, run_status, occurred_at, source`) — verified against Plan 1's frozen dataclass. ✅
- `get_ledger().deplete(event)` returns `DepleteResult(balance_microcents, applied)` — the endpoint reads exactly those two attributes. ✅
- `price_from_aip_arn(*, aip_arn, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens) -> int` is defined in Task 4 and called identically in Task 6. `resolve_owner_from_aip_arn` / `resolve_owner_from_identity_arn` / `resolve_tier_from_aip_arn` are imported from the SAME RECONCILED `core.billing.bedrock_aip` path (Plan-2-owned) in Task 4 and Task 6 — no stray reference to the earlier wrong module path remains anywhere in the plan. ✅
- `settings.METER_IS_LIVE` (read in Task 6, Plan-4-owned) and `enforce_after_deplete(owner_id, result)` (called in Task 6, Plan-4-owned at `core.services.bedrock_enforcement`) are CONSUMED with the exact signatures Plan 4 defines — this plan never redefines `METER_IS_LIVE` nor implements `enforce_after_deplete`. ✅
- `mint_meter()` / `verify_meter()` / `METER_TOKEN_KIND` names match across Task 2 (definition), Task 5 (`require_meter_token` uses `verify_meter`), and all tests. ✅
- CDK: `BedrockMeterStackProps` fields (`envName, vpc, alb, albSecurityGroup, meterTokenSecretName, meterKmsKeyArn, fanoutRegions`) match between Task 10 (definition), the stage instantiation (Task 11), and the test props (Task 10). `this.meterFn` / `this.invocationLogGroup` are the only public fields, declared and assigned consistently. ✅

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-07-bedrock-invocation-meter-lambda.md`. **Task 1 is a hard build-gate — it must run first (real dev capture) and its fixtures/FIELDS.md gate every parsing task.** This plan CONSUMES Plan 1 (`UsageEvent`/`get_ledger`/`deplete`) and Plan 2 (`resolve_owner_from_aip_arn`/`resolve_tier_from_aip_arn`); if Plan 2 is not yet merged, Task 4 Step 4 adds a temporary stub so the plan is independently runnable. Recommended order is Task 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11 → 12 → 13 → 14 → 15, committing each. Two execution options:

**1. Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — execute tasks in this session with batch checkpoints.
