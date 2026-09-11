# Local Claude Code and Codex multiplayer — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This document authorizes no implementation, account changes, or deployment by itself.

**Goal:** After one-time desktop enrollment, starting an ordinary `claude` or `codex` session on your computer automatically makes it visible and steerable to the people sharing a Lightbulb conversation with you.

**Architecture:** Keep humans and company agents as the only conversation participants. Add local-session adapters to the existing Tauri host; expose their resources through the existing messaging backend. Keep OpenClaw node execution separate from harness-session control.

**Tech Stack:** Existing Tauri 2/Rust desktop, FastAPI, DynamoDB messaging table, authenticated machine WebSocket infrastructure, AppSync Events, and shared React/TypeScript messaging client. Native harness interfaces before wrappers or new dependencies.

**Status:** Tasks 1–7 and the desktop production-dispatch gate are implemented, with native limitations and live release gates below still open. Local implementation is independently reviewed through `44a4c9b2`, including recovery/external-review corrections, native Claude 2.1.269 qualification, enrollment UI polish, and integration with main's new starter-workspace/composer changes. That integration passed 162 focused backend, 179 frontend, 46 skill and 69 plugin tests plus strict TypeScript checking; existing warnings remain. Earlier full frontend (4,027) and native (101 library + 9 probe; 3 explicit-live ignored) are bounded prior checkpoints, not deployed acceptance.

PR [#2492](https://github.com/TheLightbulbCompany/lightbulb/pull/2492) is ready, not merged. Earlier candidate `7630b8e84` passed frontend/infra/mobile/image-proof CI and substantive Claude review, but backend run `34639801818` exceeded its 45-minute limit at 96%, without a final pytest summary. Terra's independently reviewed, test-only lifespan-isolation correction `6b5f43f3` now passes the full workflow-environment/coverage command locally: 9,475 passed, 408 skipped, 1 xfailed, 42 warnings in 28m02s, with 84.83% coverage. No timeout increase, new skip or coverage cut. Clean merge `e2ebbd273` combines that fix with the reviewed main integration and stale-submit regression `25da6f739`; corrected combined CI is still required. The stale-submit test passes both 403/404 cases and fails when the unchanged access-loss guard is temporarily removed; all 32 resource UI tests pass after restoration. Separately, the three downstream Lambda suites pass 115 tests. Owned ordinary Claude 2.1.269 idle/busy and Codex 0.149.0 same-session input/mirroring passed against a local mock backend; signed-app, authenticated deployed relay and two-person/two-Mac validation remain unrun. No feature deployment or real-session enrollment has occurred.

**Document lifecycle:** Working plan. Delete when shipped; preserve decisions and verification evidence in the shipping PR, following `CLAUDE.md`.

## 1. The experience we are building

You install Lightbulb Desktop, sign in, and enable local-session sharing once. The setup explains that your connected sessions are shared with the current human members of conversations you belong to in the enrolled Lightbulb workspace. It is not a per-session or per-chat “Share” button.

Remote contributions run with that local session's existing credentials, permissions and provider usage allowance. Make that consequence visible; do not copy provider credentials to Lightbulb or count observed native inference as another OpenClaw inference call.

Then:

1. You type `claude` in your usual terminal and work normally.
2. Aden opens your DM or a group conversation containing you.
3. Its **Devices & sessions** panel shows your desktop and all your connected sessions, including sessions started outside Lightbulb.
4. Aden opens the load-test investigation and sees its actual conversation and ongoing activity.
5. Aden sends “Check whether the timeout also happens at concurrency 50.” That input reaches the same native session, with Aden's authorship recorded.
6. You see the contribution in the terminal; Aden sees the resulting activity in Lightbulb. Neither of you transfers ownership or starts a replacement agent.

The equivalent experience must work for Codex. A read-only transcript importer or an independently resumed copy does **not** satisfy this goal.

### What “whenever I start Claude or Codex” means

- Automatic discovery of every supported local session in the enrolled desktop context, across projects; no manually maintained session list.
- The desktop host must be running, including in the background/tray. Offline sessions reconnect and reconcile when it returns.
- Start with the existing supported macOS desktop distribution. Remote SSH hosts, containers, IDE-specific runtimes, custom harness data directories, and other operating systems are not silently treated as discovered local sessions. Display actual adapter coverage in setup.
- Discover sessions already running when Lightbulb starts as well as subsequently created ones. If an already-running session lacks the control endpoint needed for attachment, label the limitation; do not secretly restart it.
- Enrolling shares new sessions and currently active sessions, including their existing transcript needed for a handoff. It does not bulk-upload every historical session or personal memory file on the machine. Completed sessions enrolled earlier remain available as history.
- Harness versions/configurations that disable integration must be surfaced as unsupported or disconnected. “All sessions” is a supported-configuration promise, not permission to bypass managed settings.

## 2. Product model and routing

```text
Conversation: you + Aden + Friday
  Conversation messages and replies
  Devices & sessions
    Your desktop                 ← existing OpenClaw execution surface
      Claude: load-test investigation
      Codex: retry fix
    Aden's desktop
      Claude: deployment review
```

| Concept | Meaning | Not this |
| --- | --- | --- |
| Human | A conversation member who owns enrolled desktop resources | A container owner identity reused as the human ID |
| Company agent | Friday, Atlas, or another intentional teammate | Every local CLI process |
| Desktop | A human's paired execution surface; hosts node connections and local sessions | A new person or agent to DM |
| Local session | One provider-native conversation on one desktop | A room member, Paperclip employee, or duplicated room chat |
| Conversation/room | Membership and the human collaboration log | The only possible unit of harness context |
| Reply thread | Replies to a conversation message | An instruction to fork or start a harness session |

One conversation can expose many local sessions. One local session can appear in several conversations. These are projections of the **same session ID and transcript**, not separate runs.

Friday is the example company teammate here, not a required new-workspace seed or a name-based authorization rule. Current main creates six role-keyed starter agents for new workspaces and preserves existing/founder rosters. The room-bound adapter applies to the actual authorized company-agent ID; this feature does not migrate rosters or reintroduce personal workspaces.

### Where input goes

- Normal conversation composer: send a human message to the conversation using existing Friday/agent dispatch rules.
- Normal reply composer: reply in that conversation thread. This alone creates no new local session and changes no session target.
- Session-panel composer: send to the explicitly selected local session. Keep provider, human owner, desktop, and session title visible beside Send.
- A session link on a conversation message opens that same session panel. It does not reroute every subsequent conversation message there.
- Friday: use an explicit session reference from the user's request or a unique, unambiguous match among this conversation's resources. If two sessions match, ask which one; do not send to “the most recent Claude.”

No controller role, ownership transfer, or exclusive human control lease. Every current human member can steer. The transport still orders competing inputs; that is concurrency handling, not an access role.

### Cross-conversation visibility is intentional

Suppose your session appears in both `you + Aden` and `you + Maya`. Aden's contributions to that session are visible to Maya through the same transcript, even if Aden and Maya share no conversation themselves. The shared resource's audience is the union of its eligible conversations.

Explain this in enrollment and the session panel. Do not automatically inject a private conversation's full message history, attachments, or private Paperclip content into the session. A user can explicitly send selected text knowing it becomes session content. Room membership scopes access; it does not partition the transcript internally. Adding a member grants access to eligible session history, not just future output.

## 3. What is already here

Paths below are relative to the Lightbulb repository root.

| Existing code | Reuse / constraint |
| --- | --- |
| `apps/desktop/src-tauri/src/lib.rs`, `bootstrap.rs`, `tray.rs` | Auth lifecycle, resident desktop process, startup/shutdown integration |
| `apps/desktop/src-tauri/src/device_token.rs`, `keychain.rs`, `pair_state.rs` | Paired device identity and short-lived signed machine credentials; no provider credential upload |
| `apps/backend/routers/desktop_pair.py`, `core/services/desktop_pair.py`, `desktop_pair_live.py` | Existing enrollment and device revocation; inspect current per-human pairing ceiling |
| `apps/backend/core/services/node_proxy.py` | Existing OpenClaw relay; leave its node protocol separate |
| `apps/infra/lambda/ws-connect/index.py`, `ws-message/index.py`, `websocket-authorizer/index.py` | Existing machine admission and forwarding; currently admit node/operator connections, not local-session transport |
| `apps/backend/routers/websocket_chat.py` | Trusted machine identity verification and relay dispatch |
| `apps/backend/core/repositories/messaging_repo.py` | Existing messaging table, strong base-row membership reads, private notification outbox |
| `apps/backend/core/services/messaging.py` | `require_room_member`, membership mutation, errors and live-event contract |
| `packages/shared/src/messaging/`, `packages/shared/src/live/` | HTTP client/types and realtime client |
| `apps/frontend/src/components/messaging/RoomPanel.tsx`, `MessagingWorkspaceContext.tsx`; `apps/frontend/src/hooks/useRooms.ts` | Conversation UI and membership-bound state |
| `apps/infra/openclaw/plugins/lightbulb-channel/src/channel.ts` | Friday's canonical room/session binding, not a generic arbitrary room argument |
| `apps/desktop/src-tauri/src/claude_export.rs` | Existing **memory/skills archive import**, explicitly excludes transcripts/runtime state; do not repurpose it for live sharing |

The desktop already depends on Tokio, JSON serialization, WebSockets, HTTP, UUIDs and Unix support. Start with those. No new container per local session, second desktop daemon, vector database, generic orchestration framework, or third-party realtime service.

Current pairing/node bookkeeping has per-human assumptions and two logical node identities for a desktop. Do not display those as two physical computers or key session ownership only by the org/container owner. V1 can retain the existing one-paired-desktop-per-human ceiling; supporting several simultaneous Macs requires a deliberate pairing change, not just a list in the UI.

## 4. Harness integration: evidence and first choice

**Integration objective: the tightest practical integration with the native harness, preserving the ordinary terminal workflow.** After one-time setup, users should invoke `claude` or `codex` as usual and keep their existing project context, configuration, tools, skills, memory, authentication and approval behavior. Lightbulb adds multiplayer visibility and control to that same live session without requiring a separate Lightbulb-specific agent workflow. Prefer native lifecycle, streaming and control interfaces so the terminal and Lightbulb stay in sync. Native workflow parity is the target; any gaps must be verified and surfaced explicitly.

Lightbulb is the surrounding collaboration client, not an MCP tool that the model must decide to call to share its work. Observe outside the model loop and send input through the harness's native control boundary. Friday may use its own adapter to request an action, but session visibility must work while Friday is absent or idle.

### Claude Code

Initially inspected: **2.1.268**; the native auto-updater installed **2.1.269** on September 11. Both exact versions are qualified, not an open-ended range. Runtime identity comes from consistent current-generation native records, separately from the installed CLI version; unknown, mixed, stale-only or malformed evidence fails closed. `claude agents --json` returned metadata for live interactive/background sessions (`sessionId`, `pid`, `cwd`, `kind`, status fields). Background attach/logs/stop must not be confused with arbitrary live-terminal attachment. The probe evidence records the verified 2.1.269 idle/busy delivery and bounded verifier checks.

Use native listing for reconciliation, plus a small globally installed hook for prompt registration and lifecycle notifications. Hook inputs expose the session ID, transcript path and working directory. Merge the Lightbulb hook without replacing existing user hooks. [Claude hooks](https://code.claude.com/docs/en/hooks)

Claude now documents a per-session inbox socket and exports its location to hooks as `CLAUDE_CODE_MESSAGING_SOCKET`. Scripts can post to that socket. Delivery remains subject to inbound controls; peer messages cannot approve tools. Local messaging is available across providers on sufficiently recent versions. This is the preferred non-MCP ingress to validate, not a claim that Lightbulb's wire adapter already works. Do not use a child-process token to disguise a remote teammate as the session's own trusted child. [Native messaging and inbox socket](https://code.claude.com/docs/en/cross-session-messaging)

Read transcript changes incrementally for output; combine with lifecycle/status updates. Native session metadata should come from the CLI rather than guessing the meaning of its internal supervisor files. [Session listing reference](https://code.claude.com/docs/en/agent-view#list-sessions-as-json)

**Task 1 evidence (2026-09-10):** native ordinary-session idle/busy input, temporary SessionStart registration, completed-text observation and terminal visibility passed. The no-auth envelope is verified from the installed binary's diagnostic and live delivery, not a published stable schema. The native terminal attributes it generically to another Claude session; no first-class human author or correlated receipt was proved. Pending native Bash approval remained open on peer ingress; after local denial the message was processed. Two rapid identical envelopes produced one persisted message in one sample; durable retry safety is unproven. Explicit inbound hold/refuse configurations remain untested. [Full matrix and rerun](2026-09-10-local-session-native-probe-results.md)

Do not choose Channels as the foundation: that is an MCP-based channel integration and is not the requested product coupling. Do not assume Anthropic's Remote Control offers a public third-party transport merely because its own clients can connect. [Channels](https://code.claude.com/docs/en/channels), [Remote Control](https://code.claude.com/docs/en/remote-control)

### Codex

Locally inspected: `codex --version` reports **0.149.0**. Its CLI exposes:

```text
codex app-server proxy     # stdio bridge to an existing control socket
codex queue               # message an existing session
codex agents              # shared local-server session browser
codex --remote            # connect the interactive client to a server
```

The locally generated protocol schema includes `thread/list`, `thread/loaded/list`, `thread/read`, `thread/resume`, `thread/queue/add`, `turn/start`, `turn/steer`, and `turn/interrupt`. `turn/steer` requires `expectedTurnId`. Use provider thread ID as the conversation identity; do not collapse branches by a shared session-tree root.

Prefer connecting the desktop adapter to the **same** app-server used by the native CLI. Initialize the client, discover/read threads, and subscribe using the version-tested native behavior. A read is distinct from a resume; do not resume dormant threads merely to list them. Keep configuration unchanged when attaching. [Codex app-server protocol](https://developers.openai.com/codex/app-server/)

**Important local observation:** querying daemon version found no default control socket on this machine. The available commands are evidence of an integration surface, not proof that every normal `codex` invocation currently uses it. Task 1 must establish default CLI topology. If a one-time launch/config integration is needed to make normal `codex` use the shared server, show and validate it during enrollment. Do not start a separate server and call `thread/resume` on a live transcript as an “attach.”

**Task 1 outcome:** an ordinary `codex` launch still had no shared control socket. However, native `codex queue --thread UUID --message TEXT` delivered idle and busy follow-ups to that same original terminal session, observed by reading its existing rollout without a second writer. This is the accepted alternative to the shared-server hypothesis for this spike; it requires no wrapper or enrollment to keep typing `codex`. Discovery uses cwd + process-owned rollout metadata and is unavailable before the first prompt creates a rollout. Output is completed text, not token streaming; queue IDs acknowledge enqueue only, repeated sends produced duplicate answers, and attribution appears as a normal user turn. Codex approval-wait input remains unproven because the safe challenge never displayed a permission dialog. V1 is not marked complete. [Evidence](2026-09-10-local-session-native-probe-results.md)

### Capability policy

Report capabilities per session: `observe`, `send`, `interrupt`, and `resumeWhenStopped`, with an unavailable reason. Discovering an ID does not imply every operation is supported. Harness approval UI remains native initially; no remote permission-mode changes or approval bypass.

The mandatory V1 baseline is automatic discovery, continuing output visibility, and follow-up/steering of the same live session for **both** harnesses. Immediate interruption, token-by-token fidelity, stopped-session restart, and nonstandard launch environments must be described according to what is actually verified.

If native ingress fails the spike, keep observation as a development milestone and record the precise blocker. A launcher/PTY approach is a separate fallback decision, not the default: it must preserve normal terminal use and prove it cannot inject text into a permission dialog or a shell after the CLI exits. No hidden switch to an SDK worker, copied transcript, relay LLM, or provider fork.

## 5. Minimal implementation architecture

```text
Native Claude / Codex sessions
        ↕ native listing, output and input adapters
Existing Lightbulb Tauri host
        ↕ dedicated local-session connection over existing machine WS service
FastAPI local-session service ↔ existing DynamoDB messaging table
        ↕ authorized room-scoped HTTP + content-free AppSync invalidations
Existing conversation UI → Devices & sessions → selected session

Friday → existing OpenClaw node relay → desktop actions       [unchanged]
Friday → room-bound local-session adapter → selected session [new]
```

### Desktop responsibilities

- One supervisor inside the existing Rust process, with concrete Claude and Codex adapters. No provider plugin framework. Start/stop it with auth enrollment and logout; closing the window follows the existing tray lifecycle.
- Discover automatically. Hooks/native events are the fast path; a bounded reconciliation tick repairs missed events. Start with a two-second discovery target on an awake, connected desktop; measure CPU before reducing it.
- Keep provider endpoints, full local paths, auth material and native permission settings local. Upload only the allowed session record and normalized conversation/activity data.
- Validate registration paths/endpoints against the enrolled OS user, actual session process and supported provider roots. Reject symlink substitution and stale/reused process IDs. Never accept a server-supplied filesystem path to read.
- Track provider item/event identifiers and an upload cursor. Buffer bounded batches; backpressure uploads without blocking the user's CLI. Re-read native history to repair gaps when available. Expose a gap when not recoverable.
- On enrollment, pin the session to a Lightbulb workspace and human. Merely switching the web UI to another org must not silently upload the same session there. Changing its sharing workspace requires an explicit enrollment action.
- A provider upgrade invalidates unverified control capabilities until the compatibility check passes. Observation must not alter the model, sandbox, instructions or native memory.

### Backend storage

Add `core/repositories/local_sessions_repo.py` against the existing messaging table, with a separate key prefix. Reuse its DynamoDB connection helpers; do not add a new table solely for this feature.

```text
PK: LOCALUSER#<ownerScopeId>#<humanUserId>
  SK: DEVICE#<deviceId>                          paired desktop projection
  SK: SESSION#<localSessionId>                   session directory row

PK: LOCALSESSION#<ownerScopeId>#<localSessionId>
  SK: META                                      canonical session identity/status
  SK: EVENT#<paddedSeq>                          normalized transcript/activity
  SK: COMMAND#<requestId>                        request hash + actor + outcome
```

`ownerScopeId` is the active organization workspace returned by the existing owner resolver; `humanUserId` is the actual device owner. Workspace-first onboarding is already merged: product APIs, desktop pairing and session enrollment reject missing workspace context with `409 workspace_required`. Do not restore a personal-user owner fallback. Deterministically derive `localSessionId` from that scope, paired desktop identity, provider, local provider-profile fingerprint and native conversation ID. The profile fingerprint distinguishes separate local harness data roots; it contains no raw path or secret. Reconnection must not duplicate the session. Track an execution generation separately from the conversation ID.

Keep the canonical session record and user directory projection consistent with a transaction. Do not use an eventually consistent directory projection to authorize anything. Never copy event history into every room.

Persist bounded, normalized transcript/activity batches, not raw terminal screens, arbitrary hook payloads, hidden reasoning, provider configuration, or credentials. Start with at most two text batches/second/session and at most 16 KiB per wire envelope, splitting UTF-8 safely. Flush lifecycle/tool boundaries promptly. Native provider output granularity remains the upper bound on fidelity. Keep large tool output bounded and mark truncation explicitly.

Retain enrolled history until the existing account/workspace deletion policy or an explicit delete removes it; do not give canonical handoff history an accidental outbox TTL. The existing stream projector should ignore these new prefixes. Session ingestion publishes only a content-free resource invalidation to eligible room channels; HTTP fetches provide the authorized content. Polling while the panel is open repairs lost hints.

This deliberately uses the existing database before introducing blob storage. Measure storage/write volume; migrate old transcript batches to the existing private artifact storage only if actual volume warrants it.

### Wire/API shape

Add the dedicated machine client ID `lightbulb-local-sessions`, authenticated with the existing paired-device proof. Admit it as its own connection type. It speaks a small Lightbulb protocol, **not** OpenClaw node RPC. A node outage should not sever otherwise healthy harness sharing.

Bind ingestion to the verified connection's owner scope, human and device. Ignore or reject payload attempts to choose another owner. Revalidate device enrollment/revocation on messages, not only when the socket initially connects. Reuse existing cross-instance connection lookup/Management API routing; do not rely on a FastAPI worker's in-memory registry to find another worker's desktop.

Human endpoints, using the existing authenticated messaging router:

```text
GET  /messaging/rooms/{roomId}/resources
GET  /messaging/rooms/{roomId}/local-sessions/{sessionId}/events?afterSeq=...
POST /messaging/rooms/{roomId}/local-sessions/{sessionId}/commands
GET  /messaging/rooms/{roomId}/local-sessions/{sessionId}/commands/{requestId}
```

Add `PUT /desktop/local-sessions/enrollment` to the existing desktop router for the one-time enable/disable action. Authenticate the human with Clerk, verify they own the paired device in the current owner scope, and persist the enrollment server-side. A machine connection cannot self-enable sharing merely by claiming it in an upload. Disabling enrollment blocks further ingestion/control and removes room projections without deleting native history.

```typescript
// Public command contract; IDs are validated, not inferred from names.
type LocalSessionCommand = {
  requestId: string;
  expectedGeneration: string;
  kind: "send" | "interrupt";
  text?: string;                 // required for send; bounded length
  expectedTurnId?: string;       // required when steering a particular busy turn
};
```

The server derives actor identity from authentication and room provenance from the validated route and membership. The client cannot impersonate the session owner. Do not expose arbitrary native JSON-RPC, shell commands, process IDs, provider socket addresses, file paths or permission changes through this endpoint.

### Admission and ordering

Every session read/control operation checks:

1. The room belongs to the authenticated owner scope and the requester is a current human member, using `require_room_member`.
2. The session belongs to that scope and its owning human is also a current member, using the canonical member row.
3. Sharing remains enabled. Commands additionally require the expected device/generation to be online and the requested capability to be supported.

Centralize that check in the session service and reuse it for list hydration, event reads, commands and command receipts. Resource-list hydration checks each owner's membership rather than trusting a stale directory cache. There is no special admin bypass or org-wide room-resource endpoint.

Commands are persisted with an idempotency key, canonical request hash, server-assigned author and session ordering. The desktop serializes remote commands with the provider's own native input arbitration. Concurrent native keyboard input must remain visible and must not be overwritten. A stale active-turn ID produces a conflict, not an automatic send to a different turn.

Track `pending → dispatched → applied`, or `rejected/unknown`. `applied` means the harness acknowledged/admitted the input, not that the requested work succeeded. Providers with held input must report that separately. Reuse a request ID when retrying the same command; changed text under the same ID conflicts.

Recheck membership/device authorization before claiming and dispatching a pending command. Expire undelivered commands promptly (initially 30 seconds); never silently execute old queued instructions after a long offline period. Reconnects may retry proven-undelivered requests, but not blindly replay a dispatched request whose outcome is unknown. A local receipt journal plus provider message IDs helps reconciliation; neither makes arbitrary native side effects exactly-once.

An unresolved native outcome blocks further input within that native process generation. A verified process successor can admit new explicit commands while retaining the old unknown receipt; it never replays that request or proves it did not run. Current-generation reconciliation takes priority. A transient resource refresh failure keeps the selected panel and uncertain request ID/payload with a visible stale-data notice; authorization failures and membership changes still clear immediately. Queued/held receipts are not evidence of approval waiting. Unchanged heartbeat lease renewals still persist, dispatch and acknowledge, but do not fan out resource hints; public state changes and offline recovery do.

## 6. Membership, device scope and approvals

The resource directory and every functional picker must use the same server-authorized room-resource response. No informational sidebar sourced from an org roster while the Send picker uses room members.

Resolve physical-device ownership and node IDs from verified pairing records. Do not trust a session upload to associate itself with another human's node, and do not return an unfiltered gateway node list to the browser.

Removing you from a conversation removes your device/session projection there. Removing Aden stops his access through that conversation. Clear selected-session state, cached transcript UI and pending sends on the existing access-removed/membership-change events; subsequent fetches and commands must fail server-side. Re-adding someone permits fresh authorized access. Access through a different eligible room can still remain.

Do not claim that removal erases data someone already saw, or rolls back an action admitted before removal. A command already handed to the native harness may be running. Device revocation stops further remote transport/control, not the owner's local CLI or files.

**OpenClaw node selection is advisory in V1, by decision.** Give Friday a preference list of currently present human members' node IDs; show only those nodes in this conversation's UI. Prefer an explicit target, then an appropriate available preferred node. If no preferred node can perform the action, report that instead of presenting an unrelated node as an authorized room resource.

The underlying OpenClaw runtime may still be connected to a wider node set. Its generic node invocation path does not uniformly carry trusted room context. This plan does not turn the preference into an enforceable room-level node sandbox or promise backend confinement that does not exist.

Existing device/OS/harness approval boundaries remain. Everyone can steer, but steering is not authority to switch permission modes, answer native security dialogs, copy API keys, or bypass a managed policy. Initially show “Waiting for approval on owner's machine” where required.

Likewise, a broadly permitted shell on the same machine can access files independently of the session API. The strict room checks secure Lightbulb's sharing/control endpoints; they do not create OS isolation between Claude, Codex and Friday. Strong machine isolation is separate future work.

## 7. Friday, memory and Paperclip

### Friday and the harness are different paths

Friday using `system.run` on your node changes machine state. Friday sending a message to your Claude session changes that session's input. Neither action magically informs the other system.

Implement a narrow Friday adapter for listing eligible sessions, reading selected history, and submitting the same validated commands. Bind it to the actual canonical room execution context. An owner-wide hook token plus a model-supplied `roomId` is not sufficient room provenance. Use a backend-issued, expiring invocation grant bound to owner, room and agent; verify current membership again when it is used. Do not let agent credentials masquerade as a human requester.

If the existing channel/plugin execution hooks cannot carry that grant safely, stop this phase and add the necessary scoped hook to the maintained OpenClaw fork. Do not weaken the human-facing session API to make Friday work. Human multiplayer can ship independently of this optional caller.

Friday selecting an existing session does not create a new employee or room participant. A future explicitly requested new worker can be recorded with `origin=friday` and linked to the initiating message; starting managed workers is not necessary for discovering all ordinary user-started sessions.

### Memory: preserve sources of truth

| State | Source of truth / V1 treatment |
| --- | --- |
| Human collaboration | Existing canonical room messages and replies |
| What the local agent was told/did | Native session plus its normalized Lightbulb transcript mirror |
| Current files | Actual filesystem/worktree and Git; not a narrative memory summary |
| Personal harness memory | Remains provider-native and local; no automatic upload/merge of `~/.claude` or `~/.codex` |
| Company decisions | Existing shared wiki, with deliberate promotion and source references |
| Assigned work/status | Paperclip's existing issue/run/workspace graph |

Start with filesystem change awareness, not a universal memory synchronizer. Record the session's working directory and Git/worktree identity locally. Watch relevant workspace changes and track a coarse revision/fingerprint. Before a Lightbulb-submitted turn, include an advisory notice of external changes and have the harness re-read affected files. Surface the same stale-workspace notice in the panel. Use an available native hook for local-turn notices where supported; do not claim every unmodified native turn can already be intercepted.

Attribute changes to Friday only when observed execution provenance proves it; otherwise say “external workspace change.” Provider activity, shell edits and file watchers can overlap. The notification is evidence to refresh, not proof of perfect coherence or a filesystem lock. Two agents modifying the same checkout can still conflict; show overlapping activity and prefer worktrees for independent tasks, without relocating an existing session automatically.

Do not inject private room history or automatically promote session content into the company wiki as part of stale-file reconciliation.

### Paperclip

Allow an explicit link from a session to an existing Paperclip issue/workspace and from a conversation message to the session. Keep the session running if an issue closes, and keep the issue if a session ends. A handoff can reference the transcript, relevant files, outstanding questions and verification results.

No automatic issue per CLI session, no local-session employee in the roster, and no Paperclip scheduler in the critical path. Check Paperclip access separately when showing linked work; sharing a session does not grant access to a private issue, and an issue link does not grant session control.

## 8. Implementation sequence

Each task should land with its smallest meaningful automated check, using existing test frameworks. Keep unrelated worktree changes untouched. Tests listed here are future implementation checks, not tests already run for this planning document.

### Task 1 — Prove native same-session attachment before building the UI

**Create:** `apps/desktop/src-tauri/examples/local_session_probe.rs`; add synthetic protocol fixtures under `apps/desktop/src-tauri/tests/fixtures/local_sessions/` only for messages actually used. Do not commit personal transcripts, sockets, tokens or complete generated provider schemas.

- [x] Start disposable native sessions with the same ordinary terminal commands a user uses. Record CLI versions and whether normal Codex uses an attachable shared server. Test the installed version first, not a hypothetical future API.
- [x] Claude: register via a temporary test hook, reconcile native listing, validate the documented ingress using verified wire messages, and observe the existing conversation's output. Do not run a second Claude to act as a message courier.
- [ ] Codex: connect to the original server, identify the exact native thread, and prove observation plus follow-up input without a fork, configuration reset, or second owner of the rollout file.
  - Shared-server hypothesis failed; accepted native queue + read-only original rollout alternative passed (§4). Leaving literal server-attach requirement unchecked.
- [ ] For each harness, test input while idle, mid-turn, and waiting for approval; record admission, holds/refusals, duplicate behavior, terminal visibility and attribution. Inspect whether native local input can race with remote input safely.
  - Idle/busy, rapid duplicate, terminal draft preservation, and Claude pending approval tested. Codex pending approval and explicit Claude inbound hold/refuse remain unproven; simultaneous-writer ordering not established.
- [x] Measure discovery and output-update latency. Mark whether output is token deltas, message updates or completed messages. Complete an active-session-before-desktop-start case.
- [x] Make the probe reject mismatched session/process/generation and unsupported versions. Add a replay test for the minimum observed protocol, including fragmented/partial JSON. Claude ingress additionally requires supported-version native records after the current PID/native-start cutoff; installed-version or historical-only evidence is insufficient. Unknown public-message wrappers/phases/channels are excluded.
- [x] Update §4 with pass/fail evidence. If normal Codex requires launch integration, specify the exact reversible enrollment change and test that typing `codex` still works. If either harness cannot meet same-session input, do not mark its V1 complete or disguise an import as success.

**Check:** Put the probe's replay tests in its example target and run `cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml --example local_session_probe`. A disposable two-client live demo is additionally required; fixtures alone cannot establish attachment semantics.

**Exit:** Both provider paths demonstrated, or a precise provider limitation documented before estimating the rest. Ship no auto-sharing of a real account as part of this probe.

### Task 2 — Room-scoped registry and authorization

**Create:** `apps/backend/core/repositories/local_sessions_repo.py`, `apps/backend/core/services/local_sessions.py`, `apps/backend/tests/unit/services/test_local_sessions.py`.

**Modify:** `apps/backend/routers/messaging.py`; `apps/backend/routers/desktop_pair.py` and its existing router test for enrollment; `apps/backend/core/services/messaging.py` for any added contract/error variants; `packages/shared/src/messaging/types.ts`, `client.ts`, `client.test.ts`, and `packages/shared/src/index.ts` as needed for exports.

- [x] First add failing tests for two humans sharing a room, an unrelated org member, cross-org IDs, session owner leaving, requester leaving, and access through a different room still being valid.
- [x] Implement the canonical records and deterministic identity described in §5 using the existing table/resource helpers. Enforce request limits, ownership and consistent identity projection.
- [x] Add human-authorized enrollment and disable behavior, with tests rejecting another member's device and a machine-only attempt to enable sharing. Keep enrollment distinct from permission to use the existing OpenClaw node.
- [x] Implement one session-authorization helper reused by all protected operations. Add transaction conditions for membership at command admission; directory access alone never authorizes a send.
- [x] Add room resources, paginated events, submit-command and command-status endpoints. Test forged actor/device fields, duplicate request ID with changed input, stale generations, unsupported operations and offline devices.
- [x] Add typed shared client methods without adding local sessions to `MemberKind` or the company roster. Keep optional Paperclip links absent until Task 7.

**Checks:**

```bash
cd apps/backend
uv run pytest tests/unit/services/test_local_sessions.py tests/unit/repositories/test_messaging_repo.py -q
```

```bash
pnpm --filter @lightbulb/shared test -- src/messaging/client.test.ts
pnpm --filter @lightbulb/shared typecheck
```

**Exit:** A room can enumerate/read/control only its current members' enrolled resources, independent of frontend filtering.

### Task 3 — Dedicated desktop transport on existing infrastructure

**Create:** `apps/backend/core/services/local_session_relay.py`.

**Modify:** `apps/infra/lambda/ws-connect/index.py` and `test_index.py`; `apps/backend/routers/websocket_chat.py`; `apps/backend/core/services/connection_service.py`; existing `ws-message`/`ws-disconnect` routing where required; `apps/backend/tests/unit/routers/test_websocket_chat.py`.

- [x] Add failing admission tests: device token + new client ID succeeds; browser token, Paperclip token, forged device ID and cross-kind pairing fail. Existing node/operator cases retain their behavior.
- [x] Admit the dedicated connection type and route local-session envelopes without opening a gateway/node connection. Preserve trusted identity headers through every forwarding hop.
- [x] Check device revocation for ongoing ingestion/control, bind uploads to that device/human/scope, and reject unknown message types or oversized frames. Never log transcript content or tokens.
- [x] Route commands using shared connection records and Management API. Demonstrate submit on one backend worker and delivery on another worker's connection; no sticky-worker assumption. Verified hermetically with shared Moto records and a stubbed external Management API, not deployed workers.
- [x] Add a short-lived pre-dispatch claim/revalidation and durable outcomes. Exercise disconnect before delivery, after delivery but before acknowledgment, duplicate delivery, and revoked membership before dispatch.
- [x] Publish a content-free `room.resources.changed` hint through the existing publisher and registered live-event union. Do not use an org-wide payload fanout or add session text to opaque private-user notifications.

**Checks:** existing WebSocket router tests plus `ws-connect/test_index.py` under the backend's Python test environment; shared live-event contract tests. Verify the messaging projector ignores `LOCALUSER#`/`LOCALSESSION#` data rows.

**Exit:** Sharing/control transport operates independently of OpenClaw container availability and survives reconnects without silent double execution.

**Local checkpoint:** Dedicated transport is implemented and reviewed; ambiguous dispatch remains `unknown` and is never blindly replayed. Actual native admission, receipt-journal reconciliation and integrated desktop demonstrations remain Task 4/release obligations, not properties established by transport tests alone.

### Task 4 — Desktop enrollment, discovery, mirroring and commands

**Create:** `apps/desktop/src-tauri/src/local_sessions.rs`, `local_sessions/claude.rs`, `local_sessions/codex.rs`. Keep tests next to their Rust implementation and reuse Task 1 fixtures.

**Modify:** `apps/desktop/src-tauri/src/lib.rs`; `bootstrap.rs`/`pair_state.rs` only where lifecycle/enrollment requires; `apps/desktop/src-tauri/src/main.rs` if the bundled executable also handles the tiny hook-registration subcommand. Reuse `device_token.rs` and existing dependencies unchanged where possible.

- [x] Add failing tests for discovery deduplication, already-running discovery, PID reuse, process restart, partial transcript records, truncation/rotation, profile roots, and logout/re-enrollment scope changes.
- [x] Implement a concrete two-provider supervisor with version/capability checks, local transcript cursors and bounded upload/retry. The probe remains a rerunnable diagnostic and reuses the production normalizer.
- [x] Implement one-time enrollment explaining the complete sharing audience, requiring an existing paired Mac. Reversible Claude hook installation is tested in temporary settings; no Codex launch change is required. No real enrollment or permanent provider setting change was performed.
- [x] Use a private local registration channel restricted to the OS user. Keep provider inbox credentials local; represent teammate authorship honestly. Validate session endpoints rather than accepting arbitrary paths from hooks or the backend.
- [x] Wire lifecycle registration/reconciliation, output normalization and supported native input. Native approval waiting is a state, not a prompt to inject Enter or `yes`.
- [x] Add command receipt persistence and conservative unknown-outcome handling, including durable generation transitions and failed-write recovery. Local disable stops immediately; revision-fenced remote cleanup remains visibly pending until confirmed. Native sessions/history are preserved.
- [x] Exercise outage/reconnect, persisted restart, identity/version rejection, logout and immediate disable through hermetic tests; report unknown/offline state honestly.
- [ ] Release validation: real enrollment/Keychain, signed background tray, deployed outage/revocation, sleep/wake and on-disk provider upgrade. These remain Task 8 gates, not claims established by fixtures.

**Checks:** `cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml local_sessions`; run the approved Task 1 live cases against the integrated host. Verify observation causes no extra model calls and requires no shared provider login.

**Exit:** Normal supported terminal launches automatically register and mirror; commands reach the same session through the authenticated transport.

**Local checkpoint:** Implementation and two scoped fix rounds reviewed clean through `8dc9e7d86`. Production adapters with a hermetic WebSocket service admitted input and mirrored replies from owned disposable ordinary Claude/Codex sessions; both processes were exited. Completed-public-message updates and queue/admission receipts are verified, not token streaming, immediate interruption, authenticated native teammate authorship or completed-work success. Integrated busy/approval parity, two-account/two-desktop handoff and many-session supervisor performance/capacity remain release gates.

**Final enrollment correction (`98d675335`, reviewed):** Preflight managed hook settings before a remote enable. Persist a nominated revision and disabled predecessor in the existing private enrollment file first; the existing backend row admits it conditionally. Cancellation can fence an enable that has not arrived, and old enable/cleanup requests cannot affect a newer revision. Verify the authenticated owner assertion and returned owner/human/device/revision before publication. Settings distinguishes local stop from unconfirmed server-history access and offers retry; hook cleanup cannot suppress remote revocation. No supervisor starts after a failed final save, although a successful rename followed by disk-sync failure may leave a complete active record with valid Disable authority and permit later authenticated restoration. Lost local revision/state or an existing target-workspace row with an unknown revision fails closed; seamless multi-workspace recovery and downgrade are not promised. Backend admission must precede upgraded desktop enrollment. Tests cover these production boundaries with synthetic files/Moto, not a packaged-app or real-account demonstration.

### Task 5 — Conversation-scoped UI

**Create:** `apps/frontend/src/components/messaging/RoomResources.tsx`, `LocalSessionPanel.tsx`, and `__tests__/RoomResources.test.tsx`.

**Modify:** `RoomPanel.tsx`, `MessagingWorkspaceContext.tsx`, `messaging.css`; add resource fetching/invalidations to `apps/frontend/src/hooks/useRooms.ts` or a narrowly scoped sibling hook if necessary. Extend existing hook/component tests for access removal.

- [x] Render devices grouped by current human member, with local sessions nested underneath. Paired OpenClaw endpoints remain visible independently of sharing. Only explicit, revalidated Mac/CLI associations group together; unassociated endpoints are labeled as logical nodes with unknown availability, not invented physical computers.
- [x] Add selected-session transcript/activity and an explicitly targeted input box in the existing responsive secondary pane. Keep conversation/reply composers separate; reuse safe Markdown without a fake OpenClaw `agentId`.
- [x] Show provider, owner, device, honest provider/short-ID title fallback, state and actual capability limits. Include shared-transcript audience, native permission/usage, local-approval/offline/unknown-command explanations.
- [x] Reuse resource hints and open-pane polling with coalesced refreshes, immediate access invalidation and stale-response guards. Initial recent pages use canonical latestEventSeq; older/newer navigation and the 200-event retained window stay bounded and recoverable. Uncertain sends retain request ID/payload.
- [x] Test removed-owner selection/input/history clearing, stale room/history responses, sustained hints, scope changes and hidden workspace. Include keyboard navigation, focus restoration, loading and empty states.

**Checks:**

```bash
pnpm --filter @lightbulb/frontend test -- src/components/messaging/__tests__/RoomResources.test.tsx src/components/messaging/__tests__/RoomPanel.test.tsx src/hooks/__tests__/useRooms.test.tsx
```

**Exit:** Aden can find and steer a normally started local session without inviting a new agent, picking a duplicate “session room,” or leaving the conversation.

**Local checkpoint:** Reviewed through `2f516af22`, including a reproduced/fixed frequent-hint starvation bug. Controller's 105 frontend tests and TypeScript check pass; separate synthetic desktop/mobile previews were inspected. These are component/hook/service and rendered-fixture checks, not a real authenticated two-person/native/deployed handoff. Those remain Task 8 gates.

### Task 6 — Friday adapter and shared-workspace awareness

**Modify:** `apps/infra/openclaw/plugins/lightbulb-channel/src/index.ts`, `src/channel.ts`, `src/index.test.ts`; `apps/backend/core/services/local_sessions.py` and the existing messaging/channel delivery path; desktop local-session modules.

- [x] Test room-bound execution provenance before implementing Friday's session actions. Issue and validate an expiring invocation grant from trusted backend context, not a room ID supplied by model text. Check the agent is still a room member at use time.
- [x] Give Friday room-scoped resource discovery and explicit-session read/send capabilities via the backend. Keep its execution-node preference list separate from the enforceable local-session API checks. Test expired/cross-room grants and missing node availability.
- [x] Add workspace revision/change notices before remote input and UI stale-state indicators. Hermetic external edits exercise detection and a journaled reread advisory; this does not establish that a native harness reread the files.
- [ ] Release gate: demonstrate a real Friday node edit followed by local-session input and an observed native reread before subsequent requested editing.
- [x] Make unknown provenance and concurrent writers visible; do not claim automatic memory synchronization or prevent ordinary native local use with a global lock.
- [x] Update the bundled `lightbulb` skill's relevant reference and `MAINTENANCE.md` anchors to describe implemented capabilities and advisory node scope. Read its routing instructions before editing; keep new detail out of its thin `SKILL.md` unless needed for routing.

**Checks:** existing channel plugin `pnpm test`, backend session authorization tests, Rust local-session tests, and one live Friday-node edit followed by a local-session follow-up. If fork changes are required, they belong in the maintained fork with their own tests/image promotion, not a copied source tree here.

**Exit:** Friday can intentionally collaborate with a selected local session without becoming its owner or claiming knowledge of unseen machine changes.

**Local checkpoint:** Reviewed through `4b3661403`, including fixes for blocking FIFO opens and a test dependency on Git history absent in shallow CI. A distinct-purpose grant lasts at most 300 seconds and travels only in `X-Lightbulb-Local-Session-Token`; canonical ingress JSON and existing gateway authentication remain unchanged for mixed-version rollout. Actual SDK tests cover per-run bindings and secret redaction; full gateway/queue/log behavior remains a live gate. Agent authorship stays typed, not a forged human identity.

Workspace evidence is private and bounded: 512 paths, 4 MiB total, 1 MiB/file; background scanning permits one checkout per 30 seconds across the host, with an immediate target scan before remote commands. Many roots can make background evidence old (512 roots imply roughly 256 minutes per sweep). Coverage is explicitly bounded/unknown, and a notice requests a reread without claiming it happened. No filesystem lock or personal-memory synchronization was added.

**Integration checkpoint:** `af5ee04d6` merges exact `68c52c359`, preserving its ambient-agent admission, Slack-parity messaging, canonical message permalinks and run-status surfaces. Reviewed fix `49f5f5a3e` makes same-room message navigation leave the resources pane before targeting hidden content on narrow screens. Combined regressions also cover private invocation forwarding through the ambient gate, controlled thread/resources selection and native Markdown isolation. Task 7 extends this existing singleton Messages navigation; it must not introduce a second session-room router.

### Task 7 — Paperclip links and a real handoff

**Modify:** existing session record/service/shared types and session panel. Use the existing Paperclip proxy/client access path; do not invent a second issue store.

- [x] Add an optional issue/workspace link with independent authorization checks and a session deep link on an explicit conversation message.
- [x] Test that issue closure does not terminate a session, session completion does not close an issue, and inaccessible work links reveal no protected issue details.
- [ ] Demonstrate the original handoff: one human runs an investigation; the second reads its actual history, asks the same session a follow-up and continues the work. Link the findings to existing work only when requested.

**Exit:** Paperclip organizes the work without owning local-session identity or being required for sharing.

**Local checkpoint:** Reviewed commit `ce2ce82b9` stores canonical work identifiers and an independent link revision in existing session metadata. Explicit Teams routes use the current viewer's per-user cookie and actual company/workspace validation; ordinary session history/control never depends on Paperclip. Real repository/relay race tests preserve session counters, generation and links. The collapsed linked-work section validates protected details afresh, and canonical session links reuse the singleton Messages pane; copying or appending a link never sends a command. The two-human handoff check uses synthetic history and real local storage/relay contracts, not two live enrolled desktops. Deployed Paperclip permissions, full browser-shell navigation and real native continuation remain release gates.

### Task 8 — Release gate and rollout

- [x] Gate the entire desktop production job on explicit workflow dispatch, retaining automatic dev builds and the dev prerequisite. Reviewed `1e88cb324`; 10 promotion tests, focused TypeScript and parsed-YAML checks pass. Dispatch rebuilds both tracks and does not itself verify earlier human acceptance; select the tested ref only after dev acceptance.
- [ ] Run a two-account, two-desktop scenario in dev for both providers: ordinary CLI start, discovery, output updates, bidirectional input and attribution, restart/reconnect, and an existing-running session discovered later.
- [x] Add an automated room-access regression covering a third nonmember, cross-org access, owner removal, requester removal and same-session projection across two eligible rooms. Backend membership matrix is covered in `test_local_sessions.py`; independently reviewed `25da6f739` adds stale-tab POST 403/404 coverage with no removal hint, verifies history/composer clearing and preserves the ordinary room draft. This does not replace deployed two-person validation.
- [ ] Demonstrate busy-turn conflicts, pending approval, duplicate requests, unknown acknowledgments, local keyboard input during remote steering, and device revocation.
- [ ] Verify enrollment/uninstall preserves existing hooks/configuration and never uploads provider credentials, full personal memory roots or unrelated historical sessions. Use synthetic transcript fixtures containing sentinel secrets to check the normalization boundary; do not promise perfect secret redaction of user-authored text.
- [ ] Measure discovery delay, provider-output delay, transport delay, command acknowledgment delay, CPU, reconnect gaps and database writes. Expose provider/config limitations explicitly; do not market completed-message observation as token streaming.
- [ ] Ship backend admission/contracts first, then the signed desktop, then expose enrollment/UI. Old desktops remain valid node clients. Roll back by disabling session enrollment/commands, not by breaking existing node admission or deleting native sessions.
- [ ] Use normal version-controlled CI/CD and watch pushed runs. Backend/frontend and desktop production are dispatch-gated in this branch; actual dev verification must precede production dispatch. Earlier7630 passed substantive Claude review and non-backend CI; its backend run timed out before completion. The independently reviewed test-isolation correction passes the full local coverage run; corrected combined CI remains required. Earlier billing-admission/reviewer failures are historical, not the current diagnosis.

**Definition of done:** In a supported configuration, you type `claude` or `codex` normally, Aden discovers that session automatically from your conversation, sees its ongoing work, and can contribute to that very same native session. Membership governs every Lightbulb view/control path. No new agent identity, provider account sharing, per-session manual publish, or transcript fork is needed.

## 9. Deliberately not in V1

- Enforced room-specific OpenClaw node ACLs or OS-level isolation between already-authorized local processes.
- A new “local Claude agent” in the room roster; managed cloud harness agents are a separate product decision.
- Automatic migration of a live process or filesystem to another human's machine.
- Universal bidirectional memory merge, transcript-to-wiki promotion, or automatic tasks for every session.
- A provider-independent raw terminal, security-dialog remote control, or arbitrary native RPC proxy.
- A new realtime service, database cluster, message bus, or orchestration framework.

The difficult part is proving and maintaining native harness attachment semantics. Build that first; the room/member model and most of the distribution infrastructure already exist.
