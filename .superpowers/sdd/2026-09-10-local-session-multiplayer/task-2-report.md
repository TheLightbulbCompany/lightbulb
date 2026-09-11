# Task 2 — room-scoped local-session registry and authorization

Status: DONE. Base: `a079bb9ab06df64bb2fe54f8de70106eb45e306d`.
Worktree: `.worktrees/local-session-native-probe`, branch `codex/local-session-native-probe`.
Implementation and tests are local only. No real account enrollment, AWS mutation, provider change, push, merge, or deployment occurred.

## Implementation

- Added `local_sessions_repo.py` using the existing messaging DynamoDB resource, strong reads/query helper and transaction helper. Uses exactly the `LOCALUSER#scope#human` and `LOCALSESSION#scope#session` partitions in the task contract. UUIDv5 identity hashes the scope, mac device identity, provider, profile fingerprint and native conversation ID. Generation is separate. Canonical and directory writes are atomic and CAS-protected; command ordering survives re-registration.
- Added the local-session service with the common `authorize_session` path for directory hydration, events, command submission and receipts. It checks the authenticated organization policy through unchanged `resolve_owner_id`, `require_room_member`, the session owner's current canonical human membership, enrollment and current authoritative pairing. There is no admin bypass. A session can remain accessible through another eligible room after removal from one room.
- Human enrollment accepts the mac `deviceId` plus optional `cliDeviceId`. It validates the caller's ownership and expected `openclaw-macos`/`node-host` client IDs on the container's strong `paired_devices` read, then checks the same pairing atomically when writing enrollment. This is the controller-approved explicit association; it never guesses sibling IDs from map order. Responses contain only these enrolled IDs and the owning human; no pairing keys/public keys. Disabling removes resources from authorized responses and blocks register/read/control without deleting native/canonical history or changing node permissions.
- Added internal-only `register_session`; it cannot self-enable sharing and is not mounted as a machine endpoint. Provider/profile/native IDs are bounded, extra properties forbidden, profile is a 64-hex fingerprint rather than a raw path. Unverified capabilities default to unavailable and do not inherit trust across registration/provider upgrades.
- Added HTTP room resources, paginated normalized events, durable command admission (`202`, always initially `pending`) and room-bound command receipts, plus `PUT /desktop/local-sessions/enrollment` under the existing Clerk-authenticated router.
- Admission binds the canonical request hash to scope/session/room/actor and request input. Reusing an ID with changed text, actor or route conflicts. Session sequence allocation, requester and owner membership, room scope, enrollment revision, authoritative pairing, session generation/revision and liveness deadline are checked in one real cross-table DynamoDB transaction. Read/transaction races retry by re-authorizing, never by trusting directory projections. A same-human actor uses one membership condition to avoid DynamoDB's duplicate-item prohibition.
- Capabilities distinguish queue-only input from immediate input and turn-specific steering/interruption. Unsupported operations, old generations, stale active turns and offline devices fail closed. Commands have a 30-second `deliverBy`, not a TTL. No command is represented as dispatched/applied by Task 2.
- Command canonical JSON is capped at **8 KiB UTF-8**, per the controller's final ruling, reserving framing/attribution space under the 16 KiB transport ceiling. Normalized event DTO JSON is capped at **16 KiB UTF-8**, accepts text/tool/lifecycle content with explicit truncation and rejects arbitrary raw fields. HTTP event pages have 1–100 items and a sequence cursor.
- Added shared TypeScript DTOs/client methods and public exports. Local sessions are not `MemberKind` and do not enter the company roster. No Paperclip/Friday/UI features were added.
- Extended existing `messaging_repo.delete_owner_partition` to delete retained local rows during the existing account wipe. The controller approved this small additional source touch. Exact owner-delimited prefixes, real filtered scan pagination (including empty pages), idempotent retry and preservation of another similarly prefixed owner are covered. A deletion-only scan is explicitly marked with its scale ceiling.

## Files

New:

- `apps/backend/core/repositories/local_sessions_repo.py`
- `apps/backend/core/services/local_sessions.py`
- `apps/backend/tests/unit/services/test_local_sessions.py`

Modified:

- `apps/backend/core/repositories/messaging_repo.py`
- `apps/backend/core/services/messaging.py`
- `apps/backend/routers/messaging.py`
- `apps/backend/routers/desktop_pair.py`
- `apps/backend/tests/unit/repositories/test_messaging_repo.py`
- `apps/backend/tests/unit/routers/test_desktop_pair_router.py`
- `packages/shared/src/messaging/types.ts`
- `packages/shared/src/messaging/client.ts`
- `packages/shared/src/messaging/client.test.ts`
- `packages/shared/src/index.ts`

## RED/GREEN evidence

Commands below use the backend directory unless a root command is specified.

1. **Hermetic fixture first**, before running old desktop router tests: replaced the `main.app`/full-lifespan fixture with a minimal FastAPI instance, current organization identity and a mock at the workspace adapter boundary. Production auth policy remains unchanged.

   `uv run --offline pytest tests/unit/routers/test_desktop_pair_router.py -q --no-cov`

   Result: **6 passed in 0.41s**.

2. Wrote authorization/enrollment/identity/command/transaction/pagination/HTTP tests before source implementation.

   `uv run --offline pytest tests/unit/services/test_local_sessions.py -q --no-cov --tb=short`

   RED: **26 failed in 2.59s**, `AssertionError: local session service is missing`. This was an explicit temporary discovery assertion so absent source produced failures, not collection-import errors. After implementation the discovery helper was removed in favor of ordinary imports.

   GREEN with the same command: **26 passed in 3.44s**.

3. Added shared client and owner-deletion regressions before implementing those changes.

   Root: `pnpm --filter @lightbulb/shared test -- src/messaging/client.test.ts`

   RED: **1 failed, 3 passed**, `client.listResources is not a function`.

   `uv run --offline pytest tests/unit/repositories/test_messaging_repo.py::test_wipe_deletes_room_partitions_owner_rows_and_streams_only_for_that_owner -q --no-cov --tb=short`

   RED: local owner rows remained; **6 rows found instead of 3**. GREEN was included in the focused suites below. Shared GREEN: **4 passed**.

4. Self-review found a receipt-validation edge: an accepted near-limit request could commit then fail because inherited validation counted added receipt fields against the request budget. The request size check now explicitly measures only canonical command fields.

   `uv run --offline pytest tests/unit/services/test_local_sessions.py::test_maximum_valid_command_returns_receipt_after_admission -q --no-cov --tb=short`

   RED at the then-16 KiB bound: a 16,200-character ASCII command was valid, but returning `CommandReceipt` raised `ValidationError: command exceeds 16 KiB`. GREEN after correction. The final bound is now 8 KiB; the retained regression admits and reads back an 8,000-character command.

5. Event UTF-8 boundary regression:

   `uv run --offline pytest tests/unit/services/test_local_sessions.py::test_normalized_event_envelope_is_utf8_bounded_and_forbids_raw_payloads -q --no-cov --tb=short`

   RED: a 5,000-emoji event was accepted (`DID NOT RAISE ValidationError`). GREEN after validating serialized UTF-8 bytes.

6. Final controller limit ruling, test first:

   `uv run --offline pytest tests/unit/services/test_local_sessions.py::test_command_reserves_space_for_server_framing_inside_wire_envelope -q --no-cov --tb=short`

   RED: a request below 16 KiB but above the reserved 8 KiB budget was accepted (`DID NOT RAISE ValidationError`). GREEN after the final 8 KiB canonical-request cap. Tests cover ASCII overhead and multibyte text.

## Final verification

Final focused backend command:

```sh
uv run --offline pytest tests/unit/services/test_local_sessions.py tests/unit/repositories/test_messaging_repo.py tests/unit/routers/test_desktop_pair_router.py -q --no-cov --tb=short
```

**60 passed in 7.45s** (32 local-session, 22 messaging repository, 6 desktop router).

Broader backend regression run before the final limit tightening/pagination-test extension:

```sh
uv run --offline pytest tests/unit/services/test_local_sessions.py tests/unit/repositories/test_messaging_repo.py tests/unit/routers/test_desktop_pair_router.py tests/unit/services/test_messaging_service_review_findings.py tests/unit/services/test_desktop_pair.py tests/unit/services/test_desktop_pair_live.py tests/unit/repositories/test_container_repo.py tests/unit/services/test_account_wipe.py tests/unit/routers/test_messaging_run_status.py -q --no-cov --tb=short
```

**268 passed in 22.48s**. The final changed boundaries were rerun in the focused 60-test suite.

Root checks:

- `pnpm --filter @lightbulb/shared test -- src/messaging/client.test.ts`: **4 passed**.
- `pnpm --filter @lightbulb/shared test`: **136 passed across 17 files**.
- `pnpm --filter @lightbulb/shared typecheck`: **passed**.
- `pnpm --filter @lightbulb/shared exec eslint src/messaging/client.ts src/messaging/types.ts src/messaging/client.test.ts src/index.ts`: **passed**.
- `git diff --check`: **passed**.
- `uv run --offline ruff check` on all 9 touched Python source/test files: **All checks passed**.
- `uv run --offline ruff format --check` on those 9 Python files: **9 files already formatted**.

## Self-review and limits

- Reviewed canonical lookup vs projection, membership removal vs multi-room access, pair ownership vs explicit sibling association, membership/paired-device/enrollment/generation races, idempotency/actor provenance, per-session ordering, pagination, schema allowlists, multibyte limits, retention/deletion and the current organization-only auth path.
- New transaction tests use real Moto tables and actual DynamoDB transaction conditions. Six command-admission races delete/mutate authority after reads and prove no receipt or sequence increment lands. Registration rollback, provider upgrade behavior, the session owner issuing their own command, a machine-token enrollment attempt and a revoked owner remaining in the directory are covered.
- Existing messaging stream projector accepts only `MSG#` and `LIVE#`; all added sort-key prefixes are ignored. Controller also checked existing IAM already covers cross-table `TransactWriteItems`/`ConditionCheckItem`. No infra changes are needed.
- **Task 3 handoff:** this task intentionally has no machine socket, event ingestion/rate scheduler, dispatcher/claim/outcome journal, or realtime invalidations. The internal registration identity must come from a verified connection; transport must derive/bound heartbeat liveness using server time and verify capabilities for the reported provider version. It must recheck current membership/enrollment/pairing before dispatch, honor `deliverBy`, reject stale generations/turns, never blindly replay dispatched work, and validate the **complete final wire envelope** at 16 KiB. The 8 KiB command cap alone is not that validation.
- Mac/CLI enrollment is an explicit human-approved association of authoritative same-human paired entries, not cryptographic proof of OS co-location. Stale DDB pair siblings are not guessed/exposed. Underlying OpenClaw node scope is unchanged; this task does not claim an OS/node sandbox.
- History is retained without TTL until account/workspace deletion; optional explicit per-session deletion is not a new endpoint in this task. Cleanup is a paginated table scan at account deletion; hydration is per-human strong queries within bounded room membership. Both ceilings are noted in code instead of adding infrastructure.
- Commands remain durable `pending` with a deadline until Task 3 processes them; no live native steering was demonstrated or claimed.

## Test noise

- The first new-suite attempt used pytest's reserved parameter name `request`, causing one collection error. It was renamed before establishing RED; no source implementation existed yet.
- Initial local lint found one unused test import and formatting changes in five touched files. Removed/formatted; final checks are clean.
- An initial near-limit receipt probe at 16,000 characters passed; increasing it to 16,200 exposed the actual old-bound receipt bug. Both the failure and final tightened-bound regression are documented above.
- No final test failures or warnings. No full application lifespan or live AWS account was used by the new/hermetic tests.
