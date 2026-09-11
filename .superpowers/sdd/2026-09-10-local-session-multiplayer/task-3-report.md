# Task 3 — dedicated desktop local-session transport

Status: DONE_WITH_CONCERNS (implementation and hermetic checks complete; native receipt trust and deployment verification belong to subsequent tasks).

Base: `6282f31fb06e24c70c53cb89be7306a330fd9536`.
Worktree: `/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/.worktrees/local-session-native-probe`.
Branch: `codex/local-session-native-probe`.
No real account enrollment, AWS mutations, provider configuration, dependency changes, push, merge, or deployment. No subagents/reviewers spawned. Tests never import `main.app` or its lifespan.

## Implementation

- `lightbulb-local-sessions` is a distinct `local-session` connection type, using existing paired-device proof. Connect preserves verified user/org/device/client provenance. The backend strongly reads the connection and verifies current canonical `openclaw-macos` pairing plus human-controlled enrollment. A node-host key, another human's key, browser credential, Paperclip credential, forged device header, or wrong scope cannot authorize it. The local-session Lambda handshake fails closed when backend admission fails and removes its provisional row; existing node/operator startup-failure behavior is unchanged.
- `LocalSessionRelay` is stateless: each worker reads the canonical session's connection ID, hydrates that shared connection record, and uses existing `ManagementApiClient`. No OpenClaw gateway/node connection, provider wrapper, worker-local registry, new table, or new dependency.
- Strict normalized registration, heartbeat, event-batch, and native-receipt envelopes reject unknown fields/types. Identity, enrollment, capabilities, and liveness deadlines cannot be supplied by the machine. The raw HTTP-forwarded frame is capped at 16 KiB only for this dedicated connection. Complete outgoing envelopes use the actual Management API compact `json.dumps` encoding for the same cap, including non-ASCII expansion, author, room provenance, receipt fields, and wrapper. The public request remains capped at 8 KiB UTF-8.
- Session IDs use the existing deterministic scope/device/provider/profile/conversation derivation. Canonical session and user directory writes remain transactional. Session ownership cannot move to a different human on re-pair. Connection `connectedAt` and connection ID order replacements; old connections cannot register over, refresh, upload to, or receipt through the new binding. A generation change must name the previous generation. Transcript sequence and command sequence survive reconnect/generation changes.
- Event batches use the expected `afterSeq` and canonical session revision for serialization. Sequence numbers and stored timestamps are server-assigned. A batch digest/end sequence on its first event makes an identical retry idempotent without another row kind. Changing the payload at an already-used position conflicts. History has no TTL. Text-containing batches have a shared minimum 500 ms spacing (at most two/sec); tool/lifecycle-only batches can flush immediately. Each batch has 1–32 normalized events. The desktop must split UTF-8 safely and mark deliberately shortened output `truncated: true`.
- Durable command admission from Task 2 remains intact. The HTTP POST initiates delivery; admitted desktop registration, heartbeat, and receipt also drive progression. Public receipt GET remains read-only. A five-second pre-dispatch claim is reclaimable because no network operation occurs until a second transaction fences the claim with fresh room/actor/owner membership, enrollment, pairing, connection, and session revision checks. A final local time check after the transaction refuses to send if the DB call consumed the claim.
- Commands dispatch in `commandSeq` order. `dispatched` is committed before posting. A duplicate concurrent dispatcher cannot deliver the same logical request again. A definitive native receipt transitions to `applied` or `rejected`; `applied` means harness admission, with `held` and optional native provider message ID recorded separately. Late network results cannot overwrite a newer native receipt.
- Any attempted post with an ambiguous result, including `False`/Gone, becomes `unknown`: the underlying SDK may have retried an earlier ambiguous attempt. It is never automatically replayed or used to prune a socket. A dispatched command becomes unknown upon reconnect or deadline expiry when progression next runs. Unknown/dispatched commands block later delivery until a native journal receipt reconciles them. Pending commands expire to rejected at 30 seconds, including those behind a blocked command.
- A pre-network missing connection leaves pending work eligible for revalidation/retry before its deadline. No GET starts work. No background worker/sweeper was introduced. Desktop heartbeat or a subsequent POST/receipt advances stored states; an expired record can still display pending until one of those mutating drivers runs, but it cannot dispatch after expiry.
- Disconnect uses the expressly permitted bounded-expiry design: the existing Lambda deletes the connection row first. Session liveness expires after 30 seconds without a heartbeat; no deleted-row identity is reconstructed. An old disconnect only removes its own connection and cannot mark a replacement offline. Native history is retained.
- Resource hints use the existing AppSync publisher with `room.resources.changed`, payload exactly `{roomId}`. Discovery uses the existing membership GSI and rechecks canonical membership/scope. There is no owner-wide or private-user transcript fanout. The shared live-event union/parser registers the hint and rejects added payload fields. The existing DynamoDB projector ignores all actual `LOCALUSER#` and `LOCALSESSION#` data-row forms, now explicitly regression-tested.

## RED / GREEN evidence

All Python commands used per-command defense-in-depth credentials:

```sh
AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_SESSION_TOKEN=testing AWS_DEFAULT_REGION=us-east-1
```

Commands below ran from `apps/backend` unless noted. `uv run --offline` used already-installed dependencies.

1. Initial dedicated admission RED:

```sh
uv run --offline pytest ../infra/lambda/ws-connect/test_index.py --no-cov -q
```

Output: `1 failed, 21 passed in 0.42s`; dedicated local-session device got `403`, expected `200`.

2. Initial real-Moto transport RED:

```sh
uv run --offline pytest tests/unit/services/test_local_session_relay.py --no-cov -q
```

Output: `14 failed in 2.53s` because `core.services.local_session_relay` did not yet exist. The first router fixture also had an async-mock return-value error discovered on the first implementation run; that fixture was corrected to return the real ConnectionService. It was not counted as a product failure.

3. Post-wiring RED and GREEN iterations:

```sh
uv run --offline pytest tests/unit/services/test_local_session_relay.py --no-cov -q --tb=short
```

- `2 failed, 15 passed in 5.37s`: POST still returned pending; old-generation receipt on a new connection was rejected. Both corrected, then `17 passed in 5.09s`.
- Further tests: `3 failed, 18 passed in 6.03s`: a slow dispatch commit could consume the deadline before posting; reconnect lacked the blocking request needed to reconcile a journal; deferred HTTP-dispatch logging lacked its logger. Corrected.
- `52 passed in 6.56s` for relay + router + ws-connect after these fixes (21 relay, 9 router, 22 Lambda).
- Final ownership/admission RED: `3 failed, 44 passed in 6.95s`: re-pair could reassign session ownership; local handshake still returned 200 on backend 403/503. Corrected; `47 passed in 6.14s` for 23 relay + 24 ws-connect tests.

4. Shared hint contract RED/GREEN, from `packages/shared`:

```sh
pnpm test src/live/resourcesChanged.test.ts
```

RED: `1 failed`, parser returned null for `room.resources.changed`. GREEN: `1 passed` after registering the type and strict content-free payload.

All transport integration tests use actual Moto-backed messaging, container-pairing, and connection rows and actual DynamoDB transactions. Management API is stubbed at `post_to_connection`; the late-result interleaving test stubs the async network boundary so it can deliver a real receipt before returning an error. AppSync is stubbed at outbound HTTP. Revocation/expiry/crash interleavings retain real transactions.

## Final broader checks

```sh
AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_SESSION_TOKEN=testing AWS_DEFAULT_REGION=us-east-1 uv run --offline pytest tests/unit/services/test_local_sessions.py tests/unit/services/test_local_session_relay.py tests/unit/routers/test_websocket_chat.py tests/unit/services/test_connection_service_owner.py tests/unit/services/test_connection_service_errors.py tests/unit/services/test_connection_service_liveness.py tests/unit/services/test_connection_service_query.py tests/unit/services/test_management_api_client_errors.py tests/unit/services/test_messaging_service_review_findings.py tests/unit/repositories/test_messaging_repo.py ../infra/lambda/ws-connect/test_index.py --no-cov -q --tb=short
```

Result: **148 passed in 14.50s**. All Task 2 registry/admission and existing node/operator tests remain passing.

From `apps/infra/lambda/messaging-projector` (separate invocation avoids unrelated Lambdas' shared `index` module names):

```sh
AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_SESSION_TOKEN=testing AWS_DEFAULT_REGION=us-east-1 ../../../backend/.venv/bin/python -m pytest test_index.py --no-cov -q
```

Result: **17 passed in 0.22s**.

From `packages/shared`:

```sh
pnpm test src/live src/messaging
pnpm typecheck
pnpm exec eslint src/live/types.ts src/live/resourcesChanged.test.ts src/messaging/types.ts
```

Result: **32 tests passed in 6 files**, TypeScript passed, ESLint passed.

Scoped Ruff check of every touched backend Python source/test plus ws-connect Lambda files: **All checks passed**. Backend changed files were formatted with Ruff. `git diff --check`: passed. The pre-existing projector test style was preserved.

Read-only infrastructure inspection confirmed `DynamoDbOwnedTables` already grants `TransactWriteItems` and `ConditionCheckItem` over the env-owned messaging, pairing, and connection tables; no new infra policy is needed.

## Files

- New: `apps/backend/core/services/local_session_relay.py`.
- New: `apps/backend/tests/unit/services/test_local_session_relay.py`.
- New: `packages/shared/src/live/resourcesChanged.test.ts`.
- Modified: `apps/backend/core/repositories/local_sessions_repo.py`, `core/services/local_sessions.py`, `core/services/connection_service.py`, `core/services/messaging.py`.
- Modified: `apps/backend/routers/websocket_chat.py`, `routers/messaging.py`, `tests/unit/routers/test_websocket_chat.py`.
- Modified: `apps/infra/lambda/ws-connect/index.py`, `test_index.py`; `apps/infra/lambda/messaging-projector/test_index.py`.
- Modified: `packages/shared/src/live/types.ts`, `src/messaging/types.ts`.
- This report.

## Self-review and limitations

- Reviewed every caller of the changed registry/connection helpers and preserved the Task 2 server-internal registration contract. That internal API is not exposed as the machine wire; wire registration explicitly removes machine-supplied capability/deadline authority.
- Reviewed cross-worker claim races, claim crash recovery, revocation between check and write, raw-size versus encoded-size bounds, same-command duplicate delivery, lost acknowledgment, old-connection replacement, receipt regressions, generation reconciliation, expiration behind unknown, and pair reassignment. Added the failing tests listed above where review found missing guards.
- Compatibility is server-allowlisted only for Claude `2.1.268` and Codex `0.149.0`, capped to queue send, no steer or interrupt. `capabilitiesVerified` here means this reported version has server-known compatibility; it is not cryptographic proof of the running executable. Task 4 must verify the actual local runtime before reporting that identity or sending native input. Unknown versions remain visible but cannot accept control.
- No deployed two-worker/two-desktop demonstration, provider execution, actual receipt journal, or native approval handling was claimed. Those remain later release/native gates.
- History/receipt retention means the current command driver queries and sorts a session's command history. The deliberate bounded-scope simplification is marked with a ponytail comment; add an ordered pending index only if measured volume warrants it.
- Resource hints are best-effort and content-free. Authorized panel polling must repair missing hints and observe device disable/revocation or liveness expiry. No org-wide fallback was added.
- An AWS post cannot be atomic with a native side effect. Claims and durable unknown outcomes avoid blind server replay; they do not establish exactly-once native execution. Disconnect/revocation cannot undo already-admitted native work.

## Task 4 interfaces

Use existing signed paired **mac** proof, `clientId=lightbulb-local-sessions`. The optional node-host device enrollment is not the credential for this socket. Sharing must already be human-enabled through Task 2's enrollment endpoint.

Client → server:

```json
{"type":"local.session.register","session":{"provider":"codex","profileFingerprint":"<64 lowercase hex>","nativeConversationId":"<opaque id>","generation":"generation-1","providerVersion":"0.149.0","status":"busy","activeTurnId":"turn-1"}}
{"type":"local.session.heartbeat","sessionId":"<uuid>","generation":"generation-1","status":"idle","activeTurnId":null}
{"type":"local.session.events","sessionId":"<uuid>","generation":"generation-1","batchId":"batch-1","afterSeq":0,"events":[{"kind":"text","role":"assistant","text":"hello","truncated":false}]}
{"type":"local.session.receipt","sessionId":"<uuid>","generation":"generation-1","requestId":"request-1","outcome":"applied","providerMessageId":"native-1","held":true}
```

- Generation changes add top-level `previousGeneration` to registration. Same generation reconnect omits it. Persist previous generation locally so replacing the execution is deliberate; stale-generation uploads fail. Status is one of idle/busy/waiting-approval/offline. Runtime-specific status observation still belongs to Task 4.
- Heartbeat about every 10 seconds; the server owns the 30-second lease. Never send `onlineUntil`, capabilities, verified flags, owner/human/device overrides, paths, provider configuration, credentials, arbitrary native RPC, or hidden reasoning.
- Event kind: text/tool/lifecycle; role: user/assistant/system/tool. Retry the identical batch at its original `afterSeq`; batching identity is the expected log position plus digest (which includes batch ID and normalized content). Use registered/heartbeat `afterSeq` to recover the current server cursor. Do not change a retry's content or advance its position and pretend it is the same upload.
- Receipts use `applied`, `rejected`, or `unknown`; held input is separate. An old execution's journal can be reconciled from the current connection by naming the old command generation. Terminal applied/rejected receipts cannot be regressed by conflicting duplicates. No receipt may claim an undispatched pending command was applied.

Server → client:

```json
{"type":"local.session.registered","sessionId":"<uuid>","generation":"generation-1","afterSeq":0,"reconcile":null}
{"type":"local.session.heartbeat.ack","sessionId":"<uuid>","generation":"generation-1","afterSeq":1,"reconcile":{"requestId":"request-1","generation":"generation-1","state":"unknown"}}
{"type":"local.session.events.ack","sessionId":"<uuid>","batchId":"batch-1","afterSeq":1}
{"type":"local.session.receipt.ack","sessionId":"<uuid>","requestId":"request-1","state":"applied"}
{"type":"local.session.error","code":"LOCAL_SESSION_CONFLICT"}
```

Commands arrive as `{"type":"local.session.command","command":<LocalSessionCommandReceipt>}`. The receipt contains server-authored actor, room, command sequence, creation/delivery times, the original request/generation/text, state `dispatched`, held false, and providerMessageId null. These are the shared `LocalSessionCommandReceipt` fields; no native process addresses or permission controls appear.

Task 4 MUST journal/deduplicate stable request IDs and payload identity, arbitrate by command sequence alongside native keyboard input, recheck `deliverBy` and generation at native admission, and reconcile an uncertain result instead of automatically sending it again. Management API/SDK retries can deliver duplicate frames or deliver a frame after the backend's initial time check. A missing local journal entry alone is insufficient to reject unknown while an older delivery might still be in flight: use the native journal/deadline/tombstone protocol to keep late frames from executing. Keep the request unknown when actual native admission cannot be proven. The transport does not manufacture exactly-once guarantees.

The register/heartbeat `reconcile` field exposes only one blocking request ID/generation/state, with no transcript or room text. A native receipt unblocks subsequent authorized pending commands. Human callers poll the existing receipt GET; it deliberately performs no dispatch or durable mutation.

## Review fix round 1 — retry session revision contention

Base: `6f920f55a9daca404db88a9c8ab229a660205b24`.

Independent review reproduced valid input becoming permanently rejected when a lifecycle upload changed the session revision between the dispatch snapshot and authorization. `_dispatch_checks` had combined this harmless contention with substantive identity/control failures under the same `ApiProblem`; both pre-claim and post-claim callers rejected it.

The helper now returns a distinct retry result for a revision-only mismatch after checking current authorization, capability, generation, connection identity and deadlines. The pre-claim path immediately retries its bounded loop with a fresh snapshot. The post-claim path first releases only its own revision-fenced claim, then retries. No network attempt has happened at either point. A concurrent worker's newer claim/outcome cannot be cleared by that release. Command sequence and delivery deadline are unchanged; repeated contention can leave the command pending for the existing drivers rather than terminally reject it. Substantive failures retain their existing rejection behavior.

Added a parameterized real-Moto regression covering both phases. It injects an actual normalized lifecycle upload immediately before the real authorization read. Each case verifies the upload persisted, the first command dispatched exactly once, and the next command remained pending in order.

Exact RED/GREEN command, from `apps/backend`:

```sh
AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_SESSION_TOKEN=testing AWS_DEFAULT_REGION=us-east-1 uv run --offline pytest tests/unit/services/test_local_session_relay.py -k revision_contention --no-cov -q --tb=short
```

RED: **2 failed, 23 deselected in 3.40s**. Both phases reported `rejected` instead of `dispatched`.
GREEN: **2 passed, 23 deselected in 1.11s**.

Covering check:

```sh
AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_SESSION_TOKEN=testing AWS_DEFAULT_REGION=us-east-1 uv run --offline pytest tests/unit/services/test_local_sessions.py tests/unit/services/test_local_session_relay.py tests/unit/routers/test_websocket_chat.py tests/unit/services/test_connection_service_owner.py tests/unit/services/test_connection_service_errors.py tests/unit/services/test_connection_service_liveness.py tests/unit/services/test_connection_service_query.py tests/unit/services/test_management_api_client_errors.py tests/unit/services/test_messaging_service_review_findings.py tests/unit/repositories/test_messaging_repo.py ../infra/lambda/ws-connect/test_index.py --no-cov -q --tb=short
uv run --offline ruff format core/services/local_session_relay.py tests/unit/services/test_local_session_relay.py
uv run --offline ruff check core/services/local_session_relay.py tests/unit/services/test_local_session_relay.py
git diff --check
```

Results: **150 passed in 17.10s**, scoped Ruff passed, diff check passed. Existing lease, ordering, expiry, revoked membership/enrollment/pairing, reconnect and uncertain-outcome checks still pass.

Self-review checked both helper consumers, bounded retries, post-claim release CAS, preserved original deadlines/order, and the existing substantive rejection branches. Only the relay, its focused test file, and this report changed. No protocol/Task 4 interface change and no additional concerns beyond the native/deployment limitations already documented. No external actions or provider changes.
