# Multiplayer Messaging Implementation Plan

> **For agentic workers:** Use `superpowers:subagent-driven-development` (SDD). The user selected autonomous SDD on 2026-09-09; subagents are authorized for execution, not separate user-owned tasks. Keep bounded task-level test/review loops inside the two product PRs below. Drafting the goal prompt does not itself start execution. Coordinate with the existing runtime task and respect its sharing permissions.

**Goal:** Ship organization messaging with public/private rooms, human and agent DMs, threads, verified authorship, explicit agent execution, and presence.

**Architecture:** Qualify and extend the verified native company-room path merged in #2403. OpenClaw is the conditional canonical-conversation authority; Paperclip owns work; Lightbulb owns verified organization admission, transport, presentation, and projection of native presence. Strict authorship and private execution isolation must pass before this architecture is enabled.

**Tech Stack:** Next.js 16/React, Tauri, Expo, shared TypeScript, FastAPI, Clerk, DynamoDB connection registry, API Gateway WebSockets, OpenClaw fork, Paperclip, AWS CDK.

**Spec:** `docs/superpowers/specs/2026-09-09-multiplayer-messaging-design.md`

## Global constraints

- Humans and agents are first-class participants; permissions still differ.
- Public/private rooms, human–human/human–agent/agent–agent DMs, group DMs, and threads are release requirements, not decorative UI labels.
- Existing private history stays private. DM expansion creates a new conversation.
- A human-only message must persist and deliver without a model call, including when AI credits are exhausted.
- Canonical sender identity comes from verified admission/native evidence, never message text or a client-supplied sender.
- Native human scopes remain `operator.read` and `operator.write`; no owner/admin fallback.
- Agent outputs never automatically wake all other agents; explicit delegation is limited to three hops per root request.
- Prefer native presence/viewer/typing primitives. Lightbulb's liveness target is heartbeat 30 seconds, expiry 90 seconds, away threshold five minutes; these are not native defaults. Adopt native non-preview typing throttle one second and active expiry 2.5 seconds; no draft preview.
- Do not mirror native transcripts in DynamoDB or add a broker/vector database speculatively.
- Preserve the existing owner billing and approval rails; do not change pricing in these PRs.
- Keep `/chat`, permanent panel keys, existing work panels, and Lightbulb brand tokens.
- Apply the Impeccable Operate brief and status table in spec §2. Human presence, message persistence, agent execution, and required decisions are distinct UI states. No palette expansion or global design-system rewrite.
- Upstream audit baseline is OpenClaw v2026.9.3 at `1391f7cd2d40ab5bbcf2f5f831d3a64f520e72d7`; exact source links and known gaps are in spec §4. A release tag, test fixture, or room lock icon is not capability/security proof.
- Code/config/image changes go through repositories and CI/CD. Do not edit production hosts.
- Any change under `apps/infra/lib/` requires CDK synth and its generated workflow diff in the same PR.
- All schema examples below are **proposed application contracts**, not claims that 9.3 already exposes them. Task 1 freezes the native mapping before dependent implementation.

## Delivery milestones and two-PR packaging

The user requested high iteration speed and fewer PRs on 2026-09-09. A–G below
are functional milestones, **not one PR each**. Keep testable commits and scoped
SDD reviews; group complete customer outcomes into these default product PRs:

1. **Core messaging, web/Tauri:** B–E plus Lightbulb-side qualification integration
   from A. Ship backend, shared contracts, room UX, presence, and useful agent work
   together against the image/backend task's reviewed runtime dependency.
2. **Expo core parity:** F on the same proven contracts, without holding the
   complete web/Tauri release for native-client delivery.

Reuse existing image/backend/fork PRs for A instead of opening duplicate foundation
work. Repository boundaries or a necessary safe deployment order can justify an
additional PR; record the concrete reason. PR-count reduction never permits
bypassing security, review, CI, or keeping a broken intermediate deployment.
G remains the explicit follow-on scope from the spec, not part of this core goal.

| Milestone / independently reviewable result | Depends on | Primary responsibility | Acceptance |
| --- | --- | --- | --- |
| A. Qualified runtime and verified human/machine coexistence | Current runtime work | Existing 9.3/runtime task | Real runtime evidence; capability stays off on mismatch |
| B. General room/DM/message semantics and non-inference sends | A | Runtime + backend | Two humans and two agents, one canonical room; private isolation |
| C. Presence and connection lifecycle | A, B access contract | Backend + shared client | Multi-tab, expiration, revocation, two backend processes |
| D. Web/Tauri messaging workspace | B, C | Frontend | Complete room/DM/thread matrix in two browser contexts |
| E. Agent work, unread/mentions, conversation search | B, D | Backend/frontend + Paperclip integration | Useful work loop; no unauthorized snippets or duplicate runs |
| F. Expo messaging | B–E shared contract | Mobile | Same identity/history/recovery semantics on real mobile lifecycle |
| G. Attachments and messaging completeness | B–F | Backend/frontend/mobile | Authorized downloads, edit/delete, reactions, notification preferences/push |

A–E form the first web/Tauri customer release. F is required before claiming
native mobile parity. G is the next product slice, not hidden unfinished work in
the first release. Launch acceptance is a final step on the integrated candidate,
not a separate implementation PR. Rebase each dependent PR after its predecessor.

Coordinate review of A/B against the other task's auth, backend, container,
release-reservation, and Paperclip changes. Do not race on `config.py`,
`websocket_chat.py`, `connection_pool.py`, or the version pins. On 2026-09-09 the
user explicitly authorized sharing this plan and non-secret integration details
with task `01a082b2-b1d7-7c20-9521-74cff5c220ac`, titled
“Research OpenClaw 9.x multiplayer AI”. Permission is not a delivered contract:
record the actual reply, branch/PRs, and exact runtime evidence before integration.

## Proposed shared data contract

Define these in `packages/shared/src/messaging/types.ts`; matching backend request
schemas belong in `apps/backend/core/gateway/room_protocol.py`. These are public
application types, not a second persistent message model. Preserve native IDs and
evidence through normalization.

```ts
type ParticipantRef = { kind: "human" | "agent"; id: string };
type RoomKind = "public" | "private" | "dm" | "group_dm";
type Room = {
  id: string;
  kind: RoomKind;
  name: string | null;
  archived: boolean;
  managerId: string | null;
  membershipRevision: string;
};
type Message = {
  id: string;
  roomId: string;
  threadRootId: string | null;
  replyToId: string | null;
  sender: ParticipantRef;
  body: string;
  createdAt: string;
  nativeEntryId: string;
};
type SendMessage = {
  roomId: string;
  clientMessageId: string;
  body: string;
  threadRootId: string | null;
  replyToId: string | null;
  mentions: ParticipantRef[];
  expectedLeafEntryId: string | null;
};
type SendReceipt = {
  message: Message;
  execution: "none" | "accepted" | "blocked" | "uncertain";
  runIds: string[];
};
type HumanPresence = {
  personId: string;
  state: "available" | "away" | "offline" | "unknown";
  inRoom: boolean;
  observedAt: string;
};
```

`ParticipantRef.id` is a server-resolved principal reference; display names and
emails are separate directory decoration. The backend verifies that each mention
belongs to the current room and qualifies any runtime offset-based mention form.
`membershipRevision` is authoritative access-version evidence, not a browser
permission grant. Task 1 must supply a native revision or a server-owned durable
equivalent before it is consumed by caching.

Client send state is local: `draft → sending → persisted`, with `uncertain` and
`rejected` branches. Agent execution is a separate state. Canonical order uses
native history ordering/entry relationships, never client clocks or sorted UUIDs.
User submissions must not contain `sender`, role, profile, or owner overrides.

## Task 1: Freeze the runtime contract and make qualification real

**Files:**

- Modify: `apps/backend/core/gateway/native_rooms.py` (on `main`, absent from this old checkout).
- Modify: `apps/backend/core/services/connection_service.py`.
- Modify only with runtime owner: `apps/backend/core/containers/config.py`, `apps/backend/core/containers/ecs_manager.py`, `openclaw-version.json`, `apps/infra/lib/stacks/container-stack.ts`.
- Test: `apps/backend/tests/unit/gateway/test_native_rooms.py`.
- Create: `apps/backend/tests/integration/test_native_room_runtime.py`.
- Fork changes live in `TheLightbulbCompany/agent`, not in this repo. The documented local fork path was absent during planning; resolve its active checkout before execution.

**Consumes:** `HumanIdentity`, `RoomQualification`, `NativeRoomPool`, the runtime task's exact build/config evidence.

**Produces:** A durable, revocable server-owned qualification source, plus frozen native request/response fixtures and capability mapping for Task 2. No claim of readiness from a version string alone.

- [ ] Start a fresh `codex/` implementation branch from current `main`; confirm #2403 exists and preserve unrelated work. Inspect the current runtime task's merged changes before selecting edit targets.
- [ ] Reconcile the source audit in spec §4 with the actual fork diff: enumerate native reuse, Lightbulb integration, and required extensions. In particular, `sessions.viewers.set` is the native method name; native owner reassignment is not transfer of sharing authority, and session visibility modes are not product room kinds.
- [ ] Write the matrix from spec §5 into integration scenarios: two humans, two agents, legacy chat, machine operator, desktop node, private history, reconnect, revocation, human-only send, agent-to-agent authored message, and alternating requests across two backend processes.
- [ ] Capture exact native envelopes for room creation/listing/discovery, public/private ACLs, DM identity, append without inference, multi-agent authorship, threads, messages/history, presence/typing, read cursors, and ACL-filtered search. Record an unsupported capability as a failed required scenario, not a successful fallback.
- [ ] Replace test-only in-memory qualification with the reviewed owner-release qualification source. Persist exact evidence/build/config/profile binding; keep private device material in Secrets Manager. Reject stale release/IP/build, missing profiles, `gateway-owner`, changed config, revoked evidence, or failed membership lookups.
- [ ] Demonstrate the expected negative case before wiring the loader: a correct-looking image tag with no evidence still returns `ROOM_UNAVAILABLE`.

Concrete admission regression to retain alongside existing tests:

```python
import pytest

def test_unqualified_owner_cannot_enable_rooms():
    from core.gateway.native_rooms import RoomError, qualification_for

    with pytest.raises(RoomError) as error:
        qualification_for("org_without_runtime_evidence")
    assert error.value.error["code"] == "ROOM_UNAVAILABLE"
```

- [ ] Run `uv run pytest tests/unit/gateway/test_native_rooms.py tests/integration/test_native_room_runtime.py -q` from `apps/backend`, first against missing/incorrect qualification and then the candidate runtime. The real-runtime suite must explicitly report skipped when no test runtime is provided; a skipped run cannot qualify release.
- [ ] Resolve multi-process connection ownership before enabling rollout. API Gateway's Lambda-to-ALB path is not sticky. Test subscription on process A, send on B, revoke on A, and reconnect on B. Use a reviewed routing/subscription strategy proved by this test; do not assume the legacy owner fan-out solves native per-human socket ownership.
- [ ] Integrate qualification through the runtime owner's existing PR or the core product PR, by agreed file ownership. Save sanitized protocol fixtures with their producing build IDs in the integration test area; put release evidence in the PR. An unsupported required primitive is not an invented client API call. If secure rooms or a canonical message ledger require broad runtime restructuring, present the single-authority alternative from spec §5 before starting that expanded implementation.

## Task 2: General conversations and messages without inference

**Files:**

- Modify: `apps/backend/core/gateway/native_rooms.py`, `apps/backend/routers/websocket_chat.py`.
- Create: `apps/backend/core/gateway/room_protocol.py` (validated application contract/native mapping).
- Create: `packages/shared/src/messaging/types.ts`; export via `packages/shared/src/index.ts`.
- Test: `apps/backend/tests/unit/gateway/test_native_rooms.py`.
- Create: `apps/backend/tests/unit/gateway/test_room_protocol.py`.
- Extend: `apps/backend/tests/integration/test_native_room_runtime.py`.
- Fork: extend the frozen native schema/handlers identified by Task 1 only where a required capability is absent.

**Consumes:** Qualified human connections and exact native fixtures from Task 1.

**Produces:** Stable product room IDs, public/private/DM/group-DM/thread semantics, canonical authored messages, and the `SendMessage → SendReceipt` contract above.

- [ ] Add failing integration cases for every room-kind/participant pairing in spec §1. Test agent removal/rename without room disappearance, same-pair DM creation races, and adding a human to a DM without exposing prior messages.
- [ ] Freeze room operation schemas at the backend boundary. Enforce name length 1–80 characters for named rooms, text size at most 16 KiB UTF-8 initially, valid canonical room/reply IDs, at most ten distinct mention targets, and actor-scoped idempotency keys. The native limits may be stricter; return a clear validation error before forwarding. Empty text is invalid until attachment-only messages ship in G.
- [ ] Make `#general` creation idempotent per organization. Use native atomic uniqueness for pairwise DMs and room identity; concurrent creation must return the same canonical record. Preserve immutable visibility and explicit private membership rules from the spec.
- [ ] Enforce the room-manager rules from spec §1. Any human member can create a conversation; only its manager can invite/remove/rename/archive a private room. An org admin without membership cannot use that role to read it. Agents cannot self-admit; leaving a room with no remaining manager requires transfer or archive. Test these alongside the DM membership immutability rule.
- [ ] Test manager transfer against immutable native creator authority; `sessions.assignOwner` alone cannot implement this product operation. Keep historical participation, current membership, and assignable responsibility distinct. Native participant previews/32-identity admission bounds must not silently cap or fabricate the full organization directory.
- [ ] Keep one canonical transcript across human and agent sends. If native storage only represents one host agent per session, implement the qualified multi-participant representation in the fork; do not stitch independent private histories together in the frontend.
- [ ] Implement pure message append independently from agent dispatch using a qualified runtime primitive. Reviewed upstream `chat.send` and `sessions.send` dispatch agent work; `chat.inject` writes synthetic assistant entries and is not a human-send substitute. A normal room message must bypass inference gating and run holds. An agent DM/mention persists once, then dispatches once under existing budget checks. A blocked agent produces `execution: "blocked"` while retaining the human message.
- [ ] Send two human messages concurrently while the agent is running. Require two canonical IDs and exact independent sender/body records after reconnect; native steering/collection cannot collapse them into one attributed turn. A started run ACK alone must not mark a message saved.
- [ ] Enforce room permission checks for list, read, write, subscribe, typing, invite/remove, history selectors, search, and file access. Public discovery uses org membership; private discovery requires explicit membership. A matching owner/room key prefix alone never grants private access. Do not use the best-effort `/teams/members` display list as authority.
- [ ] Ensure agent sends use authenticated agent principals and are limited to their joined rooms. Browser-supplied `sender`, forged agent IDs, inherited owner permissions, and room-to-legacy key access must fail. Prove context/retrieval isolation when the same agent belongs to both a private and a public room.
- [ ] Test a public-room participant asking that shared agent to read a private transcript, file, summary, or task through its tools. Enforce the audience at the runtime/tool boundary or isolate execution placement. Native sidebar filtering and `operator.write` scopes alone do not satisfy this security gate.
- [ ] Implement room membership removal in native state as well as socket revocation. Clear visibility/read/search caches and constrain in-flight delegated work. Re-inviting a person to the organization alone must not resurrect former private memberships.

Behavioral test example, using the real-runtime fixture introduced in Task 1:

```python
async def test_human_only_room_message_has_no_inference(runtime):
    alice, bob = await runtime.humans_in_public_room()
    before = await runtime.model_request_count()
    sent = await alice.send_text("Can someone review the launch checklist?")
    received = await bob.wait_for_message(sent.id)
    assert received.sender == alice.principal
    assert received.id == sent.id
    assert await runtime.model_request_count() == before
```

`runtime` is the integration harness implemented in Task 1: it provisions an
isolated runtime, establishes separately verified human clients, counts calls at
its fake model boundary, and consumes actual native events. It must not simulate
the room server itself. Add the analogous credit-exhausted case and two-agent case.

- [ ] Run focused native/protocol tests, plus legacy session ownership, list/search filtering, and approval isolation tests. Commit this milestone in the core product PR and request its scoped SDD review against the runtime PR's exact build/config changes.

## Task 3: Presence, subscription recovery, and revocation

**Files:**

- Modify: `apps/backend/core/services/connection_service.py`, `apps/backend/core/gateway/native_rooms.py`.
- Create: `apps/backend/core/services/room_presence.py` only if Task 1 proves native presence insufficient.
- Modify if registry fields/index projection change: `apps/infra/lib/stacks/api-stack.ts` and generated `.github/workflows/deploy.yml`.
- Create: `packages/shared/src/messaging/presence.ts`, `packages/shared/src/messaging/presence.test.ts`.
- Create: `apps/backend/tests/unit/services/test_room_presence.py` only if a registry supplement is needed; otherwise extend native-room tests.
- Read/reuse without changing old wake semantics: `apps/frontend/src/lib/presence.ts`, `apps/mobile/src/lib/presence.ts`.

**Consumes:** Verified connection identity, current room membership, native room subscriptions.

**Produces:** `HumanPresence[]` snapshots plus bounded room/thread typing events; recoverable history subscriptions with no identity guessing.

- [ ] Write tests for 89/90-second product liveness boundary, multiple active tabs, last-tab close, hidden client, five-minute inactivity, failed or truncated presence inventory, stale membership, and node/operator exclusion. Verify native one-second typing throttling/2.5-second expiry with no preview. Use an injected clock; do not wait in tests.
- [ ] Qualify and admit `sessions.viewers.set`, `system-presence`, native connect snapshots, and `presence` events in the existing room path. Keep explicit viewed-room declarations separate from subscriptions. Project only authorized person data; do not leak raw IP/device inventory. Test native user-visible client admission, 32-key viewer bounds, and the 200-entry inventory ceiling.
- [ ] Measure native liveness on abrupt transport loss and backend restart. Its five-minute cleanup TTL does not meet a 90-second guarantee by itself. Supplement only missing liveness evidence from the existing connection registry when required; do not build a second viewer/typing subsystem.
- [ ] Implement `aggregateHumanPresence(connections, nowMs)` only over qualified, complete normalized evidence. Native or registry adapters retain incomplete/failed snapshots explicitly; neither omission nor a failed presence read means offline. Use the spec's available/away precedence across verified connections.

```ts
it("keeps a human available when one of two tabs expires", () => {
  const now = 100_000;
  expect(aggregateHumanPresence([
    { personId: "alice", lastSeenMs: 1_000, active: false, inRoom: false },
    { personId: "alice", lastSeenMs: 99_000, active: true, inRoom: true },
  ], now)).toMatchObject([{ personId: "alice", state: "available", inRoom: true }]);
});
```

The example assumes complete, fresh connection evidence normalized to
`{personId, lastSeenMs, active, inRoom}` and returns `HumanPresence[]` for visible
directory participants. It does not define a native wire schema. Incomplete or
failed snapshots are represented separately and map unevidenced states to unknown.

- [ ] Scope membership checks and fan-out to the room, not all owner connections. Preserve native human isolation. Bound per-event membership checks without inventing an unsafe positive cache: test Clerk failure/revocation explicitly. If replacing the current Clerk-read-per-event path, use a durable revocation epoch with a measured invalidation bound and fail-closed lookup behavior.
- [ ] Subscribe before history catch-up where supported; buffer incoming events until the canonical snapshot/cursor is installed. Deduplicate by native message/entry identity. Handle `native.room.historyRequired` and `native.room.reconnectRequired` by restoring state and subscriptions. Never replay an uncertain send automatically with a new key.
- [ ] Reconcile subscriptions across two backend processes and rolling restart. Multiple per-human sockets may produce duplicate delivery; they must not duplicate persisted messages, typing identities, or runs. Explicit unsubscribe/revocation must clear all relevant sockets, not just the local instance's set.
- [ ] Separate room presence heartbeats from execution wake logic. Prove typing/heartbeat do not invoke a model or wake a sleeping execution service. Keep the messaging gateway's availability policy explicit.
- [ ] Run backend presence/native tests and `pnpm --filter @lightbulb/shared test`. If infra changes, run `pnpm --filter @lightbulb/infra test` and `pnpm --filter @lightbulb/infra exec cdk synth`, then inspect/commit generated workflow changes. Commit/review the lifecycle milestone inside the core product PR.

## Task 4: Web and Tauri messaging workspace

**Files:**

- Create: `packages/shared/src/messaging/useRoomConnection.ts` (shared native socket lifecycle; separate from legacy chat).
- Reuse/extend only transport primitives that apply: `packages/shared/src/gateway/useGatewayConnection.ts`.
- Create: `apps/frontend/src/hooks/useRooms.ts`, `apps/frontend/src/hooks/useRoomMessages.ts`.
- Create: `apps/frontend/src/components/messaging/RoomPanel.tsx`, `RoomDirectory.tsx`, `RoomComposer.tsx`.
- Modify: `apps/frontend/src/components/workspace/WorkspaceSidebar.tsx`, `apps/frontend/src/components/workspace/WorkspaceShell.tsx`.
- Modify: `apps/frontend/src/components/workspace/panelRegistry.ts`, `apps/frontend/src/components/workspace/urlChannel.ts`, `apps/frontend/src/components/workspace/layoutStorage.ts`, `apps/frontend/src/components/workspace/workspaceController.ts`.
- Modify: `apps/frontend/src/components/workspace/workspace.css`.
- Create: `apps/frontend/src/components/messaging/__tests__/RoomPanel.test.tsx`, `RoomComposer.test.tsx`.
- Create: `apps/frontend/tests/e2e/messaging.spec.ts`.
- Extend existing registry, URL, and layout tests beside their files.

**Consumes:** Shared message/presence types, qualified native methods, room directory and member evidence.

**Produces:** The spec §2 UI and complete first-release conversation creation/navigation.

- [ ] Before UI edits, apply Impeccable's shape/Operate brief in spec §2 against the current room components and runtime capability map. Keep the established visual identity. Review populated/empty, working/approval, reconnect/send-uncertain, and access-removed compositions; no synthetic presence, estimated progress, or unqualified private badge.
- [ ] Add capability-gated room transport. It must connect with the native company-room client identity, complete the server-owned `connect` handshake, and verify capabilities before showing enabled actions. Clear state on sign-out/org switch; never reuse another org's connection or cache.
- [ ] Add `room:<roomId>` panel identities and `?panel=room&room=<id>` deep links. Keep existing `chat:<agentId>` links opening their legacy private history. Label that history accurately; do not silently convert it into a shared room.
- [ ] Build Rooms and Direct Messages navigation, public room discovery, private room invitation, mixed human/agent picker, room header audience, and current participant list. Reuse the directory/identity primitives but distinguish “not provisioned” from “not a member.” Show private invite history disclosure.
- [ ] Implement the timeline from canonical messages and verified sender records. Reuse safe markdown rendering; treat missing sender evidence as a protocol error/unknown entry, not as the current viewer. Keep agent run cards collapsible.
- [ ] Implement composer draft/sending/persisted/uncertain/rejected states and explicit agent target chips. Send button must not be labeled as starting an agent in human-only conversations. Handle concurrent native leaf conflicts with history refresh and clear retry status.
- [ ] Add room/thread presence, a member list reachable by keyboard, accessible typing updates, and unknown/reconnecting states. Only the focused/visible room counts as “in room”; background dock panels remain subscribed without falsely reporting attention.
- [ ] Show thread/task/artifact context on the right using existing dock behavior. On narrow web screens use a single active view and back navigation. Keep Tasks/Inbox/Library reachable without additional nested tabs.
- [ ] Apply the spec's status table using existing neutral/`info`/`ok`/`warn`/`err` tokens. Make room navigation quiet, keep author identity legible, collapse tool traffic, reserve count badges for actionable mentions/approvals, and show concrete recovery actions. Keep the selected detail pane closed when unused; preserve draft, scroll, and focus on return.

Concrete two-context browser acceptance:

```ts
await alice.openRoom("general");
await bob.openRoom("general");
await alice.sendMessage("Launch checklist is ready");
await expect(bob.message("Launch checklist is ready")).toHaveAttribute(
  "data-author-id", alice.userId,
);
await bob.reload();
await expect(bob.message("Launch checklist is ready")).toHaveCount(1);
```

Implement the `alice`/`bob` fixture helpers in `messaging.spec.ts` using isolated
browser contexts and the existing sanctioned Clerk test-account fixtures. Repeat
the journey for private invite/removal, human DM, agent DM, group DM, threads,
and one agent-to-agent message observed by an authorized human. Use seeded test
credentials/identities; no production account resets.

- [ ] Run focused messaging and workspace tests, then frontend lint/typecheck/build and the existing brand-token scanner. Batch visual checks at 1440px, 1024px, and 390px with long names, busy rooms, markdown overflow, keyboard-only navigation, 200% zoom, and reduced motion; include a Tauri smoke check. Fix material findings together and confirm once. Follow Impeccable's build-time review handoff when implementing, without treating this planning audit as a rendered/a11y pass. Commit/review the UI milestone inside the core product PR; capability-disabled tests do not prove rooms live.

## Task 5: Agent work, unread state, mentions, and search

**Files:**

- Modify: `apps/backend/core/gateway/native_rooms.py`, `apps/backend/core/gateway/room_protocol.py`.
- Reuse/extend: `apps/backend/routers/teams/{issues,work,interactions,approvals}.py`.
- Reuse/extend: `apps/frontend/src/components/teams/issues/IssueDetailPage.tsx` and existing issue/run/approval components.
- Modify: `apps/frontend/src/components/messaging/{RoomPanel,RoomComposer}.tsx`, `apps/frontend/src/hooks/useRooms.ts`.
- Create: `apps/backend/tests/unit/gateway/test_room_work.py`, `apps/frontend/src/components/messaging/__tests__/RoomWork.test.tsx`.
- Update: `apps/infra/openclaw/skills/lightbulb/references/` file selected by that skill's `MAINTENANCE.md`, plus its routing file only if necessary.

**Consumes:** Persisted room messages, verified actor/member permissions, existing Paperclip work endpoints and native mention evidence.

**Produces:** An explicit message-to-task workflow, bounded agent delegation, unread/mention navigation, and authorized messaging search.

- [ ] Add a test in which `@Friday` dispatches exactly once across an acknowledgment-loss retry, an ordinary human message dispatches zero times, and Friday's reply dispatches zero additional agents. Explicit Friday-to-Atlas delegation executes under Atlas's identity with a root/parent reference; a fourth automatic delegation hop is blocked and visible.
- [ ] Link a thread to a Paperclip task through an explicit action. Pass the verified creator and selected message references; never create a company-visible task containing private text without the disclosed share action. Reopening the task reference must enforce both sides' visibility rules.
- [ ] Render actual task/run/artifact and approval records using existing components. Keep approval actor/permission checks on the server, and add a test that plain text `approved` does not execute an approval action.
- [ ] Implement durable per-person room/thread read cursors using qualified native support or minimal single-authority application metadata where absent. Consume `users.mentionable`, `mentions.list`, `mentions.dismiss`, and `mentions.changed` with verified human targets. Native mentions retain at most seven days/100 entries per profile/10,000 per gateway and are not a complete unread ledger. Only an actually viewed message advances a read cursor; hidden panels do not. Human mentions notify; agent targets invoke the explicit dispatch policy.
- [ ] Add conversation-history search separately from existing workspace/file search. Query the qualified native authorized search surface; if absent, add it in the fork before advertising message search. Unauthorized room names, snippets, result counts, and deleted access must not leak through the index or cache.
- [ ] Test private context isolation through the same agent in two rooms and through derived summaries/work objects. Enforce access at retrieval/file boundaries; do not substitute system-prompt instructions for authorization.
- [ ] Run focused room-work, Paperclip action, mention/read cursor, and search tests; update agent-facing navigation references so agents can accurately explain rooms and work actions. Commit/review the work/search milestone inside the core product PR.

## Task 6: Expo delivery and the next completeness slice

**Expo files:**

- Modify: `apps/mobile/src/app/(app)/chat.tsx`, `apps/mobile/src/app/(app)/_layout.tsx`.
- Create: `apps/mobile/src/app/(app)/room/[roomId].tsx`.
- Reuse: `packages/shared/src/messaging/`, `apps/mobile/src/lib/presence.ts`.
- Add tests beside the existing mobile chat tests and shared messaging contract tests.

- [ ] Render room/DM directory and a native room screen using the same canonical IDs and transport contracts. Preserve old agent-chat links and history.
- [ ] Test foreground/background transitions, temporary disconnection, organization switch, and account sign-out. Backgrounding marks away, clears typing, and does not schedule AI wake-ups; resumption restores history/cursors and only one active connection.
- [ ] Run the mobile CI's lint/typecheck/Jest/Metro export gates and a real device/simulator two-person messaging smoke test. Publish through the existing mobile release mechanism only when the release is authorized.

**Completeness slice G:** Separate follow-on plan after the core native schema is frozen. Its accepted scope is room file uploads/downloads, reactions, edit/delete, room mute/notification settings, and mobile push. Required behavior is already fixed here:

- Attachments use room-authorized metadata and short-lived download access; copying a URL or task reference cannot bypass membership. Upload limits/type validation and cleanup use existing storage patterns after inspecting their actual audience checks.
- Edit/delete permissions apply to the author or an explicitly authorized moderator. Reconnect and search reflect revisions/tombstones; deletion cannot be presented as guaranteed recall from recipients' devices. Transcript branching/rewind is not a substitute for editing a shared message.
- Reactions are idempotent per message/principal/reaction and inherit message access. They never invoke an agent.
- Muted rooms suppress routine notifications but remain readable. Notification content is minimized; private previews require current membership at dispatch. Push delivery and device-token lifecycle reuse the existing mobile provider after source verification.
- Each item adds focused authorization/retry tests and joins the existing two-user end-to-end suite before rollout. Do not claim Slack feature parity before this slice and the core release both pass.

## Final integration, review, and rollout

- [ ] Rebase the integrated candidate onto the current auth/backend/container changes. Record exact frontend/backend/fork commits, image digests, qualification evidence, and CI links in the PR descriptions.
- [ ] Run all affected unit/contract suites and the real-runtime suite. Run the existing legacy private-history, session-key, questions/approvals, Paperclip operator, node, billing gate, and idle-teardown regressions. Any failure is investigated before merge, including apparently unrelated dependent systems.
- [ ] Run two-human browser acceptance on dev: public room, private room, DM, group DM, threaded agent request, artifact/task review, member removal, stale socket, reload/reconnect, duplicate submit, and exhausted AI budget. Prove an unauthorized browser cannot retrieve history/search/attachments or claim an agent identity.
- [ ] Include concurrent steering/collection authorship and cross-room tool-context exfiltration cases. Review final UI states against the same runtime evidence: no “saved” from a run-start ACK, no “offline” from an incomplete presence snapshot, and no approval action inferred from chat text.
- [ ] Exercise a synthetic 20-human/10-agent/60-connection organization against two backend processes; measure send/ack p95 <500 ms, cross-client delivery p95 <1 second, and presence expiry ≤90 seconds. These are warm-path targets; record cold/restart behavior separately.
- [ ] Review native session/file/memory authorization, trusted header admission, qualification storage, durable revocation, agent delegation, generated CDK workflow drift, and machine-client compatibility. The required `claude-review` gate must pass; inspect Codex review's supported surfaces as the second opinion. Do not bypass branch protection.
- [ ] Watch all push-triggered CI/build runs. Merge in dependency order, watch dev deployment, perform dev acceptance against the exact deployed candidate, then use the existing authorized production-promotion workflow. Production status requires deployment/runtime evidence, not a merge SHA alone.
- [ ] Roll out to a qualified internal organization first. Removing qualification disables new room admission and presents an unavailable state without altering stored messages or broadening access. Test data-format downgrade compatibility before any image rollback; otherwise roll forward with access disabled.
- [ ] Record the actual acceptance result and remaining scope (Expo/G if not yet shipped). Delete shipped working plans/specs when their scope is complete, preserving decisions in PR descriptions and living documentation.

## Planning verification

This turn refines documents only. No code, version pin, runtime qualification,
customer data, deployment, or pricing has changed. Validate document paths against
the stated `main` baseline, distinguish proposed files from existing files, check
that all spec requirements map to tasks, and run `git diff --check` on the documents.
Runtime tests are explicitly scheduled above; they have not been run as proof of
features that do not yet exist.
