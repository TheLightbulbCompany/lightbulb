# Native multiplayer AWS foundation implementation plan

> **For agentic workers:** use `superpowers:subagent-driven-development` when
> executing this packet. Keep this PR limited to AWS resources, their Lambda, and
> service wiring; do not implement backend room behavior or UI.

**Goal:** Provision the canonical messaging table, secured AppSync Event API,
durable OpenClaw work queue, direct DynamoDB-to-SQS Pipe, live projector,
permissions, configuration, and alarms required by the frozen multiplayer
contract.

**Architecture:** `POST` handlers transact canonical data and `WORK` outbox rows
into DynamoDB. One EventBridge Pipe sends work rows directly to FIFO SQS with
at-least-once delivery. One independent Lambda publishes `MSG` and `LIVE` rows to
AppSync. Web/mobile subscribes with Clerk OIDC; backend/Lambda publishes with IAM.

**Tech stack:** AWS CDK TypeScript, DynamoDB, AppSync Events, Lambda Python 3.13,
SQS FIFO, KMS, CloudWatch, Vitest, Python `unittest`.

**Spec:**
`docs/superpowers/specs/2026-09-09-native-multiplayer-messaging-architecture.md`

## Ownership and prerequisites

This packet owns:

- `apps/infra/package.json`
- `apps/infra/package-lock.json` only for the CDK pin
- `pnpm-lock.yaml` only for the `apps/infra` CDK importer and resolved graph
- `apps/infra/lib/stacks/api-stack.ts`
- `apps/infra/lib/stacks/service-stack.ts`
- `apps/infra/lib/lightbulb-stage.ts`
- `.github/workflows/deploy.yml` (generated asset inventory only)
- `apps/infra/scripts/replay-openclaw-work-pipe-failure.py`
- `apps/infra/lambda/messaging-projector/`
- `apps/infra/lambda/ws-connect/` browser branches and focused tests
- `apps/infra/lambda/websocket-authorizer/` Clerk-browser branch and focused tests
- `apps/infra/test/api-stack.test.ts`
- `apps/infra/test/service-stack.test.ts`

It must not edit backend Python application code, OpenClaw plugin code, shared
frontend code, or `openclaw-version.json`.

Start from Lightbulb `origin/main` at or after
`00b8b48d5671d5fc8d4d549ab2748151a07a1a9c`. Preserve the existing API Gateway
WebSocket resources for paired desktop nodes and Paperclip operator connections.

## Frozen resource contract

Use these physical names through the existing `tablePrefix`/`resourcePrefix`
helpers:

- DynamoDB table: `lightbulb-{env}-messaging`
- FIFO queue: `lightbulb-{env}-openclaw-work.fifo`
- FIFO DLQ: `lightbulb-{env}-openclaw-work-dlq.fifo`
- Pipe failure queue: `lightbulb-{env}-openclaw-work-pipe-failures`
- projector failure bucket: `lightbulb-{env}-messaging-projector-fail-<account>-<region>`
  (the shortest spelling that keeps env/account/region and fits S3's 63-character limit)
- work Pipe: `lightbulb-{env}-openclaw-work`
- Lambda: `lightbulb-{env}-messaging-projector`
- AppSync Event API: `lightbulb-{env}-messaging-events`
- channel namespaces: `rooms`, `users`

Backend task environment:

```text
MESSAGING_TABLE=lightbulb-{env}-messaging
OPENCLAW_WORK_QUEUE_URL=<deployed FIFO URL>
APPSYNC_EVENTS_HTTP_ENDPOINT=<https endpoint ending /event>
APPSYNC_EVENTS_REALTIME_ENDPOINT=<wss endpoint ending /event/realtime>
APPSYNC_EVENTS_REGION=<stack region>
```

## Task 1: Pin the CDK release that supports Event API data-source handlers

The current `aws-cdk-lib` 2.190.0 exposes `EventApi`, but its L2
`ChannelNamespaceOptions` cannot attach DynamoDB-backed `onSubscribe` handler
configuration. Version 2.267.0 exposes `addDynamoDbDataSource`,
`subscribeHandlerConfig`, and `publishHandlerConfig` and was published before the
repository's seven-day dependency-age boundary.

- [ ] In `apps/infra/package.json`, change `aws-cdk-lib` from `2.190.0` to
  exact `2.267.0` and `aws-cdk` CLI from `2.1013.0` to exact `2.1141.0`, the
  minimum CLI that supports the cloud-assembly schema emitted by that library.
- [ ] Update `apps/infra/package-lock.json` with `npm install
  --package-lock-only --ignore-scripts`, then update only the `apps/infra`
  importer and required CDK graph in the root `pnpm-lock.yaml`. Infra deploy uses
  `npm ci`, while `infra-ci` first runs root `pnpm install --frozen-lockfile`, so
  both locks must agree. Do not upgrade unrelated transitive packages.
- [ ] Add a synth assertion to `apps/infra/test/api-stack.test.ts` that references
  an `AWS::AppSync::ChannelNamespace` with `HandlerConfigs.OnSubscribe` and its
  DynamoDB data source. Before the pin/resource work it must fail because the
  resource is absent.
- [ ] Push the commit and require `infra-ci` to pass `pnpm --filter
  @lightbulb/infra build` and `pnpm --filter @lightbulb/infra test`. Per the
  founder instruction, do not spend time duplicating CI locally.

Commit as `infra: pin CDK for AppSync Events handlers`.

## Task 2: Add the messaging table and one durable work queue

- [ ] In `apps/infra/lib/stacks/api-stack.ts`, create the `messaging` table in
  this stack so the same construct can own its stream event source without a
  cross-stack stream-ARN export.
- [ ] Give it string partition key `pk`, string sort key `sk`, on-demand billing,
  the existing customer-managed KMS key, point-in-time recovery, environment
  removal policy (`DESTROY` dev, `RETAIN` prod), and stream view
  `NEW_IMAGE`. Enable numeric epoch-seconds TTL attribute `expiresAt`; only
  one-shot `WORK#` and `LIVE#` outbox rows use it.
- [ ] Add sparse GSI `by-member` with string keys `memberKey` and `memberSort` and
  projection `ALL`. Authorization must never query this GSI.
- [ ] Create the FIFO work queue and FIFO DLQ. Set 14-day DLQ retention, 7-day
  source retention, 5-minute visibility, 20-second long polling,
  `contentBasedDeduplication: false`, and KMS encryption with the existing CMK.
  Set `maxReceiveCount: 8`.
- [ ] Create a standard Pipe failure queue with 14-day retention and the same CMK.
  It receives failed `WORK` records after the Pipe retry policy; it is not an
  application work queue.
- [ ] Create a private, CMK-encrypted, lifecycle-expiring S3 bucket for the live
  projector's complete failed stream records. Block all public access, retain it
  in production, and expire failure objects after 30 days.
- [ ] Extend `apps/infra/test/api-stack.test.ts` to assert table keys, stream,
  GSI, PITR, on-demand mode, CMK, `expiresAt` TTL, queue type, DLQ redrive,
  retention, visibility, long polling, and private failure bucket. Assert there
  is exactly one new application FIFO queue.

Commit as `infra: add canonical messaging storage and work queue`.

## Task 3: Add OIDC-subscribe/IAM-publish AppSync Events

- [ ] Add `clerkIssuerSecretName` to `ApiStackProps` and pass
  `lightbulb/{env}/clerk_issuer` from `apps/infra/lib/lightbulb-stage.ts`.
- [ ] Import that secret by name in `api-stack.ts`; use its dynamic value as the
  AppSync OIDC issuer. Do not log or output the issuer token value.
- [ ] Before deploy, require the environment's Clerk instance to contain the JWT
  template `lightbulb-appsync` with claims
  `{"aud":"lightbulb-appsync"}`, 60-second lifetime, and five-second clock skew.
  The rollout packet creates/verifies it through Clerk's Backend API using the
  existing environment secret; CDK never receives the Clerk secret key.
- [ ] Create an `appsync.EventApi` whose configured providers are OIDC and IAM.
  Configure the OIDC issuer plus client-ID regex `^lightbulb-appsync$`.
  Connect/subscribe defaults are OIDC; publish defaults are IAM. Do not add an API
  key, Cognito identity pool, Lambda authorizer, or browser IAM credentials.
- [ ] Add the messaging table as one AppSync DynamoDB data source.
- [ ] Add namespace `rooms` with OIDC subscribe and IAM publish. Its
  `onSubscribe` AppSync JavaScript handler must:
  1. require exactly `/rooms/<roomUuid>/<deliveryUuid>`;
  2. require an OIDC `ctx.identity.sub`;
  3. `BatchGetItem` with `consistentRead: true` for
     `ROOM#<roomId>/META` and
     `ROOM#<roomId>/MEMBER#human#<sub>`; and
  4. call `util.unauthorized()` unless membership exists and
     `META.deliveryKey` equals the channel key.
- [ ] Give `rooms` an IAM `onPublish` handler backed by the same DynamoDB data
  source. It requires the exact room path, strongly reads `META`, and rejects a
  publish unless the path key equals the current `deliveryKey`. This makes a
  cached backend/projector key fail closed after revocation.
- [ ] Add namespace `users` with OIDC subscribe and IAM publish. Its handler
  requires exactly `/users/<channelUuid>`, strongly reads
  `STREAM#<channelUuid>/META`, and denies unless its `userId` equals
  `ctx.identity.sub` and `active` is true.
- [ ] Give `users` an IAM `onPublish` handler backed by the same table. It
  strongly reads the reverse mapping and rejects publishing unless that exact
  channel remains active.
- [ ] Use UUID-format path validation because AppSync limits path segments to
  alphanumeric/dash and 50 characters. Do not place raw Clerk IDs in paths.
- [ ] Leave AppSync Event API logs disabled. Unlike GraphQL `LogConfig`, Event
  `EventLogConfig` has no verbose-content exclusion, and `ERROR` still records
  failed handler request/response mappings. Use AppSync metrics and the existing
  delivery alarms so credentials, identities, and message bodies never reach logs.
- [ ] Output HTTP endpoint, realtime endpoint, API ID, and API ARN for deployment
  evidence. Never output a token or room/user channel key.
- [ ] Add synth assertions for auth providers, connect/publish/subscribe auth
  modes, both namespace handlers, DynamoDB integration role, and the absence of
  API-key resources.

Commit as `infra: add secured AppSync messaging events`.

## Task 4: Implement the live stream projector

Create:

- `apps/infra/lambda/messaging-projector/index.py`
- `apps/infra/lambda/messaging-projector/test_index.py`

The handler contract is:

```python
def handler(event: dict, context: object) -> dict:
    return {"batchItemFailures": [{"itemIdentifier": sequence_number}, ...]}
```

- [ ] Parse only DynamoDB `INSERT` records whose new-image `sk` begins `MSG#` or
  `LIVE#`. Ignore updates, removes, `WORK#`, and other inserts successfully.
- [ ] For `MSG#`, validate the canonical public payload, strong-read current room
  `META`, and publish `room.message.created` to
  `/rooms/<roomId>/<currentDeliveryKey>`. Never use a historical delivery key
  stored on a message.
- [ ] For `LIVE#`, strongly read `STREAM#<channelUuid>/META`; treat an inactive,
  missing, or replaced mapping as a successful no-op, otherwise publish the
  stored frozen event to that current private user channel. Validate that all
  event/channel fields are UUID-safe and the serialized event is no larger than
  230 KiB.
- [ ] Sign the exact AppSync HTTP request with the Lambda's current role
  credentials using botocore SigV4 service `appsync`; never embed AWS credentials.
  Treat a response as successful only when HTTP status is 2xx,
  `successful[0]` exists, and `failed` is empty.
- [ ] Unit-test ignored records, stale room-key avoidance, inactive/replaced user
  stream suppression, room/user projection, SigV4 request shape, HTTP-200
  per-event failure, partial batch failure, payload cap, replayed successes after
  a failure, and malformed records using fakes. The tests make no network calls.
- [ ] In `api-stack.ts`, create the Python 3.13 Lambda with 30-second timeout and
  512 MiB memory; grant table read, failure-bucket/KMS use, and AppSync
  `EventPublish` only. It receives no SQS permission.
- [ ] Attach a DynamoDB event source with partial batch responses,
  bisect-on-error, batch size 10, zero-second batching window, maximum record age
  23 hours, three retries, and the S3 bucket as the on-failure destination. A
  partial failure checkpoints at the lowest failed sequence and may replay later
  successes; all event IDs must therefore remain idempotent.
- [ ] Add `apps/infra/lambda/messaging-projector/replay.py`, which accepts one
  explicit S3 failure-object URI, extracts the original DynamoDB records, invokes
  the same record processor, and deletes nothing. Its output reports only record
  IDs and success/failure. This is the alarm runbook, not a scheduled sweeper.

Commit as `infra: project canonical messages to live events`.

## Task 5: Pipe durable work outbox rows to FIFO SQS

- [ ] Create an `AWS::Pipes::Pipe` from the messaging table stream to
  `openclaw-work.fifo`. Filter only DynamoDB `INSERT` records whose `sk.S`
  has prefix `WORK#` with the exact EventBridge pattern
  `{"eventName":["INSERT"],"dynamodb":{"NewImage":{"sk":{"S":[{"prefix":"WORK#"}]}}}}`;
  use batch size 10, zero-second window, starting position
  `TRIM_HORIZON`, `AUTOMATIC_BISECT`, maximum record age 23 hours, and 185
  retries. Historical work rows are safe because outbox IDs, SQS deduplication,
  delivery cursors, and operation terminal states are idempotent; do not use
  `LATEST`, which can miss records while stream polling becomes consistent.
- [ ] Store the native worker pointer under each outbox row's DynamoDB `payload`
  map. Use no input template: an object placeholder such as
  `<$.dynamodb.NewImage.payload.M>` renders as Java-map text, not JSON. SQS
  receives the raw stream record and the backend worker deserializes its
  `dynamodb.NewImage.payload.M` to exactly one of the two frozen schemas.
  Configure FIFO `MessageGroupId` and `MessageDeduplicationId` with dynamic JSON
  paths `$.dynamodb.NewImage.messageGroupId.S` and
  `$.dynamodb.NewImage.messageDeduplicationId.S`; do not derive them in the Pipe
  transformation.
- [ ] Give the Pipe role only DynamoDB Stream read, source-CMK decrypt, target
  queue `SendMessage`, standard failure-queue `SendMessage`, and the queue CMK's
  `kms:Decrypt` plus `kms:GenerateDataKey` required by SSE-KMS producers.
  Configure the standard queue as its DLQ.
- [ ] Add synth assertions for the filter, transformation, FIFO dynamic paths,
  retry/age policy, DLQ, `TRIM_HORIZON`, KMS data-key permission, and
  least-privilege role. The direct Pipe is the only producer granted
  `SendMessage` to the work queue.
- [ ] Assert that the messaging stream has exactly two consumers in this phase:
  the AppSync projector event-source mapping and this work Pipe. Add no Lambda
  relay between the stream and FIFO queue.
- [ ] Add a fail-closed replay script that takes the two explicit queue URLs and
  one Pipe-DLQ message ID, verifies the embedded source event is one `WORK#`
  insert, re-sends the record as the body with the stored FIFO IDs,
  and deletes the DLQ message only after successful enqueue. It never prints the
  pointer or source event. Test malformed/non-work events and send-before-delete.

Commit as `infra: pipe durable OpenClaw work to FIFO`.

## Task 6: Wire the backend task without broadening IAM

- [ ] Add explicit string props to `ServiceStackProps` for the messaging table
  name, work queue URL/ARN, AppSync HTTP endpoint, AppSync realtime endpoint, API
  ARN, and region.
  Pass them from `lightbulb-stage.ts`.
- [ ] Inject the frozen environment names into the backend container.
- [ ] Grant the backend task role `ReceiveMessage`, `DeleteMessage`,
  `ChangeMessageVisibility`, and `GetQueueAttributes` on only the work queue.
- [ ] Do not grant the backend task or live projector `sqs:SendMessage`; work is
  committed as DynamoDB outbox rows and the Pipe is the sole queue producer.
- [ ] Grant `appsync:EventPublish` on only the two messaging channel namespaces.
- [ ] Ensure the existing DynamoDB name-scoped policy covers the new table; add
  only the missing transaction/stream-independent actions. The backend does not
  receive `dynamodb:DescribeStream` or AppSync subscribe permission.
- [ ] Add service-stack tests for env values, queue consume permissions, AppSync
  publish-only permissions, both Event API endpoints, and the continued presence
  of the existing machine WebSocket env/permissions.
- [ ] Remove Clerk browser authorization from `websocket-authorizer` and remove
  `chat`/`native_human` classification and storage from `ws-connect`. Accept only
  the existing paired node and signed Paperclip operator identities. Keep the
  API Gateway, connection table, disconnect/default routes, and permissions those
  two machine protocols still use.
- [ ] In `ws-connect`, derive connection type from the authenticated authorizer
  context and require the untrusted query `clientId` to match it exactly:
  `authKind=device` permits only `node-host` or `openclaw-macos` and stores `node`;
  `authKind=paperclip_service` permits only `paperclip-operator` and stores
  `operator`. Reject missing, unknown, Clerk, and cross-kind combinations before
  writing a row. Add the signature-verified device ID to the device authorizer
  context, then forward it, the verified auth kind, and validated client ID through
  the internal-secret-authenticated backend notification. The backend asserts the
  same mapping before registering relay state; neither side classifies solely from
  the query string.
- [ ] Add focused tests proving Clerk/query-token browser connections are denied,
  paired nodes and Paperclip operators remain admitted, auth-kind/client-ID
  mismatches fail closed, the backend notification carries verified provenance,
  and no new connection row can have type `chat` or `native_human`.

Commit as `infra: wire messaging resources to backend`.

## Task 7: Regenerate deployment workflow, add alarms, and finish the PR

- [ ] Add CloudWatch alarms for work-queue DLQ visible messages, work-queue oldest
  age over five minutes, Pipe DLQ visible messages/failures/stopped state,
  projector errors/S3 failures, and DynamoDB stream iterator age over five
  minutes. Route them to the existing environment alert topic using the repository
  helper; do not create another SNS topic.
- [ ] Run the repository CDK synth twice because the generated deployment
  workflow enumerates Lambda file assets. The second synth must make no changes.
  Commit the converged `.github/workflows/deploy.yml`; otherwise the new projector
  asset can retain an obsolete artifact-download job.
- [ ] Update infra tests to assert alarms and actions.
- [ ] Push the final branch and wait for `infra-ci`; fix only failures introduced
  by this packet.
- [ ] In the PR description include the synthesized physical names, environment
  contract, IAM boundary, and explicit statement: "API Gateway WebSocket remains
  only for node/operator machine relays; all browser admission is removed here."

Commit as `infra: alarm multiplayer delivery failures`.

## Acceptance evidence

The PR is ready to merge when CI proves:

```text
@lightbulb/infra build: pass
@lightbulb/infra test: pass
messaging-projector unittest: pass
```

The synthesized template must show one messaging table, one application FIFO
queue plus its DLQ, one direct Pipe plus its standard DLQ, one live-projector
failure bucket, one Event API, two namespaces, and no new public API key. Do not
deploy from this packet; deployment is owned by the rollout packet.
