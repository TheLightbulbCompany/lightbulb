# Native multiplayer founder production rollout plan

> **For the coordinating agent:** dispatch the four implementation packets in
> parallel. There is no user migration, compatibility sink, staged cutover, or
> separate cleanup PR. Merge the replacement and deletion work, then perform one
> production dispatch and reroll only the founder container.

**Goal:** Ship one canonical Slack-like Lightbulb room containing humans, Friday,
Gauge, and future agents; remove the custom human browser/mobile socket and the
obsolete native-room/broker path; publish one exact 9.3-based plugin image; and
prove the complete product in the production founder container.

**Architecture:** HTTP owns commands and reconciliation, DynamoDB owns the ordered
transcript, AppSync Events owns human live delivery, one FIFO queue owns durable
OpenClaw work, and a first-party Lightbulb channel maps each room participant to a
native agent-owned OpenClaw session. AppSync uses a small dependency-free client
implementing AWS's documented WebSocket protocol; Amplify is not used.

**Implementation packets:**

1. `2026-09-09-native-multiplayer-aws-foundation.md`
2. `2026-09-09-native-multiplayer-openclaw-channel.md`
3. `2026-09-09-native-multiplayer-backend.md`
4. `2026-09-09-native-multiplayer-frontend.md`

**Frozen spec:**
`docs/superpowers/specs/2026-09-09-native-multiplayer-messaging-architecture.md`

## PR map

Do not merge PR #2404 as written. Reuse only its reviewed room presentation,
accessibility, uncertain-send, and cache-isolation work in PR D. Delete its
synthetic `sessions.room.*` contract and design documents.

| PR | Branch | Scope | Develop | Merge dependency |
| --- | --- | --- | --- | --- |
| A | `feat/messaging-aws-foundation` | DynamoDB, AppSync, Pipe/SQS, projector, IAM, alarms, remove browser admission from API Gateway WebSocket | parallel | none |
| B | `feat/lightbulb-openclaw-channel` | first-party channel plugin and extended image | parallel | none |
| C | `feat/messaging-backend` | canonical service, HTTP, worker, config, delete native-room/human-broker/browser fanout | parallel | merge after A and E |
| D | `feat/messaging-frontend` | native AppSync protocol client, HTTP clients, PR #2404 UI, delete browser/mobile gateway socket | parallel | merge after C |
| E | `chore/promote-multiplayer-image` | exact image tag and artifact/runtime digests only | after B build | B merged and image published |

The merge order is exactly:

```text
A -> B -> E -> C -> D -> one production dispatch -> founder reroll
```

A–D open as draft PRs as soon as their first scoped commit exists. They share the
frozen models/event registry and must not invent compatibility APIs. Existing
automatic dev deployments may run while merges progress, but no dev-user migration
or manual dev acceptance phase is required.

## Phase 0: Publish the coordination contract

### Task 1: Land the docs-only PR

Check in these six files from a clean branch based on current `origin/main`:

```text
docs/superpowers/specs/2026-09-09-native-multiplayer-messaging-architecture.md
docs/superpowers/plans/2026-09-09-native-multiplayer-aws-foundation.md
docs/superpowers/plans/2026-09-09-native-multiplayer-openclaw-channel.md
docs/superpowers/plans/2026-09-09-native-multiplayer-backend.md
docs/superpowers/plans/2026-09-09-native-multiplayer-frontend.md
docs/superpowers/plans/2026-09-09-native-multiplayer-rollout.md
```

- [ ] Require one contract review from each packet owner.
- [ ] Fix cross-boundary inconsistencies in the spec, not with packet-local
  aliases.
- [ ] Merge as `docs: freeze founder multiplayer architecture`.

## Phase 1: Build in parallel and merge once

### Task 2: Open PRs A–D

- [ ] Give each worker exactly one packet and its ownership boundary.
- [ ] Open a draft PR after the first commit; push after each task so review can
  overlap implementation.
- [ ] Do not run the broad monorepo suite locally. Use the existing focused CI for
  changed Lightbulb code. Unrelated upstream OpenClaw checks are advisory; a
  compile/test failure in changed product, plugin, image, or infrastructure code
  must be fixed before its PR merges.
- [ ] Add no Amplify package, event bus, second room database, second work queue,
  browser publisher, generic event framework, or feature-flagged compatibility
  sink.

### Task 3: Merge PR A

- [ ] Update both infra lockfiles for the exact CDK pin and synth twice until
  `.github/workflows/deploy.yml` converges.
- [ ] Verify the synthesized contract: one messaging table with stream/TTL, two
  stream consumers, one FIFO work queue, one direct work Pipe, one live projector,
  one Event API, `rooms`/`users` handlers, OIDC subscribe, IAM publish, no API key,
  and least-privilege KMS/IAM.
- [ ] Verify API Gateway WebSocket accepts only paired Tauri nodes and signed
  Paperclip operators; Clerk browser, `chat`, and `native_human` admission are
  absent, and every authenticated auth-kind/client-ID mismatch fails closed.
- [ ] Merge and observe the normal automatic deployment workflow. Do not create
  or migrate any user container.

### Task 4: Merge PR B and promote its one image in PR E

The current base is already fork `v2026.9.3-isol8.2`. PR B adds the Lightbulb
channel to the extended image; it is not another upstream-version upgrade.

- [ ] Merge B and let `.github/workflows/build-openclaw-image.yml` build once from
  the merge commit.
- [ ] Require source plugin tests, pinned-9.3 compile, boot-chain smoke, Docker
  build, runtime qualification, `message` tool proof, SOCI conversion, and one
  immutable ECR push.
- [ ] Require two-account route-registration failure safety, explicit
  `groupPolicy: "open"`, `session.groupScope: "per-group"`, shared workspace
  pair-loop scope, event-kind/command suppression, 1-MiB bounded ordered history,
  restart-persistent effect replay, required durable-final/unknown-send
  reconciliation, and both stable outbound event-ID paths in qualification.
- [ ] Resolve the tag as
  `2026.9.3-isol8.2-<short B merge SHA>`; never guess or reuse a partial tag.
- [ ] Read the ECR artifact digest and runtime manifest digest from the successful
  workflow and ECR.
- [ ] In E, update only `openclaw-version.json` dev/prod tag plus those exact two
  digests. Require provenance to match the annotated fork tag, peeled fork commit,
  plugin version, artifact, and runtime manifest.
- [ ] Merge E. Do not reroll the founder container yet.

### Task 5: Merge PR C

- [ ] Rebase C on A/B/E. Confirm its config emitter is enabled only now that the
  pinned image contains the plugin.
- [ ] Confirm the backend/AppSync/OpenClaw contracts byte-for-byte: public
  camelCase DTOs/status codes, frozen event names, channel routes, work pointer
  schemas, FIFO IDs, and session keys.
- [ ] Confirm internal agent output is owner-path/hook-token bound, roster-authored,
  durably idempotent by stable opaque event ID, and resolves peer
  `@<mentionHandle>` tokens to canonical UUIDs without trusting model-supplied identity or run
  metadata.
- [ ] Confirm canonical message + idempotency receipt + per-agent work outboxes
  commit in one DynamoDB transaction, and runtime operations use the same outbox
  pattern with reclaimable leases.
- [ ] Confirm there is no browser dual sink, native-room caller, human-broker
  device, dedicated cursor table, browser fanout, or human WebSocket request path.
- [ ] Confirm internal OpenClaw gateway RPC/events, paired Tauri node relay,
  Paperclip operator relay/proxy, and VNC remain.
- [ ] Merge and observe backend/projector/Pipe/queue health. Do not reroll the
  founder container yet.

### Task 6: Merge PR D

- [ ] Rebase D on C and import only the selected PR #2404 presentation commits.
- [ ] Confirm no `aws-amplify` package or generated Amplify configuration exists.
- [ ] Confirm the shared native client implements exactly the AppSync Event API
  subprotocol, ACK/keepalive/subscription/reconnect behavior, and exposes no
  publish method.
- [ ] Confirm all durable writes, abort, steer, approval/question resolution, and
  runtime RPC use HTTP and remain enabled while live delivery reconnects.
- [ ] Confirm the old web/mobile gateway socket, park/ping logic, connection-driven
  wake, Teams socket subscription, and public WS environment variables are gone.
- [ ] Confirm Tauri token delivery no longer depends on room-event connectivity.
- [ ] Merge and record the resulting exact `main` commit.

## Phase 2: One founder production release

### Task 7: Create or verify the production Clerk template

Use the production Clerk secret from Secrets Manager only in process memory. Never
print it, save it, or put it in CI output.

- [ ] Through Clerk Backend API version `2026-05-12`, list JWT templates and
  create `lightbulb-appsync` only when absent; otherwise patch it only when its
  exact settings differ.
- [ ] Freeze claims `{"aud":"lightbulb-appsync"}`, lifetime 60 seconds, and
  allowed clock skew five seconds. Create/update bodies contain only `name`,
  `claims`, `lifetime`, and `allowed_clock_skew`.
- [ ] Read it back and verify a signed-in production founder session can mint a
  token whose `iss`, `sub`, `aud`, `iat`, and `exp` are correct. Do not log the
  token.

### Task 8: Dispatch production once

- [ ] Dispatch `.github/workflows/deploy.yml` from the recorded exact `main`
  commit. Do not rebuild the image for production.
- [ ] Watch every production CloudFormation, backend, and frontend job.
- [ ] Verify the deployed Event API auth/handlers, messaging table, projector,
  Pipe, work queue, alarms, backend health, and frontend release before rerolling
  the founder task.

### Task 9: Reroll only the founder container

Resolve `admin@thelightbulb.company` through the production admin UI/API at
execution time; do not hardcode its Clerk ID.

- [ ] Call the existing audited `reapply-config` endpoint so Friday and Gauge get
  one Lightbulb channel account/binding each and the frozen ambient settings.
- [ ] Call the existing audited `apply-image-update` endpoint with the exact E tag
  and rollout description.
- [ ] Wait for ECS to stabilize and verify the live task artifact digest, runtime
  manifest digest, gateway build ID, plugin version, two accounts, two routes, and
  effective `message` tool match the promotion record.

### Task 10: Run the production founder acceptance once

Collect only IDs/sequences/correlation IDs; do not copy tokens, credentials, or
message bodies into logs.

- [ ] Create one room with the founder, Friday, and Gauge. Reload and prove the
  directory/history come from canonical HTTP data.
- [ ] Mention both agents once. Prove one human message yields two parallel FIFO
  groups, two native session keys, two separately authored replies, and one
  ordered canonical transcript.
- [ ] Have Friday address Gauge publicly. Prove Gauge's turn includes Friday's
  canonical post and Friday is not self-echoed.
- [ ] Send an unmentioned message with both agents `active`. Either silence is
  valid; any interjection must arrive through the native `message` adapter, never
  an ambient private final.
- [ ] Switch Gauge through `mentions` and `observe`; verify unmentioned dispatch
  stops while the next mention still contains recent canonical room history.
- [ ] Exercise concurrent streaming/tool output, abort, steer, one approval, one
  question, progress, and one long runtime operation. Verify private events never
  appear in the room channel.
- [ ] Reload during traffic and force one sequence gap; verify subscribe-first
  HTTP reconciliation returns exactly one ordered transcript and HTTP send stays
  enabled during event reconnect.
- [ ] If a disposable second production member is available, verify join/post,
  room removal/key rotation, organization removal/old private key silence, and a
  new key after rejoin. This is the only additional identity used; no existing
  customer account is touched.
- [ ] Link one room/message to one existing Paperclip object and run one routine;
  verify Paperclip remains the work/schedule authority and agent speech returns
  through the canonical room.
- [ ] Smoke one paired Tauri invocation/result, Paperclip operator operation,
  VNC connection, and Paperclip proxy page.
- [ ] Require expected room sequences/cursors, zero stuck operations, zero work or
  Pipe DLQ records, zero projector/AppSync errors, and no secret/message-body
  leakage in logs.

If a scenario fails, fix only its owning PR path, merge, dispatch production once
more, reroll the founder only if image/config changed, and rerun the failed
scenario plus the dual-agent mention.

## Rollback

- Application failure: redeploy the last known-good `main` commit as one unit;
  retain the messaging table and canonical transcript.
- Plugin/image failure: use `apply-image-update` to restore the prior immutable
  9.3 tag; never retag ECR or mutate a running container.
- Corrupt work item: stop work consumption before explicit DLQ replay; never edit
  canonical messages or advance delivery cursors manually.

There is no rollback to a dual human transport.

## Definition of done

1. Friday and Gauge use separate native OpenClaw sessions in one canonical room
   and see one another's public messages.
2. Mentioned and ambient behavior uses upstream 9.3 routing, room-event, message
   delivery, session, and pair-loop primitives through the Lightbulb channel.
3. Human commands are HTTP; live human delivery is AppSync Events through the
   dependency-free protocol client.
4. No browser Clerk, `chat`, `native_human`, human broker, synthetic native-room,
   dedicated cursor-table, browser fanout, or compatibility path remains in the
   Lightbulb application.
5. OpenClaw gateway RPC/events, Tauri nodes, Paperclip operator/proxy, and VNC
   remain functional.
6. Production infrastructure, `openclaw-version.json`, and the running founder
   task agree on the exact immutable image artifact and runtime manifest digests.
7. The complete production founder acceptance suite passes.
