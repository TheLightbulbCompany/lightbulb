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
