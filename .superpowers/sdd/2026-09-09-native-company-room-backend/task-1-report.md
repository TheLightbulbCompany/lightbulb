# Native company-room backend phase

Status: deterministic backend implementation complete for independent review; native runtime/auth activation unavailable. This is not shipped multiplayer. Branch `codex/founder-multiplayer-backend`, base `ff8da6b2cbb5da09e2bdbb5b7e60913c39d71270`. No pushes, PRs, merges, deployments, actual accounts, browser actions, auth flips, or sibling-fork/UI edits.

## Interface and decisions

The controller accepted the additive contract before implementation. UI PR2402 (`df06758aa4d78fb7d70d899b26171e8a315824c2`) does not introduce a native wire shape or enable room capabilities; no UI/shared files were edited here.

- A dedicated edge connection selects `clientId=lightbulb-company-room`; Lambda persists `connectionType=native_human`. Only verified Clerk authorizer context can create it. `verifiedIssuer`, `verifiedSubject`, `userId`, `orgId`, and the API Gateway `requestContext.identity.sourceIp` are stored as server facts. External identity/scope headers and query claims are not forwarded as authority. Backend native admission reads this row consistently and always requires the internal WebSocket secret, including when legacy enforcement is off.
- Immutable identity is `(owner_id, verified issuer, verified subject, API Gateway connection_id, source_ip)`. The hashed native alias is `clerk-<sha256(UTF8(JSON.stringify([issuer, subject])))>@identity.lightbulb.invalid`. Frozen `src/state/user-profiles.ts:88` trims/lowercases email aliases; lowercase hex preserves case-only subjects and equal subjects under different issuers. Display/profile metadata is separate.
- Native connections are separate per human/frontend connection and never enter the admin pool or its frontend fanout. Each uses an explicitly qualified, distinct human device and `operator.read,operator.write` only. No browser-supplied native connect frame is relayed. The handshake has no shared token/password, uses the existing signed-device primitive with an empty token binding, and sends only server-authored identity, forwarding and scope headers. Hello must match the qualified build/protocol/scopes; `users.self` must match the qualified durable human profile.
- Dedicated clients send `{type:"req", id, method, params}`; the first `connect` has `params:{}`. A successful reply adds `payload.companyRooms = {enabled:true,audience:"company",methods:[...],profile,historySource:"native"}` only after real admission checks. `ping`/`pong` remains available. Legacy personal clients keep their existing messages, UUID grammar, pool and guards.
- `sessions.create` accepts only `agentId`, `idempotencyKey`, optional `displayName` and optional authorized `parentSessionKey`. The backend generates a deterministic canonical key `agent:<agent>:lightbulb-room:<sha256(JSON ownerId)>:<sha256(JSON [issuer,subject,createId])>` and sets native `visibility:shared`. This explicitly separate namespace allows company room targets without accepting existing private, foreign, cron, Paperclip or arbitrary native keys. It stores no parallel room/transcript/membership database. Existing arbitrary native session promotion is outside this phase.
- Sharing/typing/mentionable/chat methods use `sessionKey`; message subscriptions and `sessions.assignOwner` use `key`. Parameters are closed, method-specific subsets of frozen9.3 schemas. Approved participant/assignee profile IDs come from server qualification. Company-readability is distinct from participation; visibility can be `shared`, `read-only`, or `suggest`, never an invented confidential member ACL.
- Allowed room methods: `sessions.create`; `chat.send/history/abort`; `sessions.messages.subscribe/unsubscribe`; `session.members.list/listEvidence/add/remove`; `session.visibility.set`; `sessions.assignOwner`; `session.typing`; `users.mentionable`; `mentions.list/dismiss`. Global lists, search, previews, attachments, public sharing, arbitrary patching, admin, device, node, service, tools and approval RPCs remain denied on this path. No broad legacy allowlist expansion.
- `chat.send` requires `expectedLeafEntryId` (string or null), preserves `sessionId`, routing expectation, reply lineage and validated mentions, and forces `deliver:false`. Its idempotency key is namespaced by owner, issuer, subject and full room key; the returned native `runId` is authoritative. Same human/same room retries converge across tabs; another human cannot consume that key. Budget/subscription admission still runs before sends. Native stale-leaf and concurrent errors are returned intact; there is no automatic replay.
- Native `sender`, profile identity, `createdBy`, entry/run/parent/session fields pass through history and events unchanged. Unknown authors are not filled with the current user. Oversized history fallback retains bounded native attribution/lineage; oversized events emit `native.room.historyRequired` with `sessionKey`. Loss/revocation emits `native.room.reconnectRequired` with an empty payload and closes the edge connection. Clients reconnect, restore native canonical history, and explicitly resubscribe.
- Events are restricted to admitted room keys and this exact frontend's subscriptions. Native member checks have no positive cache, and the durable connection row is strongly read. Membership deletion removes matching durable rows, closes frontend/native/node/operator sockets, drops Teams subscriptions, and invalidates warm owner recipient caches. Local in-flight humans supplement the eventually consistent owner GSI. Warm native readers revalidate on events and on a five-second idle/continuous-traffic deadline; failed admission closes upstream and frontend. Failures in webhook revocation request a retry before Paperclip/billing side effects.

## Concrete activation gates

`_qualified_owners` is empty and there is deliberately no production loader, env toggle, version-tag inference, or deployment/config edit. Qualification records bind exact owner, private endpoint, release update ID, native build ID, evidence ID, known agents, human device/profile bindings, and positive human-auth/machine-coexistence/legacy-privacy results. Synthetic records exist only in tests.

Frozen native `connect-device-pairing.ts:117–125` rejects device-only operator authentication with configured human roles (`AUTH_VERIFIED_USER_REQUIRED`). Native paired/bootstrap nodes are separate valid candidates; the transparent relay still forwards signed frames verbatim. Trusted proxy peers without valid forwarded attribution fail before device fallback. No service-principal semantics, header requirements/network placement, credential provisioning/cache migration, fresh node enrollment, existing Tauri/CLI reconnect, backend/Paperclip operator coexistence or actual signature acceptance was operationally proved here. No custom bridge, fake human service identity, global auth change or signed-token rewrite was introduced.

The controller must supply the reviewed native qualification/provisioning mechanism after demonstrating two approved dev identities on the exact candidate, native persisted authors, private-history protection, subscriptions/reconnect, membership/device removal and machine operation. Qualification must include exact auth config/ingress placement, service audit semantics, and steady-state stream-rate behavior. Current uncached Clerk checks favor revocation correctness but may hit Clerk limits under dense streaming; this is unmeasured and must be qualified before activation. No new test account or expired-trial/billing bypass is required or performed by this code.

The webhook now closes existing legacy member sockets too, but this phase does not redesign legacy Clerk JWT admission for every HTTP/legacy reconnect route. Native room reconnect always rechecks current membership. The qualified owner must not rely on the old admin path as its human room authority.

## Changes

Added `core/gateway/native_rooms.py` for immutable identity, namespace/typed admission, native human transport, isolated pool and revocation. Integrated the dedicated path into `websocket_chat.py`, lifecycle into the existing pool, provenance into backend auth/authorizer/connection storage, and the membership deletion webhook. Added synthetic gateway integration tests and Lambda provenance/negative cases. The existing untracked `docs/superpowers/plans/2026-09-09-native-company-room-backend.md` was read but not edited/staged.

## Verification

Used only `/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python` (Python3.12). Common environment: `AWS_EC2_METADATA_DISABLED=true AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_SESSION_TOKEN=testing AWS_DEFAULT_REGION=us-east-1 PYTHONDONTWRITEBYTECODE=1`. No real credentials were loaded. The installed pytest/jsonschema/crypto tooling was reused; no dependencies added.

Initial phase command, from `apps/backend`:

```sh
AWS_EC2_METADATA_DISABLED=true AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_SESSION_TOKEN=testing AWS_DEFAULT_REGION=us-east-1 PYTHONDONTWRITEBYTECODE=1 /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m pytest -q -p no:cacheprovider -o addopts='' tests/unit/routers/test_websocket_chat.py tests/unit/routers/test_websocket_chat_teams.py tests/unit/routers/test_websocket_chat_method_allowlist.py tests/unit/core/test_connection_pool_org.py tests/unit/gateway/test_connection_pool_rpc_id_isolation.py tests/unit/routers/test_webhooks.py tests/contract/test_websocket_contracts.py tests/unit/gateway/test_native_rooms.py tests/unit/test_ws_authorizer_azp.py tests/unit/test_ws_authorizer_device.py tests/unit/services/test_connection_service_owner.py tests/unit/services/test_connection_service_errors.py tests/unit/routers/test_chat_history_frame_trim.py tests/unit/core/test_auth.py tests/unit/routers/test_websocket_chat_internal_auth.py
```

Interrupted after **5 passed in30.03s** when an existing route test reached an unstubbed boto path. Repeated the same phase selection with `AWS_ENDPOINT_URL=http://127.0.0.1:9 AWS_MAX_ATTEMPTS=1`: **234 passed,1 failed,15 errors in4.79s**. Failures were solely the custom endpoint bypassing Moto's URL matching in the12 contract and4 connection-service tests. An affected-only attempt adding `MOTO_CUSTOM_ENDPOINTS=http://127.0.0.1:9` also failed (**1 failed,15 errors in3.08s**); DynamoDB Moto does not consume that S3-style setting. The sandbox rejected the unmocked transport; no AWS operation succeeded.

Corrected affected-only rerun, without endpoint overrides, preserving Moto while forbidding real AWS transport:

```sh
AWS_EC2_METADATA_DISABLED=true AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_SESSION_TOKEN=testing AWS_DEFAULT_REGION=us-east-1 AWS_MAX_ATTEMPTS=1 PYTHONDONTWRITEBYTECODE=1 /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -c 'import sys, pytest
from unittest.mock import patch
with patch("botocore.httpsession.URLLib3Session.send", side_effect=AssertionError("Unexpected live AWS transport")):
    sys.exit(pytest.main(["-q", "--tb=short", "-p", "no:cacheprovider", "-o", "addopts=", "tests/contract/test_websocket_contracts.py", "tests/unit/services/test_connection_service_owner.py"]))'
```

**16 passed,1 AsyncMock RuntimeWarning in3.72s**. Warning surfaced in the existing contract disconnect mock; no test failed.

After the concrete self-review additions (actual simultaneous sends; HTTP durable identity/no legacy downgrade; revoked qualification/oversized event; webhook ordering; warm-reader frontend revocation), used the identical guarded command with its two file arguments replaced by `"tests/unit/gateway/test_native_rooms.py"`: first **54 passed in0.80s**, final **55 passed in1.30s**. Across the completed phase selection and affected-only corrections, **255 distinct backend cases pass** (the initial250 plus5 added cases). These are mocked protocol results, not a real native gateway/auth handshake or container/Paperclip/device qualification.

From `apps/infra/lambda/ws-connect`:

```sh
AWS_EC2_METADATA_DISABLED=true AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_SESSION_TOKEN=testing AWS_DEFAULT_REGION=us-east-1 PYTHONDONTWRITEBYTECODE=1 /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m pytest -q -p no:cacheprovider -o addopts='' test_index.py
```

**15 passed in0.36s**. This includes forged external-header rejection, verified issuer/subject forwarding, nonhuman denial and backend-refusal cleanup.

`python -m ruff check --no-cache apps/backend/core/gateway/native_rooms.py apps/backend/tests/unit/gateway/test_native_rooms.py apps/backend/routers/websocket_chat.py`: passed after fixing an imported `field` name shadow. `git diff --check`: passed. Formatting scoped to the new module/test and touched router required authorized filesystem escalation because the assigned worktree is outside the original writable root; approved. No network escalation.

## Self-review and handoff

Verified no owner-admin fallback, no client actor/scopes/device-role input, correct native method key spellings, private/foreign namespace denial, distinct human and tab keys, original native error/lineage preservation, actor+room idempotency, no automatic consequential retry, gate-before-send, current membership before replies/events, per-client subscription cleanup and fail-closed unknown runtime. Existing legacy route/pool/allowlist/history/auth tests pass. Native/private target checks and qualification flags are defensive code, not evidence that native machine coexistence or legacy privacy migration is complete.

Commit: the code and this report are committed together on the assigned branch; exact commit ID is returned in the implementer's final handoff (avoids embedding a self-referential hash). Controller owns independent security/integration review, PR creation and any runtime qualification/activation decision.

## PR 2403 CI formatting correction

Controller reported CI run `34302638132` failed only `ruff format --check` for `core/services/connection_service.py`; Ruff lint passed and the other 936 files were formatted. Reproduced locally with the installed Python3.12 toolchain: `python -m ruff --version` returned `ruff 0.15.6`, exactly matching `apps/backend/uv.lock:1587`. No dependency changes.

From the worktree root, using `/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python`:

```sh
python -m ruff format --check --no-cache apps/backend/core/services/connection_service.py
# Exit 1: 1 file would be reformatted.
python -m ruff format --no-cache apps/backend/core/services/connection_service.py
# Exit 0: 1 file reformatted.
python -m ruff format --check --no-cache apps/backend/core/services/connection_service.py
# Exit 0: 1 file already formatted.
git diff --check
# Exit 0.
```

The sole code-file delta splits four provenance mapping tuples onto separate lines. No behavior change; no broader tests rerun. Commit contains only that file and this report; no push performed.

## Review fix round 1 — I1/I2 and M1/M2

Read the complete task review against original feature commit `9eba0426bf7c9858faa7fd6b4a4ec220ce5d39dc`; retained formatting commit `9d7c4fff8072589b3e70fa0f15ec8aa8c8982227`. This round follows the controller's paired-selector ruling; no scope, auth activation or runtime qualification change.

### Corrections and exact contract

- **I1:** moved `NativeRoomPool` construction out of `GatewayConnection` into the actual `GatewayConnectionPool` constructor. The native harness now constructs that real shared pool, never injects a mock `.native_rooms` attribute, and tears down through its real `close_all()`. Only external transport/storage/membership/billing dependencies and legacy-RPC non-call observation are mocked. New integration cases exercise native route admission, Lambda-first durable-row deletion followed by native disconnect, shared shutdown, and the actual membership-deletion webhook for ordinary legacy sockets while `_qualified_owners == {}`. The latter verifies connection/subscription removal, preservation of the other member, and subsequent Paperclip archive/close plus seat synchronization.
- **I2:** `chat.history` now accepts optional nonempty `messageId` (bounded to the existing 512-character subset), alone or paired with `sessionId`. A `sessionId` requires `messageId`; offset/messageId and cursor/(offset or messageId) combinations are rejected before any dial. Full owner/agent/room `sessionKey` authorization is unchanged. Both selectors are forwarded unchanged, and transcript-to-canonical-key ownership stays authoritative upstream. Focused cases cover both successful selectors, four invalid combinations, native foreign-transcript rejection with the error preserved, and backend rejection of a foreign/private full key before an upstream call. The synthetic gateway models these specific frozen constraints; it does not prove real native transcript behavior.
- Frozen native evidence: read `src/gateway/server-methods/chat-history-handler.ts:160–180` (selector combinations) and `:229–249` (transcript ownership) in the sibling read-only fork. No sibling source/install changes.
- **M2:** `mentions.dismiss.ids[]` and optional `users.mentionable.agentId` share the native 1–256-character mention-reference bound, also reused for mentioned profile IDs. Tests accept 256 and reject 257.
- **M1:** traced the warning to the contract disconnect test's inherited AsyncMock `connection_service.get_connection()` (a synchronous production method). Replaced only that incorrect service test double with a synchronous MagicMock and explicit absent row; asserted both synchronous calls. No production coroutine behavior was changed.
- Complete PR-changed Python checks also exposed one unused `importlib` import in `ws-connect/test_index.py` and formatting in the native test plus three Lambda files. Removed only that unused import and ran pinned Ruff formatting. Inspected those Lambda diffs: whitespace/wrapping/trailing commas only otherwise. No dependency upgrades or behavioral Lambda edits in this round.

### Exact affected verification and raw evidence

The earlier **255 backend +15 Lambda** evidence above remains historical evidence; it was not rerun wholesale or replaced by a claim of complete runtime qualification. All commands below used the existing Python3.12 interpreter at `/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python`, synthetic AWS values, metadata disabled, no custom AWS endpoint, and a fail-fast live AWS transport guard. No actual AWS/browser/production/account access occurred.

Warning diagnosis: from `apps/backend`, the same common environment and guarded Python command below first selected only `tests/contract/test_websocket_contracts.py::test_ws_disconnect_success`, with `-W always::RuntimeWarning`. A `PYTHONTRACEMALLOC=5` attempt produced **1 passed,1 warning in26.78s**, but its allocation trace was not specific. The concrete follow-up added `sys.set_coroutine_origin_tracking_depth(8)` and `--log-cli-level=WARNING` (without tracemalloc):

```sh
AWS_EC2_METADATA_DISABLED=true AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_SESSION_TOKEN=testing AWS_DEFAULT_REGION=us-east-1 AWS_MAX_ATTEMPTS=1 PYTHONDONTWRITEBYTECODE=1 /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -c 'import sys, pytest
from unittest.mock import patch
sys.set_coroutine_origin_tracking_depth(8)
with patch("botocore.httpsession.URLLib3Session.send", side_effect=AssertionError("Unexpected live AWS transport")):
    sys.exit(pytest.main(["-q", "--tb=short", "-W", "always::RuntimeWarning", "--log-cli-level=WARNING", "-p", "no:cacheprovider", "-o", "addopts=", "tests/contract/test_websocket_contracts.py::test_ws_disconnect_success"]))'
```

Raw warning output (before the test-double correction):

```text

tests/contract/test_websocket_contracts.py::test_ws_disconnect_success PASSED [100%]

=============================== warnings summary ===============================
tests/contract/test_websocket_contracts.py::test_ws_disconnect_success
  /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/lib/python3.12/site-packages/_pytest/stash.py:104: RuntimeWarning: coroutine 'AsyncMockMixin._execute_mock_call' was never awaited
  Coroutine created at (most recent call last)
    File "/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/lib/python3.12/site-packages/fastapi/routing.py", line 130, in app
      await wrap_app_handling_exceptions(app, request)(scope, receive, send)
    File "/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/lib/python3.12/site-packages/starlette/_exception_handler.py", line 42, in wrapped_app
      await app(scope, receive, sender)
    File "/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/lib/python3.12/site-packages/fastapi/routing.py", line 116, in app
      response = await f(request)
    File "/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/lib/python3.12/site-packages/fastapi/routing.py", line 670, in app
      raw_response = await run_endpoint_function(
    File "/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/lib/python3.12/site-packages/fastapi/routing.py", line 324, in run_endpoint_function
      return await dependant.call(**values)
    File "/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/.worktrees/founder-multiplayer-backend/apps/backend/routers/websocket_chat.py", line 809, in ws_disconnect
      connection = connection_service.get_connection(x_connection_id)
    File "/Users/prasiddhaparthsarthy/.local/share/uv/python/cpython-3.12.11-macos-aarch64-none/lib/python3.12/unittest/mock.py", line 1139, in __call__
      return self._mock_call(*args, **kwargs)
    File "/Users/prasiddhaparthsarthy/.local/share/uv/python/cpython-3.12.11-macos-aarch64-none/lib/python3.12/unittest/mock.py", line 1143, in _mock_call
      return self._execute_mock_call(*args, **kwargs)
    del self._storage[key]
  Enable tracemalloc to get traceback where the object was allocated.
  See https://docs.pytest.org/en/stable/how-to/capture-warnings.html#resource-warnings for more info.

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
========================= 1 passed, 1 warning in 1.76s =========================

```

Affected backend run after I1/I2/M1/M2, from `apps/backend`:

```sh
AWS_EC2_METADATA_DISABLED=true AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_SESSION_TOKEN=testing AWS_DEFAULT_REGION=us-east-1 AWS_MAX_ATTEMPTS=1 PYTHONDONTWRITEBYTECODE=1 /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -c 'import sys, pytest
from unittest.mock import patch
with patch("botocore.httpsession.URLLib3Session.send", side_effect=AssertionError("Unexpected live AWS transport")):
    sys.exit(pytest.main(["-q", "--tb=short", "-W", "error::RuntimeWarning", "-p", "no:cacheprovider", "-o", "addopts=", "tests/unit/gateway/test_native_rooms.py", "tests/contract/test_websocket_contracts.py", "tests/unit/core/test_connection_pool_org.py", "tests/unit/gateway/test_connection_pool_rpc_id_isolation.py"]))'
```

Raw output, exit 0 (no warnings):

```text
........................................................................ [ 75%]
........................                                                 [100%]
96 passed in 4.20s

```

After Lambda formatting/unused-import cleanup, from `apps/infra/lambda/ws-connect`, ran the exact same guarded command with the four test-file arguments replaced by `"test_index.py"`. Raw output, exit 0 (no warnings):

```text
...............                                                          [100%]
15 passed in 0.52s

```

All changed Python lint/format commands from the worktree root (12 files, including the contract test and all three Lambda files), with pinned Ruff **0.15.6**, matching the existing lock:

```sh
git diff --name-only -z ff8da6b2 -- '*.py' | xargs -0 /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m ruff check --no-cache
git diff --name-only -z ff8da6b2 -- '*.py' | xargs -0 /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m ruff format --check --no-cache
```

Initial raw checks (both exit 1):

```text
F401 [*] `importlib` imported but unused
 --> apps/infra/lambda/ws-connect/test_index.py:3:8
  |
1 | """Tests for $connect Lambda forwarding semantics."""
2 |
3 | import importlib
  |        ^^^^^^^^^
4 | import os
5 | import sys
  |
help: Remove unused import: `importlib`

Found 1 error.
[*] 1 fixable with the `--fix` option.
Would reformat: apps/backend/tests/unit/gateway/test_native_rooms.py
Would reformat: apps/infra/lambda/websocket-authorizer/index.py
Would reformat: apps/infra/lambda/ws-connect/index.py
Would reformat: apps/infra/lambda/ws-connect/test_index.py
4 files would be reformatted, 8 files already formatted

```

Applied only the unused-import edit plus:

```sh
/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m ruff format --no-cache apps/backend/tests/unit/gateway/test_native_rooms.py apps/infra/lambda/websocket-authorizer/index.py apps/infra/lambda/ws-connect/index.py apps/infra/lambda/ws-connect/test_index.py
```

Raw format output: `4 files reformatted` (exit 0). Scoped filesystem approval was granted; no network approval/access. Final exact lint/format commands above, raw output (both exit 0):

```text
All checks passed!
12 files already formatted

```

`git diff --check`: exit 0, no output. No broad suite was rerun after the passing affected checks or formatting-only cleanup.

### Round-1 self-review and remaining gate

Confirmed the real shared container owns exactly one native pool, legacy connections no longer allocate one, disconnect works after its durable record is gone, and ordinary membership deletion continues when native qualification is empty. The added selector cannot bypass the existing full-key target gate; a selected transcript still must belong to that key according to native. The native fake is explicitly contract-only, not an alternate authorization implementation or live integration proof. No other RPC families, native loader, global auth toggle, bridge, billing bypass, UI/shared code or fork changes were added.

Native activation remains **unavailable/inactive**. Actual per-owner two-human/service admission, trusted ingress/header provenance, transcript privacy, device revocation, native signatures, Paperclip/CLI/Tauri coexistence and deployment/runtime behavior still require the controller's candidate integration proof. This commit fixes deterministic reviewed defects, not shipped multiplayer. Used receiving-code-review and verification-before-completion skills for confirmed-findings and fresh-evidence discipline; the controller's consolidated affected-only test instruction overrides per-slice repetition. Exact fix commit is returned with handoff; no push, PR mutation, merge or deployment is performed here.

## Whole-branch fix wave — WB-I1 through WB-I4

Fix base: `9016207343755b04eeed1e5653c537edc5dfe85b`. Read `whole-branch-review.md` completely; all four Important findings addressed together. Earlier feature/formatting/fix commits and historical **255 backend +15 Lambda**, then **96 affected backend +15 Lambda**, remain unchanged evidence, not a fresh whole-suite claim.

### Finding dispositions

- **WB-I1 — addressed.** Socket revocation failure is retained until the existing Paperclip archive/defensive disable/retry and seat-sync paths have run. The real Clerk dispatcher rolls back its membership-deletion Svix dedup claim using existing `webhook_dedup.delete_event` on handler failure. Tests run the real dispatcher, handler, revoker and shared pool with a failed external socket query; Moto executes the actual conditional dedup put/delete. Same-Svix redelivery completes and only a subsequent completed delivery is suppressed. Both successful archive and retryable archive failure (immediate disable + existing retry enqueue) are covered.
- **WB-I2 — addressed.** `ConnectionService.begin_member_revocation` atomically adds the exact connection ID to an owner/member-scoped retry marker and deletes original admission in one existing-table DynamoDB transaction. Marker keys use a digest of the owner/member pair, have no authorizing identity/type or GSI recipient attributes, and no expiry that could discard incomplete cleanup. The next revocation strongly reads that marker even when the original owner GSI row is gone. Native, cached frontend, node, operator, Teams and edge cleanup are independent; one failure no longer skips the others. The marker member is removed only when all steps succeed; empty markers are conditionally deleted without erasing concurrently added IDs. The real-service/Moto/real-pool regression proves delete-success + node/edge failures, independent operator/Teams cleanup, another member preserved, no original GSI target, and a second edge-close attempt on retry.
- **WB-I3 — addressed under the controller's explicit conservative-hold ruling.** The existing durable idle-stop counter now includes fresh `native_human` presence and strongly read unresolved native-run holds. Native runs reserve a non-authorizing hold before consequential `chat.send`; storage failure prevents sending. Identity is exact owner + full canonical room key + already actor-scoped native idempotent run ID. The frozen native `chat-send-session.ts:88` uses the idempotency key as `clientRunId`. Holds have no TTL and are not cleared by frontend removal, transport uncertainty, another room/run's evidence, or a generic native error. Matching authorized native `chat` final/error/aborted evidence clears that exact hold, even when the sender tab is no longer subscribed. The narrow explicit rejection release is the frozen pre-admission `INVALID_REQUEST / details.reason=active-leaf-changed` response (`chat-send-pre-admission.ts:67–73`), and only for a newly created hold; a retry cannot release an earlier uncertain hold. Existing spend/trial enforcement remains before the native send path.
- Native local presence/holds participate in grace and final idle-stop vetoes without insertion into legacy admin fanout. Final stop checking also serializes with the separate native dial lock: an in-flight human handshake finishes/fails before the stop decision. Tests cover real native route/RPC/reader/pool hold-before-send, matched/mismatched terminal release, proven rejection, uncertain send/disconnect, hold-write failure, same-worker grace, and an in-progress-handshake race. Actual ConnectionService/Moto and a new empty shared pool cover another worker's grace/boot-reconciliation veto followed by exact hold release permitting the stop.
- **WB-I4 — addressed.** Failed subscribe **or** unsubscribe restores the captured pre-call subscription set. The focused case keeps actual router/RPC/reader methods, rejects only the upstream unsubscribe response, verifies the error reaches the client, then verifies an authorized room event still forwards.

### Shared-root caller trace / self-review

`pending_member_revocations`, `begin_member_revocation`, and `finish_member_revocation` are used only by the member socket revoker; the unconditional membership-deletion caller now preserves existing independent deauthorization. `get_connection` additionally returns no identity for synthetic marker rows (no `userId`); normal edge identity callers in the message/disconnect routes and native current-member checks keep their existing behavior. Retry/run markers omit both user/owner GSI fields, so existing owner fanout and Teams user fanout cannot discover them. No authorizing row is recreated for retry discovery.

`begin_native_run` is used by the native human RPC method immediately before send; `finish_native_run` is called only for the qualified native terminal evidence or narrow proven pre-admission rejection described above. `count_live_chat_connections` has only one production caller, the existing trial idle-stop veto; its expanded count intentionally means human presence plus unresolved native work, not a displayed connection count. Its errors still propagate to the stop path's over-preserve catch. New local activity checks are limited to grace/idle-stop safety; legacy `has_active_runs`, readiness markers, owner delivery membership and native authority remain separate.

Reviewed the invoked node/operator disconnect helpers: they already provide idempotent mapping removal and best-effort upstream close, and remain unchanged. The revoker still calls both and the Teams broker independently; edge closure errors propagate and preserve durable retry IDs. Transaction failure cannot leave admission deleted without its marker: neither transaction operation commits. External cleanup still gets an attempt on failure and the webhook is not acknowledged complete. The existing Svix rollback primitive/storage availability and provider redelivery remain the retry mechanism; no queue, table, resource, generic service or detached reader was introduced.

Self-review after the consolidated run added the native-handshake lock fence described above. Existing legacy/native lock order has no reverse acquisition: native ensure takes only its own lock; shared shutdown releases native close-all's lock before acquiring the legacy lock. Final synchronous local checks remain in place after awaited cross-worker reads. The existing cross-worker distributed-stop race boundary is not claimed to have become a distributed transaction.

### Exact affected commands and raw output

All runs were local/synthetic only, with existing Python3.12 and pinned Ruff0.15.6 (unchanged lock). No AWS/live/production/browser/account/secret access, dependencies, deployment, fork/UI/shared changes or subagents. A fail-fast botocore transport guard forbids an accidental real AWS request while allowing Moto.

Consolidated affected run, from `apps/backend`:

```sh
AWS_EC2_METADATA_DISABLED=true AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_SESSION_TOKEN=testing AWS_DEFAULT_REGION=us-east-1 AWS_MAX_ATTEMPTS=1 PYTHONDONTWRITEBYTECODE=1 /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -c 'import sys, pytest
from unittest.mock import patch
with patch("botocore.httpsession.URLLib3Session.send", side_effect=AssertionError("Unexpected live AWS transport")):
    sys.exit(pytest.main(["-q", "--tb=short", "-W", "error::RuntimeWarning", "-p", "no:cacheprovider", "-o", "addopts=", "tests/unit/gateway/test_native_rooms.py", "tests/unit/routers/test_webhooks_paperclip.py", "tests/unit/routers/test_webhooks.py", "tests/unit/services/test_connection_service_liveness.py", "tests/unit/services/test_connection_service_errors.py", "tests/unit/services/test_connection_service_owner.py", "tests/unit/gateway/test_arm_idle_teardown.py", "tests/unit/gateway/test_grace_close_guard.py"]))'
```

Raw output, exit0, no warnings:

```text
........................................................................ [ 50%]
........................................................................ [100%]
144 passed in 5.12s

```

After the self-review handshake fence, used the exact same environment/interpreter/guard/pytest flags, replacing the eight test-file arguments with:

```text
"tests/unit/gateway/test_native_rooms.py::test_idle_stop_waits_for_native_handshake_before_final_local_veto",
"tests/unit/gateway/test_native_rooms.py::test_native_presence_and_disconnected_run_veto_same_worker_grace",
"tests/unit/gateway/test_arm_idle_teardown.py"
```

Raw affected-only output, exit0, no warnings:

```text
..................                                                       [100%]
18 passed in 1.00s

```

These counts overlap; they must not be summed as distinct tests. No whole-suite or unchanged broad baseline rerun occurred.

Formatting used the existing pinned interpreter, from the worktree root:

```sh
git diff --name-only -z 901620734 -- '*.py' | xargs -0 /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m ruff format --no-cache
# Exit0: 7 files reformatted, 2 files left unchanged.
/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m ruff format --no-cache apps/backend/core/gateway/connection_pool.py apps/backend/tests/unit/gateway/test_native_rooms.py
# Final handshake-fence formatting, exit0: 1 file reformatted, 1 file left unchanged.
git diff --name-only -z ff8da6b2 -- '*.py' | xargs -0 /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m ruff check --no-cache
git diff --name-only -z ff8da6b2 -- '*.py' | xargs -0 /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m ruff format --check --no-cache
git diff --check
```

Final raw lint/format output for **all15 PR-changed Python files**, both exit0:

```text
All checks passed!
15 files already formatted

```

`git diff --check`: exit0, no output. Scoped filesystem escalation authorized the formatter/commit in this assigned worktree; no network escalation.

### Accepted ceiling / handoff

Qualification remains **empty, unavailable and inactive**. The controller explicitly accepted non-expiring unresolved native-run holds rather than a detached-drain framework. An uncertain send, disconnected sender with no later terminal observer, or completed idempotent response without a matching terminal can conservatively retain an idle trial container. No completion is inferred from elapsed time. This is an explicit ponytail ceiling: **native-runtime reconciliation and bounded operational recovery must be qualified before enabling trial native rooms**. The existing trial/spend gate is unchanged, but that is not proof of bounded idle-retention cost.

All prior exact-candidate requirements remain: verified human/machine/ingress coexistence, persisted native authors and transcript privacy, live revocation, signatures, service/device lifecycle, stream rate/backpressure, legacy privacy and operational recovery. These synthetic checks and non-authorizing records are not shipped multiplayer or activation approval. Used systematic-debugging, receiving-code-review and verification-before-completion discipline to trace shared callers and validate the scoped corrections; the user's consolidated phase-end test cadence governs execution. Commit contains intended fixes/tests and this appended report; exact SHA is returned in handoff. No push, merge, deployment or auth activation.

## Backend CI 34303786186 — final test-isolation correction

Controller supplied the old `9016207343755b04eeed1e5653c537edc5dfe85b` Backend CI failure evidence: six membership-deleted/dedup cases in `test_webhooks_paperclip.py` and `test_webhooks_seat_sync.py::test_membership_deleted_calls_seat_sync` reached real DynamoDB transport through the new revoker with synthetic tokens. Supplied CI summary (not rerun wholesale here):

```text
7 failed, 9886 passed, 370 skipped, 51 warnings
```

The six Paperclip cases already have external socket-storage isolation in approved whole-branch fix `0abfa7ea59c0a3ea165776d7a3c95468fdce0c3f`. This final delta changes **only** the remaining seat-sync deletion test: construct the real `GatewayConnectionPool`, leave the real revoker in the handler path, and provide empty mocked external owner-query/pending-revocation storage. Assertions verify both discovery calls and the existing seat-sync call. No production behavior, exception swallowing, runtime boundary, or qualification change.

Fresh affected-only command from `apps/backend`:

```sh
AWS_EC2_METADATA_DISABLED=true AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_SESSION_TOKEN=testing AWS_DEFAULT_REGION=us-east-1 AWS_MAX_ATTEMPTS=1 PYTHONDONTWRITEBYTECODE=1 /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -c 'import sys, pytest
from unittest.mock import patch
with patch("botocore.httpsession.URLLib3Session.send", side_effect=AssertionError("Unexpected live AWS transport")):
    sys.exit(pytest.main(["-q", "--tb=short", "-W", "error::RuntimeWarning", "-p", "no:cacheprovider", "-o", "addopts=", "tests/unit/routers/test_webhooks_seat_sync.py", "tests/unit/routers/test_webhooks_paperclip.py"]))'
```

Raw output, exit0, no warnings:

```text
.....................                                                    [100%]
21 passed in 2.19s

```

Pinned existing Python3.12/Ruff0.15.6, from the worktree root:

```sh
/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m ruff check --no-cache apps/backend/tests/unit/routers/test_webhooks_seat_sync.py
/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m ruff format --check --no-cache apps/backend/tests/unit/routers/test_webhooks_seat_sync.py
git diff --check
```

Raw lint/format output, both exit0:

```text
All checks passed!
1 file already formatted
```

Whitespace check exit0, no output. No broader test suite, network/AWS/browser/production access, dependency change, push, merge, deployment or agents. Used the CI-fix skill with controller-provided evidence and explicitly approved narrow scope; no additional GitHub fetch was needed. Self-review confirms only the seat-sync test and this report are staged. Whole-branch code approval remains the controller's decision; this local test evidence does not claim new remote CI success or activate native rooms.
